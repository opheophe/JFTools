from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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
