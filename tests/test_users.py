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

# ---- library access ---------------------------------------------------------


class _LibraryClient:
    def __init__(self, user, libraries):
        self.user = user
        self.libraries = libraries
        self.saved = None

    async def get_user(self, user_id):
        return self.user

    async def get_libraries(self):
        return self.libraries

    async def update_user_policy(self, user_id, policy):
        self.saved = (user_id, policy)


def _library_client(policy=None, libraries=None):
    return _LibraryClient(
        user={"Id": "u1", "Name": "alice", "Policy": policy or {}},
        libraries=libraries if libraries is not None else [
            {"Name": "Filmer", "ItemId": "lib1", "CollectionType": "movies"},
            {"Name": "Serier", "ItemId": "lib2", "CollectionType": "tvshows"},
        ],
    )


def _api_client():
    import httpx

    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _get_libraries(monkeypatch, fake):
    from app.main import app
    from app.web import require_login

    monkeypatch.setattr(users, "get_client", lambda: fake)
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with _api_client() as client:
            return await client.get("/api/users/u1/libraries")
    finally:
        app.dependency_overrides.pop(require_login, None)


async def _post_libraries(monkeypatch, fake, data):
    from app.main import app
    from app.web import require_login

    monkeypatch.setattr(users, "get_client", lambda: fake)
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with _api_client() as client:
            return await client.post("/api/users/u1/libraries", data=data)
    finally:
        app.dependency_overrides.pop(require_login, None)


async def test_user_libraries_reports_explicit_ids(monkeypatch):
    fake = _library_client(
        policy={"EnableAllFolders": False, "EnabledFolders": ["lib1", "gone"]}
    )

    response = await _get_libraries(monkeypatch, fake)

    data = response.json()
    assert data["ok"] is True
    assert data["all"] is False
    # "gone" is no longer on the server, so it is not reported as granted.
    assert data["enabled"] == ["lib1"]
    assert [lib["name"] for lib in data["libraries"]] == ["Filmer", "Serier"]


async def test_user_libraries_reports_all_folders(monkeypatch):
    fake = _library_client(
        policy={"EnableAllFolders": True, "EnabledFolders": ["lib1"]}
    )

    data = (await _get_libraries(monkeypatch, fake)).json()

    assert data["all"] is True
    assert data["enabled"] == ["lib1", "lib2"]


async def test_user_libraries_save_keeps_unrelated_policy_fields(monkeypatch):
    fake = _library_client(policy={
        "EnableAllFolders": True,
        "EnabledFolders": ["lib1", "lib2"],
        "EnableContentDownloading": True,
        "MaxParentalRating": 12,
        "BlockedTags": ["horror"],
    })

    response = await _post_libraries(
        monkeypatch, fake, {"all": "false", "ids": "lib2,lib1"}
    )

    assert response.json() == {"ok": True, "all": False, "enabled": ["lib1", "lib2"]}
    user_id, policy = fake.saved
    assert user_id == "u1"
    assert policy["EnableAllFolders"] is False
    # Canonical library order, not the request's order.
    assert policy["EnabledFolders"] == ["lib1", "lib2"]
    # Everything else is sent back untouched: Jellyfin replaces the whole policy.
    assert policy["EnableContentDownloading"] is True
    assert policy["MaxParentalRating"] == 12
    assert policy["BlockedTags"] == ["horror"]


async def test_user_libraries_save_all_folders(monkeypatch):
    fake = _library_client(policy={"EnableAllFolders": False})

    response = await _post_libraries(monkeypatch, fake, {"all": "true", "ids": ""})

    assert response.json()["all"] is True
    assert fake.saved[1]["EnableAllFolders"] is True


async def test_user_libraries_save_rejects_unknown_library(monkeypatch):
    fake = _library_client()

    response = await _post_libraries(
        monkeypatch, fake, {"all": "false", "ids": "lib1,libx"}
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert fake.saved is None


async def test_user_libraries_save_maps_forbidden_to_a_hint(monkeypatch):
    from app.jellyfin.client import JellyfinError

    class Denied(_LibraryClient):
        async def update_user_policy(self, user_id, policy):
            raise JellyfinError("Jellyfin returned HTTP 403 for /Users/u1/Policy.")

    fake = Denied(
        user={"Id": "u1", "Name": "alice", "Policy": {}},
        libraries=[{"Name": "Filmer", "ItemId": "lib1"}],
    )

    response = await _post_libraries(monkeypatch, fake, {"all": "true", "ids": ""})

    assert response.status_code == 502
    assert "admin API key" in response.json()["error"]
