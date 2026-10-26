"""JFTools web dashboard application."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import backup
from .about import check_for_updates, router as about_router
from .audio import router as audio_router
from .auth import clear_failures, is_locked, is_local_ip, record_failure, verify_credentials
from .config import config
from .files import router as files_router
from .jellyfin.client import JellyfinClient, JellyfinError, close_shared_client, get_client
from .jellyfin_id import migrate_cached_series, router as jellyfin_id_router
from .libraries import router as libraries_router
from .logs import log, router as logs_router
from .media import router as media_router
from .paths import resolve_path
from .player import prune_play_cache, router as player_router
from .remotesync import run_import
from .scheduler import router as scheduler_router, set_timezone, shutdown_scheduler, start_scheduler
from .subtitles import router as subtitles_router
from .users import router as users_router
from .web import BASE_DIR, RevalidatedStaticFiles, require_login, templates


@asynccontextmanager
async def lifespan(app):
    removed = migrate_cached_series()
    if removed:
        log("Movie Match", "INFO", f"Removed {removed} legacy series row(s)")
    start_scheduler()
    asyncio.create_task(check_for_updates())
    prune_play_cache()
    if config.settings.remote_sync_enabled and config.settings.remote_sync_on_startup:
        asyncio.create_task(_startup_remote_sync())
    yield
    shutdown_scheduler()
    await close_shared_client()
    from .opensubtitles import close_shared_client as close_os_client

    await close_os_client()


async def _startup_remote_sync() -> None:
    """Run the configured remote import once the server is fully up.

    Waits a few seconds so uvicorn has bound the port and the app is serving
    before any request goes out; the import itself is backgrounded and its
    result only logged, so a slow or unreachable remote never blocks startup.
    """
    await asyncio.sleep(5)
    result = await run_import(
        config.settings.remote_sync_url, config.settings.remote_sync_api_key
    )
    if result.get("ok"):
        log(
            "Remote sync",
            "INFO",
            f"Startup import: {result['rows']} media item(s), "
            f"{result['scanned']} scan entrie(s)",
        )
    else:
        log("Remote sync", "WARN", f"Startup import failed: {result.get('error')}")


app = FastAPI(title="JFTools", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    https_only=config.cookie_secure,
    same_site="lax",
)

app.mount("/static", RevalidatedStaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount(
    "/resources",
    RevalidatedStaticFiles(directory=str(BASE_DIR.parent / "resources")),
    name="resources",
)


@app.middleware("http")
async def _pages_are_not_cached(request: Request, call_next):
    """Never let a browser keep a rendered page.

    A cached page served next to a stale script (or the reverse) is how the
    Settings form ends up posting natively instead of through fetch, which
    reloads the page and saves nothing.
    """
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def _wait_for_restart(request: Request, call_next):
    """Refuse settings writes after a restore has replaced config.json.

    The running process still has the old settings in memory; letting a save
    through would write them back over the restored file.
    """
    if (
        backup.RESTORED
        and request.method == "POST"
        and (
            request.url.path.startswith("/settings")
            or request.url.path == "/api/profiles"
        )
    ):
        return JSONResponse(
            {"ok": False, "error": "Restart JFTools to finish applying the restored backup."},
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    return await call_next(request)

app.include_router(about_router)
app.include_router(audio_router)
app.include_router(files_router)
app.include_router(jellyfin_id_router)
app.include_router(libraries_router)
app.include_router(logs_router)
app.include_router(media_router)
app.include_router(player_router)
app.include_router(scheduler_router)
app.include_router(subtitles_router)
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
            "path_maps": config.settings.path_maps,
            "file_mgmt": config.settings.file_mgmt,
            "os_api_key_is_set": bool(config.settings.opensubtitles_api_key),
            "os_username": config.settings.opensubtitles_username,
            "preferred_languages": config.settings.opensubtitles_languages,
            "backup_dir": config.settings.backup_dir,
            "backup_keep": config.settings.backup_keep,
            "backup_include_logs": config.settings.backup_include_logs,
            "play_cache_keep_days": config.settings.play_cache_keep_days,
            "play_cache_max_gb": config.settings.play_cache_max_gb,
            "api_keys": _api_keys_for_page(),
            "remote_sync_url": config.settings.remote_sync_url,
            "remote_sync_api_key_set": bool(config.settings.remote_sync_api_key),
            "remote_sync_enabled": config.settings.remote_sync_enabled,
            "remote_sync_on_startup": config.settings.remote_sync_on_startup,
        },
    )


def _api_keys_for_page() -> list[dict]:
    """The API keys as the Settings page renders them (created as text)."""
    from datetime import datetime

    out = []
    for entry in config.settings.api_keys:
        created = entry.get("created") or 0
        try:
            stamp = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            stamp = ""
        out.append({
            "name": entry["name"],
            "key": entry["key"],
            "short": f"{entry['key'][:10]}…{entry['key'][-6:]}",
            "created": stamp,
        })
    return out


def _saved(request: Request, message: str) -> Response:
    """Reply to a settings save.

    The Settings page's scripts POST with fetch and render the JSON reply. A
    plain form submit — which happens when the browser is running a stale or
    missing app.js — is redirected back to the page instead of being dropped
    into a raw JSON view, so the values still save.
    """
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/settings", status_code=303)
    return JSONResponse({"ok": True, "message": message})


async def api_key_or_login(request: Request) -> str:
    """A session login or one of the Settings API keys.

    The media export is read-only, but it contains the whole library, so it
    must not be public: a logged-in user or a bearer of a named API key may
    read it. Returns the username or the key's name.
    """
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        key = (request.query_params.get("api_key") or "").strip()
    entry = config.valid_api_key(key)
    if entry:
        return entry["name"]
    return await require_login(request)


@app.get("/api/sync/export")
async def sync_export(request: Request, user: str = Depends(api_key_or_login)):
    """The Movie Match rows and the subtitle/audio scan store, as one JSON
    document another JFTools instance can import with Remote sync."""
    from . import jellyfin_id, subtitles
    from .version import current_version

    id_state = jellyfin_id._state
    sub_state = subtitles._state
    return JSONResponse({
        "ok": True,
        "app": "jftools",
        "version": current_version(),
        "media": {
            "rows": id_state["rows"],
            "marks": sorted(id_state["marks"]),
            "favorites": id_state["favorites"],
            "last_sync": id_state["last_sync"],
        },
        "scans": {
            "files": sub_state["files"],
            "ok": sub_state["ok"],
            "full_scan_at": sub_state.get("full_scan_at"),
            "change_scan_at": sub_state.get("change_scan_at"),
        },
    })


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
    return _saved(request, "Jellyfin settings saved.")


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
    return _saved(request, "Login settings saved.")


@app.post("/settings/integrations")
async def settings_integrations_save(
    request: Request,
    user: str = Depends(require_login),
    radarr_url: str = Form(""),
    sonarr_url: str = Form(""),
):
    config.save_integrations(radarr_url, sonarr_url)
    log("Jellyfin", "INFO", "Integration links saved")
    return _saved(request, "Integrations saved.")


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
    return _saved(request, "Time zone saved.")


@app.post("/settings/paths")
async def settings_paths_save(request: Request, user: str = Depends(require_login)):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON."}, status_code=400)
    if not isinstance(data, dict) or "maps" not in data:
        return JSONResponse({"ok": False, "error": "Missing 'maps'."}, status_code=400)
    config.save_path_maps(data["maps"])
    log("Jellyfin", "INFO", "Media path mappings saved")
    return JSONResponse({"ok": True, "message": "Media paths saved."})


@app.post("/settings/files")
async def settings_files_save(
    request: Request,
    user: str = Depends(require_login),
    enabled: str = Form(""),
):
    config.save_file_mgmt(enabled.strip().lower() in ("1", "true", "yes", "on"))
    state = "enabled" if config.settings.file_mgmt else "disabled"
    log("Jellyfin", "INFO", f"File management {state}")
    return _saved(request, f"File management {state}.")


@app.post("/settings/subtitles")
async def settings_subtitles_save(
    request: Request,
    user: str = Depends(require_login),
    preferred_languages: str = Form(""),
):
    from .subtitles import normalize_language_codes

    languages = normalize_language_codes(preferred_languages) or "en"
    config.save_subtitle_languages(languages)
    log("Jellyfin", "INFO", f"Preferred subtitle languages set to {languages}")
    return _saved(request, "Subtitle preferences saved.")


@app.post("/settings/opensubtitles")
async def settings_opensubtitles_save(
    request: Request,
    user: str = Depends(require_login),
    opensubtitles_api_key: str = Form(""),
    opensubtitles_username: str = Form(""),
    opensubtitles_password: str = Form(""),
):
    # Blank API key/password fields keep the stored value (they are never
    # echoed back to the page), so a user can save a login without retyping it.
    # Languages now live under Settings -> Subtitle preferences.
    api_key = opensubtitles_api_key.strip() or config.settings.opensubtitles_api_key
    password = opensubtitles_password or config.settings.opensubtitles_password
    config.save_opensubtitles(
        api_key,
        opensubtitles_username,
        password,
        config.settings.opensubtitles_languages,
    )
    from .opensubtitles import invalidate

    invalidate()
    log("Jellyfin", "INFO", "OpenSubtitles settings saved")
    return _saved(request, "OpenSubtitles settings saved.")


@app.post("/api/paths/test")
async def paths_test(request: Request, user: str = Depends(require_login)):
    try:
        data = await request.json()
    except Exception:
        data = {}
    path = str((data or {}).get("path") or "").strip()
    to = str((data or {}).get("to") or "").strip()
    if not path and not to:
        return JSONResponse({"ok": False, "error": "No path supplied."}, status_code=400)

    # If the row already has a local path, test that directly (it's what the
    # user is trying to reach). Otherwise resolve the Jellyfin path through the
    # saved mappings.
    target = to if to else resolve_path(path, config.settings.path_maps)
    result = {"ok": True, "path": path or to, "resolved": target}
    try:
        os.stat(target)
        result["exists"] = True
        result["is_dir"] = os.path.isdir(target)
        result["readable"] = os.access(target, os.R_OK)
        result["writable"] = os.access(target, os.W_OK)
    except OSError:
        result["exists"] = False
        result["is_dir"] = False
        result["readable"] = False
        result["writable"] = False
    return JSONResponse(result)


@app.post("/settings/backup")
async def settings_backup_save(
    request: Request,
    user: str = Depends(require_login),
    backup_dir: str = Form(""),
    backup_keep: str = Form("7"),
    backup_include_logs: str = Form(""),
):
    raw = backup_dir.strip()
    if raw:
        folder = Path(raw).expanduser()
        if not folder.is_absolute():
            return JSONResponse(
                {"ok": False, "error": "Backup folder must be an absolute path, e.g. /backups."},
                status_code=400,
            )
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".jftools-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "error": f"Cannot write to {folder}: {exc}"},
                status_code=400,
            )
        raw = str(folder)
    try:
        keep = int(backup_keep)
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "Keep last must be a whole number between 0 and 100."},
            status_code=400,
        )
    if keep < 0 or keep > 100:
        return JSONResponse(
            {"ok": False, "error": "Keep last must be between 0 and 100."},
            status_code=400,
        )
    include_logs = backup_include_logs.strip().lower() in ("1", "true", "yes", "on")
    config.save_backup_settings(raw, keep, include_logs)
    log("Backup", "INFO", f"Backup settings saved (keep={keep}, logs={include_logs})")
    return _saved(request, "Backup settings saved.")


@app.post("/settings/play-cache")
async def settings_play_cache_save(
    request: Request,
    user: str = Depends(require_login),
    keep_days: str = Form("7"),
    max_gb: str = Form("5"),
):
    try:
        days = int(keep_days)
        gb = int(max_gb)
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "Retention must be whole numbers."}, status_code=400
        )
    if not 1 <= days <= 365 or not 1 <= gb <= 1024:
        return JSONResponse(
            {"ok": False, "error": "Days must be 1–365 and the size cap 1–1024 GB."},
            status_code=400,
        )
    config.save_play_cache(days, gb)
    log(
        "Settings",
        "INFO",
        f"Player cache retention saved "
        f"(keep {config.settings.play_cache_keep_days} days, "
        f"max {config.settings.play_cache_max_gb} GB)",
    )
    return _saved(request, "Player cache settings saved.")


@app.post("/settings/api-keys")
async def settings_api_keys_create(
    request: Request,
    user: str = Depends(require_login),
    name: str = Form(""),
):
    name = name.strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "Give the key a name so you know what it is for."},
            status_code=400,
        )
    if len(name) > 64:
        return JSONResponse(
            {"ok": False, "error": "Key names are at most 64 characters."},
            status_code=400,
        )
    key = config.create_api_key(name)
    log("Settings", "INFO", f"API key created ({name})")
    return JSONResponse({"ok": True, "key": key, "message": "API key created."})


@app.post("/settings/api-keys/revoke")
async def settings_api_keys_revoke(
    request: Request,
    user: str = Depends(require_login),
    key: str = Form(""),
):
    if config.revoke_api_key((key or "").strip()):
        log("Settings", "INFO", "API key revoked")
        return _saved(request, "API key revoked.")
    return JSONResponse(
        {"ok": False, "error": "That API key does not exist."}, status_code=400
    )


@app.post("/settings/remote-sync")
async def settings_remote_sync_save(
    request: Request,
    user: str = Depends(require_login),
    url: str = Form(""),
    api_key: str = Form(""),
    enabled: str = Form(""),
    on_startup: str = Form(""),
):
    key = api_key.strip() or config.settings.remote_sync_api_key
    config.save_remote_sync(
        url,
        key,
        enabled.strip().lower() in ("1", "true", "yes", "on"),
        on_startup.strip().lower() in ("1", "true", "yes", "on"),
    )
    log("Settings", "INFO", "Remote sync settings saved")
    return _saved(request, "Remote sync settings saved.")


@app.post("/api/remotesync/run")
async def remotesync_run(request: Request, user: str = Depends(require_login)):
    """Pull the configured remote export into this server's stores, now."""
    result = await run_import(
        config.settings.remote_sync_url, config.settings.remote_sync_api_key
    )
    if result.get("ok"):
        return JSONResponse({
            "ok": True,
            "message": (
                f"Imported {result['rows']} media item(s) and "
                f"{result['scanned']} scan entrie(s)."
            ),
            **result,
        })
    return JSONResponse(
        {"ok": False, "error": result.get("error") or "Import failed."},
        status_code=502,
    )


