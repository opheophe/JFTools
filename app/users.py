"""Users — list Jellyfin users and their watch activity.

Per-user "last watched" requires one Jellyfin request per user, so it is fetched
in the background and cached to ``data/users.json``. A background refresh is
kicked off on every page load. The cheap parts — the user list, last-login dates
and live sessions — are always fetched live so the page renders instantly.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError, get_client
from .logs import log
from .media import _image_ref, _item_label
from .web import require_login, templates

router = APIRouter()

COMPLETION_THRESHOLD = 90  # watched % at or above this counts as "Completed"

_USERS_FILE = config.data_dir / "users.json"


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
_in_flight = False


# ---- fetching ---------------------------------------------------------------


async def _build_last_watched(client: JellyfinClient) -> dict:
    """Fetch each user's most recently played item (one request per user).

    The per-user calls are independent, so they run concurrently (bounded) —
    a sequential loop took roughly one round-trip per user.
    """
    users = await client.get_users()
    uids = [u.get("Id") for u in users if u.get("Id")]
    sem = asyncio.Semaphore(8)

    async def one(uid: str) -> tuple[str, dict] | None:
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Filters": "IsPlayed",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Limit": "1",
            "Fields": "SeriesPrimaryImage",
        }
        try:
            async with sem:
                data = await client.get_user_items(uid, params)
        except JellyfinError:
            return None
        items = data.get("Items") or []
        if not items:
            return None
        it = items[0]
        ud = it.get("UserData") or {}
        return uid, {
            "name": _item_label(it),
            "date": ud.get("LastPlayedDate"),
            "id": it.get("Id"),
            "type": it.get("Type"),
            "image": _image_ref(it),
        }

    results = await asyncio.gather(*(one(uid) for uid in uids))
    out: dict[str, dict] = {}
    for r in results:
        if r:
            out[r[0]] = r[1]
    return out


async def _run_fetch() -> None:
    global _in_flight
    _in_flight = True
    _fetch.update({"status": "running", "error": None})
    try:
        last_watched = await _build_last_watched(get_client())
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
    finally:
        _in_flight = False


def _maybe_start_fetch() -> None:
    """Start a background last-watched refresh; no-op if one is already running."""
    if _in_flight:
        return
    asyncio.create_task(_run_fetch())


# ---- routes -----------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, user: str = Depends(require_login)):
    # Every page load refreshes the cached last-watched data in the background.
    _maybe_start_fetch()
    return templates.TemplateResponse(request, "users.html", {"request": request})


@router.get("/api/users")
async def api_users(request: Request, user: str = Depends(require_login)):
    # Cold start (no cache yet, e.g. deep link straight to the JSON).
    if not _state["last_sync"]:
        _maybe_start_fetch()

    client = get_client()
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
                    "pos_s": int(pos // 10_000_000),
                    "total_s": int(total // 10_000_000),
                    "paused": bool(ps.get("IsPaused")),
                    "image": _image_ref(np),
                    "item_id": np.get("Id"),
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

    result.sort(key=lambda r: r.get("last_login") or "", reverse=True)

    return JSONResponse(
        {
            "ok": True,
            "users": result,
            "last_sync": _state["last_sync"],
            "fetching": _fetch["status"] == "running",
            "fetch_error": _fetch.get("error"),
        }
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
    client = get_client()
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
                "id": it["Id"],
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


@router.get("/api/users/{user_id}/favorites")
async def api_user_favorites(user_id: str, request: Request, user: str = Depends(require_login)):
    # Cached during the Movie Match/Search fetch job (see jellyfin_id._run_fetch).
    from .jellyfin_id import favorites_fetched, favorites_for_user

    return JSONResponse({
        "ok": True,
        "items": favorites_for_user(user_id),
        "fetched": favorites_fetched(),
    })
