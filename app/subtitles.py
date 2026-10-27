"""Media scanning — probe files for subtitle and audio tracks with ffprobe.

Scan jobs walk the Movie Match items, resolve each path to this machine, run
ffprobe once to list the embedded subtitle and audio streams, and detect
external subtitle files sitting next to the video. Results are cached to
``data/scans.json`` and shown in the Search tab.

One probe fills both halves of an entry, so a single scan collects subtitles and
audio together: an entry's ``scanned`` flag means both have been probed. New
files are added unscanned by the reconcile step and picked up by the next scan.

An external subtitle can also appear (or change, or go away) next to a video
that has not changed at all, which an unscanned-only worklist would never
notice. So before probing, the scan lists each folder once and re-checks the
sidecar files of every already-scanned item — no ffprobe — and flags the ones
that differ for a fresh probe in the same pass.

The subtitle classification mirrors Subber's "Movie List" tab so the two tools
agree on what "acceptable", "AI sub", "forced", "foreign" and "no subs" mean.
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
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import JSONResponse

from .config import config
from .logs import log
from .paths import resolve_path
from .web import require_login

router = APIRouter()

_SCANS_FILE = config.data_dir / "scans.json"
# The pre-audio store. Read once on upgrade, then kept on disk as a backup.
_LEGACY_SUBS_FILE = config.data_dir / "subtitles.json"

# External subtitle file extensions.
SUBTITLE_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup", ".idx", ".sbv", ".ttml",
}

# Files below this size (bytes) are placeholders: they never count as a
# usable subtitle, but the Search filter can still list them as "Tiny".
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
    "ca": "Catalan", "cat": "Catalan",
    "eu": "Basque", "eus": "Basque", "baq": "Basque",
    "gl": "Galician", "glg": "Galician",
    "kn": "Kannada", "kan": "Kannada",
    "ml": "Malayalam", "mal": "Malayalam",
    "ms": "Malay", "msa": "Malay", "may": "Malay",
    "ta": "Tamil", "tam": "Tamil",
    "te": "Telugu", "tel": "Telugu",
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
    "catalan": "Catalan", "catala": "Catalan", "català": "Catalan",
    "basque": "Basque", "euskara": "Basque", "euskera": "Basque",
    "galician": "Galician", "galego": "Galician", "gallego": "Galician",
    "kannada": "Kannada", "ಕನ್ನಡ": "Kannada",
    "malayalam": "Malayalam", "മലയാളം": "Malayalam",
    "malay": "Malay", "melayu": "Malay",
    "norsk": "Norwegian", "bokmal": "Norwegian", "bokmål": "Norwegian",
    "nynorsk": "Norwegian",
    "tamil": "Tamil", "தமிழ்": "Tamil",
    "telugu": "Telugu", "తెలుగు": "Telugu",
}
# Canonical display name -> the shortest code we know. Used when the Settings
# field accepts names ("Swedish") but OpenSubtitles expects codes ("sv").
_LANG_CODES: dict[str, str] = {}
for _code, _name in _LANG_NAMES.items():
    _LANG_CODES.setdefault(_name, _code)

# ---- persistence ------------------------------------------------------------


def _load_state() -> dict:
    data = {}
    legacy = False
    try:
        data = json.loads(_SCANS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not data.get("files"):
        # Upgrading from the subtitle-only store. Its entries carry no audio;
        # one full scan fills that in.
        try:
            data = json.loads(_LEGACY_SUBS_FILE.read_text(encoding="utf-8"))
            legacy = isinstance(data, dict) and bool(data.get("files"))
        except (OSError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    for entry in files.values():
        if isinstance(entry, dict):
            entry.setdefault("audio", [])
            # Dropped when audio joined the one scan engine.
            entry.pop("audio_scanned", None)
    ok = data.get("ok")
    if not isinstance(ok, list):
        ok = []
    return {
        "files": files,
        "ok": ok,
        "full_scan_at": data.get("full_scan_at"),
        "change_scan_at": data.get("change_scan_at"),
        "legacy": legacy,
    }


def _save_state() -> None:
    payload = {
        "version": 3,
        "full_scan_at": _state["full_scan_at"],
        "change_scan_at": _state["change_scan_at"],
        "files": _state["files"],
        "ok": _state["ok"],
    }
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _SCANS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_SCANS_FILE)
    except OSError:
        return
    if _state.get("legacy"):
        # The merged file now owns the data. Keep the old subtitle-only store:
        # it is a free backup of a scan over 26k files, and deleting it the
        # moment the first save runs is not worth the risk.
        log(
            "Search",
            "INFO",
            "Subtitle store moved to scans.json; subtitles.json is no longer read and may be deleted.",
        )
        _state["legacy"] = False


_state = _load_state()
_fetch = {"status": "idle", "phase": None, "scanned": 0, "total": 0, "error": None}


def scans_map(preferred: list[str] | None = None) -> dict:
    """The scan store, keyed by Jellyfin path (``{path: entry}``).

    Each entry carries the subtitle half (``internal``/``external``/``subs``/
    ``flags``) and the audio half (``audio``), plus one scanned flag per half.

    With ``preferred``, the flags are rebuilt from the stored tracks so a
    change to the Settings preferred languages applies without rescanning.
    The stored entries are not modified.
    """
    files = _state["files"]
    if preferred is None:
        return files
    out: dict = {}
    for path, entry in files.items():
        if entry.get("scanned") and not entry.get("folder"):
            entry = {
                **entry,
                "flags": _classify_tracks(
                    entry.get("internal") or [],
                    entry.get("external") or [],
                    preferred,
                ),
            }
        out[path] = entry
    return out


def subtitles_ok() -> list[str]:
    """Jellyfin paths the user has manually marked as "Subtitle Ok"."""
    return list(_state["ok"])


def prune_to_paths(paths: set[str]) -> int:
    """Drop scan entries and "Subtitle Ok" marks for paths no longer listed.

    Called after a Movie Match fetch so the store follows the library without
    waiting for a media scan (which prunes the same way via ``_reconcile``).
    Returns the number of scan entries removed.
    """
    files = _state["files"]
    keep = {k: v for k, v in files.items() if k in paths}
    removed = len(files) - len(keep)
    ok = [k for k in _state["ok"] if k in paths]
    if removed or len(ok) != len(_state["ok"]):
        _state["files"] = keep
        _state["ok"] = ok
        _save_state()
    return removed


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


def _language_token(value: str) -> str:
    """One language token, lowercased and stripped of a region suffix."""
    token = re.split(r"[-_]", (value or "").strip(), maxsplit=1)[0].lower()
    return re.sub(r"[^a-z0-9]", "", token)


def normalize_language_codes(raw: str) -> str:
    """Settings input -> comma-separated codes, accepting full names too.

    OpenSubtitles expects codes, while the Settings field is friendlier if
    ``English, Swedish`` works as well as ``en,sv``. Unknown tokens are kept
    as-is so a code we do not know still reaches the search API.
    """
    codes: list[str] = []
    for part in re.split(r"[\s,;]+", (raw or "").strip()):
        token = _language_token(part)
        if not token:
            continue
        name = _LANG_NAMES.get(token) or _FULL_LANG_NAMES.get(token)
        code = _LANG_CODES.get(name) if name else None
        code = code or token
        if code not in codes:
            codes.append(code)
    return ",".join(codes)


def preferred_language_names(raw: str | None = None) -> list[str]:
    """Canonical display names from the preferred-languages setting."""
    if raw is None:
        raw = config.settings.opensubtitles_languages
    if not (raw or "").strip():
        raw = "en"
    names: list[str] = []
    for part in re.split(r"[\s,;]+", (raw or "").strip()):
        token = _language_token(part)
        name = _LANG_NAMES.get(token) or _FULL_LANG_NAMES.get(token)
        if name and name not in names:
            names.append(name)
    return names


def _text_tokens(lowered: str) -> list[str]:
    """The words of ``lowered``, keeping letters and combining marks.

    A plain ``\\w+`` regex splits Indic words at their virama and vowel signs
    (those are marks, not word characters): "ಕನ್ನಡ" would come out as "ಕನ" +
    "ನಡ" and never match the language table. Keeping mark characters makes
    every script read as one word.
    """
    cleaned = "".join(
        c if unicodedata.category(c)[0] in ("L", "M") else " "
        for c in lowered
    )
    return cleaned.split()


def _language_from_text(text: str) -> str:
    """Best-effort language from a subtitle title, used only when the tag is
    missing/unknown.

    The language is often carried in parentheses — ``Unknown (Català)`` is
    Catalan — so those words count just like the rest of the title. Full names
    and 3-letter codes are safe anywhere; a 2-letter code is only trusted when
    it is the sole meaningful token, so titles like "No" do not turn into
    Norwegian by accident."""
    lowered = (text or "").lower()
    # Two-word names whose first word alone would point at the wrong language:
    # "Bahasa Melayu" is Malay, "Bahasa Indonesia" is Indonesian.
    if "melayu" in lowered:
        return "Malay"
    if "bahasa indonesia" in lowered or "indonesia" in lowered or "indonesian" in lowered:
        return "Indonesian"
    tokens = [
        t for t in _text_tokens(lowered)
        if t not in _SUBTITLE_NOISE_TOKENS and t != "unknown"
    ]
    for token in tokens:
        if len(token) < 3:
            continue
        name = _LANG_NAMES.get(token) or _FULL_LANG_NAMES.get(token)
        if name:
            return name
        for full, full_name in _FULL_LANG_NAMES.items():
            if token.startswith(full):
                rest = token[len(full):]
                if not rest or rest in _SUBTITLE_NOISE_TOKENS:
                    return full_name
    if len(tokens) == 1 and len(tokens[0]) == 2:
        return _LANG_NAMES.get(tokens[0], "")
    return ""


def smart_subtitle_marks(
    subtitles: list[dict], raw: str | None = None
) -> tuple[list[int], list[str]]:
    """Indices of subtitles Smart mark would delete: non-preferred and forced.

    ``Unknown`` non-forced tracks are deliberately left unmarked: deleting a
    track whose language could not be read is the one mistake Smart mark must
    not make. Forced tracks are different: they are normally disposable when a
    full subtitle is available, so they are marked even when their language is
    preferred. Audio marks are handled by the dialog and are never touched here.
    """
    preferred = preferred_language_names(raw)
    if not preferred:
        return [], []
    wanted = set(preferred)
    marks: list[int] = []
    for track in subtitles:
        index = track.get("index")
        if index is None:
            continue
        if track.get("forced"):
            marks.append(index)
            continue
        lang = track.get("lang") or "Unknown"
        if lang == "Unknown":
            lang = _language_from_text(str(track.get("name") or ""))
        if lang and lang != "Unknown" and lang not in wanted:
            marks.append(index)
    return marks, preferred


def external_smart_marks(external: list[dict], preferred: list[str]) -> list[str]:
    """External subtitle files Smart mark would delete.

    The same policy as :func:`smart_subtitle_marks`: every forced file, plus
    every file whose language is not preferred. A file whose language could
    not be read is left alone unless its name says otherwise.
    """
    wanted = set(preferred)
    marks: list[str] = []
    for entry in external:
        if entry.get("forced"):
            marks.append(entry["file"])
            continue
        lang = entry.get("lang") or "Unknown"
        if lang == "Unknown":
            lang = _language_from_text(entry["file"])
        if lang and lang != "Unknown" and lang not in wanted:
            marks.append(entry["file"])
    return marks


def _classify_tracks(
    internal: list[dict],
    external: list[dict],
    preferred: list[str] | None = None,
) -> dict:
    """Classify a file's subtitle tracks for the Search filters.

    ``internal`` tracks are ``{lang, name, forced, codec}``; ``external``
    entries are ``{file, lang, forced, ai, tiny}``. ``preferred`` is the
    Settings language list; when omitted it is read from the current settings.

    Every returned flag is independent so the four Search filter groups can
    each be exhaustive when all of their options are selected:

    - ``preferred`` / ``foreign`` / ``unknown`` describe the language of every
      track, including forced and tiny ones.
    - ``good`` is any normal subtitle (not forced, not tiny) in any language,
      AI or otherwise.
    - ``acceptable`` is a normal subtitle in a preferred language, AI or not;
      ``acceptable_ai`` is the subset generated by AI.
    - ``forced`` and ``tiny`` are the two special kinds that are not ``good``.
    - ``nosubs`` means the file has no subtitle tracks at all.
    """
    if preferred is None:
        preferred = preferred_language_names()
    wanted = set(preferred)

    tracks: list[dict] = []
    for t in internal:
        tracks.append({
            "lang": t.get("lang") or "Unknown",
            "forced": bool(t.get("forced")),
            "tiny": False,
            "ai": False,
        })
    for e in external:
        tracks.append({
            "lang": e.get("lang") or "Unknown",
            "forced": bool(e.get("forced")),
            "tiny": bool(e.get("tiny")),
            "ai": bool(e.get("ai")),
        })

    normal = [t for t in tracks if not t["forced"] and not t["tiny"]]
    preferred_normal = [t for t in normal if t["lang"] in wanted]
    return {
        "preferred": any(t["lang"] in wanted for t in tracks),
        "foreign": any(
            t["lang"] not in wanted and t["lang"] != "Unknown" for t in tracks
        ),
        "unknown": any(t["lang"] == "Unknown" for t in tracks),
        "good": bool(normal),
        "acceptable": bool(preferred_normal),
        "acceptable_ai": any(t["ai"] for t in preferred_normal),
        "ai": any(t["ai"] for t in tracks),
        "forced": any(t["forced"] for t in tracks),
        "tiny": any(t["tiny"] for t in tracks),
        "nosubs": not tracks,
    }


def _list_dir(folder: Path) -> list[os.DirEntry] | None:
    """Directory entries, or ``None`` when the folder cannot be listed.

    ``None`` and ``[]`` mean different things to the change check: an empty
    folder really has no subtitles, an unreachable one says nothing at all.
    """
    try:
        with os.scandir(folder) as it:
            return list(it)
    except OSError:
        return None


def _external_subtitles_in(children: list[os.DirEntry], stem: str) -> list[dict]:
    """External subtitle files for ``stem`` among already-listed ``children``.

    Split out from :func:`_external_subtitles` so a caller checking many videos
    that share a folder lists that folder once instead of once per video.
    """
    if not stem:
        return []
    stem_lower = stem.lower()
    entries: list[dict] = []
    for f in children:
        try:
            if not f.is_file() or Path(f.name).suffix.lower() not in SUBTITLE_EXTENSIONS:
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
        # A filename like "Movie.Norsk.Bokmål.srt" has its language in plain
        # words the tag list does not know; let the title parser read it.
        if lang == "Unknown" and tokens:
            lang = _language_from_text(" ".join(tokens)) or "Unknown"
        try:
            tiny = f.stat().st_size < TINY_SUBTITLE_MAX_BYTES
        except OSError:
            # Gone between the listing and the stat: treat it as absent rather
            # than failing the whole scan.
            continue
        entries.append({"file": f.name, "lang": lang, "forced": forced, "ai": is_ai, "tiny": tiny})
    return entries


def _external_subtitles(movie_path: str) -> list[dict]:
    """External subtitle files next to ``movie_path`` (same base name)."""
    p = Path(movie_path)
    if not p.stem:
        return []
    children = _list_dir(p.parent)
    if children is None:
        return []
    return _external_subtitles_in(children, p.stem)


def _external_signature(external: list[dict] | None) -> list[tuple]:
    """Order-independent summary of an external subtitle list.

    Only what the scan stores is compared, so a re-listing that returns the
    same files in another order is not a change.
    """
    return sorted(
        (
            str(e.get("file") or ""),
            str(e.get("lang") or ""),
            bool(e.get("forced")),
            bool(e.get("ai")),
            bool(e.get("tiny")),
        )
        for e in (external or [])
    )


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


# ---- audio formatting -------------------------------------------------------


# ffprobe's codec names to the short labels people know from their player.
_AUDIO_CODEC_NAMES = {
    "aac": "AAC", "ac3": "AC3", "eac3": "E-AC-3", "dts": "DTS",
    "truehd": "TrueHD", "flac": "FLAC", "opus": "Opus", "mp3": "MP3",
    "vorbis": "Vorbis", "alac": "ALAC", "pcm_s16le": "PCM", "pcm_s24le": "PCM",
    "pcm_s16be": "PCM", "pcm_s24be": "PCM",
}


def _audio_codec_name(codec: str) -> str:
    c = (codec or "").strip().lower()
    if not c:
        return "Unknown"
    return _AUDIO_CODEC_NAMES.get(c) or c.upper()


def _audio_layout(channels, layout) -> str:
    """A compact channel description: ``5.1``, ``stereo``, ``7.1``, ``6ch``."""
    lay = (layout or "").strip().lower()
    if lay in ("mono", "stereo", "5.1", "7.1"):
        return lay
    try:
        n = int(channels)
    except (TypeError, ValueError):
        return lay
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(n, f"{n}ch")


def _audio_line(track: dict) -> str:
    """``English (E-AC-3 5.1)*`` — the star marks the default track."""
    label = f"{track.get('lang') or 'Unknown'} ({_audio_codec_name(track.get('codec'))}"
    layout = _audio_layout(track.get("channels"), track.get("layout"))
    if layout:
        label += " " + layout
    label += ")"
    if track.get("default"):
        label += "*"
    return label


# ---- ffprobe -----------------------------------------------------------------


def _probe_streams(
    ffprobe: str, video_path: str
) -> tuple[list[dict], list[dict], float, str | None]:
    """Probe one file once for its subtitle and audio streams.

    Returns ``(subtitles, audio, duration, error)``. Both lists carry the
    absolute ffprobe stream index, which is what the player and the audio
    delete use to address exactly one stream.
    """
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,channels,channel_layout"
        ":stream_tags=language,title"
        # Every disposition flag, not just forced/default: the Internal track
        # editor re-applies the original flags when it changes the default, so
        # setting a default must not clear commentary/dub/etc.
        ":stream_disposition:format=duration",
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
        return [], [], 0.0, f"ffprobe error: {exc}"
    if result.returncode != 0:
        return [], [], 0.0, f"ffprobe exit {result.returncode}: {(result.stderr or '').strip()[:200]}"
    try:
        data = json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
        return [], [], 0.0, f"ffprobe JSON parse failed: {exc}"

    internal = []
    audio = []
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        tags = s.get("tags") or {}
        disposition = s.get("disposition") or {}
        if kind == "subtitle":
            lang = _subtitle_lang_name(tags.get("language"))
            title = (tags.get("title") or "").strip()
            name = lang if (not title or title.lower() == lang.lower()) else f"{lang} ({title})"
            forced = bool(disposition.get("forced")) or "forced" in title.lower()
            internal.append({
                # The absolute stream index, so the player can map this track
                # exactly (``-map 0:<index>``) when it extracts it to WebVTT.
                "index": s.get("index"),
                "lang": lang,
                "name": name,
                "forced": forced,
                "default": bool(disposition.get("default")),
                "codec": s.get("codec_name") or "",
                # Kept for the Internal editor's rewrite; the scan store drops
                # it because only the flags themselves are needed there.
                "disposition": disposition,
            })
        elif kind == "audio":
            code = (tags.get("language") or "").strip().lower()
            title = (tags.get("title") or "").strip()
            track = {
                "index": s.get("index"),
                "lang": _subtitle_lang_name(code),
                "code": code,
                "title": title,
                "codec": s.get("codec_name") or "",
                "channels": s.get("channels"),
                "layout": s.get("channel_layout") or "",
                "default": bool(disposition.get("default")),
                "commentary": "commentary" in title.lower(),
                "disposition": disposition,
            }
            track["name"] = _audio_line(track)
            audio.append(track)

    duration = 0.0
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return internal, audio, duration, None


def _probe_subtitles(ffprobe: str, video_path: str) -> tuple[list[dict], float, str | None]:
    """Subtitle-only view of :func:`_probe_streams` (used by the player)."""
    internal, _audio, duration, err = _probe_streams(ffprobe, video_path)
    return internal, duration, err


# ---- scanning ---------------------------------------------------------------


def _stat_path(local: str):
    try:
        return os.stat(local)
    except OSError:
        return None


def _stub(path: str) -> dict:
    """A minimal store entry for a library item that has not been probed yet."""
    return {
        "path": path,
        "file": Path(path).name,
        "scanned": False,
        "audio": [],
    }


def _reconcile(files: dict, ok: list[str], current_paths: set[str]) -> tuple[dict, list[str]]:
    """Sync the store with the library list: add unscanned stubs for new items,
    drop entries (and ok marks) whose path is no longer listed."""
    for p in current_paths:
        if p not in files:
            files[p] = _stub(p)
    files = {k: v for k, v in files.items() if k in current_paths}
    ok = [k for k in ok if k in current_paths]
    return files, ok


def _changed_entries(rows: list[dict], files: dict) -> list[str]:
    """Paths whose external subtitle files differ from what the store recorded.

    One directory listing per folder, no ffprobe. Only already-scanned entries
    are checked: unscanned ones are queued for probing anyway. Folders are
    grouped and listed once because a season folder holds many episodes — 26k
    items live in far fewer folders, and every listing is a round trip when the
    library is on a share.
    """
    by_folder: dict[str, list[tuple[str, str, list]]] = {}
    for r in rows:
        key = r.get("path")
        entry = files.get(key) or {}
        if not entry.get("scanned") or entry.get("folder"):
            continue
        local = resolve_path(key, config.settings.path_maps)
        folder = os.path.dirname(local)
        if not folder or not os.path.basename(local):
            continue
        by_folder.setdefault(folder, []).append((local, key, entry.get("external") or []))

    changed: list[str] = []
    for folder, items in by_folder.items():
        children = _list_dir(Path(folder))
        if children is None:
            # Unreachable right now. Keep what the last scan recorded: clearing
            # it here would throw away good data because of one SMB hiccup.
            continue
        for local, key, stored in items:
            fresh = _external_subtitles_in(children, Path(local).stem)
            if _external_signature(fresh) != _external_signature(stored):
                changed.append(key)
    return changed


def _build_worklist(
    rows: list[dict], files: dict
) -> list[tuple[str, str, object]]:
    """Return the (key, local, st) triples to probe — every entry not yet
    scanned.

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
        "audio": [],
        "error": None,
    }
    if st is None:
        entry["error"] = "unreachable"
        return key, entry
    if stat.S_ISDIR(st.st_mode):
        # Jellyfin lists a series as an item whose Path is the show's folder.
        # There is no file to probe — its episodes are rows of their own — so
        # record it as done instead of failing it: a failure would keep it in
        # every worklist and in the "Not scanned" filter forever.
        entry["folder"] = True
        entry["scanned"] = True
        return key, entry
    if not stat.S_ISREG(st.st_mode):
        entry["error"] = "not a file"
        return key, entry
    internal, audio, duration, err = _probe_streams(ffprobe, local)
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
            "default": bool(t.get("default")),
            "codec": t["codec"],
        }
        for t in internal
    ]
    entry["external"] = external
    entry["subs"] = _display_lines(internal, external)
    entry["flags"] = _classify_tracks(internal, external)
    # The full disposition map is only needed while rewriting a file. Keeping
    # it out of scans.json keeps the store small and stable.
    entry["audio"] = [
        {k: v for k, v in t.items() if k != "disposition"} for t in audio
    ]
    entry["scanned"] = True
    return key, entry


