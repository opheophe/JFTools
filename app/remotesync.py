"""Remote sync — mirror another JFTools server's media and scan data.

A test/dev instance can pull the Movie Match rows and the subtitle/audio scan
store from a production server (which exposes them on ``/api/sync/export``,
authenticated by an API key created on its Settings page). The local stores are
replaced wholesale, so the mirror matches the source exactly; nothing is merged.
"""
from __future__ import annotations

import httpx

from . import jellyfin_id, subtitles
from .logs import log


async def run_import(url: str, api_key: str) -> dict:
    """Fetch a remote export and apply it. Returns a summary dict.

    ``{"ok": True, "rows": n, "scanned": m}`` on success, otherwise
    ``{"ok": False, "error": ...}``. Any transport or payload problem is
    reported as an error — a bad sync must never clear the local stores.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return {"ok": False, "error": "No remote URL configured."}
    if not api_key:
        return {"ok": False, "error": "No API key configured."}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(f"{url}/api/sync/export", headers={"X-API-Key": api_key})
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Could not reach {url}: {exc}"}
    if resp.status_code == 401:
        return {"ok": False, "error": "The remote server refused the API key."}
    if resp.status_code != 200:
        return {"ok": False, "error": f"The remote server answered {resp.status_code}."}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "The remote server did not answer with JSON."}
    return apply_export(data)


def apply_export(data: dict) -> dict:
    """Replace the local Movie Match and scan stores with a remote export."""
    if not isinstance(data, dict):
        return {"ok": False, "error": "The export is not an object."}
    media = data.get("media") if isinstance(data.get("media"), dict) else {}
    scans = data.get("scans") if isinstance(data.get("scans"), dict) else {}
    rows = media.get("rows") if isinstance(media.get("rows"), list) else None
    files = scans.get("files") if isinstance(scans.get("files"), dict) else None
    if rows is None or files is None:
        return {"ok": False, "error": "The export is missing media rows or scan data."}

    id_state = jellyfin_id._state
    id_state["rows"] = [r for r in rows if isinstance(r, dict)]
    marks = media.get("marks")
    id_state["marks"] = {m for m in (marks or []) if isinstance(m, str)}
    favorites = media.get("favorites")
    id_state["favorites"] = favorites if isinstance(favorites, dict) else {}
    id_state["last_sync"] = media.get("last_sync")
    jellyfin_id._save_state()

    sub_state = subtitles._state
    sub_state["files"] = {
        k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, dict)
    }
    ok = scans.get("ok")
    sub_state["ok"] = [p for p in (ok or []) if isinstance(p, str)]
    sub_state["full_scan_at"] = scans.get("full_scan_at")
    sub_state["change_scan_at"] = scans.get("change_scan_at")
    subtitles._save_state()

    log(
        "Remote sync",
        "INFO",
        f"Imported {len(id_state['rows'])} media item(s) and "
        f"{len(sub_state['files'])} scan entrie(s)",
    )
    return {
        "ok": True,
        "rows": len(id_state["rows"]),
        "scanned": len(sub_state["files"]),
    }
