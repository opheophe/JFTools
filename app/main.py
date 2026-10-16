"""MedServ web dashboard application."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import clear_failures, is_locked, is_local_ip, record_failure, verify_credentials
from .config import config
from .jellyfin.client import JellyfinClient, JellyfinError, close_shared_client, get_client
from .jellyfin_id import router as jellyfin_id_router
from .libraries import router as libraries_router
from .logs import log, router as logs_router
from .media import router as media_router
from .scheduler import router as scheduler_router, set_timezone, shutdown_scheduler, start_scheduler
from .users import router as users_router
from .web import BASE_DIR, require_login, templates


@asynccontextmanager
async def lifespan(app):
    start_scheduler()
    yield
    shutdown_scheduler()
    await close_shared_client()


app = FastAPI(title="MedServ", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    https_only=config.cookie_secure,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/resources", StaticFiles(directory=str(BASE_DIR.parent / "resources")), name="resources")

app.include_router(jellyfin_id_router)
app.include_router(libraries_router)
app.include_router(logs_router)
app.include_router(media_router)
app.include_router(scheduler_router)
app.include_router(users_router)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _jellyfin_unconfigured() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "Jellyfin is not configured. Add the URL and API key on the Settings page.",
        },
        status_code=400,
    )


_SCAN_WINDOW_SECONDS = 300
_recent_scans: list[dict] = []


def _record_scan(name: str) -> None:
    now = time.time()
    _recent_scans[:] = [s for s in _recent_scans if now - s["started_at"] < _SCAN_WINDOW_SECONDS]
    _recent_scans.append({"name": name, "started_at": now})


# ---- pages ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "jellyfin_configured": config.jellyfin_configured,
        },
    )


def _setup_error(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"request": request, "error": message},
        status_code=400,
    )


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if not config.needs_setup:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"request": request, "error": None})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if not config.needs_setup:
        return RedirectResponse("/", status_code=303)

    username = username.strip()
    if not username:
        return _setup_error(request, "Username cannot be empty.")
    if len(password) < 8:
        return _setup_error(request, "Password must be at least 8 characters.")
    if password != confirm_password:
        return _setup_error(request, "Passwords do not match.")

    config.save_auth(username, password)
    request.session["user"] = username
    log("Jellyfin", "INFO", f"Initial login created: {username}")
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if config.needs_setup:
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if config.needs_setup:
        return RedirectResponse("/setup", status_code=303)
    ip = _client_ip(request)
    if is_locked(ip):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again later."},
            status_code=429,
        )

    # 1. Local password — only accepted from the local network (localhost/LAN).
    if is_local_ip(ip) and verify_credentials(config, username, password):
        clear_failures(ip)
        request.session["user"] = username
        log("Jellyfin", "INFO", f"Login: {username} (local password)")
        return RedirectResponse("/", status_code=303)

    # 2. Jellyfin admin account — accepted from anywhere (once configured).
    jf_user = {}
    if config.jellyfin_configured:
        try:
            client = get_client()
            jf_user = await client.authenticate_user(username, password)
        except JellyfinError:
            jf_user = {}
    if jf_user.get("Policy", {}).get("IsAdministrator"):
        clear_failures(ip)
        request.session["user"] = username
        log("Jellyfin", "INFO", f"Login: {username} (Jellyfin admin)")
        return RedirectResponse("/", status_code=303)

    record_failure(ip)
    log("Jellyfin", "WARN", f"Failed login: {username}")
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
            "radarr_url": config.settings.radarr_url,
            "sonarr_url": config.settings.sonarr_url,
            "timezone": config.settings.timezone,
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
    log("Jellyfin", "INFO", "Jellyfin settings saved")
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
    log("Jellyfin", "INFO", "Local login settings saved")
    return JSONResponse({"ok": True, "message": "Login settings saved."})


@app.post("/settings/integrations")
async def settings_integrations_save(
    request: Request,
    user: str = Depends(require_login),
    radarr_url: str = Form(""),
    sonarr_url: str = Form(""),
):
    config.save_integrations(radarr_url, sonarr_url)
    log("Jellyfin", "INFO", "Integration links saved")
    return JSONResponse({"ok": True, "message": "Integrations saved."})


@app.post("/settings/timezone")
async def settings_timezone_save(
    request: Request,
    user: str = Depends(require_login),
    timezone: str = Form(...),
):
    from zoneinfo import ZoneInfo

    tz = timezone.strip()
    try:
        ZoneInfo(tz)
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid time zone."}, status_code=400)
    config.save_timezone(tz)
    set_timezone(tz)
    log("Jellyfin", "INFO", f"Time zone set to {tz}")
    return JSONResponse({"ok": True, "message": "Time zone saved."})


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
        log("Jellyfin", "ERROR", f"Test connection: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    log("Jellyfin", "INFO", "Test connection OK")
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


@app.post("/api/jellyfin/scan/{library_id}")
async def jellyfin_scan_library(
    library_id: str,
    request: Request,
    user: str = Depends(require_login),
):
    if not config.jellyfin_configured:
        return _jellyfin_unconfigured()
    client = get_client()
    try:
        libs = await client.get_libraries()
        lib = next((l for l in libs if l.get("ItemId") == library_id), None)
        if lib is None:
            return JSONResponse({"ok": False, "error": "Library not found."}, status_code=404)
        locations = lib.get("Locations") or []
        if not locations:
            return JSONResponse({"ok": False, "error": "Library has no folder paths."}, status_code=400)
        await client.scan_paths(locations)
    except JellyfinError as exc:
        log("Jellyfin", "ERROR", f"Scan {lib.get('Name', '?')}: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    _record_scan(lib.get("Name", "library"))
    log("Jellyfin", "INFO", f"Scan started: {lib.get('Name', 'library')}")
    return JSONResponse({"ok": True, "message": f"Scan started for {lib.get('Name', 'library')}."})


@app.post("/api/jellyfin/scan-all")
async def jellyfin_scan_all(request: Request, user: str = Depends(require_login)):
    if not config.jellyfin_configured:
        return _jellyfin_unconfigured()
    client = get_client()
    try:
        await client.scan_all()
    except JellyfinError as exc:
        log("Jellyfin", "ERROR", f"Scan all: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    _record_scan("all libraries")
    log("Jellyfin", "INFO", "Scan all started")
    return JSONResponse({"ok": True, "message": "Full library scan started."})


@app.get("/api/jellyfin/tasks")
async def jellyfin_tasks(request: Request, user: str = Depends(require_login)):
    if not config.jellyfin_configured:
        return _jellyfin_unconfigured()
    client = get_client()
    try:
        tasks = await client.get_scheduled_tasks()
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    running = []
    for t in tasks:
        if t.get("State") == "Running":
            running.append(
                {
                    "name": t.get("Name") or t.get("Key") or "?",
                    "key": t.get("Key", ""),
                    "progress": t.get("CurrentProgressPercentage"),
                }
            )
    now = time.time()
    recent_scans = [
        {"name": s["name"], "age": int(now - s["started_at"])}
        for s in _recent_scans
        if now - s["started_at"] < _SCAN_WINDOW_SECONDS
    ]
    return JSONResponse({"ok": True, "running": running, "recent_scans": recent_scans})


@app.get("/api/jellyfin/activity")
async def jellyfin_activity(request: Request, user: str = Depends(require_login)):
    if not config.jellyfin_configured:
        return _jellyfin_unconfigured()
    client = get_client()
    try:
        entries = await client.get_activity_log(limit=50)
    except JellyfinError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    items = []
    for e in entries:
        raw_date = e.get("Date") or ""
        items.append(
            {
                "name": e.get("Name", ""),
                "short_overview": e.get("ShortOverview", ""),
                "date": raw_date[:19].replace("T", " "),
                "severity": e.get("Severity", ""),
            }
        )
    return JSONResponse({"ok": True, "entries": items})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
