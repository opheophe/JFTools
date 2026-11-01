"""Shared test setup.

The app writes per-tab log files under ``data/logs``. Tests exercise the routes
that log (searches, downloads, scheduled runs), so without this the test run
would append its fixture names to the real logs an admin reads on the Logs page.
"""
import pytest


@pytest.fixture(autouse=True)
def _keep_test_logs_out_of_the_app(tmp_path, monkeypatch):
    """Send every ``log()`` call to a throwaway directory."""
    from app import logs

    monkeypatch.setattr(logs, "LOG_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _keep_test_writes_out_of_the_scan_store(tmp_path, monkeypatch):
    """Point the scan store at temporary files.

    ``app.subtitles`` loads ``data/scans.json`` (or the legacy
    ``subtitles.json``) at import time. A test that saves state — a scan, an Ok
    mark, an audio delete — would otherwise write the real store, and the
    first save also removes the legacy file after a migration. That is real
    user data, so no test may touch it.
    """
    from app import subtitles

    monkeypatch.setattr(subtitles, "_SCANS_FILE", tmp_path / "scans.json")
    monkeypatch.setattr(subtitles, "_LEGACY_SUBS_FILE", tmp_path / "subtitles.json")
    # The in-memory state came from the real store at import; never let an
    # unpatched save treat it as a migration and unlink anything.
    monkeypatch.setitem(subtitles._state, "legacy", False)


@pytest.fixture(autouse=True)
def _keep_test_notifications_out_of_the_app(tmp_path, monkeypatch):
    """Point the durable notification store at a throwaway file."""
    from app import logs, notifications

    monkeypatch.setattr(notifications, "_STORE_FILE", tmp_path / "notifications.json")
    notifications._reset_for_tests()
    logs._reset_recent_for_tests()
    yield
    logs._reset_recent_for_tests()

