"""Formats — find files whose container, video or audio a device cannot play.

The data comes from the Movie Match fetch, which also requests Jellyfin's
``MediaSources`` and stores a compact ``fmt`` blob on every row. This module
serves that blob plus a compact summary of the Search media scan, so the tab
loads a few MB instead of the 39 MB scan payload Search needs.

Playback verdicts are computed in the browser from savable *device profiles*
(what a given player accepts). Built-in presets cover the common cases; saved
profiles live in ``config.settings.format_profiles`` so they survive browser
clears and travel with a settings backup.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import jellyfin_id
from .config import config
from .logs import log
from .paths import resolve_local_path, resolve_path
from .subtitles import resolve_ffprobe, scans_map
from .web import require_login, templates

router = APIRouter()

# What a player accepts. ``video`` maps a codec to its maximum bit depth, so a
# 10-bit H.264 file is flagged on a player that only decodes 8-bit H.264.
BUILTIN_PROFILES: dict[str, dict] = {
    "Vardagsrum TV": {
        "containers": ["mkv", "mp4"],
        "video": {"h264": 8, "hevc": 10, "vp9": 10},
        "audio": ["aac", "ac3", "eac3", "mp3"],
    },
    "Web browser": {
        "containers": ["mp4", "webm"],
        "video": {"h264": 8, "vp9": 10},
        "audio": ["aac", "mp3", "opus", "vorbis"],
    },
    "Strict": {
        "containers": ["mp4"],
        "video": {"h264": 8},
        "audio": ["aac"],
    },
}


def _profiles() -> dict:
    """Built-ins with any saved profile of the same name taking precedence."""
    merged = {name: dict(body) for name, body in BUILTIN_PROFILES.items()}
    merged.update(config.settings.format_profiles or {})
    return merged


def _saved_names() -> list[str]:
    return sorted(config.settings.format_profiles or {})


# ---- probing files Jellyfin has no stream info for -------------------------

_PROBE_LIMIT = 500
_probe = {"running": False}


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _lower(value) -> str:
    return str(value or "").lower()


def _bit_depth(stream: dict) -> int:
    """A stream's bit depth, from the explicit fields or the pixel format.

    ffprobe only sets ``bit_depth`` for some codecs; for the rest the depth is
    encoded in ``pix_fmt`` (``yuv420p10le``, ``p010le``). An unnamed format
    stays 0 rather than guessing, but a plain 8-bit pixel format is named so it
    answers 8 — matching what Jellyfin reports.
    """
    for key in ("bits_per_raw_sample", "bit_depth"):
        depth = _as_int(stream.get(key))
        if depth:
            return depth
    pix = str(stream.get("pix_fmt") or "").lower()
    match = re.search(r"(?:p|gray|gbrp|ya)0?(9|10|12|14|16)(?:le|be)?$", pix)
    if match:
        return int(match.group(1))
    # Any other named pixel format (yuv420p, nv12, …) is 8-bit. Leaving it 0
    # would show a normal H.264 file as "Unknown depth" after a probe.
    return 8 if pix else 0


def _video_range(stream: dict) -> str:
    """Jellyfin-style range label from ffprobe's colour metadata."""
    for side in stream.get("side_data_list") or []:
        kind = str(side.get("side_data_type") or "").upper()
        if "DOVI" in kind or "DOLBY VISION" in kind:
            return "DOVI"
    transfer = str(stream.get("color_transfer") or "").lower()
    if transfer == "smpte2084":
        return "HDR10"
    if transfer == "arib-std-b67":
        return "HLG"
    return "SDR"


