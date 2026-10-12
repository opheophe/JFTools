"""Libraries — the Jellyfin library list and item counts.

Counting items means one extra Jellyfin request per library, so the result is
cached to ``data/libraries.json`` and refreshed in the background (on a schedule
and on demand). Opening the page never blocks on Jellyfin.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError
from .logs import log
from .web import require_login, templates

router = APIRouter()

_LIBRARIES_FILE = config.data_dir / "libraries.json"


def _client() -> JellyfinClient:
    return JellyfinClient(config.settings.jellyfin_url, config.settings.jellyfin_api_key)


# ---- persistence ------------------------------------------------------------


def _load_state() -> dict:
    try:
        data = json.loads(_LIBRARIES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    libraries = data.get("libraries")
    if not isinstance(libraries, list):
        libraries = []
    recent = data.get("recent")
    if not isinstance(recent, list):
        recent = []
    return {"libraries": libraries, "recent": recent, "last_sync": data.get("last_sync")}


def _save_state() -> None:
    payload = {
        "libraries": _state["libraries"],
        "recent": _state["recent"],
        "last_sync": _state["last_sync"],
    }
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _LIBRARIES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_LIBRARIES_FILE)
    except OSError:
        pass


def clear_cache() -> None:
    """Reset the cached library data (and last-sync)."""
    _state["libraries"] = []
    _state["recent"] = []
    _state["last_sync"] = None
    _save_state()


_state = _load_state()
_fetch = {"status": "idle", "error": None}


# ---- fetching ---------------------------------------------------------------


async def _build_libraries(client: JellyfinClient) -> list[dict]:
    libraries = []
    for lib in await client.get_libraries():
        item_id = lib.get("ItemId")
        count = None
        if item_id:
            try:
                count = await client.get_library_item_count(item_id)
            except JellyfinError:
                count = None
        libraries.append(
            {
                "id": item_id,
                "name": lib.get("Name", "?"),
                "type": lib.get("CollectionType") or "mixed",
                "locations": lib.get("Locations") or [],
                "items": count,
            }
        )
    return libraries


async def _build_recent(client: JellyfinClient, limit: int = 50) -> list[dict]:
    data = await client.get_recent(limit=limit)
    rows = []
    for it in data.get("Items") or []:
        name = it.get("Name") or "?"
        parent = it.get("SeriesName") or it.get("Album") or ""
        title = f"{parent} — {name}" if parent else name
        rows.append(
            {
                "name": title,
                "type": it.get("Type") or "",
                "year": it.get("ProductionYear"),
                "added": (it.get("DateCreated") or "")[:10],
            }
        )
    return rows


async def _run_fetch() -> None:
    _fetch.update({"status": "running", "error": None})
    try:
        client = _client()
        libraries = await _build_libraries(client)
        recent = await _build_recent(client)
        _state["libraries"] = libraries
        _state["recent"] = recent
        _state["last_sync"] = time.time()
        _save_state()
        _fetch["status"] = "done"
        log("Jellyfin", "INFO", f"Libraries refresh complete: {len(libraries)} library(ies), {len(recent)} recent")
    except JellyfinError as exc:
        _fetch["status"] = "error"
        _fetch["error"] = str(exc)
        log("Jellyfin", "ERROR", f"Libraries refresh failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        _fetch["status"] = "error"
        _fetch["error"] = f"Unexpected error: {exc}"
        log("Jellyfin", "ERROR", f"Libraries refresh failed: {exc}")


# ---- routes -----------------------------------------------------------------


@router.get("/libraries", response_class=HTMLResponse)
async def libraries_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "libraries.html", {"request": request})


@router.get("/api/libraries")
async def api_libraries(request: Request, user: str = Depends(require_login)):
    # First view with no cached data (or an empty recent list, e.g. a cache
    # written before that field existed): kick off a background fetch.
    if _fetch["status"] == "idle" and (not _state["last_sync"] or not _state["recent"]):
        from .scheduler import trigger_operation

        trigger_operation("refresh_libraries")
    return JSONResponse(
        {
            "ok": True,
            "libraries": _state["libraries"],
            "recent": _state["recent"],
            "last_sync": _state["last_sync"],
            "fetching": _fetch["status"] == "running",
            "fetch_error": _fetch.get("error"),
        }
    )


@router.post("/api/libraries/refresh")
async def api_libraries_refresh(request: Request, user: str = Depends(require_login)):
    from .scheduler import trigger_operation

    started, _ = trigger_operation("refresh_libraries")
    return JSONResponse(
        {"ok": True, "message": "Refresh started." if started else "Already refreshing."}
    )


@router.get("/api/libraries/status")
async def api_libraries_status(request: Request, user: str = Depends(require_login)):
    return JSONResponse(
        {
            "ok": True,
            "status": _fetch["status"],
            "error": _fetch["error"],
            "last_sync": _state["last_sync"],
        }
    )
