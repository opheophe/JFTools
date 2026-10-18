"""Playback — stream a library video to the browser, with its subtitle tracks.

The subtitle dialog and the Actions menus open ``/play`` in a small popup
window. That page streams the original file (or a remuxed copy the browser can
decode) and offers every subtitle track the scanner knows about — embedded
streams are extracted with ffmpeg, external files are converted to WebVTT.

Everything here is read-only, so it needs a login but not the File management
setting. Paths go through the Media-path mappings and must land inside the
browsable media roots, exactly like the file browser.
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .config import config
from .files import _resolve, browser_roots
from .logs import log
from .subtitles import (
    _external_subtitles,
    _library_row,
    _probe_subtitles,
    resolve_ffmpeg,
    resolve_ffprobe,
    subtitle_tracks_for,
)
from .web import require_login, templates

router = APIRouter()

# Subtitle codecs that are bitmaps rather than text: a browser can't render
# them, so the player can list them but not show them.
IMAGE_SUB_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext",
}

# Containers the browser should be told about; ``mimetypes`` misses most of them.
VIDEO_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ogv": "video/ogg",
}

# External subtitle formats ffmpeg can turn into text for the browser.
CONVERTIBLE_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".sbv", ".ttml"}

# Remuxed "browser copies" the player can fall back to.
PLAY_CACHE_MAX_BYTES = 5 * 1024**3
PLAY_CACHE_MAX_AGE = 7 * 24 * 3600
PREPARE_TIMEOUT = 2 * 3600

# Converted WebVTT text, keyed by (path, track, mtime). Tiny: a subtitle file is
# a few hundred kilobytes at most.
_SUB_CACHE: dict[tuple[str, str, float], str] = {}
_SUB_CACHE_MAX = 24

# One remux job at a time, polled by the player page.
_prepare: dict = {"status": "idle", "path": "", "percent": 0, "error": None}
_prepare_task: asyncio.Task | None = None


class SubError(Exception):
    """A subtitle track that can't be shown in a browser."""


# ---- paths and helpers -------------------------------------------------------


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _local_video(path: str) -> str | None:
    """Canonical local path of a library video inside the media roots."""
    raw = (path or "").strip()
    if not raw:
        return None
    target = _resolve(raw, browser_roots())
    if not target or not os.path.isfile(target):
        return None
    return target


def _video_media_type(local: str) -> str:
    ext = Path(local).suffix.lower()
    if ext in VIDEO_TYPES:
        return VIDEO_TYPES[ext]
    return mimetypes.guess_type(local)[0] or "application/octet-stream"


def _parse_spec(spec: str) -> tuple[str, str]:
    kind, _, key = (spec or "").partition(":")
    return kind.strip().lower(), key.strip()


def _internal_spec(track: dict, ordinal: int) -> str:
    """Track spec for an embedded stream.

    ``internal:<n>`` is ffprobe's absolute stream index; ``sub:<n>`` (used when
    an older scan store has no index yet) is the n-th subtitle stream.
    """
    index = track.get("index")
    if isinstance(index, int):
        return f"internal:{index}"
    return f"sub:{ordinal}"


