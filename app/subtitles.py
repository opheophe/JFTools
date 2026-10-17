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
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
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


def resolve_ffprobe() -> str | None:
    """Return a path to a working ffprobe binary, or ``None``.

    Resolution order: ``JFTOOLS_FFPROBE`` env var -> ``ffprobe`` on PATH -> the
    bundled ``Tools/ffprobe.exe`` (Windows) next to the app.
    """
    candidates = []
    env = (os.environ.get("JFTOOLS_FFPROBE") or "").strip()
    if env:
        candidates.append(env)
    on_path = shutil.which("ffprobe")
    if on_path:
        candidates.append(on_path)
    root = Path(__file__).resolve().parent.parent
    for folder in ("Tools", "tools"):
        for name in ("ffprobe.exe", "ffprobe"):
            candidates.append(str(root / folder / name))
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


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


def _needs_scan(entry: dict | None, st, mode: str) -> bool:
    if mode == "full":
        return True
    if entry is None:
        return True
    if entry.get("error"):
        return True
    if st is None:
        return True
    return entry.get("size") != st.st_size or entry.get("mtime") != st.st_mtime


def _probe_one(ffprobe: str, key: str, local: str, st) -> tuple[str, dict]:
    entry = {
        "path": key,
        "file": Path(key).name,
        "resolved": local,
        "size": st.st_size if st else None,
        "mtime": st.st_mtime if st else None,
        "scanned": time.time(),
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
        {"lang": t["lang"], "name": t["name"], "forced": t["forced"], "codec": t["codec"]}
        for t in internal
    ]
    entry["external"] = external
    entry["subs"] = _display_lines(internal, external)
    entry["flags"] = _classify_tracks(internal, external)
    return key, entry


def _prune_files(files: dict, ok: list[str], current_paths: set[str]) -> tuple[dict, list[str]]:
    """Drop entries (and ok marks) whose path is no longer in the item list."""
    files = {k: v for k, v in files.items() if k in current_paths}
    ok = [k for k in ok if k in current_paths]
    return files, ok


async def run_scan(mode: str) -> None:
    """Probe the Movie Match items for subtitle tracks and refresh the store."""
    if _fetch["status"] == "running":
        return
    _fetch.update({"status": "running", "scanned": 0, "total": 0, "error": None})
    log("Search", "INFO", f"Subtitle {mode} scan started")
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
        files, ok = _prune_files(dict(_state["files"]), list(_state["ok"]), current_paths)
        _state["files"] = files
        _state["ok"] = ok

        # Resolve + stat each path, then decide what still needs probing.
        to_scan = []
        for r in rows:
            key = r["path"]
            local = resolve_path(key, config.settings.path_maps)
            st = _stat_path(local)
            if _needs_scan(files.get(key), st, mode):
                to_scan.append((key, local, st))

        _fetch["total"] = len(to_scan)
        if to_scan:
            loop = asyncio.get_running_loop()
            done = 0
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="subscan") as pool:
                futures = [
                    loop.run_in_executor(pool, _probe_one, ffprobe, key, local, st)
                    for key, local, st in to_scan
                ]
                for fut in asyncio.as_completed(futures):
                    key, entry = await fut
                    _state["files"][key] = entry
                    done += 1
                    _fetch["scanned"] = done

        if mode == "full":
            _state["full_scan_at"] = time.time()
        else:
            _state["change_scan_at"] = time.time()
        _save_state()
        _fetch["status"] = "done"
        log("Search", "INFO", f"Subtitle {mode} scan complete: {len(to_scan)} probed")
    except Exception as exc:  # noqa: BLE001
        _fetch["status"] = "error"
        _fetch["error"] = f"Unexpected error: {exc}"
        log("Search", "ERROR", f"Subtitle {mode} scan failed: {exc}")


# ---- routes -----------------------------------------------------------------


@router.post("/api/subtitles/scan")
async def subtitles_scan(
    request: Request,
    user: str = Depends(require_login),
    mode: str = Form("changed"),
):
    if mode not in ("full", "changed"):
        return JSONResponse({"ok": False, "error": "Unknown scan mode."}, status_code=400)
    from .scheduler import trigger_operation

    started, _ = trigger_operation(f"subtitle_scan_{mode}")
    return JSONResponse(
        {"ok": True, "message": "Scan started." if started else "Already scanning."}
    )


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
