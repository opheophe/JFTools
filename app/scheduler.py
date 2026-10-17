"""Scheduler — run operations (fetch, scan, ...) on a schedule.

Backed by APScheduler's AsyncIOScheduler. Job definitions persist to
``data/schedules.json`` and are re-registered on startup. Scheduled runs log to
the operation's own tab log.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import config
from .logs import log
from .web import require_login, templates

router = APIRouter()

# ---- operations -------------------------------------------------------------


async def _op_fetch_jellyfin_id() -> None:
    from .jellyfin_id import _run_fetch

    await _run_fetch()


async def _op_scan_all() -> None:
    from .jellyfin.client import get_client

    client = get_client()
    await client.scan_all()


async def _op_clear_logs() -> None:
    from .logs import clear_logs

    clear_logs()


async def _op_clear_cache() -> None:
    from .jellyfin_id import clear_cache

    clear_cache()


async def _op_refresh_libraries() -> None:
    from .libraries import _run_fetch as _libraries_run_fetch

    await _libraries_run_fetch()


async def _op_check_updates() -> None:
    from .about import check_for_updates

    await check_for_updates()


async def _op_subtitle_scan_full() -> None:
    from .subtitles import run_scan

    await run_scan("full")


async def _op_subtitle_scan() -> None:
    from .subtitles import run_scan

    await run_scan("scan")


async def _op_users_watching() -> None:
    from .users import check_watching

    await check_watching()


def _subtitle_scan_progress() -> dict | None:
    """Live progress (done/total) for the running subtitle scans."""
    from .subtitles import _fetch

    if _fetch.get("status") != "running":
        return None
    return {"done": _fetch.get("scanned", 0), "total": _fetch.get("total", 0)}


def _jellyfin_id_progress() -> dict | None:
    """Live progress (done/total) for the Movie Match fetch."""
    from .jellyfin_id import _fetch

    if _fetch.get("status") != "running":
        return None
    return {"done": _fetch.get("fetched", 0), "total": _fetch.get("total", 0)}


OPERATIONS = [
    {"id": "jellyfin_id_fetch", "label": "Fetch Movie Match", "tab": "Movie Match", "handler": _op_fetch_jellyfin_id,
     "description": "Fetch all media items, re-score their name/year match, and record each user's favorites.",
     "progress": _jellyfin_id_progress},
    {"id": "jellyfin_scan_all", "label": "Scan all libraries", "tab": "Jellyfin", "handler": _op_scan_all,
     "description": "Start a full library scan in Jellyfin."},
    {"id": "clear_logs", "label": "Clear logs", "tab": "Jellyfin", "handler": _op_clear_logs,
     "description": "Delete all stored logs."},
    {"id": "clear_cache", "label": "Clear local cache", "tab": "Movie Match", "handler": _op_clear_cache,
     "description": "Reset Movie Match — removes saved Ok marks, fetched rows and last-sync time."},
    {"id": "refresh_libraries", "label": "Refresh libraries", "tab": "Jellyfin", "handler": _op_refresh_libraries,
     "description": "Refresh the cached library list from Jellyfin."},
    {"id": "check_updates", "label": "Check for updates", "tab": "System", "handler": _op_check_updates,
     "description": "Query GitHub for the latest JFTools release."},
    {"id": "subtitle_scan_full", "label": "Subtitle scan (full)", "tab": "Search", "handler": _op_subtitle_scan_full,
     "description": "Probe every media file for subtitle tracks with ffprobe and refresh the whole subtitle store.",
     "progress": _subtitle_scan_progress},
    {"id": "subtitle_scan", "label": "Subtitle scan", "tab": "Search", "handler": _op_subtitle_scan,
     "description": "Sync the store with the library, then probe every file not yet scanned.",
     "progress": _subtitle_scan_progress},
    {"id": "users_watching", "label": "User watching", "tab": "Jellyfin", "handler": _op_users_watching,
     "description": "Check Jellyfin sessions for an actively-watching user (powers the Users nav dot).",
     "quiet": True},
]
_OP_BY_ID = {op["id"]: op for op in OPERATIONS}
OPERATIONS_PUBLIC = [
    {"id": op["id"], "label": op["label"], "description": op.get("description", "")}
    for op in OPERATIONS
]

# Default schedule + enabled flag used when seeding a first job for an
# operation. Everything seeds disabled so a fresh install shows every operation
# without running it; the cache refreshers are the exception.
_DEFAULT_SCHEDULES = {
    "jellyfin_id_fetch": {"kind": "interval", "minutes": 720},
    "jellyfin_scan_all": {"kind": "interval", "minutes": 360},
    "clear_logs": {"kind": "cron", "hour": 0, "minute": 0},
    "clear_cache": {"kind": "interval", "minutes": 720},
    "refresh_libraries": {"kind": "interval", "minutes": 30},
    "check_updates": {"kind": "interval", "minutes": 60},
    "subtitle_scan_full": {"kind": "cron", "hour": 3, "minute": 0},
    "subtitle_scan": {"kind": "interval", "minutes": 720},
    "users_watching": {"kind": "interval", "seconds": 30},
}
_ACTIVE_BY_DEFAULT = {"refresh_libraries", "check_updates", "users_watching"}

# ---- persistence ------------------------------------------------------------

_JOBS_FILE = config.data_dir / "schedules.json"
_jobs: dict[str, dict] = {}

# Job ids currently executing (in-memory only; resets on restart).
_running: set[str] = set()

# Live asyncio tasks for running jobs, so they can be aborted.
_tasks: dict[str, asyncio.Task] = {}


def _load_jobs() -> None:
    try:
        data = json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    _jobs.clear()
    for j in (data.get("jobs") or []):
        if isinstance(j, dict) and j.get("id") and j.get("operation") in _OP_BY_ID:
            _jobs[j["id"]] = j
    _ensure_default_jobs()


def _ensure_default_jobs() -> None:
    """Seed one job per operation so a fresh install shows every option.

    Jobs are seeded disabled (visible but not running); the cache refreshers
    seed enabled instead. Existing jobs for an operation are never overwritten.
    Disabling persists, but a deleted default job is recreated on the next
    start.
    """
    for operation in _OP_BY_ID:
        if any(j.get("operation") == operation for j in _jobs.values()):
            continue
        _jobs[operation] = {
            "id": operation,
            "operation": operation,
            "trigger": dict(_DEFAULT_SCHEDULES.get(operation, {"kind": "interval", "minutes": 60})),
            "enabled": operation in _ACTIVE_BY_DEFAULT,
            "last_run": None,
            "last_status": None,
            "last_error": None,
        }
    _save_jobs()


def _save_jobs() -> None:
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = _JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jobs": list(_jobs.values())}, indent=2), encoding="utf-8")
        tmp.replace(_JOBS_FILE)
    except OSError:
        pass


_load_jobs()

# ---- scheduler --------------------------------------------------------------


def _scheduler_tz():
    try:
        return ZoneInfo(config.settings.timezone)
    except Exception:
        return None


scheduler = AsyncIOScheduler(timezone=_scheduler_tz())


def _make_trigger(spec: dict):
    kind = spec.get("kind")
    if kind == "interval":
        if spec.get("seconds"):
            return IntervalTrigger(seconds=max(1, int(spec["seconds"])))
        return IntervalTrigger(minutes=max(1, int(spec.get("minutes", 60))))
    if kind == "cron":
        kwargs = {}
        if spec.get("day_of_week"):
            kwargs["day_of_week"] = spec["day_of_week"]
        if spec.get("hour") is not None and spec.get("hour") != "":
            kwargs["hour"] = int(spec["hour"])
        if spec.get("minute") is not None and spec.get("minute") != "":
            kwargs["minute"] = int(spec["minute"])
        if not kwargs:
            kwargs = {"hour": 0, "minute": 0}
        return CronTrigger(**kwargs)
    if kind == "crontab":
        return CronTrigger.from_crontab(str(spec.get("expr") or "0 0 * * *"))
    return IntervalTrigger(minutes=60)


def _describe_schedule(spec: dict) -> str:
    kind = spec.get("kind")
    if kind == "interval":
        if spec.get("seconds"):
            return f"Every {max(1, int(spec['seconds']))} seconds"
        m = max(1, int(spec.get("minutes", 60)))
        if m < 60:
            return f"Every {m} minutes"
        if m % 60 == 0:
            h = m // 60
            return f"Every {h} hour" + ("" if h == 1 else "s")
        return f"Every {m} minutes"
    if kind == "cron":
        hh = int(spec.get("hour", 0) or 0)
        mm = int(spec.get("minute", 0) or 0)
        t = f"{hh:02d}:{mm:02d}"
        dow = spec.get("day_of_week")
        if dow:
            return f"Weekly on {str(dow).title()} at {t}"
        return f"Every day at {t}"
    if kind == "crontab":
        return f"Cron: {spec.get('expr')}"
    return "Unknown"


async def _run_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    op = _OP_BY_ID.get(job.get("operation")) if job else None
    if not job or not op:
        return
    if job_id in _running:
        return
    _running.add(job_id)
    job["last_run"] = time.time()
    quiet = bool(op.get("quiet"))
    try:
        await op["handler"]()
        job["last_status"] = "ok"
        job["last_error"] = None
        if not quiet:
            log(op["tab"], "INFO", f"Scheduled run: {op['label']} OK")
    except asyncio.CancelledError:
        job["last_status"] = "cancelled"
        job["last_error"] = "Aborted by user"
        if not quiet:
            log(op["tab"], "WARN", f"Scheduled run: {op['label']} aborted")
    except Exception as exc:  # noqa: BLE001
        job["last_status"] = "error"
        job["last_error"] = str(exc)
        if not quiet:
            log(op["tab"], "ERROR", f"Scheduled run: {op['label']} failed: {exc}")
    finally:
        _running.discard(job_id)
        _save_jobs()


def _spawn(job_id: str) -> None:
    """Start a job run in the background and track its task so it can be aborted."""
    task = asyncio.create_task(_run_job(job_id))
    _tasks[job_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(job_id, None))


def _remove_job(job_id: str) -> None:
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def _register_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    _remove_job(job_id)
    if not job.get("enabled", True):
        return
    trigger = _make_trigger(job.get("trigger", {}))

    async def runner() -> None:
        await _run_job(job_id)

    scheduler.add_job(runner, trigger=trigger, id=job_id, replace_existing=True, max_instances=1)


def start_scheduler() -> None:
    if scheduler.running:
        return
    for job_id in list(_jobs):
        _register_job(job_id)
    scheduler.start()


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def set_timezone(tz: str) -> None:
    """Apply a new time zone by swapping in a fresh scheduler (APScheduler can't
    reconfigure its timezone while running)."""
    global scheduler
    try:
        z = ZoneInfo(tz)
    except Exception:
        return
    was_running = scheduler.running
    if was_running:
        scheduler.shutdown(wait=False)
    scheduler = AsyncIOScheduler(timezone=z)
    if was_running:
        for job_id in list(_jobs):
            _register_job(job_id)
        scheduler.start()


def _next_run_label(job_id: str) -> str | None:
    """Next scheduled run time formatted in the scheduler's time zone."""
    aps_job = scheduler.get_job(job_id)
    if aps_job is None or aps_job.next_run_time is None:
        return None
    return aps_job.next_run_time.strftime("%Y-%m-%d %H:%M")


