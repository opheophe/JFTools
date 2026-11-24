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

from fastapi import APIRouter, Depends, Form, Request
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


def _session_device(session: dict) -> str:
    """A short human label for the device behind a Jellyfin session."""
    return (session.get("DeviceName") or session.get("Client") or "").strip()


def _session_active(session: dict) -> bool:
    """Whether a real client is currently connected.

    ``IsActive`` alone is not a presence signal: Jellyfin reports it true for a
    session with no controller, and those never end. A bare login or an
    HTTP-only integration therefore lingers as "online" forever. A real client
    opens a websocket and advertises media control, and Jellyfin reports
    ``SupportsMediaControl`` only while that socket is open.
    """
    active = session.get("IsActive")
    if active is False:
        return False
    control = session.get("SupportsMediaControl")
    if control is None:
        # Very old servers omit the flag; fall back to IsActive.
        return True if active is None else bool(active)
    return bool(control)


def _pick_status(sessions: list[dict]) -> tuple[str, dict | None]:
    """Derive ``(status, session)`` for one user from their Jellyfin sessions.

    Precedence: actively playing beats paused, which beats merely connected,
    which beats a stale session Jellyfin still remembers. ``status`` is one of
    ``watching`` / ``paused`` / ``online`` / ``idle`` / ``offline``.
    """
    if not sessions:
        return "offline", None

    playing = [s for s in sessions if (s.get("NowPlayingItem") or {}).get("Name")]
    if playing:
        # Prefer a session that is actually playing over one paused on another
        # device, so a phone left on pause doesn't mask the TV.
        playing.sort(key=lambda s: bool((s.get("PlayState") or {}).get("IsPaused")))
        session = playing[0]
        paused = bool((session.get("PlayState") or {}).get("IsPaused"))
        return ("paused" if paused else "watching"), session

    active = [s for s in sessions if _session_active(s)]
    if active:
        return "online", active[0]

    newest = max(sessions, key=lambda s: s.get("LastActivityDate") or "")
    return "idle", newest


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

# Cached "is anyone watching right now" state, refreshed by the scheduler's
# ``users_watching`` job (every 30 s) and read by the Users nav dot.
_watching = {"status": "idle", "users": [], "at": None}


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
            "series": it.get("SeriesName") or "",
            "season": it.get("ParentIndexNumber"),
            "episode": it.get("IndexNumber"),
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


