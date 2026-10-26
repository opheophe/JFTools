"""Remote sync: the export endpoint and applying an import locally."""

import httpx
import pytest

from app import jellyfin_id, remotesync, subtitles
from app.config import config
from app.main import app
from app.web import require_login


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def logged_in():
    app.dependency_overrides[require_login] = lambda: "tester"
    yield
    app.dependency_overrides.pop(require_login, None)


def _quiet_stores(monkeypatch):
    monkeypatch.setattr(jellyfin_id, "_save_state", lambda: None)
    monkeypatch.setattr(subtitles, "_save_state", lambda: None)


# ---- applying an export ------------------------------------------------------


def test_apply_export_replaces_the_local_stores(monkeypatch):
    _quiet_stores(monkeypatch)
    monkeypatch.setattr(
        jellyfin_id, "_state",
        {"rows": [{"path": "/old"}], "marks": {"a"}, "favorites": {}, "last_sync": None},
    )
    monkeypatch.setattr(
        subtitles, "_state",
        {"files": {"/old": {"scanned": True}}, "ok": ["/old"],
         "full_scan_at": 1, "change_scan_at": 2},
    )

    result = remotesync.apply_export({
        "media": {
            "rows": [{"path": "/Media/New.mkv", "name": "New"}],
            "marks": ["/Media/New.mkv"],
            "favorites": {"u1": [{"id": "i1"}]},
            "last_sync": 1234,
        },
        "scans": {
            "files": {"/Media/New.mkv": {"scanned": True, "internal": []}},
            "ok": ["/Media/New.mkv"],
            "full_scan_at": 5,
            "change_scan_at": 6,
        },
    })

    assert result == {"ok": True, "rows": 1, "scanned": 1}
    assert jellyfin_id._state["rows"] == [{"path": "/Media/New.mkv", "name": "New"}]
    assert jellyfin_id._state["marks"] == {"/Media/New.mkv"}
    assert jellyfin_id._state["favorites"] == {"u1": [{"id": "i1"}]}
    assert subtitles._state["files"] == {
        "/Media/New.mkv": {"scanned": True, "internal": []}
    }
    assert subtitles._state["ok"] == ["/Media/New.mkv"]
    assert subtitles._state["full_scan_at"] == 5


def test_apply_export_refuses_a_broken_payload(monkeypatch):
    _quiet_stores(monkeypatch)
    assert remotesync.apply_export({})["ok"] is False
    assert remotesync.apply_export("nope")["ok"] is False
    assert remotesync.apply_export({"media": {}, "scans": {"files": {}}})["ok"] is False
    # Rows that are not dicts are dropped rather than crashing the import.
    ok = remotesync.apply_export({
        "media": {"rows": ["junk", {"path": "/ok"}]},
        "scans": {"files": {}},
    })
    assert ok["ok"] is True
    assert jellyfin_id._state["rows"] == [{"path": "/ok"}]


# ---- fetching from a remote server -------------------------------------------


async def test_run_import_fetches_and_applies(monkeypatch):
    _quiet_stores(monkeypatch)
    monkeypatch.setattr(jellyfin_id, "_state", {"rows": [], "marks": set(), "favorites": {}, "last_sync": None})
    monkeypatch.setattr(subtitles, "_state", {"files": {}, "ok": [], "full_scan_at": None, "change_scan_at": None})
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "media": {"rows": [{"path": "/x"}], "marks": []},
                "scans": {"files": {"/x": {"scanned": True}}},
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr(remotesync.httpx, "AsyncClient", FakeClient)
    result = await remotesync.run_import("http://192.168.1.114:8099/", "jft_xyz")

    assert result == {"ok": True, "rows": 1, "scanned": 1}
    assert captured["url"] == "http://192.168.1.114:8099/api/sync/export"
    assert captured["headers"] == {"X-API-Key": "jft_xyz"}


async def test_run_import_reports_bad_answers(monkeypatch):
    class FakeResp:
        status_code = 401

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(remotesync.httpx, "AsyncClient", FakeClient)
    result = await remotesync.run_import("http://host:8099", "jft_xyz")
    assert result["ok"] is False
    assert "refused" in result["error"]

    assert (await remotesync.run_import("", "key"))["ok"] is False
    assert (await remotesync.run_import("http://host:8099", ""))["ok"] is False


