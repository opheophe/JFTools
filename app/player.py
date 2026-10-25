"""Playback — stream a library video to the browser, with its subtitle tracks.

The subtitle dialog and the Actions menus open ``/play`` in a small popup
window. That page streams the original file and offers every subtitle track the
scanner knows about — embedded streams are extracted with ffmpeg, external
files are converted to WebVTT. When the browser cannot decode the file's audio
(DTS, TrueHD, often AC-3), the player converts *only the audio* to a small
cached AAC track and plays it next to the muted video; a full remux into a
browser-friendly MP4 is kept just for files whose video itself will not play.

Everything here is read-only, so it needs a login but not the File management
setting. Paths go through the Media-path mappings and must land inside the
browsable media roots, exactly like the file browser.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
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
    _probe_subtitles,
    _subtitle_lang_name,
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

# Player cache (browser copies + converted audio) retention. These are the
# defaults; the Settings page overrides them per install.
PLAY_CACHE_MAX_BYTES = 5 * 1024**3
PLAY_CACHE_MAX_AGE = 7 * 24 * 3600
PREPARE_TIMEOUT = 2 * 3600

# Audio codecs no browser can decode, on any platform. A file whose audio is one
# of these plays with picture and no sound at all, and the video element never
# fires an error for it, so those files need a browser copy.
#
# Dolby AC-3 and E-AC-3 are deliberately not here: some browsers decode them
# through the operating system and some do not, so the page waits to see whether
# audio is really being decoded before remuxing one (see play.js). Neither are
# AAC and the open codecs, which every browser plays.
UNPLAYABLE_AUDIO = {"dts", "truehd", "mlp"}

# First-audio-stream probe, keyed like the play cache: path|size|mtime.
_AUDIO_CACHE: dict[tuple[str, int, int], dict] = {}
_AUDIO_CACHE_MAX = 64

# Converted WebVTT text, keyed by (path, track, mtime). Tiny: a subtitle file is
# a few hundred kilobytes at most.
_SUB_CACHE: dict[tuple[str, str, float], str] = {}
_SUB_CACHE_MAX = 24

# Codec of an external subtitle file, keyed by (path, mtime, demuxer). Probing
# is quick but the player lists tracks often, so keep the last few.
_EXT_CODEC_CACHE: dict[tuple[str, float, tuple], str] = {}
_EXT_CODEC_CACHE_MAX = 64

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


# An SRT timestamp: hours are optional, the millisecond separator may be a dot.
_SRT_TIME = re.compile(
    r"(?:(?P<h>\d{1,3}):)?(?P<m>\d{1,2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)


def _vtt_time(match: "re.Match[str]") -> str:
    """One SRT timestamp as WebVTT wants it: always ``HH:MM:SS.mmm``."""
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = match.group("ms")
    if len(millis) == 1:
        millis += "00"
    elif len(millis) == 2:
        millis += "0"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis}"


def srt_to_vtt(text: str) -> str:
    """Convert SubRip text to WebVTT (same cues, WebVTT header and timestamps)."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        if "-->" in line:
            line = _SRT_TIME.sub(_vtt_time, line)
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
    if ext == ".sub":
        # A .sub can be a VobSub image stream or a text MicroDVD/SubViewer
        # file; probe it rather than letting ffmpeg print a cryptic error.
        codec = _probe_sub_codec(full)
        if codec in IMAGE_SUB_CODECS:
            raise SubError(
                "This track holds image-based subtitles (PGS/DVD), which a "
                "browser can't display."
            )
        if codec in ("microdvd", "subviewer"):
            return _ffmpeg_to_vtt(["-f", codec, "-i", full])
    if ext in CONVERTIBLE_SUBTITLE_EXTENSIONS:
        return _ffmpeg_to_vtt(["-i", full])
    raise SubError(f"{ext or 'This'} subtitles can't be shown in a browser.")


