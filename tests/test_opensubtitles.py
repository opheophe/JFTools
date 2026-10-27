"""Tests for the OpenSubtitles client and the subtitle download routes."""

import json
import os
from pathlib import Path

import httpx
import pytest

import app.jellyfin_id as jellyfin_id
import app.opensubtitles as osmod
import app.subtitles as sub
from app.config import config
from app.opensubtitles import (
    OpenSubtitlesClient,
    OpenSubtitlesError,
    normalize_languages,
    opensubtitles_hash,
    sub_extension,
)


def _json(resp):
    return json.loads(resp.body)


# ---- moviehash --------------------------------------------------------------


def test_hash_small_file_known_vector(tmp_path):
    # 8 bytes -> content read twice -> two complete little-endian words.
    f = tmp_path / "m.mkv"
    f.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08")
    moviehash, size = opensubtitles_hash(str(f))
    assert size == 8
    assert moviehash == "100e0c0a0806040a"


def test_hash_short_odd_file_ignores_incomplete_word(tmp_path):
    # 10 bytes -> doubled to 20 -> words at offsets 0 and 8; the 4-byte tail
    # at offset 16 is not a complete word and must be ignored.
    f = tmp_path / "m.mkv"
    f.write_bytes(b"\x01" * 10)
    moviehash, size = opensubtitles_hash(str(f))
    assert size == 10
    assert moviehash == "020202020202020c"


def test_hash_only_uses_first_and_last_64k(tmp_path):
    f = tmp_path / "m.mkv"
    head = b"\xab" * 65536
    tail = b"\xcd" * 65536
    f.write_bytes(head + b"\x00" * 200000 + tail)

    h1, size = opensubtitles_hash(str(f))
    f.write_bytes(head + b"\x01" * 200000 + tail)  # middle changed
    h2, size2 = opensubtitles_hash(str(f))

    assert size == size2
    assert h1 == h2


def test_hash_empty_file(tmp_path):
    f = tmp_path / "m.mkv"
    f.write_bytes(b"")
    moviehash, size = opensubtitles_hash(str(f))
    assert size == 0
    assert moviehash == "0000000000000000"


# ---- helpers ----------------------------------------------------------------


def test_sub_extension_mapping():
    assert sub_extension("srt") == ".srt"
    assert sub_extension("vtt") == ".vtt"
    assert sub_extension("webvtt") == ".vtt"
    assert sub_extension("ass") == ".ass"
    assert sub_extension("weird") == ".srt"


def test_normalize_languages():
    assert normalize_languages("en, sv ,EN") == ["en", "sv"]
    assert normalize_languages("swe") == ["swe"]
    assert normalize_languages("") == []


# ---- client -----------------------------------------------------------------


def _candidate(file_id=11, lang="en", name="movie.en.srt", **over):
    base = {
        "subtitle_id": "123",
        "file_id": file_id,
        "file_name": name,
        "language": lang,
        "download_count": 500,
        "new_download_count": 1,
        "hearing_impaired": False,
        "hd": True,
        "ratings": 8.5,
        "votes": 4,
        "from_trusted": True,
        "ai_translated": False,
        "machine_translated": False,
        "moviehash_match": True,
        "upload_date": "2024-01-02T00:00:00Z",
        "release": "Movie.2020.1080p.mkv",
        "uploader": "someone",
        "title": "Movie",
        "year": 2020,
    }
    base.update(over)
    return base


async def test_search_sends_api_key_and_params():
    seen = {}

    def handler(request):
        seen["api_key"] = request.headers.get("Api-Key")
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"total_count": 1, "data": []})

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    result = await client.search(languages=["en", "sv"], moviehash="abc", query="Movie")

    assert seen["api_key"] == "key"
    assert seen["params"]["languages"] == "en,sv"
    assert seen["params"]["moviehash"] == "abc"
    assert result == {"total_count": 1, "data": []}


