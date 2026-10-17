"""About page: build info, project links, and update availability.

The running version comes from the ``JFTOOLS_VERSION`` environment variable,
which the Docker build sets from the git ref (see ``Dockerfile`` and
``.github/workflows/docker-publish.yml``). The latest release is looked up from
the GitHub API and cached so page loads don't hammer it.
"""
from __future__ import annotations

import os
import re
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .web import require_login, templates

router = APIRouter()

GITHUB_REPO = "opheophe/jftools"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Cache the latest-release lookup (including failures) for this many seconds.
_latest_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 3600.0


def current_version() -> str:
    """The running build's version (``dev`` when not running a tagged image)."""
    return os.environ.get("JFTOOLS_VERSION", "dev").lstrip("vV")


def _is_semver(value: str) -> bool:
    return bool(re.fullmatch(r"v?\d+\.\d+\.\d+", (value or "").strip()))


def _parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in value.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _newer(latest: str, current: str) -> bool:
    """True when ``latest`` is a newer semver than ``current``.

    Non-semver values (``dev``, ``main``/``edge``) never compare as "newer", so
    local builds and edge images don't get a false update flag.
    """
    if not _is_semver(latest) or not _is_semver(current):
        return False
    return _parse_version(latest) > _parse_version(current)


async def _latest_release() -> dict | None:
    now = time.time()
    cached = _latest_cache["data"]
    if isinstance(cached, dict) and now - float(_latest_cache["ts"]) <= _CACHE_TTL:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                _RELEASES_API, headers={"Accept": "application/vnd.github+json"}
            )
            resp.raise_for_status()
            _latest_cache["data"] = resp.json()
    except Exception:
        _latest_cache["data"] = cached if isinstance(cached, dict) else None
    _latest_cache["ts"] = now
    data = _latest_cache["data"]
    return data if isinstance(data, dict) else None


async def _about_payload() -> dict:
    current = current_version()
    release = await _latest_release()
    latest = ""
    latest_url = GITHUB_URL + "/releases/latest"
    published = ""
    if release:
        latest = str(release.get("tag_name") or "").lstrip("vV")
        if release.get("html_url"):
            latest_url = str(release["html_url"])
        published = str(release.get("published_at") or "")[:10]
    return {
        "version": current,
        "latest": latest,
        "update_available": bool(latest) and _newer(latest, current),
        "github_url": GITHUB_URL,
        "latest_url": latest_url,
        "published": published,
    }


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "about.html", {"request": request})


@router.get("/api/about")
async def about_data(request: Request, user: str = Depends(require_login)):
    return JSONResponse(await _about_payload())