def _ffprobe_format(ffprobe: str, local: str) -> dict | None:
    """Probe one file into the same compact ``fmt`` shape Jellyfin supplies.

    Used only for rows where Jellyfin reports a media source but no streams:
    the file usually has video and audio, and Jellyfin's missing scan must not
    be shown as "No video". ``None`` means the probe failed and the row keeps
    whatever it had.
    """
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,profile,width,height,bit_depth,"
        "bits_per_raw_sample,pix_fmt,channels,color_transfer,color_primaries,"
        "color_space:stream_tags=language:stream_side_data=side_data_type",
        "-of", "json",
        local,
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
        log("Movie Match", "WARN", f"ffprobe failed for {local}: {exc}")
        return None
    if result.returncode != 0:
        log(
            "Movie Match", "WARN",
            f"ffprobe exit {result.returncode} for {local}: "
            f"{(result.stderr or '').strip()[:200]}",
        )
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        log("Movie Match", "WARN", f"ffprobe JSON parse failed for {local}: {exc}")
        return None

    video = None
    audio: list[dict] = []
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and video is None:
            video = {
                "c": _lower(stream.get("codec_name")),
                "p": str(stream.get("profile") or ""),
                "w": _as_int(stream.get("width")),
                "h": _as_int(stream.get("height")),
                "d": _bit_depth(stream),
                "r": _video_range(stream),
            }
        elif kind == "audio":
            tags = stream.get("tags") or {}
            audio.append({
                "c": _lower(stream.get("codec_name")),
                "ch": _as_int(stream.get("channels")),
                "l": _lower(tags.get("language")),
            })

    # ffprobe knows the demuxer, not the extension; Jellyfin's container label
    # is the extension, and the device profile compares against that.
    suffix = Path(local).suffix.lower().lstrip(".")
    container = {"m4v": "mp4"}.get(suffix, suffix)
    return {"c": container, "v": video, "a": audio}


def _needs_probe(row: dict) -> bool:
    """Whether a row has no usable stream info to compute a verdict from.

    A missing ``fmt`` (older caches, or Jellyfin returned no media source) and
    a ``fmt`` with neither video nor audio both mean "unknown", not "empty
    file". A file with audio but no video is genuinely audio-only and is left
    alone.
    """
    fmt = row.get("fmt")
    if not isinstance(fmt, dict):
        return True
    return not fmt.get("v") and not fmt.get("a")


def _probe_pending(ffprobe: str, maps: list[dict], limit: int = _PROBE_LIMIT) -> dict:
    """ffprobe up to ``limit`` rows that lack Jellyfin stream info.

    Runs in a worker thread: SMB reads and ffprobe are slow and would block the
    event loop. Mutates the cached rows in place; the caller saves the state.
    """
    probed = failed = remaining = 0
    for row in jellyfin_id._state["rows"]:
        if not _needs_probe(row):
            continue
        if probed + failed >= limit:
            remaining += 1
            continue
        local = resolve_path(row.get("path") or "", maps)
        if not local or not os.path.isfile(local):
            failed += 1
            continue
        fmt = _ffprobe_format(ffprobe, local)
        if fmt is None:
            failed += 1
            continue
        row["fmt"] = fmt
        probed += 1
    return {"probed": probed, "failed": failed, "remaining": remaining}


@router.get("/formats", response_class=HTMLResponse)
async def formats_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "formats.html", {"request": request})


def _scan_summary(entry: dict | None) -> dict | None:
    """The parts of a media-scan entry the Corrupt filter needs.

    ``scans.json`` entries also carry subtitle and audio track lists; sending
    those for every row would grow this payload toward Search's. The keys follow
    the terse style of ``fmt``::

        {"e": "ffprobe exit 1: …", "s": true, "d": 814.13, "z": 200076055}

    ``e`` is the probe error (empty when there was none), ``s`` whether the
    file was scanned, ``d`` the duration in seconds and ``z`` the size in bytes.
    ``None`` means the media scan has not touched this path yet.
    """
    if not isinstance(entry, dict):
        return None
    try:
        duration = round(float(entry.get("duration") or 0), 2)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "e": str(entry.get("error") or ""),
        "s": bool(entry.get("scanned")),
        "d": duration,
        "z": entry.get("size"),
    }