def _read_text(path: str) -> str:
    """Decode a subtitle file, falling back through the usual encodings."""
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def srt_to_vtt(text: str) -> str:
    """Convert SubRip text to WebVTT (same cues, WebVTT header and timestamps)."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        if "-->" in line:
            line = re.sub(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", line)
        lines.append(line)
    body = "\n".join(lines).strip("\n")
    return f"WEBVTT\n\n{body}\n"


def _hms_to_seconds(value: str) -> float:
    parts = (value or "").strip().split(":")
    if not parts:
        return 0.0
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total
    except ValueError:
        return 0.0


def _ttl_cache_put(key: tuple[str, str, float], value: str) -> None:
    if len(_SUB_CACHE) >= _SUB_CACHE_MAX:
        for old in list(_SUB_CACHE)[: _SUB_CACHE_MAX // 2]:
            _SUB_CACHE.pop(old, None)
    _SUB_CACHE[key] = value


# ---- subtitle conversion -----------------------------------------------------


def _ffmpeg_to_vtt(args: list[str]) -> str:
    """Run ffmpeg with ``args`` and return the WebVTT it writes.

    ffmpeg's WebVTT muxer needs a seekable output, so this writes a temp file
    rather than piping to stdout.
    """
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise SubError("ffmpeg is not available, so this subtitle can't be converted.")
    tmpdir = tempfile.mkdtemp(prefix="jftools-sub-")
    out = os.path.join(tmpdir, "out.vtt")
    try:
        cmd = [ffmpeg, "-v", "error", "-y", *args, "-f", "webvtt", out]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180,
                encoding="utf-8", errors="replace", creationflags=_no_window(),
            )
        except Exception as exc:  # noqa: BLE001
            raise SubError(f"Subtitle conversion failed: {exc}") from exc
        if result.returncode != 0 or not os.path.isfile(out):
            detail = (result.stderr or "").strip().splitlines()
            raise SubError(
                "This subtitle could not be converted for the browser"
                + (f": {detail[-1][:200]}" if detail else ".")
            )
        text = _read_text(out)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if "-->" not in text:
        raise SubError(
            "This track holds image-based subtitles (PGS/DVD), which a browser "
            "can't display."
        )
    return text


def _vtt_external(local: str, filename: str) -> str:
    """WebVTT for one external subtitle file next to ``local``."""
    folder = os.path.dirname(local)
    known = {e["file"] for e in _external_subtitles(local)}
    if filename not in known:
        raise SubError("No such subtitle file for this video.")
    full = os.path.join(folder, filename)
    ext = Path(filename).suffix.lower()
    if ext == ".vtt":
        text = _read_text(full)
        return text if "WEBVTT" in text[:64].upper() else srt_to_vtt(text)
    if ext == ".srt":
        return srt_to_vtt(_read_text(full))
    if ext in CONVERTIBLE_SUBTITLE_EXTENSIONS:
        return _ffmpeg_to_vtt(["-i", full])
    raise SubError(f"{ext or 'This'} subtitles can't be shown in a browser.")


def _vtt_embedded(local: str, kind: str, key: str) -> str:
    """WebVTT for one embedded subtitle stream of ``local``."""
    try:
        number = int(key)
    except (TypeError, ValueError):
        raise SubError("Unknown subtitle track.") from None
    map_arg = f"0:{number}" if kind == "internal" else f"0:s:{number}"
    return _ffmpeg_to_vtt(["-i", local, "-map", map_arg])


# ---- browser copies (remux) --------------------------------------------------


def _play_cache_dir() -> Path:
    return config.data_dir / "play_cache"


def _cache_file(path: str, local: str) -> Path:
    try:
        st = os.stat(local)
        seed = f"{os.path.normcase(os.path.abspath(local))}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        seed = path
    key = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:20]
    return _play_cache_dir() / f"{key}.mp4"


def _prepared_file(path: str) -> str | None:
    """The cached browser copy for ``path``, if one has already been made."""
    local = _local_video(path)
    if not local:
        return None
    dest = _cache_file(path, local)
    return str(dest) if dest.is_file() else None


def prune_play_cache() -> None:
    """Drop stale browser copies: older than a week, then oldest-first over 5 GB."""
    folder = _play_cache_dir()
    if not folder.is_dir():
        return
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    for f in folder.glob("*.mp4"):
        try:
            st = f.stat()
        except OSError:
            continue
        if now - st.st_mtime > PLAY_CACHE_MAX_AGE:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        files.append((st.st_mtime, st.st_size, f))
    total = sum(size for _m, size, _f in files)
    for _m, size, f in sorted(files):
        if total <= PLAY_CACHE_MAX_BYTES:
            break
        try:
            f.unlink()
            total -= size
        except OSError:
            pass
    for part in folder.glob("*.part.mp4"):
        try:
            if now - part.stat().st_mtime > 3600:
                part.unlink()
        except OSError:
            pass


async def _duration_for(path: str, local: str) -> float:
    """Total runtime, from the scan store when it has it, else a quick ffprobe."""
    from .subtitles import _state as sub_state

    stored = ((sub_state.get("files") or {}).get(path) or {}).get("duration") or 0
    if stored:
        return float(stored)
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return 0.0
    _tracks, duration, _err = await asyncio.to_thread(_probe_subtitles, ffprobe, local)
    return float(duration or 0.0)


def _run_ffmpeg_with_progress(cmd: list[str], duration: float) -> tuple[int, str]:
    """Run ffmpeg, keeping ``_prepare["percent"]`` up to date. Returns (code, stderr)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_no_window(),
    )
    tail = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time" and duration > 0:
                seconds = _hms_to_seconds(value)
                percent = int(max(0.0, min(99.0, seconds / duration * 100)))
                _prepare["percent"] = percent
            elif key == "progress" and value.strip() == "end":
                _prepare["percent"] = 99
        tail = (proc.stderr.read() if proc.stderr else "") or ""
    finally:
        code = proc.wait()
    return code, tail


