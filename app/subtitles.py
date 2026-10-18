"""Subtitle scanning — probe media files for subtitle tracks with ffprobe.

Two scan jobs (full + changed) walk the Movie Match items, resolve each path to
this machine, run ffprobe to list embedded subtitle streams, and detect external
subtitle files sitting next to the video. Results are cached to
``data/subtitles.json`` and shown in the Search tab.

The classification mirrors Subber's "Movie List" tab so the two tools agree on
what "acceptable", "AI sub", "forced", "foreign" and "no subs" mean.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import JSONResponse

from .config import config
from .logs import log
from .paths import resolve_path
from .web import require_login

router = APIRouter()

_SUBS_FILE = config.data_dir / "subtitles.json"

# External subtitle file extensions.
SUBTITLE_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup", ".idx", ".sbv", ".ttml",
}

# A subtitle whose language resolves to one of these counts as "acceptable".
GOOD_SUBTITLE_NAMES = {"English", "Swedish", "Unknown"}

# Files below this size (bytes) are treated as placeholders and ignored.
TINY_SUBTITLE_MAX_BYTES = 1024

# Name tokens that carry no language information.
_SUBTITLE_NOISE_TOKENS = {"forced", "sdh", "cc", "hi", "default", "full", "normal"}

# Language code/name -> display name. Unknown codes resolve to "Unknown" (which,
# like Subber, counts as "acceptable").
_LANG_NAMES = {
    "en": "English", "eng": "English",
    "sv": "Swedish", "swe": "Swedish",
    "no": "Norwegian", "nb": "Norwegian", "nn": "Norwegian", "nor": "Norwegian",
    "da": "Danish", "dan": "Danish",
    "fi": "Finnish", "fin": "Finnish",
    "de": "German", "deu": "German", "ger": "German",
    "fr": "French", "fra": "French", "fre": "French",
    "es": "Spanish", "spa": "Spanish",
    "it": "Italian", "ita": "Italian",
    "nl": "Dutch", "nld": "Dutch", "dut": "Dutch",
    "pt": "Portuguese", "por": "Portuguese",
    "pl": "Polish", "pol": "Polish",
    "ru": "Russian", "rus": "Russian",
    "ja": "Japanese", "jpn": "Japanese",
    "zh": "Chinese", "zho": "Chinese", "chi": "Chinese",
    "ko": "Korean", "kor": "Korean",
    "ar": "Arabic", "ara": "Arabic",
    "hi": "Hindi", "hin": "Hindi",
    "tr": "Turkish", "tur": "Turkish",
    "el": "Greek", "ell": "Greek", "gre": "Greek",
    "cs": "Czech", "ces": "Czech", "cze": "Czech",
    "hu": "Hungarian", "hun": "Hungarian",
    "ro": "Romanian", "ron": "Romanian", "rum": "Romanian",
    "bg": "Bulgarian", "bul": "Bulgarian",
    "uk": "Ukrainian", "ukr": "Ukrainian",
    "th": "Thai", "tha": "Thai",
    "vi": "Vietnamese", "vie": "Vietnamese",
    "id": "Indonesian", "ind": "Indonesian",
    "he": "Hebrew", "heb": "Hebrew",
    "fa": "Persian", "fas": "Persian", "per": "Persian",
    "is": "Icelandic", "isl": "Icelandic", "ice": "Icelandic",
    "sk": "Slovak", "slk": "Slovak", "slo": "Slovak",
    "hr": "Croatian", "hrv": "Croatian",
    "sr": "Serbian", "srp": "Serbian",
    "sl": "Slovenian", "slv": "Slovenian",
    "et": "Estonian", "est": "Estonian",
    "lv": "Latvian", "lav": "Latvian",
    "lt": "Lithuanian", "lit": "Lithuanian",
}
_FULL_LANG_NAMES = {
    "english": "English", "swedish": "Swedish", "norwegian": "Norwegian",
    "danish": "Danish", "finnish": "Finnish", "german": "German",
    "french": "French", "spanish": "Spanish", "italian": "Italian",
    "dutch": "Dutch", "portuguese": "Portuguese", "polish": "Polish",
    "russian": "Russian", "japanese": "Japanese", "chinese": "Chinese",
    "korean": "Korean", "arabic": "Arabic", "hindi": "Hindi",
    "turkish": "Turkish", "greek": "Greek", "czech": "Czech",
    "hungarian": "Hungarian", "romanian": "Romanian", "bulgarian": "Bulgarian",
    "ukrainian": "Ukrainian", "thai": "Thai", "vietnamese": "Vietnamese",
    "indonesian": "Indonesian", "hebrew": "Hebrew", "persian": "Persian",
    "icelandic": "Icelandic",
}

# ---- persistence ------------------------------------------------------------


def _load_state() -> dict:
    try:
        data = json.loads(_SUBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    ok = data.get("ok")
    if not isinstance(ok, list):
        ok = []
    return {
        "files": files,
        "ok": ok,
        "full_scan_at": data.get("full_scan_at"),
        "change_scan_at": data.get("change_scan_at"),
    }


def _save_state() -> None:
    payload = {
        "version": 1,
        "full_scan_at": _state["full_scan_at"],
        "change_scan_at": _state["change_scan_at"],
        "files": _state["files"],
        "ok": _state["ok"],
    }
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _SUBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_SUBS_FILE)
    except OSError:
        pass


_state = _load_state()
_fetch = {"status": "idle", "scanned": 0, "total": 0, "error": None}


def subtitles_map() -> dict:
    """The scan store, keyed by Jellyfin path (``{path: entry}``)."""
    return _state["files"]


def subtitles_ok() -> list[str]:
    """Jellyfin paths the user has manually marked as "Subtitle Ok"."""
    return list(_state["ok"])


def set_ok(path: str, marked: bool) -> None:
    if marked:
        if path not in _state["ok"]:
            _state["ok"].append(path)
    else:
        _state["ok"] = [k for k in _state["ok"] if k != path]
    _save_state()


# ---- classification (ported from Subber) -----------------------------------


def _subtitle_lang_name(code: str) -> str:
    c = (code or "").strip().lower()
    if not c or c == "und":
        return "Unknown"
    return _LANG_NAMES.get(c) or _FULL_LANG_NAMES.get(c) or "Unknown"


def _classify_tracks(internal: list[dict], external: list[dict]) -> dict:
    """``internal`` tracks are ``{lang, name, forced, codec}``; ``external``
    entries are ``{file, lang, forced, ai, tiny}``. Returns a flags dict."""
    good = 0
    foreign = 0
    forced = 0
    ai = 0
    for t in internal:
        if t.get("forced"):
            forced += 1
        elif t.get("lang") in GOOD_SUBTITLE_NAMES:
            good += 1
        else:
            foreign += 1
    for e in external:
        if e.get("tiny"):
            continue
        if e.get("ai"):
            ai += 1
        if e.get("forced"):
            forced += 1
        elif e.get("lang") in GOOD_SUBTITLE_NAMES:
            if not e.get("ai"):
                good += 1
        else:
            foreign += 1
    return {
        "acceptable": good > 0,
        "ai": ai > 0,
        "forced": forced > 0,
        "foreign": foreign > 0,
        "nosubs": good == 0 and foreign == 0 and forced == 0 and ai == 0,
    }


def _external_subtitles(movie_path: str) -> list[dict]:
    """External subtitle files next to ``movie_path`` (same base name)."""
    p = Path(movie_path)
    folder = p.parent
    stem = p.stem
    if not stem or not folder.is_dir():
        return []
    stem_lower = stem.lower()
    entries: list[dict] = []
    try:
        children = list(folder.iterdir())
    except OSError:
        return entries
    for f in children:
        try:
            if not f.is_file() or f.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
        except OSError:
            continue
        name = f.name.lower()
        if not name.startswith(stem_lower):
            continue
        rest = name[len(stem_lower):]
        if rest and not rest.startswith("."):
            continue
        tokens = [t for t in rest.split(".") if t]
        if tokens and ("." + tokens[-1]) in SUBTITLE_EXTENSIONS:
            tokens = tokens[:-1]
        is_ai = any(t.endswith("_ai") for t in tokens)
        forced = "forced" in tokens
        lang = "Unknown"
        for t in reversed(tokens):
            base = t[:-3] if t.endswith("_ai") else t
            if base in _SUBTITLE_NOISE_TOKENS:
                continue
            lang = _subtitle_lang_name(base)
            break
        tiny = f.stat().st_size < TINY_SUBTITLE_MAX_BYTES
        entries.append({"file": f.name, "lang": lang, "forced": forced, "ai": is_ai, "tiny": tiny})
    return entries


def _display_lines(internal: list[dict], external: list[dict]) -> list[str]:
    lines = []
    for t in internal:
        line = t["name"] + (" (Forced)" if t.get("forced") else "")
        lines.append(line)
    for e in external:
        star = "*" if e.get("tiny") else ""
        fmark = " (Forced)" if e.get("forced") else ""
        amark = " (AI)" if e.get("ai") else ""
        lines.append(f"{e['lang']}{star}{fmark}{amark} (Ext)")
    return lines


# ---- ffprobe -----------------------------------------------------------------


def _resolve_tool(env_var: str, name: str) -> str | None:
    """Locate an external binary.

    Resolution order: ``env_var`` -> ``name`` on PATH -> the bundled
    ``Tools/<name>`` (Windows ``.exe`` first) next to the app.
    """
    candidates = []
    env = (os.environ.get(env_var) or "").strip()
    if env:
        candidates.append(env)
    on_path = shutil.which(name)
    if on_path:
        candidates.append(on_path)
    root = Path(__file__).resolve().parent.parent
    for folder in ("Tools", "tools"):
        for suffix in (".exe", ""):
            candidates.append(str(root / folder / f"{name}{suffix}"))
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def resolve_ffprobe() -> str | None:
    """Return a path to a working ffprobe binary, or ``None``."""
    return _resolve_tool("JFTOOLS_FFPROBE", "ffprobe")


def resolve_ffmpeg() -> str | None:
    """Return a path to a working ffmpeg binary, or ``None``."""
    return _resolve_tool("JFTOOLS_FFMPEG", "ffmpeg")


def resolve_ffsubsync() -> str | None:
    """Return a path to ffsubsync (subtitle auto-sync), or ``None``.

    ffsubsync aligns a subtitle to the video's audio waveform; it is what makes
    "Sync" in the subtitle dialog work. Bundled ``Tools/ffsubsync.exe`` or on
    PATH is fine.
    """
    return _resolve_tool("JFTOOLS_FFSUBSYNC", "ffsubsync")


def _probe_subtitles(ffprobe: str, video_path: str) -> tuple[list[dict], float, str | None]:
    """Run ffprobe for subtitle streams + duration. Returns (tracks, duration, error)."""
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced:format=duration",
        "-of", "json",
        video_path,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", creationflags=creationflags,
        )
    except Exception as exc:  # noqa: BLE001
        return [], 0.0, f"ffprobe error: {exc}"
    if result.returncode != 0:
        return [], 0.0, f"ffprobe exit {result.returncode}: {(result.stderr or '').strip()[:200]}"
    try:
        data = json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
        return [], 0.0, f"ffprobe JSON parse failed: {exc}"

    internal = []
    for s in data.get("streams") or []:
        tags = s.get("tags") or {}
        lang = _subtitle_lang_name(tags.get("language"))
        title = (tags.get("title") or "").strip()
        name = lang if (not title or title.lower() == lang.lower()) else f"{lang} ({title})"
        forced = bool((s.get("disposition") or {}).get("forced")) or "forced" in title.lower()
        internal.append({
            # The absolute stream index, so the player can map this track
            # exactly (``-map 0:<index>``) when it extracts it to WebVTT.
            "index": s.get("index"),
            "lang": lang,
            "name": name,
            "forced": forced,
            "codec": s.get("codec_name") or "",
        })

    duration = 0.0
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return internal, duration, None


# ---- scanning ---------------------------------------------------------------


def _stat_path(local: str):
    try:
        return os.stat(local)
    except OSError:
        return None


def _stub(path: str) -> dict:
    """A minimal store entry for a library item that has not been probed yet."""
    return {"path": path, "file": Path(path).name, "scanned": False}


def _reconcile(files: dict, ok: list[str], current_paths: set[str]) -> tuple[dict, list[str]]:
    """Sync the store with the library list: add unscanned stubs for new items,
    drop entries (and ok marks) whose path is no longer listed."""
    for p in current_paths:
        if p not in files:
            files[p] = _stub(p)
    files = {k: v for k, v in files.items() if k in current_paths}
    ok = [k for k in ok if k in current_paths]
    return files, ok


def _build_worklist(rows: list[dict], files: dict) -> list[tuple[str, str, object]]:
    """Return the (key, local, st) triples to probe — every entry not yet scanned.

    Runs in a worker thread: os.stat over SMB/NFS can be slow, and doing it on
    the event loop would freeze the app during a large scan.
    """
    to_scan = []
    for r in rows:
        key = r["path"]
        if files.get(key, {}).get("scanned"):
            continue
        local = resolve_path(key, config.settings.path_maps)
        st = _stat_path(local)
        to_scan.append((key, local, st))
    return to_scan


def _probe_one(ffprobe: str, key: str, local: str, st) -> tuple[str, dict]:
    entry = {
        "path": key,
        "file": Path(key).name,
        "resolved": local,
        "size": st.st_size if st else None,
        "mtime": st.st_mtime if st else None,
        "scanned": False,
        "scanned_at": time.time(),
        "duration": 0.0,
        "internal": [],
        "external": [],
        "subs": [],
        "flags": None,
        "error": None,
    }
    if st is None:
        entry["error"] = "unreachable"
        return key, entry
    if not stat.S_ISREG(st.st_mode):
        entry["error"] = "not a file"
        return key, entry
    internal, duration, err = _probe_subtitles(ffprobe, local)
    if err:
        entry["error"] = err
        return key, entry
    external = _external_subtitles(local)
    entry["duration"] = duration
    entry["internal"] = [
        {
            "index": t.get("index"),
            "lang": t["lang"],
            "name": t["name"],
            "forced": t["forced"],
            "codec": t["codec"],
        }
        for t in internal
    ]
    entry["external"] = external
    entry["subs"] = _display_lines(internal, external)
    entry["flags"] = _classify_tracks(internal, external)
    entry["scanned"] = True
    return key, entry


async def run_scan(mode: str) -> None:
    """Probe the Movie Match items for subtitle tracks and refresh the store.

    ``mode`` is ``"scan"`` (reconcile, then probe every unscanned file) or
    ``"full"`` (mark everything unscanned first, then the same scan).
    """
    if _fetch["status"] == "running":
        return
    label = "Subtitle scan (full)" if mode == "full" else "Subtitle scan"
    _fetch.update({"status": "running", "scanned": 0, "total": 0, "error": None})
    log("Search", "INFO", f"{label} started")
    try:
        ffprobe = resolve_ffprobe()
        if not ffprobe:
            _fetch["status"] = "error"
            _fetch["error"] = (
                "ffprobe not found. Install ffmpeg (Docker) or drop ffprobe.exe "
                "into a Tools/ folder next to the app, or set JFTOOLS_FFPROBE."
            )
            log("Search", "ERROR", _fetch["error"])
            return

        from .jellyfin_id import _state as id_state

        rows = [r for r in (id_state.get("rows") or []) if r.get("path")]
        if not rows:
            _fetch["status"] = "error"
            _fetch["error"] = "No media items to scan — fetch Movie Match first."
            log("Search", "ERROR", _fetch["error"])
            return

        current_paths = {r["path"] for r in rows}
        files, ok = _reconcile(dict(_state["files"]), list(_state["ok"]), current_paths)
        if mode == "full":
            for entry in files.values():
                entry["scanned"] = False
        _state["files"] = files
        _state["ok"] = ok

        # Resolve + stat each path off the event loop — os.stat over SMB/NFS can
        # be slow, and doing it inline would freeze the app during a big scan.
        to_scan = await asyncio.to_thread(_build_worklist, rows, files)

        _fetch["total"] = len(to_scan)
        if to_scan:
            loop = asyncio.get_running_loop()
            done = 0
            pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subscan")
            try:
                futures = [
                    loop.run_in_executor(pool, _probe_one, ffprobe, key, local, st)
                    for key, local, st in to_scan
                ]
                for fut in asyncio.as_completed(futures):
                    key, entry = await fut
                    _state["files"][key] = entry
                    done += 1
                    _fetch["scanned"] = done
                    if entry.get("error"):
                        log("Search", "WARN", f"{entry['file']}: {entry['error']}")
                    else:
                        summary = ", ".join(entry.get("subs") or []) or "no subs"
                        log("Search", "INFO", f"{entry['file']}: {summary}")
            finally:
                # On abort (or completion) stop queueing probes and drop pending
                # work without waiting for it; in-flight ffprobe calls finish on
                # their own within their timeout.
                pool.shutdown(wait=False, cancel_futures=True)

        if mode == "full":
            _state["full_scan_at"] = time.time()
        else:
            _state["change_scan_at"] = time.time()
        _save_state()
        _fetch["status"] = "done"
        log(
            "Search",
            "INFO",
            f"{label} complete: {len(to_scan)} probed, {len(rows) - len(to_scan)} skipped",
        )
    except asyncio.CancelledError:
        _fetch["status"] = "cancelled"
        _fetch["error"] = None
        log("Search", "WARN", f"{label} aborted")
        raise
    except Exception as exc:  # noqa: BLE001
        _fetch["status"] = "error"
        _fetch["error"] = f"Unexpected error: {exc}"
        log("Search", "ERROR", f"{label} failed: {exc}")


async def rescan_one(path: str) -> dict:
    """Force a re-probe of one library item and update the store.

    Used by the "Rescan sub" popup action: it ignores the ``scanned`` flag and
    probes the file now, marking it scanned only on a definitive result.
    """
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return {
            "path": path,
            "file": Path(path).name,
            "scanned": False,
            "error": (
                "ffprobe not found. Install ffmpeg (Docker) or drop ffprobe.exe "
                "into a Tools/ folder next to the app, or set JFTOOLS_FFPROBE."
            ),
        }
    local = resolve_path(path, config.settings.path_maps)
    st = await asyncio.to_thread(_stat_path, local)
    key, entry = await asyncio.to_thread(_probe_one, ffprobe, path, local, st)
    _state["files"][key] = entry
    _save_state()
    if entry.get("error"):
        log("Search", "WARN", f"Rescanned {entry['file']}: {entry['error']}")
    else:
        summary = ", ".join(entry.get("subs") or []) or "no subs"
        log("Search", "INFO", f"Rescanned {entry['file']}: {summary}")
    return entry


# ---- routes -----------------------------------------------------------------


@router.post("/api/subtitles/scan")
async def subtitles_scan(
    request: Request,
    user: str = Depends(require_login),
    mode: str = Form("scan"),
):
    if mode not in ("full", "scan"):
        return JSONResponse({"ok": False, "error": "Unknown scan mode."}, status_code=400)
    from .scheduler import trigger_operation

    op = "subtitle_scan_full" if mode == "full" else "subtitle_scan"
    started, _ = trigger_operation(op)
    return JSONResponse(
        {"ok": True, "message": "Scan started." if started else "Already scanning."}
    )


@router.post("/api/subtitles/rescan")
async def subtitles_rescan(
    request: Request,
    user: str = Depends(require_login),
    path: str = Form(""),
):
    path = path.strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    from .jellyfin_id import _state as id_state

    if not any(r.get("path") == path for r in (id_state.get("rows") or [])):
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    entry = await rescan_one(path)
    return JSONResponse({"ok": True, "entry": entry})


@router.get("/api/subtitles/status")
async def subtitles_status(request: Request, user: str = Depends(require_login)):
    return JSONResponse({"ok": True, **_fetch})


@router.post("/api/subtitles/ok")
async def subtitles_ok_set(
    request: Request,
    user: str = Depends(require_login),
    path: str = Form(...),
    marked: str = Form("true"),
):
    on = marked in ("true", "1", "yes")
    set_ok(path, on)
    log("Search", "INFO", f"Subtitle Ok {'marked' if on else 'unmarked'}: {path}")
    return JSONResponse({"ok": True})


# ---- OpenSubtitles download -------------------------------------------------


def _library_row(path: str) -> dict | None:
    """The Movie Match row for a Jellyfin path, or None."""
    from .jellyfin_id import _state as id_state

    for r in id_state.get("rows") or []:
        if r.get("path") == path:
            return r
    return None


async def subtitle_tracks_for(path: str) -> tuple[list[dict], list[dict]]:
    """``(embedded, external)`` subtitle tracks for a library video path.

    Embedded tracks come from the scan store when the scan has seen this file,
    otherwise from a one-off ffprobe. External tracks are the subtitle files
    sitting next to the video. Shared by the subtitle dialog and the player.
    """
    stored = (_state["files"].get(path) or {}).get("internal")
    internal = list(stored or [])

    external: list[dict] = []
    local = resolve_path(path, config.settings.path_maps)
    if local and os.path.isfile(local):
        folder = os.path.dirname(local)
        for e in _external_subtitles(local):
            try:
                size = os.path.getsize(os.path.join(folder, e["file"]))
            except OSError:
                size = None
            external.append({**e, "size": size})

        if stored is None:
            ffprobe = resolve_ffprobe()
            if ffprobe:
                tracks, _duration, err = await asyncio.to_thread(
                    _probe_subtitles, ffprobe, local
                )
                if not err:
                    internal = tracks

    return internal, external


def _season_episode(path: str) -> tuple[int | None, int | None]:
    """Season/episode numbers parsed from a file path (``SxxExx``/``1x02``)."""
    m = re.search(r"[sS](\d{1,2})[eE](\d{1,2})", path or "")
    if not m:
        m = re.search(r"(?:^|[^0-9])(\d{1,2})[xX](\d{2})(?:[^0-9]|$)", path or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _download_languages(requested: str) -> list[str]:
    from .opensubtitles import normalize_languages

    langs = normalize_languages(requested) or normalize_languages(
        config.settings.opensubtitles_languages
    )
    return langs or ["en"]


def _write_gate(x_jftools: str):
    """403 unless File management is on and the CSRF header is present."""
    if not config.settings.file_mgmt:
        return JSONResponse(
            {
                "ok": False,
                "file_mgmt": False,
                "error": (
                    "File management is disabled. Enable it on the Settings "
                    "page to change subtitle files."
                ),
            },
            status_code=403,
        )
    if x_jftools != "1":
        return JSONResponse({"ok": False, "error": "Missing security header."}, status_code=403)
    return None


def _mark_unscanned(path: str) -> None:
    """Flag a library item for a fresh subtitle scan after its subs change.

    The Search tab then shows "Not scanned", the flag filters stop matching
    stale data, and the next scan (the Subtitle scan button, closing the dialog,
    or the scheduled job) re-probes it. The embedded tracks are kept so the
    dialog can still list them without a fresh probe. The manual "Subtitle Ok"
    mark is dropped too — the state changed.
    """
    entry = _state["files"].get(path) or _stub(path)
    entry["scanned"] = False
    entry["external"] = []
    entry["subs"] = []
    entry["flags"] = None
    entry["error"] = None
    _state["files"][path] = entry
    _state["ok"] = [k for k in _state["ok"] if k != path]
    _save_state()


def _sync_subtitle_file(
    video: str, subtitle: str, offset: float | None
) -> tuple[bool, str | None, str]:
    """Sync ``subtitle`` to ``video`` in place. Returns ``(ok, error, method)``.

    With no offset this runs ffsubsync (audio-waveform alignment). A manual
    offset shifts the file with ffmpeg instead (srt only). The original file is
    only replaced once the tool produced valid output, so a failure leaves it
    untouched.
    """
    target = Path(subtitle)
    # A dotted temp name keeps it hidden from ``_external_subtitles`` while it
    # exists, and the real extension lets the tool infer the format.
    tmp = str(target.with_name(f".{target.stem}.jfsync{target.suffix}"))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def run(cmd: list[str]) -> tuple[bool, str | None]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                encoding="utf-8", errors="replace", creationflags=creationflags,
            )
        except FileNotFoundError:
            return False, "the tool could not be started."
        except subprocess.TimeoutExpired:
            return False, "the sync timed out."
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        if result.returncode != 0:
            lines = [ln for ln in (result.stderr or result.stdout or "").splitlines() if ln.strip()]
            return False, (lines[-1].strip() if lines else f"exit code {result.returncode}")
        return True, None

    try:
        if offset is None:
            ffsubsync = resolve_ffsubsync()
            if not ffsubsync:
                return False, "ffsubsync was not found.", "none"
            cmd = [ffsubsync, "-i", subtitle, "-o", tmp]
            ffmpeg = resolve_ffmpeg()
            if ffmpeg:
                cmd += ["--ffmpeg-path", ffmpeg]
            cmd.append(video)
            ok, err = run(cmd)
            if not ok:
                return False, f"ffsubsync: {err}", "ffsubsync"
            method = "ffsubsync"
        else:
            if target.suffix.lower() != ".srt":
                return False, "a manual offset only works for .srt subtitles.", "ffmpeg"
            ffmpeg = resolve_ffmpeg()
            if not ffmpeg:
                return False, "ffmpeg was not found.", "ffmpeg"
            cmd = [
                ffmpeg,
                "-i", video,
                "-itsoffset", str(offset),
                "-i", subtitle,
                "-map", "1:s:0",
                "-c", "srt",
                "-y",
                tmp,
            ]
            ok, err = run(cmd)
            if not ok:
                return False, f"ffmpeg: {err}", "ffmpeg"
            method = "ffmpeg"

        if not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
            return False, "the sync produced no output.", method
        os.replace(tmp, subtitle)
        return True, None, method
    finally:
        if os.path.lexists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


@router.post("/api/subtitles/search")
async def subtitles_search(
    user: str = Depends(require_login),
    path: str = Form(""),
    language: str = Form(""),
):
    """Search OpenSubtitles for a library file — by moviehash first, then by name."""
    path = path.strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    row = _library_row(path)
    if row is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    if not config.settings.opensubtitles_api_key:
        return JSONResponse(
            {
                "ok": False,
                "configured": False,
                "error": (
                    "OpenSubtitles is not configured. Add an API key on the "
                    "Settings page (opensubtitles.com → Consumers)."
                ),
            },
            status_code=400,
        )

    from .opensubtitles import OpenSubtitlesError, get_opensubtitles, opensubtitles_hash

    client = get_opensubtitles()
    langs = _download_languages(language)
    base = {
        "ok": True,
        "file": Path(path).name,
        "languages": langs,
        "file_mgmt": config.settings.file_mgmt,
        "remaining": client.remaining(),
    }

    # 1. Hash search — exact match for this specific release.
    method = "hash"
    result = None
    local = resolve_path(path, config.settings.path_maps)
    if local and os.path.isfile(local):
        try:
            moviehash, size = await asyncio.to_thread(opensubtitles_hash, local)
            result = await client.search(languages=langs, moviehash=moviehash)
            log(
                "Search",
                "INFO",
                f"Subtitle hash search {Path(path).name}: "
                f"{moviehash} ({size} B) -> {result['total_count']} result(s)",
            )
        except OpenSubtitlesError as exc:
            return JSONResponse(
                {**base, "ok": False, "error": str(exc)}, status_code=502
            )
    elif local:
        base["note"] = "File unreachable — searched by name only."

    # 2. Name fallback when the hash found nothing.
    if result is None or result["total_count"] == 0:
        method = "name"
        row_type = row.get("type") or ""
        query = row.get("series") if row_type in ("Episode", "Series") else (row.get("name") or row.get("display") or "")
        query = query.strip()
        if not query:
            query = Path(path).name
        season, episode = _season_episode(path)
        try:
            result = await client.search(
                languages=langs,
                query=query,
                type="episode" if row_type in ("Episode", "Series") else "movie",
                season_number=season,
                episode_number=episode,
            )
            log(
                "Search",
                "INFO",
                f"Subtitle name search {Path(path).name}: "
                f"\"{query}\" -> {result['total_count']} result(s)",
            )
        except OpenSubtitlesError as exc:
            return JSONResponse(
                {**base, "ok": False, "error": str(exc)}, status_code=502
            )

    return JSONResponse(
        {
            **base,
            "method": method,
            "total": result["total_count"] if result else 0,
            "candidates": (result or {}).get("data") or [],
        }
    )


@router.post("/api/subtitles/download")
async def subtitles_download(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
    file_id: str = Form(""),
    sub_format: str = Form("srt"),
    language: str = Form(""),
    overwrite: str = Form(""),
):
    """Download one subtitle file and save it next to the video.

    Writing into the media folders requires the File management setting and the
    same-origin CSRF header as the other file mutations.
    """
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = path.strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    row = _library_row(path)
    if row is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    try:
        file_id_int = int(file_id)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Invalid subtitle id."}, status_code=400)

    local = resolve_path(path, config.settings.path_maps)
    if not local or not os.path.isfile(local):
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "The video file is not reachable. Set the Media paths on "
                    "the Settings page first."
                ),
            },
            status_code=400,
        )

    from .opensubtitles import (
        OpenSubtitlesError,
        get_opensubtitles,
        normalize_languages,
        sub_extension,
    )

    langs = _download_languages(language)
    lang_code = re.sub(r"[^a-z0-9]", "", (langs[0] or "").lower()) or "und"
    ext = sub_extension(sub_format)
    target = os.path.join(os.path.dirname(local), f"{Path(local).stem}.{lang_code}{ext}")
    if os.path.lexists(target) and overwrite.strip().lower() not in ("1", "true", "yes"):
        return JSONResponse(
            {
                "ok": False,
                "exists": True,
                "error": f"{os.path.basename(target)} already exists.",
            },
            status_code=409,
        )

    client = get_opensubtitles()
    try:
        data = await client.download(file_id_int, sub_format)
    except OpenSubtitlesError as exc:
        log("Search", "ERROR", f"Subtitle download failed: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    def _write(content: bytes) -> None:
        tmp = target + ".part"
        with open(tmp, "wb") as fh:
            fh.write(content)
        os.replace(tmp, target)

    try:
        await asyncio.to_thread(_write, data["content"])
    except OSError as exc:
        log("Search", "ERROR", f"Subtitle write failed: {target}: {exc}")
        return JSONResponse(
            {"ok": False, "error": f"Could not save the subtitle: {exc}"},
            status_code=500,
        )

    remaining = data.get("remaining")
    log(
        "Search",
        "INFO",
        f"Downloaded subtitle: {os.path.basename(target)} "
        f"({remaining if remaining is not None else '?'} downloads remaining)",
    )

    # Mark the item for a fresh scan rather than probing now: closing the dialog
    # (or the Subtitle scan button / scheduled job) does the actual ffprobe pass.
    # Until then the Search tab shows "Not scanned".
    _mark_unscanned(path)

    # Tell Jellyfin a subtitle appeared so it can pick it up right away.
    jellyfin = None
    try:
        from .jellyfin.client import JellyfinError, get_client

        await get_client().scan_paths([path])
    except JellyfinError as exc:
        jellyfin = str(exc)
        log("Search", "WARN", f"Jellyfin rescan after download failed: {exc}")

    return JSONResponse(
        {
            "ok": True,
            "message": f"Saved {os.path.basename(target)}.",
            "file": os.path.basename(target),
            "remaining": remaining,
            "unscanned": True,
            "jellyfin": jellyfin,
        }
    )


@router.post("/api/subtitles/existing")
async def subtitles_existing(
    user: str = Depends(require_login),
    path: str = Form(""),
):
    """Every subtitle track for a library video: embedded tracks + files."""
    path = path.strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )

    internal, external = await subtitle_tracks_for(path)

    return JSONResponse(
        {
            "ok": True,
            "file_mgmt": config.settings.file_mgmt,
            "internal": internal,
            "external": external,
        }
    )


@router.post("/api/subtitles/delete")
async def subtitles_delete(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
    file: str = Form(""),
):
    """Delete one external subtitle file next to a library video.

    Write-gated like the other file mutations. The requested file must be one
    of the video's own external subtitles (recomputed here), so this can never
    delete an arbitrary file even if the caller is authenticated.
    """
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = (path or "").strip()
    file = (file or "").strip()
    if not path or not file:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    local = resolve_path(path, config.settings.path_maps)
    if not local or not os.path.isfile(local):
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."}, status_code=400
        )
    if file not in {e["file"] for e in _external_subtitles(local)}:
        return JSONResponse(
            {
                "ok": False,
                "error": "That subtitle does not belong to this video.",
            },
            status_code=403,
        )

    from .files import browser_roots, delete_entry

    result = delete_entry(os.path.join(os.path.dirname(local), file), browser_roots())
    if not result.get("ok"):
        log("Search", "WARN", f"Subtitle delete refused ({result.get('error')}): {file}")
        return JSONResponse(
            {"ok": False, "error": result.get("error") or "Delete failed."},
            status_code=400,
        )

    _mark_unscanned(path)

    # Tell Jellyfin the file changed so the subtitle disappears from its view.
    jellyfin = None
    try:
        from .jellyfin.client import JellyfinError, get_client

        await get_client().scan_paths([path])
    except JellyfinError as exc:
        jellyfin = str(exc)
        log("Search", "WARN", f"Jellyfin rescan after delete failed: {exc}")

    log("Search", "INFO", f"Deleted subtitle: {os.path.basename(result['src'])}")
    return JSONResponse(
        {
            "ok": True,
            "message": f"Deleted {file}.",
            "file": file,
            "jellyfin": jellyfin,
        }
    )


@router.post("/api/subtitles/sync")
async def subtitles_sync(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
    file: str = Form(""),
    offset: str = Form(""),
):
    """Re-align one external subtitle file to its video.

    Write-gated like the other file mutations. Without an offset the file is
    aligned with ffsubsync; a numeric ``offset`` (seconds) shifts it with ffmpeg
    instead. The requested file must be one of the video's own external
    subtitles, and the original is only replaced once the tool succeeds.
    """
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = (path or "").strip()
    file = (file or "").strip()
    if not path or not file:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    local = resolve_path(path, config.settings.path_maps)
    if not local or not os.path.isfile(local):
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."}, status_code=400
        )
    if file not in {e["file"] for e in _external_subtitles(local)}:
        return JSONResponse(
            {"ok": False, "error": "That subtitle does not belong to this video."},
            status_code=403,
        )

    offset_val: float | None = None
    if isinstance(offset, str) and offset.strip():
        try:
            offset_val = float(offset)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "The offset must be a number of seconds."},
                status_code=400,
            )
    if offset_val is None and not resolve_ffsubsync():
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "ffsubsync is required to sync subtitles. Put ffsubsync.exe in "
                    "the Tools folder, install it on PATH, or set JFTOOLS_FFSUBSYNC."
                ),
            },
            status_code=400,
        )

    subtitle = os.path.join(os.path.dirname(local), file)
    ok, err, method = await asyncio.to_thread(
        _sync_subtitle_file, local, subtitle, offset_val
    )
    if not ok:
        log("Search", "WARN", f"Subtitle sync failed ({err}): {file}")
        return JSONResponse({"ok": False, "error": err or "Sync failed."}, status_code=502)

    # The filename and language do not change, so there is nothing else to do:
    # Jellyfin reads the subtitle from disk, and the stored track data (name,
    # language, forced) is still accurate.
    if method == "ffmpeg":
        message = f"Shifted {file} by {offset_val}s."
    else:
        message = f"Synced {file}."
    log("Search", "INFO", f"{message} ({method})")
    return JSONResponse(
        {
            "ok": True,
            "message": message,
            "file": file,
            "method": method,
        }
    )
