"""Durable in-app task notifications.

Long-running actions (track edits, subtitle sync/delete, scans, scheduled jobs)
publish a small event here when they finish. The browser polls
``/api/notifications`` and shows new events as top-right toasts; the bell keeps
the recent history even after a page reload.

Events are deliberately explicit task results rather than parsed log lines: a
log line has no stable severity/category and parsing every warning would make
the notification centre noisy. The store is bounded and persisted to
``data/notifications.json`` so a result is not lost when the page that started
the task goes away.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .config import config
from .web import require_login

router = APIRouter()

MAX_EVENTS = 200
MAX_AGE_SECONDS = 14 * 24 * 60 * 60
MAX_MESSAGE_CHARS = 4000
KINDS = {"success", "error", "warning", "info"}

_STORE_FILE = config.data_dir / "notifications.json"
_lock = threading.RLock()
_events: list[dict[str, Any]] = []
_next_id = 1
_loaded = False


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _visible(event: dict, user: str | None) -> bool:
    owner = event.get("user")
    return owner is None or owner == user


def _prune_locked() -> None:
    cutoff = time.time() - MAX_AGE_SECONDS
    _events[:] = [e for e in _events if float(e.get("ts") or 0) >= cutoff]
    if len(_events) > MAX_EVENTS:
        del _events[: len(_events) - MAX_EVENTS]


def _load_locked() -> None:
    global _loaded, _events, _next_id
    if _loaded:
        return
    try:
        data = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    raw = data.get("events") if isinstance(data, dict) else None
    events: list[dict[str, Any]] = []
    max_id = 0
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            event_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else "info"
        ts = float(item.get("ts") or 0)
        if not ts:
            continue
        event = {
            "id": event_id,
            "ts": ts,
            "kind": kind,
            "title": _clean_text(item.get("title"), 200),
            "message": _clean_text(item.get("message"), MAX_MESSAGE_CHARS),
            "source": _clean_text(item.get("source"), 80),
            "user": item.get("user") if isinstance(item.get("user"), str) else None,
            "url": _clean_text(item.get("url"), 500),
            "link_label": _clean_text(item.get("link_label"), 80),
        }
        events.append(event)
        max_id = max(max_id, event_id)
    _events = events
    try:
        stored_next = int(data.get("next_id") or 0)
    except (TypeError, ValueError):
        stored_next = 0
    _next_id = max(max_id + 1, stored_next, 1)
    _prune_locked()
    _loaded = True


def _save_locked() -> None:
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"next_id": _next_id, "events": _events}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_STORE_FILE)
    except OSError:
        # Notifications must never make the task that produced them fail.
        pass


def publish(
    kind: str,
    title: str,
    message: str = "",
    *,
    source: str = "",
    user: str | None = None,
    url: str = "",
    link_label: str = "",
) -> int | None:
    """Record one notification and return its id (or ``None`` on failure)."""
    global _next_id
    try:
        with _lock:
            _load_locked()
            event = {
                "id": _next_id,
                "ts": time.time(),
                "kind": kind if kind in KINDS else "info",
                "title": _clean_text(title, 200),
                "message": _clean_text(message, MAX_MESSAGE_CHARS),
                "source": _clean_text(source, 80),
                "user": user if isinstance(user, str) and user.strip() else None,
                "url": _clean_text(url, 500),
                "link_label": _clean_text(link_label, 80),
            }
            _next_id += 1
            _events.append(event)
            _prune_locked()
            _save_locked()
            return int(event["id"])
    except Exception:  # noqa: BLE001 — never break the operation being reported
        return None


def query(
    after: int | None = None,
    limit: int = 50,
    user: str | None = None,
) -> tuple[list[dict], int, bool]:
    """Return ``(events, latest_id, more)`` for one browser.

    ``after`` returns events newer than that id in ascending order. Without it,
    the newest ``limit`` events are returned (also ascending). ``latest_id`` is
    the global sequence head so a client can skip events it cannot see without
    re-reading them forever.
    """
    with _lock:
        _load_locked()
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 50
        try:
            after_id = int(after) if after is not None else None
        except (TypeError, ValueError):
            after_id = None

        visible = [e for e in _events if _visible(e, user)]
        if after_id is not None:
            visible = [e for e in visible if int(e["id"]) > after_id]
            more = len(visible) > limit
            items = visible[:limit]
        else:
            more = len(visible) > limit
            items = visible[-limit:]
        latest = max((int(e["id"]) for e in _events), default=0)
        return [dict(e) for e in items], latest, more


def latest_id() -> int:
    with _lock:
        _load_locked()
        return max((int(e["id"]) for e in _events), default=0)


def _reset_for_tests() -> None:
    """Clear the in-memory store; the next call reloads ``_STORE_FILE``."""
    global _loaded, _events, _next_id
    with _lock:
        _loaded = False
        _events = []
        _next_id = 1


@router.get("/api/notifications")
async def api_notifications(
    after: int | None = None,
    limit: int = 50,
    user: str = Depends(require_login),
):
    items, latest, more = query(after=after, limit=limit, user=user)
    return JSONResponse(
        {"ok": True, "items": items, "latest": latest, "more": more},
        headers={"Cache-Control": "no-store"},
    )
