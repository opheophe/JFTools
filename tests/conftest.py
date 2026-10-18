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
