"""Shared media helpers and routes for Jellyfin items.

Hover-popup metadata, image proxying and single-file rescan are used by every
table (Users, Libraries, Movie Match), so they live here rather than under the
Users routes.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, Response

from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError, get_client
from .logs import log
from .web import require_login

router = APIRouter()

_USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_IMAGE_KINDS = ("Primary", "Backdrop", "Thumb", "Logo")

_USER_ITEM_FIELDS = ",".join(
    [
        "Name",
        "Type",
        "SeriesName",
        "SeriesPrimaryImageTag",
        "ParentIndexNumber",
        "IndexNumber",
        "ProductionYear",
        "PremiereDate",
        "Overview",
        "Genres",
        "CommunityRating",
        "CriticRating",
        "OfficialRating",
        "RunTimeTicks",
        "Studios",
        "People",
        "ImageTags",
        "BackdropImageTags",
        "ThumbImageTags",
        "ParentBackdropItemId",
        "ParentBackdropImageTags",
        "ParentThumbItemId",
        "ParentThumbImageTags",
        "Path",
    ]
)

_any_user_id: str | None = None


async def _any_user(client: JellyfinClient) -> str:
    """A cached user id, needed because Jellyfin's /Items endpoint requires one
    when authenticating with an API key. Prefer an administrator: item lookups
    are filtered by the user's library access, so a limited user 404s."""
    global _any_user_id
    if not _any_user_id:
        users = await client.get_users()
        admin = next(
            (u.get("Id") for u in users if u.get("Id") and (u.get("Policy") or {}).get("IsAdministrator")),
            "",
        )
        _any_user_id = admin or next((u.get("Id") for u in users if u.get("Id")), "")
    return _any_user_id or ""


def _item_label(it: dict) -> str:
    name = it.get("Name") or "?"
    if it.get("Type") == "Episode" and it.get("SeriesName"):
        return f"{it['SeriesName']} — {name}"
    return name


def _image_ref(np: dict) -> dict | None:
    """Pick the best primary image for a now-playing item.

    Episodes have no poster of their own, so use the series poster; otherwise
    use the item's own primary image.
    """
    if np.get("Type") == "Episode" and np.get("SeriesId") and np.get("SeriesPrimaryImageTag"):
        return {"item_id": np["SeriesId"], "tag": np["SeriesPrimaryImageTag"]}
    tags = np.get("ImageTags") or {}
    if np.get("Id") and tags.get("Primary"):
        return {"item_id": np["Id"], "tag": tags["Primary"]}
    if np.get("SeriesId") and np.get("SeriesPrimaryImageTag"):
        return {"item_id": np["SeriesId"], "tag": np.get("SeriesPrimaryImageTag")}
    return None


def _img(item_id: str | None, tags) -> dict | None:
    """Build an image ref from an item id plus Jellyfin's image-tag list."""
    if item_id and tags:
        return {"item_id": item_id, "tag": tags[0]}
    return None


def _image_refs(it: dict) -> dict:
    """All useful image refs for an item: primary, wide backdrop, episode still."""
    return {
        "primary": _image_ref(it),
        "backdrop": _img(it.get("Id"), it.get("BackdropImageTags"))
        or _img(it.get("ParentBackdropItemId"), it.get("ParentBackdropImageTags")),
        "thumb": _img(it.get("Id"), it.get("ThumbImageTags"))
        or _img(it.get("ParentThumbItemId"), it.get("ParentThumbImageTags")),
    }


def _item_details(it: dict) -> dict:
    """Shape a Jellyfin BaseItemDto into the fields the hover popup shows."""
    people = it.get("People") or []
    return {
        "name": it.get("Name") or "?",
        "type": it.get("Type") or "",
        "series": it.get("SeriesName"),
        "season": it.get("ParentIndexNumber"),
        "episode": it.get("IndexNumber"),
        "year": it.get("ProductionYear"),
        "premiere": it.get("PremiereDate"),
        "overview": (it.get("Overview") or "").strip(),
        "genres": [g for g in (it.get("Genres") or []) if g][:6],
        "community_rating": it.get("CommunityRating"),
        "critic_rating": it.get("CriticRating"),
        "official_rating": it.get("OfficialRating"),
        "runtime_s": int((it.get("RunTimeTicks") or 0) // 10_000_000),
        "studios": [s.get("Name") for s in (it.get("Studios") or []) if s.get("Name")][:3],
        "directors": [p.get("Name") for p in people if p.get("Type") == "Director" and p.get("Name")][:4],
        "cast": [p.get("Name") for p in people if p.get("Type") == "Actor" and p.get("Name")][:6],
        "images": _image_refs(it),
        "path": it.get("Path") or "",
    }


# ---- routes -----------------------------------------------------------------


@router.get("/api/media/item/{item_id}")
async def api_item(
    item_id: str,
    request: Request,
    user_id: str = "",
    user: str = Depends(require_login),
):
    """Full metadata for one item — powers the hover popup on any page."""
    if not _USER_ID_RE.fullmatch(item_id):
        return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)
    client = get_client()
    try:
        uid = user_id if user_id and _USER_ID_RE.fullmatch(user_id) else await _any_user(client)
        if not uid:
            return JSONResponse({"ok": False, "error": "No Jellyfin users."}, status_code=502)
        it = await client.get_item_any(item_id, _USER_ITEM_FIELDS, uid)
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse(
        {
            "ok": True,
            "item": _item_details(it),
            "jellyfin_url": config.settings.jellyfin_url,
            "radarr_url": config.settings.radarr_url,
            "sonarr_url": config.settings.sonarr_url,
        }
    )


@router.get("/api/media/image/{item_id}")
async def api_media_image(
    item_id: str,
    tag: str = "",
    kind: str = "Primary",
    w: int = 240,
    user: str = Depends(require_login),
):
    if not _USER_ID_RE.fullmatch(item_id):
        return Response(status_code=404)
    if kind not in _IMAGE_KINDS:
        kind = "Primary"
    try:
        content, ctype = await get_client().get_image(item_id, tag, kind, max(32, min(w, 800)))
    except JellyfinError:
        return Response(status_code=404)
    return Response(
        content=content,
        media_type=ctype,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/api/media/rescan")
async def api_media_rescan(path: str = Form(""), user: str = Depends(require_login)):
    """Rescan a single library file path (e.g. after adding a subtitle)."""
    path = path.strip()
    if not path:
        return JSONResponse({"ok": False, "error": "No file path."}, status_code=400)
    try:
        await get_client().scan_paths([path])
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    log("Jellyfin", "INFO", f"Rescan started: {path}")
    return JSONResponse({"ok": True, "message": "Rescan started."})