async def check_watching() -> None:
    """Refresh the cached "is anyone watching" state from Jellyfin sessions.

    One ``GET /Sessions`` is enough: each session carries its user, its
    now-playing item and its play state, so no per-user calls are needed. The
    resulting status is one of ``watching`` / ``paused`` / ``idle``.
    """
    if not config.jellyfin_configured:
        return
    sessions = await get_client().get_sessions()
    watching: list[dict] = []
    paused: list[dict] = []
    for s in sessions or []:
        np = s.get("NowPlayingItem") or {}
        if not np.get("Name"):
            continue
        play = s.get("PlayState") or {}
        rec = {
            "user": s.get("UserName") or s.get("UserId") or "?",
            "title": _item_label(np, episode=True),
        }
        (paused if play.get("IsPaused") else watching).append(rec)

    status = "watching" if watching else ("paused" if paused else "idle")
    old = _watching.get("status")
    _watching["status"] = status
    _watching["users"] = watching + paused
    _watching["at"] = time.time()

    # Log only on transitions so a 30 s heartbeat doesn't flood the logs.
    if status != old:
        if status == "watching":
            who = ", ".join(f"{r['user']} ({r['title']})" for r in watching)
            log("Jellyfin", "INFO", f"Watching: {who}")
        elif status == "paused":
            who = ", ".join(f"{r['user']} ({r['title']})" for r in paused)
            log("Jellyfin", "INFO", f"Watching paused: {who}")
        else:
            log("Jellyfin", "INFO", "Watching stopped")


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

    # A user can be connected from several devices at once (e.g. a TV playing
    # and a phone browsing), so keep every session rather than the last one.
    sessions_by_user: dict[str, list[dict]] = {}
    for s in sessions:
        uid = s.get("UserId")
        if uid:
            sessions_by_user.setdefault(uid, []).append(s)

    result = []
    for u in users:
        uid = u.get("Id")
        last_watched = _state["last_watched"].get(uid) or {}

        status, sess = _pick_status(sessions_by_user.get(uid) or [])

        active = None
        if sess:
            np = sess.get("NowPlayingItem") or {}
            if np.get("Name"):
                ps = sess.get("PlayState") or {}
                pos = ps.get("PositionTicks") or 0
                total = np.get("RunTimeTicks") or 0
                pct = round(pos / total * 100) if total else 0
                active = {
                    "name": _item_label(np, episode=True),
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
                "status": status,
                "device": _session_device(sess) if sess else "",
                "last_seen": (sess or {}).get("LastActivityDate"),
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


@router.get("/api/users/watching")
async def api_users_watching(request: Request, user: str = Depends(require_login)):
    from .scheduler import operation_active

    return JSONResponse(
        {
            "ok": True,
            "active": operation_active("users_watching"),
            "status": _watching.get("status"),
            "users": _watching.get("users"),
            "at": _watching.get("at"),
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


# ---- library access ---------------------------------------------------------


def _library_rows(folders: list[dict]) -> list[dict]:
    """The grantable libraries, keyed by the ItemId a policy stores.

    ``/Library/VirtualFolders`` is the same source ``libraries.py`` caches, but
    it is fetched live here: a folder added or removed since the last page
    refresh must not be offered with a stale id.
    """
    rows = []
    for folder in folders:
        item_id = folder.get("ItemId")
        if not item_id:
            continue
        rows.append(
            {
                "id": item_id,
                "name": folder.get("Name") or "?",
                "type": folder.get("CollectionType") or "mixed",
            }
        )
    return rows


def _policy_error(exc: JellyfinError) -> str:
    message = str(exc)
    if "HTTP 403" in message:
        return (
            "Jellyfin refused the change. Changing a user's library access "
            "needs an admin API key."
        )
    return message


@router.get("/api/users/{user_id}/libraries")
async def api_user_libraries(user_id: str, request: Request, user: str = Depends(require_login)):
    """Which libraries this user can see, plus every library to choose from."""
    client = get_client()
    try:
        record = await client.get_user(user_id)
        libraries = _library_rows(await client.get_libraries())
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    policy = record.get("Policy") or {}
    ids = [lib["id"] for lib in libraries]
    all_folders = bool(policy.get("EnableAllFolders"))
    if all_folders:
        enabled = ids
    else:
        # Drop ids of libraries that no longer exist so a stale policy does not
        # show up as granted.
        enabled = [i for i in (policy.get("EnabledFolders") or []) if i in ids]
    return JSONResponse(
        {
            "ok": True,
            "all": all_folders,
            "enabled": enabled,
            "libraries": libraries,
        }
    )


@router.post("/api/users/{user_id}/libraries")
async def api_user_libraries_save(
    user_id: str,
    request: Request,
    user: str = Depends(require_login),
    all_folders: bool = Form(False, alias="all"),
    ids: str = Form(""),
):
    """Grant or revoke a user's access to libraries.

    Only ``EnableAllFolders`` and ``EnabledFolders`` are changed. Jellyfin
    replaces the whole policy on write, so the rest is sent back exactly as it
    was read — parental controls, transcoding and device access stay intact.
    """
    client = get_client()
    try:
        record = await client.get_user(user_id)
        libraries = _library_rows(await client.get_libraries())
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    known = {lib["id"] for lib in libraries}
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    if any(i not in known for i in wanted):
        return JSONResponse(
            {"ok": False, "error": "That library is not on this Jellyfin server."},
            status_code=400,
        )

    policy = dict(record.get("Policy") or {})
    policy["EnableAllFolders"] = bool(all_folders)
    # Keep Jellyfin's own order, and leave out duplicates from the request.
    policy["EnabledFolders"] = [lib["id"] for lib in libraries if lib["id"] in wanted]
    try:
        await client.update_user_policy(user_id, policy)
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": _policy_error(exc)}, status_code=502)

    log(
        "Jellyfin",
        "INFO",
        f"Library access for {record.get('Name') or user_id}: "
        + ("all libraries" if all_folders else f"{len(policy['EnabledFolders'])} of {len(libraries)}"),
    )
    return JSONResponse(
        {
            "ok": True,
            "all": bool(all_folders),
            "enabled": policy["EnabledFolders"],
        }
    )