@app.get("/settings/backup/download")
async def settings_backup_download(user: str = Depends(require_login)):
    try:
        data = await asyncio.to_thread(backup.build_zip)
    except backup.BackupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    name = time.strftime("jftools-backup-%Y%m%d-%H%M%S.zip")
    log("Backup", "INFO", f"Backup downloaded ({len(data)} bytes)")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/backup/list")
async def api_backup_list(user: str = Depends(require_login)):
    return JSONResponse(
        {
            "ok": True,
            "backups": backup.list_backups(),
            "keep": config.settings.backup_keep,
            "dir": str(backup.backup_dir()),
        }
    )


@app.post("/api/backup/now")
async def api_backup_now(user: str = Depends(require_login)):
    try:
        info = await asyncio.to_thread(backup.create_backup)
    except backup.BackupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "message": f"Backup created: {info['name']}",
            "backup": info,
            "backups": backup.list_backups(),
            "keep": config.settings.backup_keep,
        }
    )


@app.get("/api/backup/file")
async def api_backup_file(name: str, user: str = Depends(require_login)):
    try:
        path = backup.path_for(name)
    except backup.BackupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "Backup not found."}, status_code=404)
    return FileResponse(path, media_type="application/zip", filename=name)


@app.post("/api/backup/delete")
async def api_backup_delete(
    user: str = Depends(require_login),
    name: str = Form(""),
):
    try:
        path = backup.path_for(name)
    except backup.BackupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "Backup not found."}, status_code=404)
    try:
        path.unlink()
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"Could not delete the backup: {exc}"},
            status_code=400,
        )
    log("Backup", "INFO", f"Backup deleted: {name}")
    return JSONResponse(
        {
            "ok": True,
            "message": f"Deleted {name}.",
            "backups": backup.list_backups(),
            "keep": config.settings.backup_keep,
        }
    )


