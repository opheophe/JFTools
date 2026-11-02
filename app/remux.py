"""Remux a library video into a fresh MKV container with ffmpeg.

The Tracks pill still opens the track dialog; hovering it reveals a **Remux**
item. Like ComTool's remux, this writes a new file next to the video, validates
the copy and only then moves the original aside. Every ffmpeg, probe or swap
error leaves the original untouched (or restores it), so a failed remux never
damages the library. Files that are not already Matroska become ``<name>.mkv``.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile

from fastapi import APIRouter, Depends, Form, Header
from fastapi.responses import JSONResponse

from . import notifications
from .audio import (
    _backup_path,
    _commit_media,
    _copy_media_metadata,
    _delete_locks,
    _failed_path,
    _recover_previous_swap,
    _retry_rename,
    _swap_error,
    _validate_media,
)
from .config import config
from .logs import log
from .paths import resolve_path
from .subtitles import (
    _library_row,
    _probe_one,
    _save_state,
    _stat_path,
    _state,
    _write_gate,
    resolve_ffmpeg,
    resolve_ffprobe,
)
from .web import require_login

router = APIRouter()

_REMUX_TIMEOUT = 7200


# ---- probing the copy -------------------------------------------------------


def _probe_layout(
    ffprobe: str, path: str
) -> tuple[list[tuple[str, str]], float, str | None]:
    """The file's ``(codec_type, codec_name)`` streams and duration."""
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name:format=duration",
        "-of", "json", path,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace", creationflags=creationflags,
        )
    except Exception as exc:  # noqa: BLE001
        return [], 0.0, f"ffprobe error: {exc}"
    if result.returncode != 0:
        return [], 0.0, f"ffprobe exit {result.returncode}: {(result.stderr or '').strip()}"
    try:
        data = json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
        return [], 0.0, f"ffprobe JSON parse failed: {exc}"
    streams = [
        (s.get("codec_type") or "", s.get("codec_name") or "")
        for s in (data.get("streams") or [])
    ]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return streams, duration, None


def _layout_error(
    before: list[tuple[str, str]],
    after: list[tuple[str, str]],
    before_duration: float,
    after_duration: float,
) -> str | None:
    """Why the copy is not a faithful remux of the original, if it is not."""
    if len(after) != len(before):
        return f"{len(before)} streams went in, {len(after)} came out"
    if [kind for kind, _ in after] != [kind for kind, _ in before]:
        return "the stream order or stream types changed"
    if before_duration > 0 and after_duration <= 0:
        return "the copy has no duration"
    if before_duration > 0 and after_duration > 0:
        tolerance = max(2.0, min(10.0, before_duration * 0.001))
        if abs(after_duration - before_duration) > tolerance:
            return (
                f"the duration changed from {before_duration:.1f}s "
                f"to {after_duration:.1f}s"
            )
    return None


# Text subtitle codecs that Matroska cannot carry as-is. ffmpeg can decode them
# and write SRT in the same pass; image subtitles (PGS, VobSub, DVB) are copied
# untouched because Matroska already supports them.
_SRT_SUBTITLE_CODECS = {"mov_text", "eia_608", "eia_708", "ttml", "webvtt", "text"}


def _subtitle_codec_args(streams: list[tuple[str, str]]) -> list[str]:
    """``-c:s:N srt`` overrides for text subtitles the MKV muxer rejects."""
    args: list[str] = []
    ordinal = 0
    for kind, codec in streams:
        if kind != "subtitle":
            continue
        if codec in _SRT_SUBTITLE_CODECS:
            args += [f"-c:s:{ordinal}", "srt"]
        ordinal += 1
    return args


# ---- committing the remuxed file -------------------------------------------


