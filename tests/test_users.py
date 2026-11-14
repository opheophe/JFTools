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


# ---- presence status --------------------------------------------------------


def _sess(user="u1", np=None, paused=False, active=True, last=None, device=None):
    s = {
        "UserId": user,
        "UserName": user,
        "NowPlayingItem": np or {},
        "PlayState": {"IsPaused": paused},
        "IsActive": active,
    }
    if last is not None:
        s["LastActivityDate"] = last
    if device is not None:
        s["DeviceName"] = device
    return s


def test_pick_status_offline_without_sessions():
    assert users._pick_status([]) == ("offline", None)


def test_pick_status_watching_prefers_playing_over_paused():
    tv = _sess(np={"Name": "Dune"}, paused=False, device="TV")
    phone = _sess(np={"Name": "Pilot"}, paused=True, device="Phone")

    status, session = users._pick_status([phone, tv])

    assert status == "watching"
    assert session is tv


def test_pick_status_paused_when_only_paused_playback():
    phone = _sess(np={"Name": "Pilot"}, paused=True)

    status, session = users._pick_status([phone])

    assert status == "paused"
    assert session is phone


def test_pick_status_online_when_connected_without_playback():
    status, session = users._pick_status([_sess(active=True, device="TV")])

    assert status == "online"
    assert users._session_device(session) == "TV"


def test_pick_status_idle_for_stale_session():
    old = _sess(active=False, last="2026-01-01T00:00:00Z")
    newer = _sess(active=False, last="2026-01-02T00:00:00Z")

    status, session = users._pick_status([old, newer])

    assert status == "idle"
    assert session is newer


def test_pick_status_treats_missing_is_active_as_connected():
    # Older Jellyfin versions may omit IsActive; a listed session still means
    # the user is connected.
    session = _sess()
    del session["IsActive"]

    status, picked = users._pick_status([session])

    assert status == "online"
    assert picked is session