def _probe_codec_with(full: str, extra: list[str] | None = None) -> str:
    """The codec of an external subtitle file from one ffprobe call.

    ``extra`` are extra ffprobe arguments (e.g. ``["-f", "microdvd"]`` for the
    text formats whose extension ffprobe cannot trust). Returns "" when the
    probe fails.
    """
    try:
        st = os.stat(full)
        key = (os.path.normcase(os.path.abspath(full)), st.st_mtime, tuple(extra or []))
    except OSError:
        key = (os.path.normcase(os.path.abspath(full)), 0.0, tuple(extra or []))
    if key in _EXT_CODEC_CACHE:
        return _EXT_CODEC_CACHE[key]
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return ""
    cmd = [
        ffprobe, "-v", "error", *(extra or []),
        "-show_entries", "stream=codec_name",
        "-of", "json", full,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", creationflags=_no_window(),
        )
    except Exception:  # noqa: BLE001
        return ""
    codec = ""
    if result.returncode == 0:
        try:
            streams = json.loads(result.stdout or "{}").get("streams") or []
        except (TypeError, ValueError):
            streams = []
        if streams:
            codec = (streams[0].get("codec_name") or "").lower()
    if len(_EXT_CODEC_CACHE) >= _EXT_CODEC_CACHE_MAX:
        _EXT_CODEC_CACHE.clear()
    _EXT_CODEC_CACHE[key] = codec
    return codec


def _probe_sub_codec(full: str) -> str:
    """The codec of an external subtitle file, or "" when it can't be decided.

    ffprobe sniffs most files by content, but not every text format whose
    extension it cannot trust, so MicroDVD and SubViewer each get one probe
    with their demuxer forced.
    """
    codec = _probe_codec_with(full)
    if codec:
        return codec
    for demuxer in ("microdvd", "subviewer"):
        codec = _probe_codec_with(full, ["-f", demuxer])
        if codec:
            return codec
    return ""


def _vtt_embedded(local: str, kind: str, key: str) -> str:
    """WebVTT for one embedded subtitle stream of ``local``."""
    try:
        number = int(key)
    except (TypeError, ValueError):
        raise SubError("Unknown subtitle track.") from None
    map_arg = f"0:{number}" if kind == "internal" else f"0:s:{number}"
    return _ffmpeg_to_vtt(["-i", local, "-map", map_arg])


# ---- audio probe -------------------------------------------------------------


def _audio_key(local: str) -> tuple[str, int, int]:
    """Cache key for an audio probe: it changes when the file does."""
    try:
        st = os.stat(local)
        return (os.path.normcase(os.path.abspath(local)), st.st_size, int(st.st_mtime))
    except OSError:
        return (os.path.normcase(os.path.abspath(local)), 0, 0)


def _audio_from_probe(payload: str) -> dict:
    """First audio stream of an ffprobe JSON payload: ``{}`` when there is none."""
    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return {}
    streams = data.get("streams") or []
    if not streams:
        return {}
    stream = streams[0] or {}
    return {
        "codec": (stream.get("codec_name") or "").lower(),
        "channels": stream.get("channels") or 0,
        "layout": stream.get("channel_layout") or "",
    }


