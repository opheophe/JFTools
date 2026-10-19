"""Audio tracks — list a video's audio streams and delete one of them.

Scanning lives in :mod:`app.subtitles` (one ffprobe fills both the subtitle and
the audio halves of ``data/scans.json``); this module serves the Search tab's
"Audio" dialog and the one destructive action it offers.

Deleting a track copies every other stream into a temporary file next to the
original with ``-c copy`` and replaces the original only after ffmpeg succeeds.
A failure (unsupported container, full disk, ffmpeg missing) therefore leaves
the video untouched. The approach mirrors ComTool's ``VTDelete``.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile

from fastapi import APIRouter, Depends, Form, Header
from fastapi.responses import JSONResponse

from .config import config
from .logs import log
from .paths import resolve_path
from .subtitles import (
    _library_row,
    _probe_one,
    _probe_streams,
    _save_state,
    _stat_path,
    _state,
    _write_gate,
    audio_tracks_for,
    resolve_ffmpeg,
    resolve_ffprobe,
)
from .web import require_login

router = APIRouter()

# One rewrite at a time per file: two deletes racing would each copy the old
# file and the second replace would undo the first.
_delete_locks: dict[str, asyncio.Lock] = {}


# ---- routes -----------------------------------------------------------------


@router.post("/api/audio/existing")
async def audio_existing(
    user: str = Depends(require_login),
    path: str = Form(""),
):
    """The audio tracks of one library video, freshly probed."""
    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )

    tracks, error = await audio_tracks_for(path, force=True)
    return JSONResponse(
        {
            "ok": True,
            "file_mgmt": config.settings.file_mgmt,
            "audio": tracks,
            "error": error,
        }
    )


@router.post("/api/audio/delete")
async def audio_delete(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
    index: str = Form(""),
):
    """Remove one audio stream from a library video, in place.

    Write-gated like the other file mutations. The stream is re-probed here and
    must be an audio stream of this very file, so the caller cannot make the
    server map an arbitrary stream away.
    """
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = (path or "").strip()
    index = (index or "").strip()
    if not path or not index:
        return JSONResponse(
            {"ok": False, "error": "No file path or audio track."}, status_code=400
        )
    try:
        stream_index = int(index)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "The audio track index must be a number."},
            status_code=400,
        )
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    local = resolve_path(path, config.settings.path_maps)
    if not local or not os.path.isfile(local):
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."}, status_code=400
        )
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return JSONResponse(
            {"ok": False, "error": "ffprobe is not available."}, status_code=400
        )
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return JSONResponse(
            {"ok": False, "error": "ffmpeg is not available."}, status_code=400
        )

    lock = _delete_locks.setdefault(path, asyncio.Lock())
    if lock.locked():
        return JSONResponse(
            {"ok": False, "error": "An audio delete is already running for this file."},
            status_code=409,
        )
    async with lock:
        tracks, error = await _probe_audio(ffprobe, local)
        if error:
            return JSONResponse(
                {"ok": False, "error": f"Could not probe the file: {error}"},
                status_code=502,
            )
        track = next((t for t in tracks if t.get("index") == stream_index), None)
        if track is None:
            return JSONResponse(
                {"ok": False, "error": "That is not an audio track of this video."},
                status_code=403,
            )
        if len(tracks) <= 1:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "That is the only audio track — refusing to leave the file silent.",
                },
                status_code=400,
            )

        # If the removed track was the default, promote the first remaining one.
        remaining_default = any(
            t.get("default") for t in tracks if t.get("index") != stream_index
        )
        set_default = bool(track.get("default")) and not remaining_default

        ok, err = await asyncio.to_thread(
            _remove_track, ffmpeg, local, stream_index, set_default
        )
        if not ok:
            log("Search", "WARN", f"Audio delete failed ({err}): {os.path.basename(local)}")
            return JSONResponse(
                {"ok": False, "error": err or "ffmpeg failed."}, status_code=502
            )

        # Re-probe both halves: removing a stream shifts the absolute indices
        # the storage and the player rely on, so the stored tracks are stale.
        st = await asyncio.to_thread(_stat_path, local)
        _key, entry = await asyncio.to_thread(_probe_one, ffprobe, path, local, st)
        _state["files"][path] = entry
        _save_state()

        # Tell Jellyfin the file changed so the removed track disappears.
        jellyfin = None
        try:
            from .jellyfin.client import JellyfinError, get_client

            await get_client().scan_paths([path])
        except JellyfinError as exc:
            jellyfin = str(exc)
            log("Search", "WARN", f"Jellyfin rescan after audio delete failed: {exc}")

        label = track.get("name") or "audio track"
        log("Search", "INFO", f"Deleted {label} from {os.path.basename(local)}")
        return JSONResponse(
            {
                "ok": True,
                "message": f"Deleted {label}.",
                "entry": entry,
                "jellyfin": jellyfin,
            }
        )


# ---- ffmpeg -----------------------------------------------------------------


async def _probe_audio(ffprobe: str, local: str) -> tuple[list[dict], str | None]:
    def run():
        _subs, audio, _duration, err = _probe_streams(ffprobe, local)
        return audio, err

    return await asyncio.to_thread(run)


def _remove_track(ffmpeg: str, local: str, index: int, set_default: bool) -> tuple[bool, str]:
    """Rewrite ``local`` without stream ``index``; ``(ok, error)``.

    The copy goes to a temporary file in the same folder (same volume, so the
    final ``os.replace`` is atomic) and only replaces the video once ffmpeg
    exits successfully.
    """
    folder = os.path.dirname(local)
    suffix = os.path.splitext(local)[1]
    fd, tmp = tempfile.mkstemp(prefix=".jftools-audio-", suffix=suffix, dir=folder)
    os.close(fd)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", local,
        # Everything, then subtract the one stream. Positive mapping would drop
        # attachments, chapters and unknown streams the file may carry.
        "-map", "0", "-map", f"-0:{index}",
        "-c", "copy",
    ]
    if set_default:
        cmd += ["-disposition:a:0", "default"]
    cmd.append(tmp)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=7200, creationflags=creationflags,
        )
        if result.returncode != 0:
            lines = (result.stderr or "").strip().splitlines()
            return False, (lines[-1] if lines else f"ffmpeg exit {result.returncode}")
        os.replace(tmp, local)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"ffmpeg error: {exc}"
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
