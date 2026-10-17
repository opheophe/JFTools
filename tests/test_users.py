from types import SimpleNamespace

import pytest

import app.users as users


@pytest.fixture(autouse=True)
def _jellyfin_configured(monkeypatch):
    # check_watching() short-circuits unless Jellyfin is configured. CI is a
    # fresh checkout with no settings/env, so stub the config to keep the tests
    # deterministic regardless of the local environment.
    monkeypatch.setattr(users, "config", SimpleNamespace(jellyfin_configured=True))


class _FakeClient:
    def __init__(self, sessions):
        self._sessions = sessions

    async def get_sessions(self):
        return self._sessions


def _session(user, np, paused):
    return {
        "UserName": user,
        "UserId": user,
        "NowPlayingItem": np,
        "PlayState": {"IsPaused": paused},
    }


async def test_check_watching_watching(monkeypatch):
    monkeypatch.setattr(users, "log", lambda *a: None)
    monkeypatch.setattr(users, "get_client", lambda: _FakeClient([
        _session("alice", {"Type": "Movie", "Name": "Dune"}, False),
        _session("bob", {"Type": "Episode", "Name": "Pilot", "SeriesName": "Show"}, True),
        {"UserName": "carol", "NowPlayingItem": {}, "PlayState": {}},
    ]))
    users._watching.update({"status": "idle", "users": [], "at": None})

    await users.check_watching()

    assert users._watching["status"] == "watching"
    assert [u["user"] for u in users._watching["users"]] == ["alice", "bob"]
    assert users._watching["at"] is not None


async def test_check_watching_paused_only(monkeypatch):
    monkeypatch.setattr(users, "log", lambda *a: None)
    monkeypatch.setattr(users, "get_client", lambda: _FakeClient([
        _session("bob", {"Type": "Movie", "Name": "Dune"}, True),
    ]))
    users._watching.update({"status": "idle"})

    await users.check_watching()

    assert users._watching["status"] == "paused"


async def test_check_watching_idle(monkeypatch):
    monkeypatch.setattr(users, "log", lambda *a: None)
    monkeypatch.setattr(users, "get_client", lambda: _FakeClient([
        {"UserName": "x", "NowPlayingItem": {}, "PlayState": {}},
    ]))
    users._watching.update({"status": "watching"})

    await users.check_watching()

    assert users._watching["status"] == "idle"