def _public_job(job_id: str) -> dict:
    job = _jobs[job_id]
    op = _OP_BY_ID.get(job.get("operation"), {})
    progress = None
    if job["id"] in _running:
        getter = op.get("progress")
        if getter:
            try:
                progress = getter()
            except Exception:  # noqa: BLE001
                progress = None
    return {
        "id": job["id"],
        "operation": job.get("operation"),
        "operation_label": op.get("label", job.get("operation", "")),
        "operation_description": op.get("description", ""),
        "trigger": job.get("trigger", {}),
        "schedule": _describe_schedule(job.get("trigger", {})),
        "enabled": bool(job.get("enabled", True)),
        "running": job["id"] in _running,
        "progress": progress,
        "next_run": _next_run_label(job_id),
        "last_run": job.get("last_run"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
    }


# ---- routes -----------------------------------------------------------------


@router.get("/scheduler", response_class=HTMLResponse)
async def scheduler_page(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(request, "scheduler.html", {"request": request})


@router.get("/api/scheduler/jobs")
async def list_jobs(request: Request, user: str = Depends(require_login)):
    jobs = sorted((_public_job(j) for j in _jobs), key=lambda j: j["operation_label"])
    return JSONResponse({"ok": True, "jobs": jobs, "operations": OPERATIONS_PUBLIC})


@router.post("/api/scheduler/jobs")
async def create_job(request: Request, user: str = Depends(require_login)):
    data = await request.json()
    operation = data.get("operation")
    trigger = data.get("trigger") or {}
    if operation not in _OP_BY_ID:
        return JSONResponse({"ok": False, "error": "Unknown operation."}, status_code=400)
    if not trigger.get("kind"):
        return JSONResponse({"ok": False, "error": "Missing schedule."}, status_code=400)
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "id": job_id,
        "operation": operation,
        "trigger": trigger,
        "enabled": bool(data.get("enabled", True)),
        "last_run": None,
        "last_status": None,
        "last_error": None,
    }
    _save_jobs()
    _register_job(job_id)
    return JSONResponse({"ok": True, "job": _public_job(job_id)})


@router.put("/api/scheduler/jobs/{job_id}")
async def update_job(job_id: str, request: Request, user: str = Depends(require_login)):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "Job not found."}, status_code=404)
    data = await request.json()
    if data.get("operation") in _OP_BY_ID:
        job["operation"] = data["operation"]
    if data.get("trigger"):
        job["trigger"] = data["trigger"]
    if "enabled" in data:
        job["enabled"] = bool(data["enabled"])
    _save_jobs()
    _register_job(job_id)
    return JSONResponse({"ok": True, "job": _public_job(job_id)})


