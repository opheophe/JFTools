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
