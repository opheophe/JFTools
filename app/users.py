"""Users — list Jellyfin users and their watch activity."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError
from .web import require_login, templates

router = APIRouter()

COMPLETION_THRESHOLD = 90  # watched % at or above this counts as "Completed"


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


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "users.html", {"request": request})


@router.get("/api/users")
async def api_users(request: Request, user: str = Depends(require_login)):
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

        last_watched = {}
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
                last_watched = {
                    "name": _item_label(it),
                    "date": ud.get("LastPlayedDate"),
                }
        except JellyfinError:
            pass

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

    return JSONResponse({"ok": True, "users": result})


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
