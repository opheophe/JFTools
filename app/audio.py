"""Track editing — list and edit a video's embedded audio/subtitle streams and
its external subtitle files.

Scanning lives in :mod:`app.subtitles` (one ffprobe fills both halves of
``data/scans.json``); this module serves the Search, Movie Match and Libraries
tabs' "Tracks" dialog. It can delete several embedded streams, delete external
subtitle files, and set one default audio and one default subtitle in a single
execute step.

The rewrite copies every other stream into a temporary file next to the
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

from . import notifications
from .config import config
from .logs import log
from .paths import resolve_path
from .subtitles import (
    _external_subtitles,
    _library_row,
    _probe_one,
    _probe_streams,
    _save_state,
    _stat_path,
    _state,
    _write_gate,
    audio_tracks_for,
    external_smart_marks,
    internal_tracks_for,
    resolve_ffmpeg,
    resolve_ffprobe,
    smart_subtitle_marks,
    subtitle_tracks_for,
)
from .web import require_login

router = APIRouter()

# One rewrite at a time per file: two edits racing would each copy the old file
# and the second replace would undo the first.
_delete_locks: dict[str, asyncio.Lock] = {}

# Disposition flags ffmpeg can write back. The two newer ffprobe-only flags
# (still_image, multilayer) are deliberately left out: older bundled ffmpeg
# builds reject them. Everything else a normal video carries is preserved.
_DISPOSITION_FLAGS = (
    "default", "dub", "original", "comment", "lyrics", "karaoke", "forced",
    "hearing_impaired", "visual_impaired", "clean_effects", "attached_pic",
    "timed_thumbnails", "non_diegetic", "captions", "descriptions", "metadata",
    "dependent",
)


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


@router.post("/api/internal/existing")
async def internal_existing(
    user: str = Depends(require_login),
    path: str = Form(""),
):
    """Every embedded audio/subtitle track and external subtitle file for one
    library video."""
    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )

    subtitles, tracks, error = await internal_tracks_for(path, force=True)
    _internal, external = await subtitle_tracks_for(path)
    smart_mark, preferred = smart_subtitle_marks(subtitles)
    return JSONResponse(
        {
            "ok": True,
            "file_mgmt": config.settings.file_mgmt,
            "subtitles": subtitles,
            "audio": tracks,
            "external": external,
            "smart_mark": smart_mark,
            "smart_mark_files": external_smart_marks(external, preferred),
            "preferred_languages": preferred,
            "error": error,
        }
    )


@router.post("/api/internal/execute")
async def internal_execute(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
    delete: str = Form(""),
    delete_files: str = Form(""),
    default_audio: str = Form(""),
    default_subtitle: str = Form(""),
):
    """Delete marked embedded streams and external subtitle files and set the
    chosen defaults in one pass.

    ``delete`` is a comma-separated list of absolute ffprobe stream indices;
    ``delete_files`` a comma-separated list of external subtitle file names;
    ``default_audio``/``default_subtitle`` are optional absolute indices. The
    file is re-probed here and every index is checked against the live stream
    list, and every file against the video's own external subtitles, so the
    caller cannot map away arbitrary streams or files.
    """
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)

    deleted, error = _parse_index_list(delete)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    files, error = _parse_file_list(delete_files)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    audio_default, error = _parse_optional_index(default_audio, "audio default")
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    subtitle_default, error = _parse_optional_index(
        default_subtitle, "subtitle default"
    )
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)

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
            {"ok": False, "error": "A track edit is already running for this file."},
            status_code=409,
        )
    async with lock:
        subtitles, tracks, _duration, probe_error = await asyncio.to_thread(
            _probe_streams, ffprobe, local
        )
        if probe_error:
            return JSONResponse(
                {"ok": False, "error": f"Could not probe the file: {probe_error}"},
                status_code=502,
            )

        external = _external_subtitles(local)
        external_names = {e["file"] for e in external}
        unknown_files = [f for f in files if f not in external_names]
        if unknown_files:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Not an external subtitle file of this video.",
                },
                status_code=403,
            )

        subs_by_index = {t["index"]: t for t in subtitles}
        audio_by_index = {t["index"]: t for t in tracks}
        deleted_set = set(deleted)
        unknown = [
            i for i in deleted if i not in subs_by_index and i not in audio_by_index
        ]
        if unknown:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Not an audio or subtitle track of this video.",
                },
                status_code=403,
            )
        if audio_default is not None and audio_default not in audio_by_index:
            return JSONResponse(
                {"ok": False, "error": "That is not an audio track of this video."},
                status_code=403,
            )
        if subtitle_default is not None and subtitle_default not in subs_by_index:
            return JSONResponse(
                {"ok": False, "error": "That is not a subtitle track of this video."},
                status_code=403,
            )
        if audio_default is not None and audio_default in deleted_set:
            return JSONResponse(
                {"ok": False, "error": "A track marked for deletion cannot become the default."},
                status_code=400,
            )
        if subtitle_default is not None and subtitle_default in deleted_set:
            return JSONResponse(
                {"ok": False, "error": "A track marked for deletion cannot become the default."},
                status_code=400,
            )
        if not [t for t in tracks if t["index"] not in deleted_set]:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "That would delete every audio track — refusing to leave the file silent.",
                },
                status_code=400,
            )

        audio_edits, audio_target = _default_overrides(
            tracks, audio_default, deleted_set
        )
        subtitle_edits, subtitle_target = _default_overrides(
            subtitles, subtitle_default, deleted_set
        )
        if not deleted_set and not files and not audio_edits and not subtitle_edits:
            entry = _state["files"].get(path)
            smart_mark, preferred = smart_subtitle_marks(
                (entry or {}).get("internal") or []
            )
            return JSONResponse(
                {
                    "ok": True,
                    "message": "Nothing to change.",
                    "entry": entry,
                    "smart_mark": smart_mark,
                    "smart_mark_files": external_smart_marks(
                        (entry or {}).get("external") or [], preferred
                    ),
                    "preferred_languages": preferred,
                    "jellyfin": None,
                }
            )

        if deleted_set or audio_edits or subtitle_edits:
            ok, err = await asyncio.to_thread(
                _rewrite_tracks,
                ffmpeg,
                local,
                deleted_set,
                audio_edits,
                subtitle_edits,
            )
            if not ok:
                error = err or "ffmpeg failed."
                log(
                    "Search",
                    "WARN",
                    f"Track edit failed ({error}): {os.path.basename(local)}",
                )
                notification_id = notifications.publish(
                    "error",
                    "Track edit failed",
                    f"{os.path.basename(local)} — {error}",
                    source="Search",
                    user=user,
                    url="/logs",
                    link_label="View logs",
                )
                return JSONResponse(
                    {
                        "ok": False,
                        "error": error,
                        "notification_id": notification_id,
                    },
                    status_code=502,
                )

        # Delete the marked external subtitle files. Any rewrite above has
        # already succeeded, so these go after it.
        deleted_files: list[str] = []
        file_errors: list[str] = []
        if files:
            from .files import browser_roots, delete_entry

            folder = os.path.dirname(local)
            for name in sorted(files):
                result = delete_entry(os.path.join(folder, name), browser_roots())
                if result.get("ok"):
                    deleted_files.append(name)
                    log("Search", "INFO", f"Deleted subtitle file: {name}")
                else:
                    err_text = result.get("error") or "delete failed"
                    file_errors.append(f"{name}: {err_text}")
                    log("Search", "WARN", f"Subtitle file delete refused ({err_text}): {name}")

        # Stream indices shift when tracks are removed, so both halves of the
        # stored entry are replaced from a fresh probe.
        st = await asyncio.to_thread(_stat_path, local)
        _key, entry = await asyncio.to_thread(_probe_one, ffprobe, path, local, st)
        _state["files"][path] = entry
        _save_state()

        jellyfin = None
        try:
            from .jellyfin.client import JellyfinError, get_client

            await get_client().scan_paths([path])
        except JellyfinError as exc:
            jellyfin = str(exc)
            log("Search", "WARN", f"Jellyfin rescan after track edit failed: {exc}")

        bits = []
        if deleted_set:
            count = len(deleted_set)
            bits.append(f"Deleted {count} track{'s' if count != 1 else ''}.")
        if deleted_files:
            count = len(deleted_files)
            bits.append(f"Deleted {count} subtitle file{'s' if count != 1 else ''}.")
        if file_errors:
            bits.append("Some files could not be deleted: " + "; ".join(file_errors) + ".")
        if audio_target is not None:
            name = audio_by_index[audio_target].get("name") or "audio track"
            bits.append(f"Default audio: {name}.")
        if subtitle_target is not None:
            name = subs_by_index[subtitle_target].get("name") or "subtitle track"
            bits.append(f"Default subtitle: {name}.")
        message = " ".join(bits) or "Updated the tracks."
        log("Search", "INFO", f"{message} ({os.path.basename(local)})")
        detail = f"{os.path.basename(local)} — {message}"
        if jellyfin:
            detail += f" Jellyfin warning: {jellyfin}."
        notification_id = notifications.publish(
            "warning" if (jellyfin or file_errors) else "success",
            "Track edit complete with warning" if (jellyfin or file_errors) else "Track edit complete",
            detail,
            source="Search",
            user=user,
            url="/search",
            link_label="Open Search",
        )
        smart_mark, preferred = smart_subtitle_marks(entry.get("internal") or [])
        return JSONResponse(
            {
                "ok": True,
                "message": message,
                "notification_id": notification_id,
                "entry": entry,
                "deleted": sorted(deleted_set),
                "deleted_files": deleted_files,
                "default_audio": audio_target,
                "default_subtitle": subtitle_target,
                "smart_mark": smart_mark,
                "smart_mark_files": external_smart_marks(
                    entry.get("external") or [], preferred
                ),
                "preferred_languages": preferred,
                "jellyfin": jellyfin,
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
            error = err or "ffmpeg failed."
            log("Search", "WARN", f"Audio delete failed ({error}): {os.path.basename(local)}")
            notification_id = notifications.publish(
                "error",
                "Audio delete failed",
                f"{os.path.basename(local)} — {error}",
                source="Search",
                user=user,
                url="/logs",
                link_label="View logs",
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": error,
                    "notification_id": notification_id,
                },
                status_code=502,
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
        detail = f"{os.path.basename(local)} — Deleted {label}."
        if jellyfin:
            detail += f" Jellyfin warning: {jellyfin}."
        notification_id = notifications.publish(
            "warning" if jellyfin else "success",
            "Audio delete complete with warning" if jellyfin else "Audio delete complete",
            detail,
            source="Search",
            user=user,
            url="/search",
            link_label="Open Search",
        )
        return JSONResponse(
            {
                "ok": True,
                "message": f"Deleted {label}.",
                "notification_id": notification_id,
                "entry": entry,
                "jellyfin": jellyfin,
            }
        )


# ---- parsing and dispositions -----------------------------------------------


def _parse_index_list(raw: str) -> tuple[list[int] | None, str | None]:
    """Parse ``"1,3,4"`` into unique non-negative stream indices."""
    values: list[int] = []
    raw = raw if isinstance(raw, str) else ""
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            return None, f"“{part}” is not a stream index."
        if value < 0:
            return None, "Stream indices cannot be negative."
        if value not in values:
            values.append(value)
    return values, None


def _parse_file_list(raw: str) -> tuple[list[str] | None, str | None]:
    """Parse a comma-separated list of external subtitle file names."""
    values: list[str] = []
    raw = raw if isinstance(raw, str) else ""
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part not in values:
            values.append(part)
    return values, None


def _parse_optional_index(raw: str, label: str) -> tuple[int | None, str | None]:
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return None, None
    try:
        value = int(raw)
    except ValueError:
        return None, f"The {label} index must be a number."
    if value < 0:
        return None, f"The {label} index cannot be negative."
    return value, None


def _output_ordinals(tracks: list[dict], deleted: set[int]) -> dict[int, int]:
    """Map each surviving input stream index to its output type ordinal.

    ``-map 0`` preserves stream order, so the nth surviving audio stream is
    ``-disposition:a:n``.
    """
    ordinal = 0
    out: dict[int, int] = {}
    for track in tracks:
        index = track.get("index")
        if index in deleted:
            continue
        out[index] = ordinal
        ordinal += 1
    return out


def _default_target(
    tracks: list[dict], selected: int | None, deleted: set[int]
) -> int | None:
    """The index that should carry the default flag after the rewrite.

    An explicit selection wins. Otherwise an existing default survives; if it
    was deleted, the first remaining track of that type is promoted.
    """
    if selected is not None:
        return selected
    current = [t for t in tracks if t.get("default")]
    surviving = [t for t in current if t.get("index") not in deleted]
    if surviving:
        return surviving[0].get("index")
    if current:
        remaining = [t for t in tracks if t.get("index") not in deleted]
        return remaining[0].get("index") if remaining else None
    return None


def _disposition_value(disposition: dict, add_default: bool) -> str:
    """A ``-disposition`` value with every known flag preserved.

    Setting a default with ``-disposition`` replaces the whole flag set, so the
    original flags (forced, comment, dub, ...) have to be put back too.
    """
    flags = [
        name for name in _DISPOSITION_FLAGS if (disposition or {}).get(name)
    ]
    if add_default:
        if "default" not in flags:
            flags.append("default")
    else:
        flags = [name for name in flags if name != "default"]
    return "+".join(flags) if flags else "0"


def _default_overrides(
    tracks: list[dict], selected: int | None, deleted: set[int]
) -> tuple[list[tuple[int, str]], int | None]:
    """``((output ordinal, disposition value), ...), target index``.

    Only tracks whose default bit actually changes are emitted, so ffmpeg's
    normal copy leaves every other stream's dispositions untouched.
    """
    ordinals = _output_ordinals(tracks, deleted)
    target = _default_target(tracks, selected, deleted)
    edits: list[tuple[int, str]] = []
    for track in tracks:
        index = track.get("index")
        if index in deleted:
            continue
        want_default = index == target
        if bool(track.get("default")) == want_default:
            continue
        disposition = track.get("disposition")
        if disposition is None:
            disposition = {
                "default": bool(track.get("default")),
                "forced": bool(track.get("forced")),
            }
        edits.append((ordinals[index], _disposition_value(disposition, want_default)))
    return edits, target


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


def _rewrite_tracks(
    ffmpeg: str,
    local: str,
    deleted: set[int],
    audio_edits: list[tuple[int, str]],
    subtitle_edits: list[tuple[int, str]],
) -> tuple[bool, str]:
    """Rewrite ``local`` deleting streams and applying default dispositions.

    Every stream is mapped, then the deleted ones are subtracted. The copy goes
    to a temporary file in the same folder so the final ``os.replace`` is
    atomic, and the original is replaced only after ffmpeg succeeds.
    """
    folder = os.path.dirname(local)
    suffix = os.path.splitext(local)[1]
    fd, tmp = tempfile.mkstemp(prefix=".jftools-internal-", suffix=suffix, dir=folder)
    os.close(fd)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", local,
        # Everything, then subtract the deleted streams. Positive mapping would
        # drop attachments, chapters and unknown streams the file may carry.
        "-map", "0",
    ]
    for index in sorted(deleted):
        cmd += ["-map", f"-0:{index}"]
    cmd += ["-c", "copy"]
    for ordinal, value in audio_edits:
        cmd += [f"-disposition:a:{ordinal}", value]
    for ordinal, value in subtitle_edits:
        cmd += [f"-disposition:s:{ordinal}", value]
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