@router.delete("/api/scheduler/jobs/{job_id}")
async def delete_job(job_id: str, request: Request, user: str = Depends(require_login)):
    if job_id not in _jobs:
        return JSONResponse({"ok": False, "error": "Job not found."}, status_code=404)
    _remove_job(job_id)
    _jobs.pop(job_id, None)
    _running.discard(job_id)
    _save_jobs()
    return JSONResponse({"ok": True})


@router.post("/api/scheduler/jobs/{job_id}/run")
async def run_job(job_id: str, request: Request, user: str = Depends(require_login)):
    if job_id not in _jobs:
        return JSONResponse({"ok": False, "error": "Job not found."}, status_code=404)
    if job_id in _running:
        return JSONResponse({"ok": True, "message": "Already running."})
    _spawn(job_id)
    return JSONResponse({"ok": True, "message": "Run started."})


@router.post("/api/scheduler/jobs/{job_id}/abort")
async def abort_job(job_id: str, request: Request, user: str = Depends(require_login)):
    if job_id not in _jobs:
        return JSONResponse({"ok": False, "error": "Job not found."}, status_code=404)
    task = _tasks.get(job_id)
    if task is None:
        return JSONResponse({"ok": True, "message": "Job is not running."})
    task.cancel()
    return JSONResponse({"ok": True, "message": "Abort requested."})