def _commit_remux(
    tmp: str, local: str, final: str, ffprobe: str | None
) -> tuple[bool, str]:
    """Swap ``tmp`` in at ``final`` without ever losing ``local``.

    Unlike :func:`app.audio._commit_media` this can target a different name
    (``.mp4`` -> ``.mkv``). The original is moved to the usual JFTools backup
    name first; every failure path puts it back.
    """
    error = _validate_media(ffprobe, tmp)
    if error:
        return (
            False,
            f"The remuxed file was not valid: {error}. "
            "The original was left unchanged.",
        )

    _copy_media_metadata(local, tmp)
    backup = _backup_path(local)
    try:
        _retry_rename(local, backup)
    except OSError as exc:
        return False, _swap_error("move the original aside", exc)

    try:
        _retry_rename(tmp, final)
    except OSError as exc:
        try:
            _retry_rename(backup, local)
        except OSError as restore_exc:
            return False, (
                f"Could not put the remuxed file in place ({exc}). The original "
                f"is at {backup}; restoring it also failed: {restore_exc}"
            )
        return False, _swap_error("replace the video", exc, restored=True)

    error = _validate_media(ffprobe, final)
    if error:
        side = None
        if os.path.lexists(final):
            side = _failed_path(local)
            try:
                _retry_rename(final, side)
            except OSError:
                side = None
        try:
            _retry_rename(backup, local)
        except OSError as exc:
            return False, (
                f"The remuxed file failed validation ({error}). The original "
                f"is at {backup}; restoring it also failed: {exc}"
            )
        if side:
            try:
                os.remove(side)
            except OSError:
                pass
        return (
            False,
            f"The remuxed file failed validation ({error}). "
            "The original was restored.",
        )

    try:
        os.remove(backup)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(
            "Search",
            "WARN",
            f"Could not remove the remux backup {backup}: {exc}",
        )
    return True, ""


# ---- the remux itself -------------------------------------------------------