@app.post("/api/backup/restore")
async def api_backup_restore(
    user: str = Depends(require_login),
    file: UploadFile = File(...),
):
    raw = await file.read(backup.MAX_RESTORE_BYTES + 1)
    if len(raw) > backup.MAX_RESTORE_BYTES:
        return JSONResponse(
            {"ok": False, "error": "The archive is too large to restore."},
            status_code=413,
        )
    try:
        info = await asyncio.to_thread(backup.restore_archive, raw)
    except backup.BackupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "message": (
                f"Restored {info['restored']} file(s). "
                "Restart JFTools to apply the backup."
            ),
            "restart_required": True,
        }
    )


@app.get("/settings/export")
async def settings_export(request: Request, user: str = Depends(require_login)):
    payload = config.export_settings()
    body = json.dumps(payload, indent=2)
    log("Jellyfin", "INFO", "Settings exported")
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="jftools-settings.json"'},
    )


@app.get("/api/profiles")
async def api_profiles(user: str = Depends(require_login)):
    return JSONResponse({"ok": True, "profiles": config.settings.filter_profiles})


@app.post("/api/profiles")
async def api_profiles_save(
    request: Request,
    user: str = Depends(require_login),
    page: str = Form(""),
    profiles: str = Form(""),
):
    """Replace the saved filter profiles for one page (Search / Movie Match)."""
    page = page.strip()
    if not page:
        return JSONResponse({"ok": False, "error": "No page given."}, status_code=400)
    try:
        data = json.loads(profiles)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "Profiles are not valid JSON."}, status_code=400
        )
    if not isinstance(data, dict):
        return JSONResponse(
            {"ok": False, "error": "Profiles must be a JSON object."}, status_code=400
        )
    config.save_filter_profiles(page, data)
    return JSONResponse({"ok": True, "profiles": config.settings.filter_profiles})


