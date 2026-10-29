import asyncio

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import app.scheduler as sch
from app.scheduler import _describe_schedule, _make_trigger


def test_describe_interval():
    assert _describe_schedule({"kind": "interval", "minutes": 30}) == "Every 30 minutes"
    assert _describe_schedule({"kind": "interval", "minutes": 60}) == "Every 1 hour"
    assert _describe_schedule({"kind": "interval", "minutes": 180}) == "Every 3 hours"


def test_describe_cron_daily_and_weekly():
    assert _describe_schedule({"kind": "cron", "hour": 3, "minute": 0}) == "Every day at 03:00"
    assert _describe_schedule(
        {"kind": "cron", "day_of_week": "mon", "hour": 3, "minute": 0}
    ) == "Weekly on Mon at 03:00"


def test_make_trigger_types():
    assert isinstance(_make_trigger({"kind": "interval", "minutes": 30}), IntervalTrigger)
    assert isinstance(_make_trigger({"kind": "cron", "hour": 3, "minute": 0}), CronTrigger)
    assert isinstance(_make_trigger({"kind": "crontab", "expr": "0 3 * * *"}), CronTrigger)


async def test_run_job_tracks_running(monkeypatch):
    calls = []

    async def handler():
        calls.append("ran")

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setitem(
        sch._OP_BY_ID, "fake_op",
        {"id": "fake_op", "label": "Fake", "tab": "Test", "handler": handler},
    )
    monkeypatch.setitem(
        sch._jobs, "j1", {"id": "j1", "operation": "fake_op", "trigger": {}, "enabled": True}
    )
    monkeypatch.setattr(sch, "_running", set())

    await sch._run_job("j1")
    assert calls == ["ran"]
    assert "j1" not in sch._running
    assert sch._jobs["j1"]["last_status"] == "ok"


async def test_run_job_publishes_and_self_reporting_ops_skip_it(monkeypatch):
    events = []

    async def handler():
        return None

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setattr(
        sch.notifications,
        "publish",
        lambda kind, title, message="", **kwargs: events.append((kind, title, message, kwargs)),
    )
    monkeypatch.setitem(
        sch._OP_BY_ID, "normal_op",
        {"id": "normal_op", "label": "Normal", "tab": "Test", "handler": handler},
    )
    monkeypatch.setitem(
        sch._OP_BY_ID, "self_op",
        {"id": "self_op", "label": "Self", "tab": "Test", "handler": handler,
         "notify": "self"},
    )
    monkeypatch.setitem(
        sch._jobs, "j-normal",
        {"id": "j-normal", "operation": "normal_op", "trigger": {}, "enabled": True},
    )
    monkeypatch.setitem(
        sch._jobs, "j-self",
        {"id": "j-self", "operation": "self_op", "trigger": {}, "enabled": True},
    )
    monkeypatch.setattr(sch, "_running", set())

    await sch._run_job("j-normal")
    await sch._run_job("j-self")

    assert len(events) == 1
    kind, title, message, kwargs = events[0]
    assert kind == "success"
    assert title == "Normal complete"
    assert message.startswith("Finished in ")
    assert kwargs["source"] == "Scheduler"


