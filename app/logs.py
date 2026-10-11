"""Per-tab file logging with rolling retention and a Logs page.

Each feature tab has its own log stream written to ``data/logs/YYYY-MM-DD
<tab>.log``. The newest ``MAX_FILES_PER_TAB`` files per tab are kept; older ones
are pruned automatically.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import config
from .web import require_login, templates

LOG_DIR = config.data_dir / "logs"
MAX_FILES_PER_TAB = 10

# Feature tabs that have their own log stream.
TABS = ("Jellyfin", "Movie Match")

router = APIRouter()

_SAFE_NAME = re.compile(r"[^A-Za-z0-9 _-]")


def _safe_tab(tab: str) -> str:
    return _SAFE_NAME.sub("", str(tab)).strip()


def _log_path(tab: str, date_str: str) -> Path:
    return LOG_DIR / f"{date_str} {_safe_tab(tab)}.log"


def _local_now() -> datetime.datetime:
    try:
        return datetime.datetime.now(ZoneInfo(config.settings.timezone))
    except Exception:
        return datetime.datetime.now()


def log(tab: str, level: str, message: str) -> None:
    """Append one line to today's log file for the tab, then prune old files."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = _local_now()
        date_str = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts}  {str(level).upper():<7}  {message}\n"
        with _log_path(tab, date_str).open("a", encoding="utf-8") as fh:
            fh.write(line)
        _prune(tab)
    except OSError:
        pass


def _prune(tab: str) -> None:
    """Keep only the MAX_FILES_PER_TAB newest files for a tab."""
    suffix = f" {_safe_tab(tab)}"
    files = sorted(
        [p for p in LOG_DIR.glob("*.log") if p.stem.endswith(suffix)],
        key=lambda p: p.name,
        reverse=True,
    )
    for old in files[MAX_FILES_PER_TAB:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_logs() -> list[dict]:
    """Return log files grouped by tab, newest first."""
    groups: dict[str, list[dict]] = {}
    try:
        for p in LOG_DIR.glob("*.log"):
            m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(.+?)\.log", p.name)
            if not m:
                continue
            date_str, tab = m.group(1), m.group(2)
            groups.setdefault(tab, []).append(
                {"name": p.name, "date": date_str, "tab": tab, "size": p.stat().st_size}
            )
    except OSError:
        pass
    result = []
    for tab in sorted(groups):
        result.append(
            {"tab": tab, "files": sorted(groups[tab], key=lambda f: f["name"], reverse=True)}
        )
    return result


def read_log(name: str) -> str | None:
    """Return a log file's contents, or None if the name is invalid/missing."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [A-Za-z0-9 _-]+\.log", name):
        return None
    path = LOG_DIR / name
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def clear_logs() -> int:
    """Delete all log files; return how many were removed."""
    removed = 0
    try:
        for p in LOG_DIR.glob("*.log"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "logs.html", {"request": request})


@router.get("/api/logs")
async def api_logs(request: Request, user: str = Depends(require_login)):
    return JSONResponse({"ok": True, "groups": list_logs()})


@router.get("/api/logs/{name}")
async def api_log_content(name: str, request: Request, user: str = Depends(require_login)):
    content = read_log(name)
    if content is None:
        return JSONResponse({"ok": False, "error": "Log file not found."}, status_code=404)
    return JSONResponse({"ok": True, "name": name, "content": content})


@router.get("/api/logs/{name}/download")
async def api_log_download(name: str, request: Request, user: str = Depends(require_login)):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [A-Za-z0-9 _-]+\.log", name):
        return JSONResponse({"ok": False, "error": "Invalid file name."}, status_code=400)
    path = LOG_DIR / name
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "Log file not found."}, status_code=404)
    return FileResponse(path, filename=name, media_type="text/plain")