async def run_scan(mode: str) -> None:
    """Probe the Movie Match items for subtitle and audio tracks.

    ``mode`` is ``"scan"`` (reconcile, re-check external subtitles, then probe
    every unscanned file) or ``"full"`` (mark every file unscanned first, then
    the same scan). One ffprobe fills both the subtitle and the audio half of
    an entry.
    """
    if _fetch["status"] == "running":
        return
    label = "Media scan" + (" (full)" if mode == "full" else "")
    _fetch.update({"status": "running", "phase": None, "scanned": 0, "total": 0, "error": None})
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

        # A sidecar subtitle can appear (or change, or go away) next to a video
        # that has not changed, so the unscanned-only worklist would never see
        # it. Re-check the sidecar files of the scanned entries first — one
        # directory listing per folder, no ffprobe — and flag whatever differs
        # so it is probed by this same pass.
        changed = 0
        if mode != "full":
            _fetch["phase"] = "checking"
            try:
                flagged = await asyncio.to_thread(_changed_entries, rows, files)
            except Exception as exc:  # noqa: BLE001
                flagged = []
                log("Search", "WARN", f"{label}: subtitle check failed: {exc}")
            finally:
                _fetch["phase"] = None
            for key in flagged:
                entry = files.get(key)
                if not entry:
                    continue
                # Same invalidation as _mark_unscanned, without a store write
                # per item: one save at the end of the run covers them all.
                entry["scanned"] = False
                entry["external"] = []
                entry["subs"] = []
                entry["flags"] = None
                entry["error"] = None
                changed += 1
            if changed:
                flagged_set = set(flagged)
                _state["ok"] = [k for k in _state["ok"] if k not in flagged_set]
                log(
                    "Search",
                    "INFO",
                    f"{label}: {changed} item(s) have new or changed subtitles",
                )

        # Resolve + stat each path off the event loop — os.stat over SMB/NFS can
        # be slow, and doing it inline would freeze the app during a big scan.
        to_scan = await asyncio.to_thread(_build_worklist, rows, files)

        _fetch["total"] = len(to_scan)
        folders = 0
        failed = 0
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
                    if entry.get("folder"):
                        folders += 1
                    elif entry.get("error"):
                        failed += 1
                        log("Search", "WARN", f"{entry['file']}: {entry['error']}")
                    else:
                        summary = ", ".join(entry.get("subs") or []) or "no subs"
                        audio = ", ".join(t.get("name", "") for t in (entry.get("audio") or []))
                        log(
                            "Search",
                            "INFO",
                            f"{entry['file']}: {summary}; {audio or 'no audio'}",
                        )
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
            f"{label} complete: {len(to_scan)} probed, {len(rows) - len(to_scan)} skipped"
            + (f", {changed} subtitle change(s)" if changed else "")
            + (f", {folders} series folder(s)" if folders else "")
            + (f", {failed} could not be probed (see warnings)" if failed else ""),
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

    Used by the "Subtitles & audio" row action: it ignores the ``scanned`` flag
    and probes the file now, marking it scanned only on a definitive result.
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
    if entry.get("folder"):
        log("Search", "INFO", f"Rescanned {entry['file']}: series folder, nothing to probe")
    elif entry.get("error"):
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
    from .scheduler import busy_message, trigger_operation

    op = "subtitle_scan_full" if mode == "full" else "subtitle_scan"
    started, _ = trigger_operation(op)
    return JSONResponse(
        {"ok": True, "started": started, "message": "Scan started." if started else busy_message()}
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


async def audio_tracks_for(path: str, force: bool = False) -> tuple[list[dict], str | None]:
    """``(tracks, error)`` for a library video's embedded audio streams.

    A fresh probe is the default: the audio dialog deletes by absolute stream
    index, so the list it shows must match the file on disk right now. It also
    refreshes the store entry, both halves, for free.
    """
    entry = _state["files"].get(path) or {}
    if not force and entry.get("scanned"):
        return list(entry.get("audio") or []), None
    fresh = await rescan_one(path)
    return list(fresh.get("audio") or []), fresh.get("error")


async def internal_tracks_for(
    path: str, force: bool = False
) -> tuple[list[dict], list[dict], str | None]:
    """``(subtitles, audio, error)`` for a library video's embedded streams.

    The Internal dialog edits by absolute stream index, so it asks for a fresh
    probe by default. That also refreshes the scan store for both halves.
    External subtitle files are deliberately not included; they are managed
    from the Subtitles dialog.
    """
    entry = _state["files"].get(path) or {}
    if not force and entry.get("scanned"):
        return (
            list(entry.get("internal") or []),
            list(entry.get("audio") or []),
            None,
        )
    fresh = await rescan_one(path)
    return (
        list(fresh.get("internal") or []),
        list(fresh.get("audio") or []),
        fresh.get("error"),
    )


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
    """Flag a library item for a fresh scan after its subtitles change.

    The Search tab then shows "Not scanned", the flag filters stop matching
    stale data, and the next scan (the Media scan button, closing the dialog,
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


def _jellyfin_query(row: dict) -> str:
    """The OpenSubtitles query for a Movie Match row.

    Episodes are searched by their series, everything else by the item's title.
    """
    if (row.get("type") or "") in ("Episode", "Series"):
        return (row.get("series") or "").strip()
    return (row.get("name") or row.get("display") or "").strip()


def _filename_query(path: str) -> str:
    """The release name of a file, without its extension.

    Dots and underscores separate words in release names, so they read as spaces
    here; the release tags are left on, that is what makes this search different
    from the name search.
    """
    stem = Path(path).stem
    return " ".join(stem.replace("_", " ").replace(".", " ").split())


@router.post("/api/subtitles/search")
async def subtitles_search(
    user: str = Depends(require_login),
    path: str = Form(""),
    language: str = Form(""),
    method: str = Form(""),
):
    """Search OpenSubtitles for a library file.

    ``method`` picks the search and is what the dialog's three search buttons
    send: "hash" (the moviehash only), "filename" (the release name on disk) or
    "jellyfin" (the Jellyfin item name and year). Empty means the default search:
    the moviehash first, the Jellyfin name when that finds nothing.
    """
    path = path.strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    row = _library_row(path)
    if row is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    method = method.strip().lower()
    if method not in ("", "hash", "filename", "jellyfin"):
        return JSONResponse(
            {"ok": False, "error": f"Unknown search method '{method}'."},
            status_code=400,
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

    row_type = row.get("type") or ""
    is_episode = row_type in ("Episode", "Series")
    season, episode = _season_episode(path)
    # Movies and episodes are two different searches for OpenSubtitles.
    kind = (
        {"type": "episode", "season_number": season, "episode_number": episode}
        if is_episode
        else {"type": "movie"}
    )
    local = resolve_path(path, config.settings.path_maps)
    reachable = bool(local) and os.path.isfile(local)
    # The method the run settles on: the one asked for, "name" for the default's
    # fallback. "query" is the text a name search was given, for the dialog.
    used = method or "hash"
    query_used = ""
    result = None

    async def run(label: str, where: str, **params) -> None:
        """Search OpenSubtitles, log it and remember what the search was."""
        nonlocal result, query_used
        result = await client.search(languages=langs, **params)
        query_used = params.get("query") or ""
        log(
            "Search",
            "INFO",
            f"Subtitle {label} search {Path(path).name}: "
            f"{where} -> {result['total_count']} result(s)",
        )

    if method == "hash" and not reachable:
        return JSONResponse(
            {
                **base,
                "ok": False,
                "error": (
                    "The video file is not reachable, so it cannot be hashed. "
                    "Set the Media paths on the Settings page first."
                ),
            },
            status_code=400,
        )

    try:
        if method == "filename":
            query = _filename_query(path)
            await run("filename", f'"{query}"', query=query, **kind)
        elif method == "jellyfin":
            query = _jellyfin_query(row) or Path(path).name
            # An episode's own year is not the series year OpenSubtitles wants.
            year = None if is_episode else row.get("year")
            where = f'"{query}"' + (f" ({year})" if year else "")
            await run("jellyfin", where, query=query, year=year, **kind)
        elif method == "hash":
            # Hash only: an exact match for this release, and nothing else.
            moviehash, size = await asyncio.to_thread(opensubtitles_hash, local)
            await run("hash", f"{moviehash} ({size} B)", moviehash=moviehash)
        else:
            # Default: hash first, name when that finds nothing.
            if reachable:
                moviehash, size = await asyncio.to_thread(opensubtitles_hash, local)
                await run("hash", f"{moviehash} ({size} B)", moviehash=moviehash)
            else:
                base["note"] = "File unreachable — searched by name only."
            if result is None or result["total_count"] == 0:
                used = "name"
                query = _jellyfin_query(row) or Path(path).name
                await run("name", f'"{query}"', query=query, **kind)
    except OpenSubtitlesError as exc:
        log("Search", "WARN", f"Subtitle search failed {Path(path).name}: {exc}")
        return JSONResponse({**base, "ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse(
        {
            **base,
            "remaining": client.remaining(),
            "method": used,
            "query": query_used,
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
    # (or the Media scan button / scheduled job) does the actual ffprobe pass.
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


@router.post("/api/subtitles/rename")
async def subtitles_rename(
    user: str = Depends(require_login),
    x_jftools: str = Header(default=""),
    path: str = Form(""),
    file: str = Form(""),
    language: str = Form(""),
):
    """Rename one external subtitle file to ``<video-stem>.<code><ext>``.

    Write-gated like the other file mutations. ``language`` is a code or a
    display name; the new name matches what the downloader writes. The file
    must be one of the video's own external subtitles, so this can never touch
    an arbitrary file.
    """
    gate = _write_gate(x_jftools)
    if gate:
        return gate

    path = (path or "").strip()
    file = (file or "").strip()
    language = (language or "").strip()
    if not path or not file:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    if _library_row(path) is None:
        return JSONResponse(
            {"ok": False, "error": "Not in the library list."}, status_code=404
        )
    code = normalize_language_codes(language).split(",")[0] if language else ""
    if not code:
        return JSONResponse(
            {"ok": False, "error": "No language to rename to."}, status_code=400
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

    folder = os.path.dirname(local)
    ext = Path(file).suffix.lower()
    target_name = f"{Path(local).stem}.{code}{ext}"
    if target_name == file:
        return JSONResponse(
            {"ok": True, "message": f"Already named {file}.", "file": file}
        )
    if os.path.lexists(os.path.join(folder, target_name)):
        return JSONResponse(
            {
                "ok": False,
                "exists": True,
                "error": f"{target_name} already exists.",
            },
            status_code=409,
        )

    from .files import browser_roots, rename_entry

    result = rename_entry(os.path.join(folder, file), target_name, browser_roots())
    if not result.get("ok"):
        log("Search", "WARN", f"Subtitle rename refused ({result.get('error')}): {file}")
        return JSONResponse(
            {"ok": False, "error": result.get("error") or "Rename failed."},
            status_code=400,
        )

    _mark_unscanned(path)

    # Tell Jellyfin the file changed so the new name shows up right away.
    jellyfin = None
    try:
        from .jellyfin.client import JellyfinError, get_client

        await get_client().scan_paths([path])
    except JellyfinError as exc:
        jellyfin = str(exc)
        log("Search", "WARN", f"Jellyfin rescan after rename failed: {exc}")

    log("Search", "INFO", f"Renamed subtitle: {file} -> {target_name}")
    return JSONResponse(
        {
            "ok": True,
            "message": f"Renamed {file} to {target_name}.",
            "file": target_name,
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
