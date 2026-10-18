"""Shared web helpers: base directory, templates, and the auth dependency."""
import os
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import config

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def asset_token() -> str:
    """Cache-buster appended to /static URLs as ``?v=``.

    Browsers otherwise happily reuse a cached app.js after an upgrade, and a
    stale script means every form on the Settings page silently falls back to a
    native submit (which just reloads the page and saves nothing). The token
    combines the build version with the newest static-file timestamp, so it
    changes whenever either the release or any asset changes.
    """
    try:
        newest = max(p.stat().st_mtime for p in STATIC_DIR.rglob("*") if p.is_file())
    except (OSError, ValueError):
        newest = 0.0
    return f"{os.environ.get('JFTOOLS_VERSION', 'dev')}-{int(newest)}"


class RevalidatedStaticFiles(StaticFiles):
    """Static assets a browser must revalidate before reusing.

    Templates append the ``?v=`` token, so this only covers stale URLs someone
    still holds; the ETag keeps revalidation down to a cheap 304.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("cache-control", "no-cache")
        return response


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["asset"] = asset_token()


async def require_login(request: Request) -> str:
    if config.needs_setup:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Setup required")
        raise HTTPException(status_code=307, headers={"Location": "/setup"})
    user = request.session.get("user")
    if user:
        return user
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    raise HTTPException(status_code=307, headers={"Location": "/login"})