# ---- the export endpoint -----------------------------------------------------


async def test_sync_export_needs_a_session_or_api_key(logged_in):
    async with _client() as client:
        resp = await client.get("/api/sync/export")
    assert resp.status_code == 401


async def test_sync_export_accepts_an_api_key(logged_in, monkeypatch):
    monkeypatch.setattr(
        config._settings, "api_keys",
        [{"name": "test", "key": "jft_testkey", "created": 0.0}],
    )
    monkeypatch.setattr(
        jellyfin_id, "_state",
        {"rows": [{"path": "/x", "name": "X"}], "marks": {"/x"},
         "favorites": {}, "last_sync": None},
    )
    monkeypatch.setattr(
        subtitles, "_state",
        {"files": {"/x": {"scanned": True}}, "ok": [],
         "full_scan_at": None, "change_scan_at": None},
    )

    async with _client() as client:
        resp = await client.get("/api/sync/export", headers={"X-API-Key": "jft_testkey"})
    data = resp.json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["app"] == "jftools"
    assert data["media"]["rows"] == [{"path": "/x", "name": "X"}]
    assert data["media"]["marks"] == ["/x"]
    assert data["scans"]["files"] == {"/x": {"scanned": True}}


async def test_sync_export_rejects_a_wrong_api_key(logged_in, monkeypatch):
    monkeypatch.setattr(
        config._settings, "api_keys",
        [{"name": "test", "key": "jft_testkey", "created": 0.0}],
    )
    async with _client() as client:
        resp = await client.get("/api/sync/export", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


# ---- settings routes ---------------------------------------------------------


async def test_api_key_create_requires_a_name(logged_in):
    async with _client() as client:
        resp = await client.post("/settings/api-keys", data={"name": "  "})
    assert resp.status_code == 400
    assert "name" in resp.json()["error"]


async def test_api_key_create_and_revoke(logged_in, monkeypatch):
    created = {}

    def fake_create(name):
        created["name"] = name
        return "jft_newkey"

    monkeypatch.setattr(config, "create_api_key", fake_create)
    async with _client() as client:
        resp = await client.post("/settings/api-keys", data={"name": "Local dev"})
    data = resp.json()
    assert data["ok"] is True
    assert data["key"] == "jft_newkey"
    assert created == {"name": "Local dev"}

    revoked = {}
    monkeypatch.setattr(config, "revoke_api_key", lambda key: revoked.setdefault("key", key) or True)
    async with _client() as client:
        resp = await client.post("/settings/api-keys/revoke", data={"key": "jft_newkey"})
    assert resp.json()["ok"] is True
    assert revoked == {"key": "jft_newkey"}


async def test_remote_sync_run_endpoint(logged_in, monkeypatch):
    async def fake_import(url, api_key):
        return {"ok": True, "rows": 12, "scanned": 9}

    monkeypatch.setattr("app.main.run_import", fake_import)
    async with _client() as client:
        resp = await client.post("/api/remotesync/run")
    data = resp.json()
    assert data["ok"] is True
    assert "12 media item(s)" in data["message"]

    async def failing_import(url, api_key):
        return {"ok": False, "error": "boom"}

    monkeypatch.setattr("app.main.run_import", failing_import)
    async with _client() as client:
        resp = await client.post("/api/remotesync/run")
    assert resp.status_code == 502
    assert resp.json()["error"] == "boom"


async def test_remote_sync_settings_save(logged_in, monkeypatch):
    saved = {}

    def fake_save(url, api_key, enabled, on_startup):
        saved.update(url=url, api_key=api_key, enabled=enabled, on_startup=on_startup)

    monkeypatch.setattr(config, "save_remote_sync", fake_save)
    monkeypatch.setattr(config._settings, "remote_sync_api_key", "jft_existing")
    async with _client() as client:
        resp = await client.post("/settings/remote-sync", data={
            "url": "http://192.168.1.114:8099/",
            "api_key": "",
            "enabled": "1",
            "on_startup": "1",
        })
    assert resp.json()["ok"] is True
    assert saved == {
        "url": "http://192.168.1.114:8099/",
        "api_key": "jft_existing",  # a blank field keeps the stored key
        "enabled": True,
        "on_startup": True,
    }