def _probe_audio(ffprobe: str, local: str) -> dict | None:
    """Read the first audio stream of ``local``; None when ffprobe can't."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,channels,channel_layout",
                "-of", "json", local,
            ],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace", creationflags=_no_window(),
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    return _audio_from_probe(result.stdout)


def _audio_info(local: str) -> dict | None:
    """First audio stream of a library file: None unprobed, {} when it has none."""
    key = _audio_key(local)
    if key in _AUDIO_CACHE:
        return _AUDIO_CACHE[key]
    ffprobe = resolve_ffprobe()
    info = _probe_audio(ffprobe, local) if ffprobe else None
    if info is None:
        return None  # don't remember a failed probe
    if len(_AUDIO_CACHE) >= _AUDIO_CACHE_MAX:
        _AUDIO_CACHE.clear()
    _AUDIO_CACHE[key] = info
    return info


def audio_copy_reason(audio: dict | None) -> str:
    """Why a browser certainly can't decode this audio, or "" when unsure.

    A video element asked to play DTS or TrueHD gives picture and silence with
    no error event, which looks exactly like a muted system — so the player has
    to know before it starts that those files need a browser copy. Everything
    else gets a chance to play; the page watches whether audio actually comes
    out and asks for a copy if it does not.
    """
    codec = ((audio or {}).get("codec") or "").lower()
    if codec not in UNPLAYABLE_AUDIO:
        return ""
    names = {"truehd": "TrueHD", "mlp": "MLP"}
    return f"{names.get(codec, codec.upper())} audio"


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


def _companion_file(path: str, local: str) -> Path:
    """The cached audio-only conversion for ``path`` (same key as the copy)."""
    return _cache_file(path, local).with_suffix(".m4a")


def _prepared_file(path: str) -> str | None:
    """The cached browser copy for ``path``, if one has already been made."""
    local = _local_video(path)
    if not local:
        return None
    dest = _cache_file(path, local)
    return str(dest) if dest.is_file() else None


def _prepared_audio(path: str) -> str | None:
    """The cached audio-only conversion for ``path``, if one exists."""
    local = _local_video(path)
    if not local:
        return None
    dest = _companion_file(path, local)
    return str(dest) if dest.is_file() else None


def _cache_limits() -> tuple[int, int]:
    """The play-cache retention as ``(max age in seconds, max total bytes)``.

    The Settings page can override the module defaults; a bogus stored value
    falls back to them.
    """
    try:
        days = max(0, int(config.settings.play_cache_keep_days))
    except (TypeError, ValueError):
        days = PLAY_CACHE_MAX_AGE // (24 * 3600)
    try:
        gb = max(0, int(config.settings.play_cache_max_gb))
    except (TypeError, ValueError):
        gb = PLAY_CACHE_MAX_BYTES // 1024**3
    return days * 24 * 3600, gb * 1024**3


def prune_play_cache() -> None:
    """Drop stale player-cache files: older than the configured age, then
    oldest-first over the configured size cap.

    The newest file always stays, even when it alone is over the cap: the video
    stream is copied, so a feature film's copy is nearly as large as the film,
    and deleting the copy just made would remux it again on every play.
    """
    max_age, max_bytes = _cache_limits()
    folder = _play_cache_dir()
    if not folder.is_dir():
        return
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    for f in list(folder.glob("*.mp4")) + list(folder.glob("*.m4a")):
        try:
            st = f.stat()
        except OSError:
            continue
        if now - st.st_mtime > max_age:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        files.append((st.st_mtime, st.st_size, f))
    total = sum(size for _m, size, _f in files)
    for _m, size, f in sorted(files)[:-1]:
        if total <= max_bytes:
            break
        try:
            f.unlink()
            total -= size
        except OSError:
            pass
    for part in list(folder.glob("*.part.mp4")) + list(folder.glob("*.part.m4a")):
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


async def _run_prepare(ffmpeg: str, path: str, local: str, kind: str) -> None:
    """Prepare a playback aid, then swap it into the cache.

    ``kind`` is ``"audio"`` (a fast audio-only AAC conversion the player pairs
    with the original video when its audio cannot be decoded) or ``"copy"`` (a
    full remux into a browser-friendly MP4, for files whose video itself will
    not play).
    """
    dest = _cache_file(path, local) if kind == "copy" else _companion_file(path, local)
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    what = "browser copy" if kind == "copy" else "audio conversion"
    try:
        prune_play_cache()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if tmp.is_file():
            tmp.unlink()
        duration = await _duration_for(path, local)
        if kind == "audio":
            cmd = [
                ffmpeg, "-v", "error", "-y",
                "-i", local,
                "-map", "0:a:0?",
                "-vn",
                "-c:a", "aac", "-b:a", "192k",
                "-progress", "pipe:1", "-nostats",
                str(tmp),
            ]
            what = "audio conversion"
        else:
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
            what = "browser copy"
        log("Player", "INFO", f"Making a {what} of {os.path.basename(local)}")
        code, tail = await asyncio.to_thread(
            _run_ffmpeg_with_progress, cmd, duration
        )
        if code != 0 or not tmp.is_file():
            detail = (tail or "").strip().splitlines()
            _prepare.update({
                "status": "error",
                "percent": 0,
                "error": "ffmpeg could not convert this file"
                         + (f": {detail[-1][:200]}" if detail else "."),
            })
            log("Player", "WARN", f"{what} failed for {os.path.basename(local)}")
            return
        os.replace(tmp, dest)
        _prepare.update({"status": "done", "percent": 100, "error": None})
        log("Player", "INFO", f"{what} ready: {dest.name}")
    except Exception as exc:  # noqa: BLE001
        _prepare.update({
            "status": "error", "percent": 0,
            "error": f"Could not make the {what}: {exc}",
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


def _preferred_languages() -> list[str]:
    """Language codes *and* display names the player should show first."""
    raw = (config.settings.opensubtitles_languages or "en")
    out: list[str] = []
    for code in (c.strip().lower() for c in raw.split(",")):
        if not code:
            continue
        out.append(code)
        name = _subtitle_lang_name(code)
        if name and name.lower() not in out:
            out.append(name.lower())
    return out


def _pick_default(tracks: list[dict]) -> int:
    """Index of the track to show without being asked, or -1.

    External subtitles first (those are the files the dialog downloads and
    syncs), starting with the configured languages, then anything playable.
    """
    def first(pred) -> int:
        for i, t in enumerate(tracks):
            if t.get("playable") and pred(t):
                return i
        return -1

    for pref in _preferred_languages():
        found = first(
            lambda t, p=pref: t["kind"] == "external"
            and not t["forced"]
            and (t.get("lang") or "").lower() == p
        )
        if found >= 0:
            return found
    for pred in (
        lambda t: t["kind"] == "external" and not t["forced"] and not t.get("ai"),
        lambda t: t["kind"] == "external" and not t["forced"],
        lambda t: t["kind"] == "external",
        lambda t: True,
    ):
        found = first(pred)
        if found >= 0:
            return found
    return -1


def _jellyfin_transcode_url(path: str) -> str:
    """A Jellyfin on-the-fly H.264/AAC stream for this library item, or "".

    Used when the browser cannot decode the original video codec (HEVC/x265
    and friends): Jellyfin transcodes the item to H.264 while the video
    element keeps its subtitle <track> elements. Needs Jellyfin configured
    and the item's id known (a file browsed outside the library has none).
    """
    import urllib.parse

    from .subtitles import _library_row

    row = _library_row(path)
    if not row or not row.get("id"):
        return ""
    base = (config.settings.jellyfin_url or "").strip().rstrip("/")
    key = (config.settings.jellyfin_api_key or "").strip()
    if not base or not key:
        return ""
    params = urllib.parse.urlencode({
        "static": "true",
        "api_key": key,
        "VideoCodec": "h264",
        "AudioCodec": "aac",
        "AudioSampleRate": "48000",
    })
    return f"{base}/Videos/{row['id']}/stream.mp4?{params}"


@router.get("/api/player/tracks")
async def player_tracks(user: str = Depends(require_login), path: str = Query("")):
    """Every subtitle track for one library video, as the player should list it."""
    path = (path or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
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
        codec = Path(e["file"]).suffix.lstrip(".").lower()
        playable = True
        if codec == "sub":
            # A .sub can be a VobSub image stream or MicroDVD/SubViewer text.
            probed = await asyncio.to_thread(
                _probe_sub_codec, os.path.join(os.path.dirname(local), e["file"])
            )
            if probed in IMAGE_SUB_CODECS:
                playable = False
                label += " — image subtitles"
        tracks.append({
            "spec": f"external:{e['file']}",
            "kind": "external",
            "label": label,
            "file": e["file"],
            "lang": e.get("lang") or "",
            "codec": codec,
            "forced": bool(e.get("forced")),
            "playable": playable,
        })

    default = _pick_default(tracks)
    if default >= 0:
        tracks[default]["default"] = True

    # What the browser will make of this file's audio. The page needs to know
    # before it starts: an undecodable track plays silently without any error.
    audio = await asyncio.to_thread(_audio_info, local)
    reason = audio_copy_reason(audio)

    return JSONResponse({
        "ok": True,
        "file": os.path.basename(path.replace("\\", "/")),
        "tracks": tracks,
        "audio": audio,
        "needs_copy": bool(reason),
        "copy_reason": reason,
        "copy_ready": _prepared_file(path) is not None,
        "audio_ready": _prepared_audio(path) is not None,
        "transcode_url": _jellyfin_transcode_url(path),
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


@router.api_route("/api/player/audio", methods=["GET", "HEAD"])
async def player_audio(
    user: str = Depends(require_login),
    path: str = Query(""),
    prepared: str = Query(""),
):
    """The cached audio-only conversion, for the companion <audio> element.

    The conversion is AAC, so every browser can decode it; the page plays it
    alongside the original (muted) video when the file's own audio cannot be
    decoded.
    """
    local = _prepared_audio(path)
    if not local:
        return JSONResponse(
            {"ok": False, "error": "No converted audio for this file."},
            status_code=404,
        )
    return FileResponse(
        local,
        media_type="audio/mp4",
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
async def player_prepare(
    user: str = Depends(require_login),
    path: str = Query(""),
    kind: str = Query("copy"),
):
    """Start (or report) a playback aid.

    ``kind="audio"`` converts only the audio track to AAC — fast and small —
    which the page plays next to the original video. ``kind="copy"`` is the
    full remux into a browser-friendly MP4, for files whose video itself will
    not play.
    """
    global _prepare_task

    path = (path or "").strip()
    kind = (kind or "copy").strip().lower()
    if kind not in ("audio", "copy"):
        return JSONResponse(
            {"ok": False, "error": "Unknown preparation kind."}, status_code=400
        )
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    local = _local_video(path)
    if not local:
        return JSONResponse(
            {"ok": False, "error": "The video file is not reachable."}, status_code=404
        )
    if kind == "audio" and _prepared_audio(path):
        return JSONResponse({"ok": True, "status": "done", "percent": 100, "kind": kind})
    if kind == "copy" and _prepared_file(path):
        return JSONResponse({"ok": True, "status": "done", "percent": 100, "kind": kind})

    if _prepare_running():
        if _prepare.get("path") == path and _prepare.get("kind") == kind:
            return JSONResponse(
                {
                    "ok": True,
                    "status": "running",
                    "percent": _prepare["percent"],
                    "kind": kind,
                }
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
                "error": "ffmpeg is not available, so the audio can't be converted.",
            },
            status_code=400,
        )

    _prepare.update({
        "status": "running", "path": path, "percent": 0, "error": None, "kind": kind,
    })
    _prepare_task = asyncio.create_task(_run_prepare(ffmpeg, path, local, kind))
    return JSONResponse({"ok": True, "status": "running", "percent": 0, "kind": kind})


@router.get("/api/player/prepare/status")
async def player_prepare_status(user: str = Depends(require_login), path: str = Query("")):
    """Progress for the audio conversion or browser copy of one file."""
    path = (path or "").strip()
    kind = _prepare.get("kind")
    if _prepare.get("path") == path and not _prepare_running():
        if _prepare["status"] == "error":
            return JSONResponse({
                "ok": True, "status": "error", "percent": 0,
                "error": _prepare["error"],
                "kind": kind,
            })
    if _prepare.get("path") == path and _prepare_running():
        percent = int(_prepare["percent"] or 0)
        label = "Converting audio…" if kind == "audio" else "Making a browser copy…"
        return JSONResponse({
            "ok": True, "status": "running", "percent": percent,
            "message": f"{label} {percent}%",
            "kind": kind,
        })
    if path and _prepared_file(path):
        return JSONResponse({"ok": True, "status": "done", "percent": 100, "kind": "copy"})
    if path and _prepared_audio(path):
        return JSONResponse({"ok": True, "status": "done", "percent": 100, "kind": "audio"})
    return JSONResponse({"ok": True, "status": "idle", "percent": 0})
