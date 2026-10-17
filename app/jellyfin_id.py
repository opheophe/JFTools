"""Movie Match — list Jellyfin media with file paths and score them against
their identified titles.

Everything comes from the Jellyfin API (no drive scanning). A manual "Fetch"
pulls all media items that have a file path; results are cached server-side and
persisted so reopening the app never re-fetches unless asked.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError, get_client
from .logs import log
from .web import require_login, templates

router = APIRouter()

# ---- persistence ------------------------------------------------------------

_ID_FILE = config.data_dir / "jellyfin_id.json"


def _load_state() -> dict:
    try:
        data = json.loads(_ID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {
        "rows": data.get("rows") or [],
        "marks": set(data.get("marks") or []),
        "last_sync": data.get("last_sync"),
    }


def _save_state() -> None:
    payload = {
        "rows": _state["rows"],
        "marks": sorted(_state["marks"]),
        "last_sync": _state["last_sync"],
    }
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _ID_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ID_FILE)
    except OSError:
        pass


def clear_cache() -> None:
    """Reset the Movie Match cache (rows, marks and last-sync)."""
    _state["rows"] = []
    _state["marks"] = set()
    _state["last_sync"] = None
    _save_state()


_state = _load_state()
_fetch = {"status": "idle", "fetched": 0, "total": 0, "error": None}

# ---- filename cleaning + matching ------------------------------------------

_RELEASE_TOKENS = re.compile(
    r"\b("
    r"2160p|1080p|720p|480p|4k|uhd|hdr10\+?|hdr|dv|dolby\s?vision|sdr|"
    r"bluray|blu-?ray|brrip|bdrip|bd-?remux|remux|webrip|web-?dl|webdl|web|hdrip|dvdrip|dvd|hdtv|camrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|10bit|8bit|hi10p|"
    r"aac|ac3|eac3|ddp?\d?(?:\.\d)?|dd\+|dts(?:-hd)?(?:\s?ma)?|truehd|atmos|flac|mp3|"
    r"\d+ch|5\.1|7\.1|2\.0|"
    r"yts|yify|rarbg|ntg|orarbg|uindex|psa|evo|cmrg|framestor|"
    r"internal|proper|repack|extended|unrated|remastered|multi|dual\s?audio|"
    r"amzn|nf|dsnp|hmax|atvp|hulu|itunes"
    r")\b",
    re.IGNORECASE,
)

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20",
}


def clean_release_tags(name: str) -> str:
    """Strip file extensions, separators and common release/quality tags."""
    s = str(name or "")
    s = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", s)
    s = re.sub(r"[._]+", " ", s)
    s = _RELEASE_TOKENS.sub(" ", s)
    s = re.sub(r"[\[\]\(\)\{\}]", " ", s)
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = " ".join(_NUM_WORDS.get(t, t) for t in s.split())
    return re.sub(r"\s+", " ", s).strip()


def _lcs_frac(short: str, long: str) -> float:
    if not short or not long:
        return 0.0
    sm = difflib.SequenceMatcher(None, short, long)
    m = sm.find_longest_match(0, len(short), 0, len(long))
    return m.size / len(short)


def _year_tokens(tokens):
    return {t for t in tokens if re.fullmatch(r"(19|20)\d{2}", t)}


def name_match_score(disc_name: str, jf_name: str, jf_year=None) -> int:
    """Score how well a (cleaned) file name matches Jellyfin's title (0-100).

    The year is ignored here — it is scored separately via year_match_category().
    """
    a = _norm(disc_name)
    b = _norm(jf_name)
    if not a or not b:
        return 0
    if a == b:
        return 100

    a_toks = a.split()
    b_toks = b.split()

    if jf_year:
        jy = str(int(jf_year))
        a_core_toks = [t for t in a_toks if t != jy]
        b_core_toks = [t for t in b_toks if t != jy]
    else:
        a_core_toks, b_core_toks = a_toks, b_toks

    if b_core_toks and len(b_core_toks) <= len(a_core_toks):
        small, large = b_core_toks, a_core_toks
    else:
        small, large = a_core_toks, b_core_toks
    if small:
        large_set = set(large)
        coverage = sum(1 for t in small if t in large_set) / len(small)
    else:
        coverage = 0.0

    a_core = " ".join(a_core_toks)
    b_core = " ".join(b_core_toks)
    short, long = (b_core, a_core) if len(b_core) <= len(a_core) else (a_core, b_core)
    partial = _lcs_frac(short, long)

    score = 0.55 * coverage + 0.45 * partial
    if a_core != b_core:
        score = min(score, 0.95)

    disc_set = set(a_core_toks)
    for t in b_core_toks:
        if t.isdigit() and t not in disc_set:
            score = min(score, 0.20)
            break

    return max(1, round(score * 100))


def _path_years(path: str) -> list[int]:
    """All standalone/[year]/(year) years found in the filename and folder."""
    p = Path(path)
    years = []
    for s in (p.name, p.parent.name):
        for m in re.findall(r"\b((?:19|20)\d{2})\b", s):
            yv = int(m)
            if 1900 <= yv <= 2100:
                years.append(yv)
    return years


def year_match_category(path: str, jf_year) -> str:
    """Classify how the path's years compare to Jellyfin's year.

    Jellyfin-first: check whether Jellyfin's year appears in the path, rather
    than guessing which number in the filename is the year. Returns one of
    "Correct", "+/-1 year", "Mismatch" or "No year".
    """
    if jf_year is None:
        return "No year"
    jf = int(jf_year)
    years = _path_years(path)
    if jf in years:
        return "Correct"
    if not years:
        return "No year"
    closest = min(years, key=lambda y: abs(y - jf))
    if abs(closest - jf) == 1:
        return "+/-1 year"
    return "Mismatch"


# ---- fetching ---------------------------------------------------------------


def _item_title(it: dict) -> str:
    if it.get("Type") == "Episode":
        return it.get("SeriesName") or it.get("Name") or ""
    return it.get("Name") or ""


def _item_display(it: dict) -> str:
    name = it.get("Name") or ""
    series = it.get("SeriesName") or ""
    year = it.get("ProductionYear")
    if it.get("Type") == "Episode" and series:
        return f"{series} — {name}"
    if year:
        return f"{name} ({year})"
    return name


def _library_for_path(path: str, locations: list[tuple[str, str]]) -> str:
    p = (path or "").lower()
    best = ""
    best_len = -1
    for name, loc in locations:
        l = (loc or "").lower().rstrip("/\\")
        if l and p.startswith(l) and len(l) > best_len:
            best = name
            best_len = len(l)
    return best


async def _fetch_items(client: JellyfinClient, progress) -> list[dict]:
    libs = await client.get_libraries()
    locations = []
    for lib in libs:
        for loc in (lib.get("Locations") or []):
            locations.append((lib.get("Name") or "", loc))

    params = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Episode,Series,MusicVideo,Video",
        "Fields": "Path,ProductionYear,ProviderIds,SeriesName",
        "Limit": "500",
        "StartIndex": "0",
        "SortBy": "SortName",
    }
    rows: list[dict] = []
    fetched = 0
    total = 0
    while True:
        data = await client.get_items(params)
        total = int(data.get("TotalRecordCount", 0) or 0)
        batch = data.get("Items") or []
        fetched += len(batch)
        for it in batch:
            path = it.get("Path")
            if not path:
                continue
            provider = it.get("ProviderIds") or {}
            title = _item_title(it)
            rows.append(
                {
                    "id": it.get("Id", ""),
                    "type": it.get("Type", ""),
                    "path": path,
                    "file": Path(path).name,
                    "name": it.get("Name") or "",
                    "series": it.get("SeriesName") or "",
                    "display": _item_display(it),
                    "year": it.get("ProductionYear"),
                    "library": _library_for_path(path, locations),
                    "imdb": provider.get("Imdb", ""),
                    "tmdb": provider.get("Tmdb", ""),
                    "match": name_match_score(clean_release_tags(Path(path).name), title, it.get("ProductionYear")),
                    "year_match": year_match_category(path, it.get("ProductionYear")),
                }
            )
        progress(fetched, total)
        if not batch or fetched >= total:
            break
        params["StartIndex"] = str(fetched)
    return rows


async def _run_fetch() -> None:
    _fetch.update({"status": "running", "fetched": 0, "total": 0, "error": None})
    log("Movie Match", "INFO", "Fetch started")
    try:
        client = get_client()

        def _progress(fetched: int, total: int) -> None:
            _fetch["fetched"] = fetched
            _fetch["total"] = total

        rows = await _fetch_items(client, _progress)
        rows.sort(key=lambda r: r["file"].lower())
        _state["rows"] = rows
        _state["last_sync"] = time.time()
        _save_state()
        _fetch["status"] = "done"
        log("Movie Match", "INFO", f"Fetch complete: {len(rows)} item(s)")
    except JellyfinError as exc:
        _fetch["status"] = "error"
        _fetch["error"] = str(exc)
        log("Movie Match", "ERROR", f"Fetch failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        _fetch["status"] = "error"
        _fetch["error"] = f"Unexpected error: {exc}"
        log("Movie Match", "ERROR", f"Fetch failed: {exc}")


# ---- routes -----------------------------------------------------------------


@router.get("/jellyfin-id", response_class=HTMLResponse)
async def jellyfin_id_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "jellyfin_id.html", {"request": request})


@router.get("/api/jellyfin-id/items")
async def jellyfin_id_items(request: Request, user: str = Depends(require_login)):
    return JSONResponse(
        {
            "ok": True,
            "fetched": bool(_state["rows"]),
            "rows": _state["rows"],
            "marks": sorted(_state["marks"]),
            "last_sync": _state["last_sync"],
            "jellyfin_url": config.settings.jellyfin_url,
            "radarr_url": config.settings.radarr_url,
            "sonarr_url": config.settings.sonarr_url,
        }
    )


@router.post("/api/jellyfin-id/refresh")
async def jellyfin_id_refresh(request: Request, user: str = Depends(require_login)):
    if _fetch["status"] == "running":
        return JSONResponse({"ok": True, "message": "Already fetching."})
    asyncio.create_task(_run_fetch())
    return JSONResponse({"ok": True, "message": "Fetch started."})


@router.get("/api/jellyfin-id/status")
async def jellyfin_id_status(request: Request, user: str = Depends(require_login)):
    return JSONResponse({"ok": True, **_fetch})


@router.post("/api/jellyfin-id/mark")
async def jellyfin_id_mark(
    request: Request,
    user: str = Depends(require_login),
    item_id: str = Form(...),
    marked: str = Form("true"),
):
    if marked in ("true", "1", "yes"):
        _state["marks"].add(item_id)
        log("Movie Match", "INFO", f"Marked {item_id}")
    else:
        _state["marks"].discard(item_id)
        log("Movie Match", "INFO", f"Unmarked {item_id}")
    _save_state()
    return JSONResponse({"ok": True})
