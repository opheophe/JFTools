"""Shared web helpers: base directory, templates, and the auth dependency."""
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from .config import config

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