def _remux_video(
    ffmpeg: str, ffprobe: str, local: str
) -> tuple[bool, str, str]:
    """Rewrite ``local`` as MKV; return ``(ok, error, final_path)``.

    The output is written to a temporary file in the same folder. It is probed
    and compared with the original before anything is swapped, so a short or
    stream-dropping copy cannot replace a good file.
    """
    ext = os.path.splitext(local)[1]
    final = local if ext.lower() == ".mkv" else os.path.splitext(local)[0] + ".mkv"
    same = os.path.normcase(os.path.abspath(final)) == os.path.normcase(
        os.path.abspath(local)
    )

    if not same and os.path.exists(final):
        return (
            False,
            f"{os.path.basename(final)} already exists next to the video; "
            "rename or remove it first.",
            final,
        )

    recovered = _recover_previous_swap(local, ffprobe)
    if recovered:
        return False, recovered, final
    if not os.path.isfile(local):
        return False, "The video file is not reachable.", final

    before, before_duration, error = _probe_layout(ffprobe, local)
    if error:
        return False, f"Could not probe the file: {error}", final
    if not before:
        return False, "ffprobe found no streams in the file.", final

    folder = os.path.dirname(local)
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=".jftools-remux-", suffix=".mkv", dir=folder
        )
        os.close(fd)
    except OSError as exc:
        return False, f"Could not create a temporary file next to the video: {exc}", final

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", local,
        # Everything, including attachments and chapters. A copy can only
        # succeed if every stream is supported by Matroska; otherwise ffmpeg
        # exits non-zero and the original is never touched.
        "-map", "0",
        "-c", "copy",
    ]
    cmd += _subtitle_codec_args(before)
    cmd += [
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-f", "matroska",
        tmp,
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_REMUX_TIMEOUT,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"ffmpeg error: {exc}", final
        if result.returncode != 0:
            lines = (result.stderr or "").strip().splitlines()
            return (
                False,
                lines[-1] if lines else f"ffmpeg exit {result.returncode}",
                final,
            )

        error = _validate_media(ffprobe, tmp)
        if error:
            return (
                False,
                f"The remuxed file was not valid: {error}. "
                "The original was left unchanged.",
                final,
            )
        after, after_duration, error = _probe_layout(ffprobe, tmp)
        if error:
            return (
                False,
                f"Could not probe the remuxed file: {error}. "
                "The original was left unchanged.",
                final,
            )
        mismatch = _layout_error(before, after, before_duration, after_duration)
        if mismatch:
            return (
                False,
                f"The remuxed file did not verify ({mismatch}). "
                "The original was left unchanged.",
                final,
            )

        # A second check closes most of the window for another process creating
        # the target while ffmpeg was running.
        if not same and os.path.exists(final):
            return (
                False,
                f"{os.path.basename(final)} appeared while remuxing; "
                "the original was left unchanged.",
                final,
            )
        if same:
            ok, error = _commit_media(tmp, local, ffprobe)
        else:
            ok, error = _commit_remux(tmp, local, final, ffprobe)
        return ok, error, final
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ---- route ------------------------------------------------------------------


@router.post("/api/internal/remux")
async def internal_remux(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
):
    """Remux one library video into MKV, keeping every stream as-is."""
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    local = resolve_path(path, config.settings.path_maps)
    if not local:
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."},
            status_code=400,
        )
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return JSONResponse(
            {"ok": False, "error": "ffprobe is not available."}, status_code=400
        )
    if not os.path.isfile(local) and not os.path.lexists(_backup_path(local)):
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."},
            status_code=400,
        )
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return JSONResponse(
            {"ok": False, "error": "ffmpeg is not available."}, status_code=400
        )

    # Share the track editor's per-file lock: a remux and an edit of the same
    # file must never copy the old file and then swap over each other.
    lock = _delete_locks.setdefault(path, asyncio.Lock())
    if lock.locked():
        return JSONResponse(
            {"ok": False, "error": "A track edit or remux is already running for this file."},
            status_code=409,
        )
    async with lock:
        ok, error, final = await asyncio.to_thread(_remux_video, ffmpeg, ffprobe, local)
        if not ok:
            log("Search", "WARN", f"Remux failed ({error}): {os.path.basename(local)}")
            notification_id = notifications.publish(
                "error",
                "Remux failed",
                f"{os.path.basename(local)} — {error}",
                source="Search",
                tab="Search",
                user=user,
                url="/logs",
                link_label="View logs",
            )
            return JSONResponse(
                {"ok": False, "error": error, "notification_id": notification_id},
                status_code=502,
            )

        renamed = os.path.normcase(os.path.abspath(final)) != os.path.normcase(
            os.path.abspath(local)
        )
        new_path = (
            os.path.splitext(path)[0] + os.path.splitext(final)[1]
            if renamed
            else path
        )

        st = await asyncio.to_thread(_stat_path, final)
        _key, entry = await asyncio.to_thread(
            _probe_one, ffprobe, new_path, final, st
        )
        _state["files"].pop(path, None)
        _state["ok"] = [new_path if p == path else p for p in _state["ok"]]
        _state["files"][new_path] = entry
        _save_state()

        if renamed:
            try:
                from .jellyfin_id import rename_cached_path

                rename_cached_path(path, new_path)
            except Exception:  # noqa: BLE001 — the media file is already safe
                pass

        jellyfin = None
        try:
            from .jellyfin.client import JellyfinError, get_client

            await get_client().scan_paths([new_path])
        except JellyfinError as exc:
            jellyfin = str(exc)
            log("Search", "WARN", f"Jellyfin rescan after remux failed: {exc}")

        name = os.path.basename(final)
        message = f"Remuxed {name}." if not renamed else f"Remuxed and saved as {name}."
        if jellyfin:
            message += f" Jellyfin warning: {jellyfin}"
        log("Search", "INFO", f"{message} ({os.path.basename(local)})")
        notification_id = notifications.publish(
            "warning" if jellyfin else "success",
            "Remux complete with warning" if jellyfin else "Remux complete",
            f"{os.path.basename(local)} — {message}",
            source="Search",
            tab="Search",
            user=user,
            url="/search",
            link_label="Open Search",
        )
        return JSONResponse(
            {
                "ok": True,
                "path": new_path,
                "renamed": renamed,
                "entry": entry,
                "message": message,
                "notification_id": notification_id,
                "jellyfin": jellyfin,
            }
        )
