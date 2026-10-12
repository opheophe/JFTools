"""Users — list Jellyfin users and their watch activity.

Per-user "last watched" requires one Jellyfin request per user, so it is fetched
in the background, cached to ``data/users.json`` and refreshed on a schedule
(see the Scheduler page). The cheap parts — the user list, last-login dates and
live sessions — are always fetched live so the page renders instantly.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError
from .logs import log
from .web import require_login, templates

router = APIRouter()

COMPLETION_THRESHOLD = 90  # watched % at or above this counts as "Completed"

_USERS_FILE = config.data_dir / "users.json"


def _client() -> JellyfinClient:
    return JellyfinClient(config.settings.jellyfin_url, config.settings.jellyfin_api_key)


def _completion(ud: dict) -> tuple[str, bool]:
    """Return (status_label, completed_bool) for a UserData dict.

    A movie watched to >= COMPLETION_THRESHOLD% is treated as completed even if
    Jellyfin hasn't marked it played (handles e.g. 98% watched).
    """
    played = bool(ud.get("Played"))
    pct = ud.get("PlayedPercentage")
    pct = round(pct) if pct is not None else (100 if played else 0)
    if played or pct >= COMPLETION_THRESHOLD:
        return "Completed", True
    if pct > 0:
        return f"In progress ({pct}%)", False
    return "Unwatched", False


def _item_label(it: dict) -> str:
    name = it.get("Name") or "?"
    if it.get("Type") == "Episode" and it.get("SeriesName"):
        return f"{it['SeriesName']} — {name}"
    return name


# ---- persistence ------------------------------------------------------------


def _load_state() -> dict:
    try:
        data = json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    last_watched = data.get("last_watched")
    if not isinstance(last_watched, dict):
        last_watched = {}
    return {"last_watched": last_watched, "last_sync": data.get("last_sync")}


def _save_state() -> None:
    payload = {
        "last_watched": _state["last_watched"],
        "last_sync": _state["last_sync"],
    }
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _USERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_USERS_FILE)
    except OSError:
        pass


def clear_cache() -> None:
    """Reset the cached per-user watch data (and last-sync)."""
    _state["last_watched"] = {}
    _state["last_sync"] = None
    _save_state()


_state = _load_state()
_fetch = {"status": "idle", "error": None}


# ---- fetching ---------------------------------------------------------------


async def _build_last_watched(client: JellyfinClient) -> dict:
    """Fetch each user's most recently played item (one request per user)."""
    users = await client.get_users()
    result: dict[str, dict] = {}
    for u in users:
        uid = u.get("Id")
        if not uid:
            continue
        try:
            data = await client.get_user_items(
                uid,
                {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Filters": "IsPlayed",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "Limit": "1",
                },
            )
            items = data.get("Items") or []
            if items:
                it = items[0]
                ud = it.get("UserData") or {}
                result[uid] = {
                    "name": _item_label(it),
                    "date": ud.get("LastPlayedDate"),
                }
        except JellyfinError:
            continue
    return result


async def _run_fetch() -> None:
    _fetch.update({"status": "running", "error": None})
    try:
        last_watched = await _build_last_watched(_client())
        _state["last_watched"] = last_watched
        _state["last_sync"] = time.time()
        _save_state()
        _fetch["status"] = "done"
        log("Jellyfin", "INFO", f"Users refresh complete: {len(last_watched)} user(s)")
    except JellyfinError as exc:
        _fetch["status"] = "error"
        _fetch["error"] = str(exc)
        log("Jellyfin", "ERROR", f"Users refresh failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        _fetch["status"] = "error"
        _fetch["error"] = f"Unexpected error: {exc}"
        log("Jellyfin", "ERROR", f"Users refresh failed: {exc}")


def trigger_refresh() -> bool:
    """Start a background refresh unless one is already running."""
    if _fetch["status"] == "running":
        return False
    asyncio.create_task(_run_fetch())
    return True


# ---- routes -----------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "users.html", {"request": request})


@router.get("/api/users")
async def api_users(request: Request, user: str = Depends(require_login)):
    # First view with no cached data: kick off a background fetch.
    if not _state["last_sync"] and _fetch["status"] != "running":
        trigger_refresh()

    client = _client()
    try:
        users = await client.get_users()
        sessions = await client.get_sessions()
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    session_by_user = {}
    for s in sessions:
        uid = s.get("UserId")
        if uid:
            session_by_user[uid] = s

    result = []
    for u in users:
        uid = u.get("Id")
        last_watched = _state["last_watched"].get(uid) or {}

        active = None
        sess = session_by_user.get(uid)
        if sess:
            np = sess.get("NowPlayingItem") or {}
            if np.get("Name"):
                ps = sess.get("PlayState") or {}
                pos = ps.get("PositionTicks") or 0
                total = np.get("RunTimeTicks") or 0
                pct = round(pos / total * 100) if total else 0
                active = {
                    "name": _item_label(np),
                    "type": np.get("Type"),
                    "pct": pct,
                    "paused": bool(ps.get("IsPaused")),
                }

        result.append(
            {
                "id": uid,
                "name": u.get("Name") or "?",
                "last_login": u.get("LastLoginDate") or u.get("LastActivityDate"),
                "last_watched": last_watched,
                "active": active,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "users": result,
            "last_sync": _state["last_sync"],
            "fetching": _fetch["status"] == "running",
            "fetch_error": _fetch.get("error"),
        }
    )


@router.post("/api/users/refresh")
async def api_users_refresh(request: Request, user: str = Depends(require_login)):
    started = trigger_refresh()
    return JSONResponse(
        {"ok": True, "message": "Refresh started." if started else "Already refreshing."}
    )


@router.get("/api/users/status")
async def api_users_status(request: Request, user: str = Depends(require_login)):
    return JSONResponse(
        {
            "ok": True,
            "status": _fetch["status"],
            "error": _fetch["error"],
            "last_sync": _state["last_sync"],
        }
    )


@router.get("/api/users/{user_id}/history")
async def api_user_history(user_id: str, request: Request, user: str = Depends(require_login)):
    client = _client()
    base = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Episode",
        "SortBy": "DatePlayed",
        "SortOrder": "Descending",
        "Fields": "UserData,ProductionYear,SeriesName",
        "Limit": "500",
    }
    try:
        played = await client.get_user_items(user_id, {**base, "Filters": "IsPlayed"})
        resumable = await client.get_user_items(user_id, {**base, "Filters": "IsResumable"})
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    items = {}
    for batch in (played.get("Items") or [], resumable.get("Items") or []):
        for it in batch:
            if it.get("Id"):
                items[it["Id"]] = it

    rows = []
    for it in items.values():
        ud = it.get("UserData") or {}
        status, completed = _completion(ud)
        rows.append(
            {
                "name": _item_label(it),
                "type": it.get("Type") or "",
                "year": it.get("ProductionYear"),
                "date": ud.get("LastPlayedDate"),
                "status": status,
                "completed": completed,
            }
        )

    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return JSONResponse({"ok": True, "items": rows})