async def _run_prepare(ffmpeg: str, path: str, local: str) -> None:
    """Remux ``local`` into a browser-friendly MP4, then swap it into the cache."""
    dest = _cache_file(path, local)
    tmp = dest.with_name(f"{dest.stem}.part.mp4")
    try:
        prune_play_cache()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if tmp.is_file():
            tmp.unlink()
        duration = await _duration_for(path, local)
        cmd = [
            ffmpeg, "-v", "error", "-y",
            "-i", local,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-sn", "-dn",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(tmp),
        ]
        log("Player", "INFO", f"Making a browser copy of {os.path.basename(local)}")
        code, tail = await asyncio.to_thread(
            _run_ffmpeg_with_progress, cmd, duration
        )
        if code != 0 or not tmp.is_file():
            detail = (tail or "").strip().splitlines()
            _prepare.update({
                "status": "error",
                "percent": 0,
                "error": "ffmpeg could not remux this file"
                         + (f": {detail[-1][:200]}" if detail else "."),
            })
            log("Player", "WARN", f"Browser copy failed for {os.path.basename(local)}")
            return
        os.replace(tmp, dest)
        _prepare.update({"status": "done", "percent": 100, "error": None})
        log("Player", "INFO", f"Browser copy ready: {dest.name}")
    except Exception as exc:  # noqa: BLE001
        _prepare.update({
            "status": "error", "percent": 0,
            "error": f"Could not make a browser copy: {exc}",
        })
    finally:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


# ---- routes ------------------------------------------------------------------


@router.get("/play", response_class=HTMLResponse)
async def play_page(
    request: Request,
    user: str = Depends(require_login),
    path: str = Query(""),
    track: str = Query(""),
):
    """The player window itself (opened by the Actions menus and the dialog)."""
    path = (path or "").strip()
    name = os.path.basename(path.replace("\\", "/")) or "Player"
    return templates.TemplateResponse(
        request,
        "play.html",
        {"request": request, "path": path, "track": (track or "").strip(), "file": name},
    )


@router.get("/api/player/tracks")
async def player_tracks(user: str = Depends(require_login), path: str = Query("")):
    """Every subtitle track for one library video, as the player should list it."""
    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )

    internal, external = await subtitle_tracks_for(path)
    tracks: list[dict] = []
    for ordinal, t in enumerate(internal):
        codec = (t.get("codec") or "").lower()
        playable = codec not in IMAGE_SUB_CODECS
        label = t.get("name") or t.get("lang") or f"Track {ordinal + 1}"
        if not playable:
            label += " — image subtitles"
        tracks.append({
            "spec": _internal_spec(t, ordinal),
            "kind": "internal",
            "label": label,
            "lang": t.get("lang") or "",
            "codec": codec,
            "forced": bool(t.get("forced")),
            "playable": playable,
        })
    for e in external:
        label = e.get("lang") or "Unknown"
        if e.get("forced"):
            label += " (forced)"
        if e.get("ai"):
            label += " (AI)"
        # The file name too: a film often has an English embedded track *and* an
        # English .srt, and "English" twice in the selector is no help.
        label += f" — {e['file']}"
        tracks.append({
            "spec": f"external:{e['file']}",
            "kind": "external",
            "label": label,
            "file": e["file"],
            "lang": e.get("lang") or "",
            "codec": Path(e["file"]).suffix.lstrip(".").lower(),
            "forced": bool(e.get("forced")),
            "playable": True,
        })

    return JSONResponse({
        "ok": True,
        "file": os.path.basename(path.replace("\\", "/")),
        "tracks": tracks,
    })


