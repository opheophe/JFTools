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


class _LastWatchedClient:
    async def get_users(self):
        return [{"Id": "u1"}]

    async def get_user_items(self, uid, params):
        return {
            "Items": [
                {
                    "Id": "e1",
                    "Type": "Episode",
                    "Name": "Revelations",
                    "SeriesName": "FROM",
                    "ParentIndexNumber": 2,
                    "IndexNumber": 14,
                    "UserData": {"LastPlayedDate": "2026-01-02T03:04:05Z"},
                }
            ]
        }


async def test_build_last_watched_carries_episode_number():
    # The Users page renders "series SxxExx" under the title, so the cached
    # record must keep the series and Jellyfin's season/episode numbers.
    out = await users._build_last_watched(_LastWatchedClient())

    rec = out["u1"]
    assert rec["name"] == "FROM — Revelations"
    assert rec["type"] == "Episode"
    assert rec["series"] == "FROM"
    assert rec["season"] == 2
    assert rec["episode"] == 14