@router.get("/api/formats/items")
async def formats_items(request: Request, user: str = Depends(require_login)):
    """Every fetched row plus its compact format, without the scan payload."""
    maps = config.settings.path_maps
    scans = scans_map()
    rows = []
    for r in jellyfin_id._state["rows"]:
        path = r.get("path") or ""
        row = {
            "id": r.get("id", ""),
            "type": r.get("type", ""),
            "path": r.get("path", ""),
            "file": r.get("file", ""),
            "name": r.get("name", ""),
            "series": r.get("series", ""),
            "display": r.get("display", ""),
            "year": r.get("year"),
            "library": r.get("library", ""),
            "fmt": r.get("fmt"),
            "local_path": resolve_local_path(path, maps),
        }
        # Jellyfin returned no media source at all: unreadable to its own
        # scanner. Kept apart from a missing ``fmt`` so an older cache does not
        # get accused of being corrupt. Absent for rows that have media info.
        if r.get("nosrc"):
            row["nosrc"] = True
        # Absent when the media scan has not touched the path.
        summary = _scan_summary(scans.get(path))
        if summary:
            row["scan"] = summary
        rows.append(row)
    return JSONResponse(
        {
            "ok": True,
            "fetched": bool(rows),
            "rows": rows,
            "last_sync": jellyfin_id._state["last_sync"],
            "jellyfin_url": config.settings.jellyfin_url,
            "radarr_url": config.settings.radarr_url,
            "sonarr_url": config.settings.sonarr_url,
            "profiles": _profiles(),
            "builtin": sorted(BUILTIN_PROFILES),
            "saved": _saved_names(),
        }
    )


@router.get("/api/formats/profiles")
async def formats_profiles(user: str = Depends(require_login)):
    return JSONResponse(
        {
            "ok": True,
            "profiles": _profiles(),
            "builtin": sorted(BUILTIN_PROFILES),
            "saved": _saved_names(),
        }
    )


@router.post("/api/formats/profiles")
async def formats_profiles_save(
    request: Request,
    user: str = Depends(require_login),
    name: str = Form(""),
    profile: str = Form(""),
):
    """Save one device profile, or delete it when ``profile`` is empty."""
    name = name.strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "No profile name given."}, status_code=400
        )
    body: dict | None = None
    if profile.strip():
        try:
            body = json.loads(profile)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Profile is not valid JSON."}, status_code=400
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "Profile must be an object."}, status_code=400
            )
    config.save_format_profile(name, body)
    return JSONResponse(
        {
            "ok": True,
            "profiles": _profiles(),
            "builtin": sorted(BUILTIN_PROFILES),
            "saved": _saved_names(),
        }
    )


@router.post("/api/formats/probe")
async def formats_probe(user: str = Depends(require_login)):
    """ffprobe files that Jellyfin has no stream info for.

    A missing stream list is not "No video": Jellyfin simply has not scanned
    the file (or failed to). Probing fills the compact format from the file
    itself, so the tab can give an honest verdict. Capped per click so a large
    library on a slow share cannot hang the request.
    """
    if jellyfin_id._fetch.get("status") == "running":
        return JSONResponse(
            {"ok": False, "error": "A Movie Match fetch is running. Try again when it finishes."},
            status_code=409,
        )
    if _probe["running"]:
        return JSONResponse(
            {"ok": False, "error": "A probe is already running."}, status_code=409
        )
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return JSONResponse(
            {
                "ok": False,
                "error": "ffprobe was not found. Put it in Tools/ next to the app, "
                         "or set JFTOOLS_FFPROBE.",
            },
            status_code=400,
        )
    _probe["running"] = True
    try:
        result = await asyncio.to_thread(
            _probe_pending, ffprobe, config.settings.path_maps, _PROBE_LIMIT
        )
    finally:
        _probe["running"] = False
    if result["probed"]:
        jellyfin_id._save_state()
    log(
        "Movie Match", "INFO",
        f"Formats probe: {result['probed']} file(s) read, "
        f"{result['failed']} failed, {result['remaining']} still missing",
    )
    return JSONResponse({"ok": True, **result})