@router.get("/api/player/sub")
async def player_sub(
    user: str = Depends(require_login),
    path: str = Query(""),
    track: str = Query(""),
):
    """The WebVTT for one subtitle track, for a ``<track>`` element."""
    local = _local_video(path)
    if not local:
        return Response(
            "The video file is not reachable. Set the Media paths on Settings.",
            status_code=404, media_type="text/plain",
        )

    kind, key = _parse_spec(track)
    try:
        mtime = os.stat(local).st_mtime
    except OSError:
        mtime = 0.0
    cache_key = (path, track, mtime)
    cached = _SUB_CACHE.get(cache_key)
    if cached is None:
        try:
            if kind == "external":
                cached = await asyncio.to_thread(_vtt_external, local, key)
            elif kind in ("internal", "sub"):
                cached = await asyncio.to_thread(_vtt_embedded, local, kind, key)
            else:
                return Response("Unknown subtitle track.", status_code=400, media_type="text/plain")
        except SubError as exc:
            return Response(str(exc), status_code=415, media_type="text/plain")
        _ttl_cache_put(cache_key, cached)

    return Response(
        cached,
        media_type="text/vtt",
        headers={"Cache-Control": "no-store"},
    )


@router.api_route("/api/player/stream", methods=["GET", "HEAD"])
async def player_stream(
    user: str = Depends(require_login),
    path: str = Query(""),
    prepared: str = Query(""),
):
    """Stream the video. ``FileResponse`` handles Range, so the browser can seek."""
    if prepared:
        local = _prepared_file(path)
        if not local:
            return JSONResponse(
                {"ok": False, "error": "No browser copy for this file."},
                status_code=404,
            )
    else:
        local = _local_video(path)
        if not local:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "The video file is not reachable. Set the Media paths "
                             "on the Settings page.",
                },
                status_code=404,
            )
    return FileResponse(
        local,
        media_type=_video_media_type(local),
        headers={"Cache-Control": "no-store"},
    )


def _prepare_running() -> bool:
    return _prepare_task is not None and not _prepare_task.done()


@router.get("/api/player/prepare")
async def player_prepare(user: str = Depends(require_login), path: str = Query("")):
    """Start (or report) an ffmpeg remux into a browser-friendly MP4."""
    global _prepare_task

    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    local = _local_video(path)
    if not local:
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."}, status_code=404
        )
    if _prepared_file(path):
        return JSONResponse({"ok": True, "status": "done", "percent": 100})

    if _prepare_running():
        if _prepare.get("path") == path:
            return JSONResponse(
                {"ok": True, "status": "running", "percent": _prepare["percent"]}
            )
        return JSONResponse(
            {"ok": False, "error": "Another file is already being prepared."},
            status_code=409,
        )

    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return JSONResponse(
            {
                "ok": False,
                "error": "ffmpeg is not available, so a browser copy can't be made.",
            },
            status_code=400,
        )

    _prepare.update({
        "status": "running", "path": path, "percent": 0, "error": None,
    })
    _prepare_task = asyncio.create_task(_run_prepare(ffmpeg, path, local))
    return JSONResponse({"ok": True, "status": "running", "percent": 0})


@router.get("/api/player/prepare/status")
async def player_prepare_status(user: str = Depends(require_login), path: str = Query("")):
    """Progress for the browser copy of one file."""
    path = (path or "").strip()
    if _prepare.get("path") == path and not _prepare_running():
        if _prepare["status"] == "error":
            return JSONResponse({
                "ok": True, "status": "error", "percent": 0,
                "error": _prepare["error"],
            })
    if _prepare.get("path") == path and _prepare_running():
        percent = int(_prepare["percent"] or 0)
        return JSONResponse({
            "ok": True, "status": "running", "percent": percent,
            "message": f"Making a browser copy… {percent}%",
        })
    if path and _prepared_file(path):
        return JSONResponse({"ok": True, "status": "done", "percent": 100})
    return JSONResponse({"ok": True, "status": "idle", "percent": 0})