async def test_run_job_blocks_reentry(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler():
        started.set()
        await release.wait()

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setitem(
        sch._OP_BY_ID, "fake_op",
        {"id": "fake_op", "label": "Fake", "tab": "Test", "handler": handler},
    )
    monkeypatch.setitem(
        sch._jobs, "j2", {"id": "j2", "operation": "fake_op", "trigger": {}, "enabled": True}
    )
    monkeypatch.setattr(sch, "_running", set())

    t1 = asyncio.create_task(sch._run_job("j2"))
    await started.wait()
    assert "j2" in sch._running  # marked running once the handler starts
    await asyncio.sleep(0)
    t2 = asyncio.create_task(sch._run_job("j2"))
    done, _ = await asyncio.wait({t2}, timeout=0.5)
    release.set()
    await t1
    assert t2 in done
    assert "j2" not in sch._running


def test_public_job_running_flag(monkeypatch):
    monkeypatch.setitem(sch._jobs, "j3", {"id": "j3", "operation": "clear_logs", "trigger": {}})
    monkeypatch.setattr(sch, "_running", {"j3"})
    assert sch._public_job("j3")["running"] is True
    monkeypatch.setattr(sch, "_running", set())
    assert sch._public_job("j3")["running"] is False


def test_refresh_users_is_not_a_scheduler_operation():
    # Users refresh now runs on page load instead of via the Scheduler.
    assert "refresh_users" not in sch._OP_BY_ID
    assert "refresh_users" not in sch._DEFAULT_SCHEDULES
    assert "refresh_users" not in sch._ACTIVE_BY_DEFAULT


def test_check_updates_runs_hourly_by_default():
    assert "check_updates" in sch._OP_BY_ID
    assert sch._DEFAULT_SCHEDULES["check_updates"] == {"kind": "interval", "minutes": 60}
    assert "check_updates" in sch._ACTIVE_BY_DEFAULT


def test_backup_job_is_off_by_default():
    assert "backup" in sch._OP_BY_ID
    assert sch._DEFAULT_SCHEDULES["backup"] == {"kind": "cron", "hour": 3, "minute": 30}
    assert "backup" not in sch._ACTIVE_BY_DEFAULT


def test_seeded_backup_job_is_disabled(monkeypatch):
    monkeypatch.setattr(sch, "_jobs", {})
    monkeypatch.setattr(sch, "_save_jobs", lambda: None)

    sch._ensure_default_jobs()

    assert sch._jobs["backup"]["enabled"] is False
    assert sch._jobs["backup"]["trigger"] == {"kind": "cron", "hour": 3, "minute": 30}


def test_describe_seconds_interval():
    assert _describe_schedule({"kind": "interval", "seconds": 30}) == "Every 30 seconds"


def test_make_trigger_seconds_interval():
    t = _make_trigger({"kind": "interval", "seconds": 30})
    assert isinstance(t, IntervalTrigger)
    assert t.interval.total_seconds() == 30


def test_users_watching_defaults():
    assert "users_watching" in sch._OP_BY_ID
    assert sch._DEFAULT_SCHEDULES["users_watching"] == {"kind": "interval", "seconds": 30}
    assert "users_watching" in sch._ACTIVE_BY_DEFAULT


def test_operation_active(monkeypatch):
    monkeypatch.setattr(sch, "_jobs", {
        "a": {"operation": "users_watching", "enabled": True},
        "b": {"operation": "users_watching", "enabled": False},
        "c": {"operation": "other", "enabled": True},
    })
    assert sch.operation_active("users_watching") is True
    sch._jobs["a"]["enabled"] = False
    assert sch.operation_active("users_watching") is False


async def test_quiet_operation_skips_logs(monkeypatch):
    logged = []
    monkeypatch.setattr(sch, "log", lambda tab, lvl, msg: logged.append((tab, lvl, msg)))
    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setattr(sch, "_running", set())

    async def handler():
        return None

    monkeypatch.setitem(sch._OP_BY_ID, "quiet_op",
        {"id": "quiet_op", "label": "Quiet Op", "tab": "Test", "handler": handler, "quiet": True})
    monkeypatch.setitem(sch._jobs, "jq",
        {"id": "jq", "operation": "quiet_op", "trigger": {}, "enabled": True})

    await sch._run_job("jq")
    assert logged == []
    assert sch._jobs["jq"]["last_status"] == "ok"


async def test_heavy_job_skipped_when_another_heavy_runs(monkeypatch):
    calls = []

    async def handler():
        calls.append("ran")

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setattr(sch, "log", lambda *a: None)
    monkeypatch.setitem(sch._OP_BY_ID, "jellyfin_id_fetch",
        {"id": "jellyfin_id_fetch", "label": "Fetch Jellyfin movies", "tab": "Movie Match", "handler": handler})
    monkeypatch.setitem(sch._jobs, "hf",
        {"id": "hf", "operation": "jellyfin_id_fetch", "trigger": {}, "enabled": True})
    monkeypatch.setattr(sch, "_running", set())
    monkeypatch.setattr(sch, "_heavy_running", "subtitle_scan")

    await sch._run_job("hf")

    assert calls == []  # the fetch never started
    assert sch._jobs["hf"]["last_status"] == "skipped"


async def test_heavy_slot_is_released_after_run(monkeypatch):
    async def handler():
        return None

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setitem(sch._OP_BY_ID, "subtitle_scan",
        {"id": "subtitle_scan", "label": "Media scan", "tab": "Search", "handler": handler})
    monkeypatch.setitem(sch._jobs, "hs",
        {"id": "hs", "operation": "subtitle_scan", "trigger": {}, "enabled": True})
    monkeypatch.setattr(sch, "_running", set())
    monkeypatch.setattr(sch, "_heavy_running", None)

    await sch._run_job("hs")

    assert sch._jobs["hs"]["last_status"] == "ok"
    assert sch._heavy_running is None


async def test_light_job_runs_while_heavy_busy(monkeypatch):
    ran = []

    async def handler():
        ran.append(1)

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setitem(sch._OP_BY_ID, "clear_logs",
        {"id": "clear_logs", "label": "Clear logs", "tab": "Jellyfin", "handler": handler})
    monkeypatch.setitem(sch._jobs, "jl",
        {"id": "jl", "operation": "clear_logs", "trigger": {}, "enabled": True})
    monkeypatch.setattr(sch, "_running", set())
    monkeypatch.setattr(sch, "_heavy_running", "subtitle_scan")

    await sch._run_job("jl")

    assert ran == [1]
    assert sch._jobs["jl"]["last_status"] == "ok"


def test_trigger_operation_refused_while_heavy_busy(monkeypatch):
    monkeypatch.setattr(sch, "_heavy_running", "subtitle_scan")
    monkeypatch.setattr(sch, "_jobs", {
        "jf": {"id": "jf", "operation": "jellyfin_id_fetch", "trigger": {}, "enabled": False},
    })

    started, via_job = sch.trigger_operation("jellyfin_id_fetch")

    assert started is False
    assert via_job is False
    assert sch.busy_message() == "Busy — Media scan is running."


async def test_run_job_records_last_duration(monkeypatch):
    async def handler():
        await asyncio.sleep(0.05)

    monkeypatch.setattr(sch, "_save_jobs", lambda: None)
    monkeypatch.setattr(sch, "log", lambda *a: None)
    monkeypatch.setattr(sch, "_running", set())
    monkeypatch.setattr(sch, "_heavy_running", None)
    monkeypatch.setitem(sch._OP_BY_ID, "quick_op",
        {"id": "quick_op", "label": "Quick Op", "tab": "Test", "handler": handler})
    monkeypatch.setitem(sch._jobs, "jq",
        {"id": "jq", "operation": "quick_op", "trigger": {}, "enabled": True})

    await sch._run_job("jq")

    assert sch._jobs["jq"]["last_duration"] >= 0.05


def test_public_job_exposes_last_duration(monkeypatch):
    monkeypatch.setattr(sch, "_jobs", {
        "jq": {
            "id": "jq", "operation": "quick_op", "trigger": {}, "enabled": True,
            "last_run": 1000.0, "last_duration": 1.5, "last_status": "ok", "last_error": None,
        }
    })
    monkeypatch.setitem(sch._OP_BY_ID, "quick_op", {"id": "quick_op", "label": "Quick Op"})
    monkeypatch.setattr(sch, "_running", set())

    payload = sch._public_job("jq")

    assert payload["last_duration"] == 1.5


def test_fetch_job_label_is_jellyfin_wording():
    assert sch._OP_BY_ID["jellyfin_id_fetch"]["label"] == "Fetch Jellyfin movies"