async def test_search_follows_the_self_redirect():
    """api.opensubtitles.com answers the first request of a connection with a
    301 to the same URL; the search has to follow it instead of giving up."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(301, headers={"location": "/api/v1/subtitles?query=Movie"})
        return httpx.Response(200, json={"total_count": 1, "data": []})

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    result = await client.search(languages=["en"], query="Movie")

    assert result["total_count"] == 1
    assert len(seen) == 2


async def test_search_reports_an_unexpected_status():
    def handler(request):
        return httpx.Response(304)

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    with pytest.raises(OpenSubtitlesError, match="HTTP 304 for search"):
        await client.search(languages=["en"], query="Movie")


async def test_search_flattens_candidates():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "data": [
                    {
                        "id": "1",
                        "attributes": {
                            "subtitle_id": "42",
                            "language": "en",
                            "download_count": 100,
                            "feature_details": {"movie_name": "Dune", "year": 2021},
                            "uploader": {"name": "u1"},
                            "files": [{"file_id": 7, "file_name": "dune.en.srt"}],
                        },
                    }
                ],
            },
        )

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    result = await client.search(query="Dune")
    assert result["total_count"] == 1
    assert result["data"][0]["file_id"] == 7
    assert result["data"][0]["language"] == "en"
    assert result["data"][0]["title"] == "Dune"
    assert result["data"][0]["year"] == 2021
    assert result["data"][0]["uploader"] == "u1"


async def test_requests_send_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json={"total_count": 0, "data": []})

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    await client.search(query="x")
    assert seen["ua"] and "JFTools" in seen["ua"]


async def test_search_requires_api_key():
    client = OpenSubtitlesClient("")
    with pytest.raises(OpenSubtitlesError, match="not configured"):
        await client.search(query="x")


async def test_search_error_maps_message():
    def handler(request):
        return httpx.Response(406, json={"message": "You cannot download more than 5"})

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    with pytest.raises(OpenSubtitlesError, match="more than 5"):
        await client.search(query="x")


async def test_login_stores_token_and_remaining():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "token": "tok123",
                "user": {"allowed_downloads": 20, "downloads_count": 3, "level": "Member"},
            },
        )

    client = OpenSubtitlesClient("key", "bob", "pw", transport=httpx.MockTransport(handler))
    user = await client.login()
    assert seen["body"] == {"username": "bob", "password": "pw"}
    assert user["level"] == "Member"
    assert client.logged_in is True
    assert client.remaining() == 17


async def test_anonymous_login_has_no_remaining():
    client = OpenSubtitlesClient("key", "", "")
    assert await client.login() == {}
    assert client.logged_in is False
    assert client.remaining() is None


async def test_download_follows_link_and_reports_remaining():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/download":
            return httpx.Response(
                200,
                json={"link": "https://cdn.example/x.gz", "file_name": "dune.en.srt", "remaining": 16},
            )
        if request.url.path == "/x.gz":
            return httpx.Response(200, content=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        return httpx.Response(404)

    client = OpenSubtitlesClient("key", transport=httpx.MockTransport(handler))
    data = await client.download(7, "srt")
    assert calls == ["/api/v1/download", "/x.gz"]
    assert data["file_name"] == "dune.en.srt"
    assert data["remaining"] == 16
    assert b"00:00:01" in data["content"]


async def test_download_rejects_unknown_format():
    client = OpenSubtitlesClient("key")
    with pytest.raises(OpenSubtitlesError, match="Unsupported subtitle format"):
        await client.download(1, "docx")


# ---- several accounts --------------------------------------------------------


def _ok_login():
    return {
        "token": "tok",
        "user": {"allowed_downloads": 20, "downloads_count": 15, "level": "2"},
    }


class FakeAccountClient:
    """Just enough of OpenSubtitlesClient for the pool."""

    def __init__(self, username, remaining=None, quota=False, login_error=""):
        self.username = username
        self._remaining = remaining
        self.quota = quota
        self.login_error = login_error
        self.downloads = []
        self.logins = 0

    @property
    def logged_in(self):
        return bool(self.username) and not self.login_error

    def remaining(self):
        return self._remaining

    async def login(self):
        self.logins += 1
        if self.login_error:
            raise OpenSubtitlesError(self.login_error)
        return _ok_login()["user"]

    async def search(self, **params):
        return {"total_count": 0, "data": []}

    async def download(self, file_id, sub_format="srt"):
        self.downloads.append(file_id)
        if self.quota:
            raise osmod.OpenSubtitlesQuotaError("out of downloads")
        if self._remaining is not None:
            self._remaining -= 1
        return {
            "content": b"1\n",
            "file_name": "a.srt",
            "remaining": self._remaining,
        }


def _pool(clients):
    pool = osmod.OpenSubtitlesPool("key", [{"username": "x"} for _ in clients])
    pool._clients = clients
    return pool


def test_download_quota_is_a_specific_error():
    assert issubclass(osmod.OpenSubtitlesQuotaError, OpenSubtitlesError)


async def test_download_406_is_reported_as_a_quota_error():
    def handler(request):
        if request.url.path == "/api/v1/login":
            return httpx.Response(200, json=_ok_login())
        return httpx.Response(
            406, json={"message": "You have downloaded your allowed 20 subtitles for 24h."}
        )

    client = OpenSubtitlesClient("key", "user", "pw", transport=httpx.MockTransport(handler))
    with pytest.raises(osmod.OpenSubtitlesQuotaError, match="allowed 20 subtitles"):
        await client.download(11)


async def test_pool_moves_to_the_next_account_when_one_is_out():
    first = FakeAccountClient("one", remaining=0, quota=True)
    second = FakeAccountClient("two", remaining=7)
    pool = _pool([first, second])

    data = await pool.download(11)

    assert data["file_name"] == "a.srt"
    assert first.downloads == [11]
    assert second.downloads == [11]
    # The spent account counts as zero; the other one lost one download.
    assert pool.remaining() == 6


async def test_pool_reports_when_every_account_is_out():
    pool = _pool([FakeAccountClient("one", quota=True), FakeAccountClient("two", quota=True)])
    with pytest.raises(osmod.OpenSubtitlesQuotaError, match="Every OpenSubtitles account"):
        await pool.download(1)


async def test_pool_with_one_account_keeps_the_api_message():
    pool = _pool([FakeAccountClient("only", quota=True)])
    with pytest.raises(osmod.OpenSubtitlesQuotaError, match="out of downloads"):
        await pool.download(1)


async def test_pool_prepare_logs_in_every_account_and_sums_remaining():
    first = FakeAccountClient("one", remaining=4)
    second = FakeAccountClient("two", remaining=9)
    pool = _pool([first, second])

    await pool.prepare()
    await pool.prepare()  # only once

    assert (first.logins, second.logins) == (1, 1)
    assert pool.remaining() == 13


async def test_pool_search_uses_the_first_logged_in_account():
    bad = FakeAccountClient("bad", login_error="wrong password")
    good = FakeAccountClient("good", remaining=3)
    pool = _pool([bad, good])

    assert await pool.search(query="movie") == {"total_count": 0, "data": []}
    assert good.logins == 1


async def test_pool_test_lists_every_account():
    pool = _pool([FakeAccountClient("one"), FakeAccountClient("two")])
    entries = await pool.test()
    assert [e["username"] for e in entries] == ["one", "two"]
    assert all(e["user"]["level"] == "2" for e in entries)


async def test_pool_test_reports_a_broken_account_beside_a_good_one():
    pool = _pool([
        FakeAccountClient("one", login_error="OpenSubtitles login failed"),
        FakeAccountClient("two"),
    ])
    entries = await pool.test()
    assert "login failed" in entries[0]["error"]
    assert entries[1]["user"]["level"] == "2"


async def test_pool_test_raises_when_every_account_fails():
    pool = _pool([
        FakeAccountClient("one", login_error="bad key"),
        FakeAccountClient("two", login_error="bad key"),
    ])
    with pytest.raises(OpenSubtitlesError, match="bad key"):
        await pool.test()


async def test_pool_without_accounts_is_anonymous(monkeypatch):
    pool = osmod.OpenSubtitlesPool("key", [])
    assert len(pool.clients) == 1 and pool.clients[0].username == ""
    called = {}

    async def fake_test():
        called["anonymous"] = True
        return {}

    monkeypatch.setattr(pool.clients[0], "test", fake_test)
    entries = await pool.test()
    assert called["anonymous"] is True
    assert entries[0]["anonymous"] is True


# ---- routes -----------------------------------------------------------------


@pytest.fixture
def os_env(tmp_path, monkeypatch):
    """A media tree, a Movie Match row for it and OpenSubtitles configured."""
    media = tmp_path / "Media"
    media.mkdir()
    video = media / "Movie (2020).mkv"
    video.write_bytes(b"x" * 1024)
    monkeypatch.setattr(
        config._settings, "path_maps", [{"from": "/Media", "to": str(media)}]
    )
    monkeypatch.setattr(config._settings, "file_mgmt", True)
    monkeypatch.setattr(config._settings, "opensubtitles_api_key", "key")
    monkeypatch.setattr(config._settings, "opensubtitles_accounts", [])
    monkeypatch.setattr(config._settings, "opensubtitles_languages", "en,sv")
    monkeypatch.setattr(
        jellyfin_id,
        "_state",
        {
            "rows": [
                {
                    "id": "i1",
                    "type": "Movie",
                    "path": "/Media/Movie (2020).mkv",
                    "file": "Movie (2020).mkv",
                    "name": "Movie",
                    "series": "",
                    "display": "Movie (2020)",
                    "year": 2020,
                }
            ],
            "marks": set(),
        },
    )
    # Keep the tests off the real data/subtitles.json and free of cross-test bleed.
    monkeypatch.setattr(sub, "_save_state", lambda: None)
    monkeypatch.setitem(sub._state, "files", {})
    monkeypatch.setitem(sub._state, "ok", [])
    return media


class FakeOSClient:
    def __init__(self, results=None, fail=None, remaining=17):
        self.results = results or []
        self.fail = fail
        self._remaining = remaining
        self.searches = []
        self.downloads = []

    def remaining(self):
        return self._remaining

    async def search(self, **params):
        self.searches.append(params)
        if self.fail:
            raise OpenSubtitlesError(self.fail)
        return {"total_count": len(self.results), "data": self.results}

    async def download(self, file_id, sub_format="srt"):
        self.downloads.append((file_id, sub_format))
        if self.fail:
            raise OpenSubtitlesError(self.fail)
        return {
            "content": b"1\n00:00:01,000 --> 00:00:02,000\nhello\n",
            "file_name": "movie.en.srt",
            "remaining": 16,
        }


async def test_search_route_hash_then_results(os_env, monkeypatch):
    fake = FakeOSClient(results=[_candidate()])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_search(path="/Media/Movie (2020).mkv", language="", method="")
    data = _json(resp)
    assert data["ok"] is True
    assert data["method"] == "hash"
    assert data["total"] == 1
    assert data["candidates"][0]["file_id"] == 11
    assert fake.searches[0]["moviehash"]
    assert fake.searches[0]["languages"] == ["en", "sv"]


async def test_search_route_name_fallback(os_env, monkeypatch):
    fake = FakeOSClient(results=[])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_search(path="/Media/Movie (2020).mkv", language="", method="")
    data = _json(resp)
    assert data["ok"] is True
    assert data["method"] == "name"
    assert data["total"] == 0
    assert fake.searches[0].get("moviehash")
    assert fake.searches[1]["query"] == "Movie"
    assert fake.searches[1]["type"] == "movie"


async def test_search_route_hash_only_skips_the_name_fallback(os_env, monkeypatch):
    fake = FakeOSClient(results=[])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_search(
        path="/Media/Movie (2020).mkv", language="", method="hash"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["method"] == "hash"
    assert data["total"] == 0
    assert fake.searches[0]["moviehash"]
    assert len(fake.searches) == 1  # nothing else was tried


async def test_search_route_hash_needs_a_local_file(os_env, monkeypatch):
    fake = FakeOSClient(results=[_candidate()])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    jellyfin_id._state["rows"].append(
        {
            "id": "i2",
            "type": "Movie",
            "path": "/Media/Gone (2021).mkv",
            "file": "Gone (2021).mkv",
            "name": "Gone",
            "series": "",
            "display": "Gone (2021)",
            "year": 2021,
        }
    )
    resp = await sub.subtitles_search(
        path="/Media/Gone (2021).mkv", language="", method="hash"
    )
    assert resp.status_code == 400
    assert "not reachable" in _json(resp)["error"]
    assert not fake.searches


async def test_search_route_filename(os_env, monkeypatch):
    fake = FakeOSClient(results=[])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_search(
        path="/Media/Movie (2020).mkv", language="", method="filename"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["method"] == "filename"
    assert data["query"] == "Movie (2020)"
    assert fake.searches[0]["query"] == "Movie (2020)"
    assert fake.searches[0]["type"] == "movie"
    assert "moviehash" not in fake.searches[0]  # the hash is not used


async def test_search_route_jellyfin_name_uses_the_year(os_env, monkeypatch):
    fake = FakeOSClient(results=[_candidate()])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_search(
        path="/Media/Movie (2020).mkv", language="", method="jellyfin"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["method"] == "jellyfin"
    assert data["query"] == "Movie"
    assert fake.searches[0]["query"] == "Movie"
    assert fake.searches[0]["year"] == 2020
    assert fake.searches[0]["type"] == "movie"
    assert len(fake.searches) == 1  # only the method that was asked for


async def test_search_route_jellyfin_episode_searches_the_series(os_env, monkeypatch):
    fake = FakeOSClient(results=[])
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    jellyfin_id._state["rows"].append(
        {
            "id": "i3",
            "type": "Episode",
            "path": "/Media/Show/Show S01E03.mkv",
            "file": "Show S01E03.mkv",
            "name": "The Third One",
            "series": "Show",
            "display": "Show — The Third One",
            "year": 2022,
        }
    )
    resp = await sub.subtitles_search(
        path="/Media/Show/Show S01E03.mkv", language="", method="jellyfin"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert fake.searches[0]["query"] == "Show"
    assert fake.searches[0]["type"] == "episode"
    assert fake.searches[0]["season_number"] == 1
    assert fake.searches[0]["episode_number"] == 3
    assert not fake.searches[0].get("year")  # an episode year is the wrong year


async def test_search_route_rejects_an_unknown_method(os_env, monkeypatch):
    fake = FakeOSClient()
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_search(
        path="/Media/Movie (2020).mkv", language="", method="imdb"
    )
    assert resp.status_code == 400
    assert "Unknown search method" in _json(resp)["error"]
    assert not fake.searches


async def test_search_route_not_configured(os_env, monkeypatch):
    monkeypatch.setattr(config._settings, "opensubtitles_api_key", "")
    resp = await sub.subtitles_search(path="/Media/Movie (2020).mkv", language="", method="")
    data = _json(resp)
    assert data["ok"] is False
    assert data["configured"] is False


async def test_search_route_unknown_path(os_env):
    resp = await sub.subtitles_search(path="/Media/nope.mkv", language="", method="")
    assert resp.status_code == 404


async def test_download_route_requires_mgmt_and_header(os_env, monkeypatch):
    fake = FakeOSClient()
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)

    monkeypatch.setattr(config._settings, "file_mgmt", False)
    resp = await sub.subtitles_download(
        path="/Media/Movie (2020).mkv", file_id="11", sub_format="srt", language="en", overwrite=""
    )
    assert resp.status_code == 403
    assert "File management" in _json(resp)["error"]

    monkeypatch.setattr(config._settings, "file_mgmt", True)
    resp = await sub.subtitles_download(
        path="/Media/Movie (2020).mkv", file_id="11", sub_format="srt", language="en", overwrite=""
    )
    assert resp.status_code == 403
    assert "security header" in _json(resp)["error"]


async def test_download_route_saves_next_to_video(os_env, monkeypatch):
    import app.jellyfin.client as jf

    fake = FakeOSClient()
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    monkeypatch.setitem(sub._state, "ok", ["/Media/Movie (2020).mkv"])
    jf_fake = FakeJFClient()
    monkeypatch.setattr(jf, "get_client", lambda: jf_fake)

    resp = await sub.subtitles_download(
        path="/Media/Movie (2020).mkv", file_id="11", language="en", sub_format="srt", overwrite="", x_jftools="1"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["remaining"] == 16
    target = os_env / "Movie (2020).en.srt"
    assert target.exists()
    assert b"00:00:01" in target.read_bytes()
    assert fake.downloads == [(11, "srt")]
    # The download marks the item for a fresh scan and drops the "ok" mark.
    assert data["unscanned"] is True
    assert sub._state["files"]["/Media/Movie (2020).mkv"]["scanned"] is False
    assert "/Media/Movie (2020).mkv" not in sub._state["ok"]
    # Jellyfin is told a subtitle appeared.
    assert jf_fake.paths == [["/Media/Movie (2020).mkv"]]


async def test_download_route_refuses_overwrite_without_flag(os_env, monkeypatch):
    (os_env / "Movie (2020).en.srt").write_text("old", encoding="utf-8")
    fake = FakeOSClient()
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)

    resp = await sub.subtitles_download(
        path="/Media/Movie (2020).mkv", file_id="11", language="en", sub_format="srt", overwrite="", x_jftools="1"
    )
    assert resp.status_code == 409
    assert _json(resp)["exists"] is True
    assert not fake.downloads  # nothing was fetched

    resp = await sub.subtitles_download(
        path="/Media/Movie (2020).mkv", file_id="11", language="en", sub_format="srt", overwrite="1", x_jftools="1"
    )
    assert _json(resp)["ok"] is True
    assert b"hello" in (os_env / "Movie (2020).en.srt").read_bytes()


async def test_download_route_quota_error(os_env, monkeypatch):
    fake = FakeOSClient(fail="You cannot download more than 5 subtitles")
    monkeypatch.setattr(osmod, "get_opensubtitles", lambda: fake)
    resp = await sub.subtitles_download(
        path="/Media/Movie (2020).mkv", file_id="11", language="en", sub_format="srt", overwrite="", x_jftools="1"
    )
    assert resp.status_code == 502
    assert "more than 5" in _json(resp)["error"]


class FakeJFClient:
    def __init__(self):
        self.paths = []

    async def scan_paths(self, locations):
        self.paths.append(list(locations))


def test_mark_unscanned_keeps_internal(os_env, monkeypatch):
    monkeypatch.setitem(
        sub._state,
        "files",
        {
            "/Media/Movie (2020).mkv": {
                "path": "/Media/Movie (2020).mkv",
                "file": "Movie (2020).mkv",
                "scanned": True,
                "internal": [
                    {"lang": "English", "name": "English", "forced": False, "codec": "subrip"}
                ],
                "external": [{"file": "Movie (2020).en.srt", "lang": "English"}],
                "subs": ["English (Ext)"],
                "flags": {"acceptable": True},
            }
        },
    )
    monkeypatch.setitem(sub._state, "ok", ["/Media/Movie (2020).mkv"])

    sub._mark_unscanned("/Media/Movie (2020).mkv")

    entry = sub._state["files"]["/Media/Movie (2020).mkv"]
    assert entry["scanned"] is False
    assert entry["internal"][0]["name"] == "English"  # kept for the dialog
    assert entry["external"] == []
    assert entry["subs"] == []
    assert entry["flags"] is None
    assert "/Media/Movie (2020).mkv" not in sub._state["ok"]


async def test_delete_route_requires_gate(os_env, monkeypatch):
    monkeypatch.setattr(config._settings, "file_mgmt", False)
    resp = await sub.subtitles_delete(
        path="/Media/Movie (2020).mkv", file="x.srt", x_jftools="1"
    )
    assert resp.status_code == 403
    assert "File management" in _json(resp)["error"]

    monkeypatch.setattr(config._settings, "file_mgmt", True)
    resp = await sub.subtitles_delete(path="/Media/Movie (2020).mkv", file="x.srt")
    assert resp.status_code == 403
    assert "security header" in _json(resp)["error"]


async def test_delete_route_unknown_path(os_env):
    resp = await sub.subtitles_delete(
        path="/Media/nope.mkv", file="x.srt", x_jftools="1"
    )
    assert resp.status_code == 404


async def test_delete_route_refuses_foreign_file(os_env):
    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    (os_env / "other.srt").write_bytes(b"x" * 2000)
    resp = await sub.subtitles_delete(
        path="/Media/Movie (2020).mkv", file="other.srt", x_jftools="1"
    )
    assert resp.status_code == 403
    assert (os_env / "other.srt").exists()


async def test_delete_route_deletes_and_notifies(os_env, monkeypatch):
    import app.jellyfin.client as jf

    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    monkeypatch.setitem(sub._state, "ok", ["/Media/Movie (2020).mkv"])
    fake = FakeJFClient()
    monkeypatch.setattr(jf, "get_client", lambda: fake)

    resp = await sub.subtitles_delete(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt", x_jftools="1"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["jellyfin"] is None
    assert not (os_env / "Movie (2020).en.srt").exists()
    # Marked for a fresh scan and no longer "ok".
    assert sub._state["files"]["/Media/Movie (2020).mkv"]["scanned"] is False
    assert "/Media/Movie (2020).mkv" not in sub._state["ok"]
    # Jellyfin was told the file changed.
    assert fake.paths == [["/Media/Movie (2020).mkv"]]


# ---- rename ------------------------------------------------------------------


async def test_rename_route_requires_gate(os_env, monkeypatch):
    monkeypatch.setattr(config._settings, "file_mgmt", False)
    resp = await sub.subtitles_rename(
        path="/Media/Movie (2020).mkv", file="x.srt", language="sv", x_jftools="1"
    )
    assert resp.status_code == 403
    assert "File management" in _json(resp)["error"]

    monkeypatch.setattr(config._settings, "file_mgmt", True)
    resp = await sub.subtitles_rename(
        path="/Media/Movie (2020).mkv", file="x.srt", language="sv"
    )
    assert resp.status_code == 403
    assert "security header" in _json(resp)["error"]


async def test_rename_route_refuses_foreign_file(os_env):
    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    (os_env / "other.srt").write_bytes(b"x" * 2000)
    resp = await sub.subtitles_rename(
        path="/Media/Movie (2020).mkv", file="other.srt", language="sv", x_jftools="1"
    )
    assert resp.status_code == 403


async def test_rename_route_renames_and_notifies(os_env, monkeypatch):
    import app.jellyfin.client as jf

    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    monkeypatch.setitem(sub._state, "ok", ["/Media/Movie (2020).mkv"])
    fake = FakeJFClient()
    monkeypatch.setattr(jf, "get_client", lambda: fake)

    resp = await sub.subtitles_rename(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt",
        language="Swedish", x_jftools="1",
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["file"] == "Movie (2020).sv.srt"
    assert "Renamed" in data["message"]
    assert (os_env / "Movie (2020).sv.srt").exists()
    assert not (os_env / "Movie (2020).en.srt").exists()
    # Marked for a fresh scan and no longer "ok".
    assert sub._state["files"]["/Media/Movie (2020).mkv"]["scanned"] is False
    assert "/Media/Movie (2020).mkv" not in sub._state["ok"]
    # Jellyfin was told the file changed.
    assert fake.paths == [["/Media/Movie (2020).mkv"]]


async def test_rename_route_refuses_an_existing_target(os_env):
    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    (os_env / "Movie (2020).sv.srt").write_bytes(b"y" * 2000)
    resp = await sub.subtitles_rename(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt",
        language="sv", x_jftools="1",
    )
    assert resp.status_code == 409
    assert (os_env / "Movie (2020).en.srt").exists()


async def test_rename_route_reports_an_unchanged_name(os_env):
    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    resp = await sub.subtitles_rename(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt",
        language="en", x_jftools="1",
    )
    data = _json(resp)
    assert data["ok"] is True
    assert "Already named" in data["message"]


# ---- sync -------------------------------------------------------------------


async def test_sync_route_requires_gate(os_env, monkeypatch):
    monkeypatch.setattr(config._settings, "file_mgmt", False)
    resp = await sub.subtitles_sync(
        path="/Media/Movie (2020).mkv", file="x.srt", x_jftools="1"
    )
    assert resp.status_code == 403
    assert "File management" in _json(resp)["error"]

    monkeypatch.setattr(config._settings, "file_mgmt", True)
    resp = await sub.subtitles_sync(path="/Media/Movie (2020).mkv", file="x.srt")
    assert resp.status_code == 403
    assert "security header" in _json(resp)["error"]


async def test_sync_route_refuses_foreign_file(os_env, monkeypatch):
    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    (os_env / "other.srt").write_bytes(b"x" * 2000)
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: "ffsubsync")
    resp = await sub.subtitles_sync(
        path="/Media/Movie (2020).mkv", file="other.srt", x_jftools="1"
    )
    assert resp.status_code == 403


async def test_sync_route_needs_ffsubsync(os_env, monkeypatch):
    (os_env / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: None)
    resp = await sub.subtitles_sync(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt", x_jftools="1"
    )
    assert resp.status_code == 400
    assert "ffsubsync" in _json(resp)["error"]


async def test_sync_route_rejects_bad_offset(os_env):
    (os_env / "Movie (2020).en.srt").write_bytes(b"old")
    resp = await sub.subtitles_sync(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt",
        offset="abc", x_jftools="1",
    )
    assert resp.status_code == 400
    assert "offset" in _json(resp)["error"]


async def test_sync_route_rewrites_file(os_env, monkeypatch):
    import app.jellyfin.client as jf

    (os_env / "Movie (2020).en.srt").write_bytes(b"old")
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setitem(sub._state, "ok", ["/Media/Movie (2020).mkv"])
    monkeypatch.setitem(
        sub._state,
        "files",
        {
            "/Media/Movie (2020).mkv": {
                "path": "/Media/Movie (2020).mkv",
                "scanned": True,
            }
        },
    )
    fake = FakeJFClient()
    monkeypatch.setattr(jf, "get_client", lambda: fake)

    def fake_sync(video, subtitle, offset):
        with open(subtitle, "wb") as fh:
            fh.write(b"synced")
        return True, None, "ffsubsync"

    monkeypatch.setattr(sub, "_sync_subtitle_file", fake_sync)

    resp = await sub.subtitles_sync(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt", x_jftools="1"
    )
    data = _json(resp)
    assert data["ok"] is True
    assert data["method"] == "ffsubsync"
    assert "Synced" in data["message"]
    assert "jellyfin" not in data
    assert (os_env / "Movie (2020).en.srt").read_bytes() == b"synced"
    # A sync only changes the timings: no rescan mark, no Jellyfin call, "ok" kept.
    assert sub._state["files"]["/Media/Movie (2020).mkv"]["scanned"] is True
    assert "/Media/Movie (2020).mkv" in sub._state["ok"]
    assert fake.paths == []


async def test_sync_route_reports_failure(os_env, monkeypatch):
    (os_env / "Movie (2020).en.srt").write_bytes(b"old")
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(
        sub,
        "_sync_subtitle_file",
        lambda video, subtitle, offset: (False, "ffsubsync: boom", "ffsubsync"),
    )
    resp = await sub.subtitles_sync(
        path="/Media/Movie (2020).mkv", file="Movie (2020).en.srt", x_jftools="1"
    )
    assert resp.status_code == 502
    assert "boom" in _json(resp)["error"]
    # A failed sync leaves the original untouched.
    assert (os_env / "Movie (2020).en.srt").read_bytes() == b"old"


# ---- _sync_subtitle_file ----------------------------------------------------


class _RunResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_sync_subtitle_file_ffsubsync(tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"v")
    sub_file = tmp_path / "Movie.en.srt"
    sub_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: "C:/tools/ffsubsync.exe")
    monkeypatch.setattr(sub, "resolve_ffmpeg", lambda: None)

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("new", encoding="utf-8")
        return _RunResult()

    monkeypatch.setattr(sub.subprocess, "run", fake_run)

    ok, err, method = sub._sync_subtitle_file(str(video), str(sub_file), None)
    assert ok is True and err is None
    assert method == "ffsubsync"
    assert sub_file.read_text(encoding="utf-8") == "new"
    assert seen["cmd"][0] == "C:/tools/ffsubsync.exe"
    assert seen["cmd"][-1] == str(video)
    # The temp file is gone.
    assert not (tmp_path / ".Movie.en.jfsync.srt").exists()


def test_sync_subtitle_file_passes_ffmpeg_to_ffsubsync(tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"v")
    sub_file = tmp_path / "Movie.en.srt"
    sub_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(sub, "resolve_ffmpeg", lambda: "C:/tools/ffmpeg.exe")

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("new", encoding="utf-8")
        return _RunResult()

    monkeypatch.setattr(sub.subprocess, "run", fake_run)

    ok, _err, _method = sub._sync_subtitle_file(str(video), str(sub_file), None)
    assert ok is True
    assert "--ffmpeg-path" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--ffmpeg-path") + 1] == "C:/tools/ffmpeg.exe"


def test_sync_subtitle_file_ffmpeg_offset(tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"v")
    sub_file = tmp_path / "Movie.en.srt"
    sub_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sub, "resolve_ffmpeg", lambda: "C:/tools/ffmpeg.exe")

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_text("shifted", encoding="utf-8")
        return _RunResult()

    monkeypatch.setattr(sub.subprocess, "run", fake_run)

    ok, err, method = sub._sync_subtitle_file(str(video), str(sub_file), 2.5)
    assert ok is True and err is None and method == "ffmpeg"
    assert sub_file.read_text(encoding="utf-8") == "shifted"
    assert "-itsoffset" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-itsoffset") + 1] == "2.5"


def test_sync_subtitle_file_ffmpeg_offset_rejects_ass(tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"v")
    sub_file = tmp_path / "Movie.en.ass"
    sub_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sub, "resolve_ffmpeg", lambda: "C:/tools/ffmpeg.exe")
    ok, err, _method = sub._sync_subtitle_file(str(video), str(sub_file), 1.0)
    assert ok is False
    assert ".srt" in err


def test_sync_subtitle_file_reports_tool_error(tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"v")
    sub_file = tmp_path / "Movie.en.srt"
    sub_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: "ffsubsync")
    monkeypatch.setattr(sub, "resolve_ffmpeg", lambda: None)
    monkeypatch.setattr(
        sub.subprocess,
        "run",
        lambda cmd, **kwargs: _RunResult(returncode=1, stderr="line one\nfatal: nope"),
    )
    ok, err, _method = sub._sync_subtitle_file(str(video), str(sub_file), None)
    assert ok is False
    assert "nope" in err
    # The original is untouched and no temp file is left behind.
    assert sub_file.read_text(encoding="utf-8") == "old"
    assert not (tmp_path / ".Movie.en.jfsync.srt").exists()


def test_sync_subtitle_file_needs_ffsubsync(tmp_path, monkeypatch):
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"v")
    sub_file = tmp_path / "Movie.en.srt"
    sub_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sub, "resolve_ffsubsync", lambda: None)
    ok, err, method = sub._sync_subtitle_file(str(video), str(sub_file), None)
    assert ok is False
    assert method == "none"
    assert "ffsubsync" in err