def operation_active(operation: str) -> bool:
    """True when the operation has at least one enabled job (i.e. it will run)."""
    return any(
        j.get("operation") == operation and bool(j.get("enabled", True))
        for j in _jobs.values()
    )


def trigger_operation(operation: str) -> tuple[bool, bool]:
    """Run an operation now, routed through its scheduler job when one exists.

    Routing through the job means manual refreshes (Users/Libraries buttons)
    show up in the Scheduler's run history exactly like scheduled runs. Falls
    back to running the handler directly if the job was deleted. Returns
    (started, via_job).
    """
    job_id = next(
        (jid for jid, j in _jobs.items() if j.get("operation") == operation), None
    )
    if job_id is not None:
        if job_id in _running:
            return False, True
        _spawn(job_id)
        return True, True
    op = _OP_BY_ID.get(operation)
    if op is None:
        return False, False
    asyncio.create_task(op["handler"]())
    return True, False


@router.post("/api/scheduler/jobs/{job_id}/toggle")
async def toggle_job(job_id: str, request: Request, user: str = Depends(require_login)):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"ok": False, "error": "Job not found."}, status_code=404)
    job["enabled"] = not bool(job.get("enabled", True))
    _save_jobs()
    _register_job(job_id)
    return JSONResponse({"ok": True, "enabled": job["enabled"]})