@app.post("/settings/import")
async def settings_import(
    request: Request,
    user: str = Depends(require_login),
    file: UploadFile = File(...),
):
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "File is not valid JSON."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse(
            {"ok": False, "error": "Settings file must be a JSON object."}, status_code=400
        )

    new_tz = str(data.get("timezone") or "").strip()
    if new_tz:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(new_tz)
        except Exception:
            return JSONResponse({"ok": False, "error": "Invalid time zone in file."}, status_code=400)

    config.import_settings(data)
    if new_tz:
        set_timezone(config.settings.timezone)
    request.session["user"] = config.resolved_username
    log("Jellyfin", "INFO", "Settings imported")
    return JSONResponse({"ok": True, "message": "Settings imported."})


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


@app.post("/api/settings/opensubtitles/test")
async def settings_opensubtitles_test(
    request: Request,
    user: str = Depends(require_login),
    opensubtitles_api_key: str = Form(""),
    opensubtitles_username: str = Form(""),
    opensubtitles_password: str = Form(""),
):
    from .opensubtitles import OpenSubtitlesClient, OpenSubtitlesError

    api_key = opensubtitles_api_key.strip() or config.settings.opensubtitles_api_key
    username = opensubtitles_username.strip() or config.settings.opensubtitles_username
    password = opensubtitles_password or config.settings.opensubtitles_password
    client = OpenSubtitlesClient(api_key, username, password)
    try:
        user_info = await client.test()
    except OpenSubtitlesError as exc:
        log("Jellyfin", "ERROR", f"OpenSubtitles test: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    items = []
    if user_info:
        allowed = user_info.get("allowed_downloads")
        used = user_info.get("downloads_count")
        items.append(["Account", username or "?"])
        items.append(["Level", user_info.get("level") or (user_info.get("vip") and "VIP") or "?"])
        items.append(["Downloads used today", f"{used} / {allowed}" if isinstance(used, int) and isinstance(allowed, int) else "?"])
    else:
        items.append(["API key", "accepted"])
        items.append(
            ["Account", "anonymous — add a username/password for a higher daily quota"]
        )
    log("Jellyfin", "INFO", "OpenSubtitles test OK")
    return JSONResponse(
        {"ok": True, "type": "kv", "title": "OpenSubtitles OK", "items": items}
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
