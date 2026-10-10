"""MedServ web dashboard application."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import modules  # noqa: F401  (registers all modules)
from .auth import clear_failures, is_locked, record_failure, verify_credentials
from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError
from .modules.base import Context, get_action, get_modules

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="MedServ")

app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    https_only=config.cookie_secure,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def require_login(request: Request) -> str:
    user = request.session.get("user")
    if user:
        return user
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    raise HTTPException(status_code=307, headers={"Location": "/login"})


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---- pages ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request, "index.html", {"request": request, "modules": get_modules()}
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    ip = _client_ip(request)
    if is_locked(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again later."},
            status_code=429,
        )

    if verify_credentials(config, username, password):
        clear_failures(ip)
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)

    record_failure(ip)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": "Invalid username or password."},
        status_code=401,
    )


@app.post("/logout")
async def logout(request: Request, user: str = Depends(require_login)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "url": config.settings.jellyfin_url,
            "api_key_is_set": bool(config.settings.jellyfin_api_key),
            "username": config.resolved_username,
        },
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    user: str = Depends(require_login),
    jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""),
):
    api_key = jellyfin_api_key.strip() or config.settings.jellyfin_api_key
    config.save_settings(jellyfin_url, api_key)
    return JSONResponse({"ok": True, "message": "Jellyfin settings saved."})


@app.post("/settings/auth")
async def settings_auth_save(
    request: Request,
    user: str = Depends(require_login),
    username: str = Form(...),
    new_password: str = Form(""),
):
    username = username.strip()
    if not username:
        return JSONResponse({"ok": False, "error": "Username cannot be empty."}, status_code=400)
    config.save_auth(username, new_password)
    request.session["user"] = username
    return JSONResponse({"ok": True, "message": "Login settings saved."})


# ---- API --------------------------------------------------------------------


@app.post("/api/settings/test")
async def settings_test(
    request: Request,
    user: str = Depends(require_login),
    jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""),
):
    url = jellyfin_url.strip()
    api_key = jellyfin_api_key.strip() or config.settings.jellyfin_api_key
    client = JellyfinClient(url, api_key)
    try:
        info = await client.get_system_info()
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return JSONResponse(
        {
            "ok": True,
            "type": "kv",
            "title": "Connection OK",
            "items": [
                ["Server name", info.get("ServerName", "?")],
                ["Version", info.get("Version", "?")],
                ["Operating system", info.get("OperatingSystem", "?")],
            ],
        }
    )


@app.post("/api/actions/{module_id}/{action_id}")
async def run_action(
    module_id: str,
    action_id: str,
    request: Request,
    user: str = Depends(require_login),
):
    action = get_action(module_id, action_id)
    if action is None:
        return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=404)

    ctx = Context(settings=config.settings)
    try:
        result = dict(await action.handler(ctx))
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return JSONResponse({"ok": False, "error": f"Unexpected error: {exc}"}, status_code=500)

    result["ok"] = True
    return JSONResponse(result)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
