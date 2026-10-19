"""Libraries page: the library of each item, and the row action pills."""
import httpx
import pytest

import app.libraries as libraries_mod
from app.libraries import _build_recent, _library_locations
from app.main import app
from app.web import require_login, templates


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def logged_in():
    app.dependency_overrides[require_login] = lambda: "tester"
    yield
    app.dependency_overrides.pop(require_login, None)


class FakeClient:
    """Stands in for JellyfinClient's /Items call."""

    def __init__(self, items):
        self.items = items
        self.limit = None

    async def get_recent(self, limit=50):
        self.limit = limit
        return {"Items": self.items}


LIBRARIES = [
    {"name": "Filmer", "locations": ["/Filmer"]},
    {"name": "Serier", "locations": ["/Serier"]},
    # Nested inside /Filmer: the longest location prefix must win.
    {"name": "Filmer/Kids", "locations": ["/Filmer/Kids"]},
]


async def test_build_recent_labels_each_item_with_its_library():
    client = FakeClient([
        {
            "Id": "m1", "Name": "Dune", "Type": "Movie", "ProductionYear": 2021,
            "DateCreated": "2026-10-01T12:00:00.0000000Z",
            "Path": "/Filmer/Dune (2021)/Dune 2021 1080p.mkv",
        },
        {
            "Id": "e1", "Name": "Pilot", "Type": "Episode", "SeriesName": "Some Show",
            "ProductionYear": 2019, "DateCreated": "2026-10-02T12:00:00.0000000Z",
            "Path": "/Serier/Some Show/Season 1/Some Show - S01E01.mkv",
        },
        {
            "Id": "k1", "Name": "Tiny", "Type": "Movie",
            "DateCreated": "2026-10-03T12:00:00.0000000Z",
            "Path": "/Filmer/Kids/Tiny (2015)/Tiny.mkv",
        },
        # No Path: Jellyfin would leave the file fields empty, and the row must
        # still render (its pills come out disabled rather than crashing).
        {"Id": "u1", "Name": "Unknown", "Type": "Movie"},
    ])

    rows = await _build_recent(client, _library_locations(LIBRARIES))

    assert client.limit == 50
    assert [r["library"] for r in rows] == ["Filmer", "Serier", "Filmer/Kids", ""]
    assert [r["added"] for r in rows] == ["2026-10-01", "2026-10-02", "2026-10-03", ""]

    movie, episode, _kid, unknown = rows
    assert movie["file"] == "Dune 2021 1080p.mkv"
    assert movie["path"] == "/Filmer/Dune (2021)/Dune 2021 1080p.mkv"
    assert movie["series"] == ""

    assert episode["name"] == "Some Show — Pilot"
    assert episode["series"] == "Some Show"
    assert episode["file"] == "Some Show - S01E01.mkv"

    assert unknown["path"] == ""
    assert unknown["file"] == ""


async def test_library_locations_ignores_libraries_without_locations():
    assert _library_locations([{"name": "Empty", "locations": []}, {"name": "B"}]) == []


def test_latest_additions_table_lists_library_then_actions():
    """The two columns added for the row pills: Library between Added and Title,
    and Actions last."""
    html = templates.env.get_template("libraries.html").render()
    assert "<tr><th>Added</th><th>Library</th><th>Title</th><th>Type</th><th>Year</th><th>Actions</th></tr>" in html
    # The empty/loading row spans the six columns now.
    assert 'id="recent-body"><tr><td colspan="6"' in html
    # The Subtitles pill opens the subtitle dialog, which lives in subdl.js.
    assert "/static/subdl.js" in html


@pytest.mark.parametrize("path,expected", [
    ("/Filmer/Dune (2021)/Dune.mkv", "Filmer"),
    ("/Filmer/Kids/Tiny/Tiny.mkv", "Filmer/Kids"),
    ("/Serier/Some Show/Season 1/e.mkv", "Serier"),
    ("/Elsewhere/x.mkv", ""),
    ("", ""),
])
def test_library_for_path_uses_the_longest_prefix(path, expected):
    from app.jellyfin_id import library_for_path

    locations = _library_locations(LIBRARIES)
    assert library_for_path(path, locations) == expected


def _stub_scheduler(monkeypatch, triggered):
    """Record which operation the page asks the scheduler to run."""
    import app.scheduler as scheduler_mod

    def trigger_operation(op):
        triggered.append(op)
        return True, None

    monkeypatch.setattr(scheduler_mod, "trigger_operation", trigger_operation)


async def test_recent_cache_from_an_older_version_is_refilled(monkeypatch, logged_in):
    """A cache whose rows have no path (written before the row actions existed)
    is stale: the Library column and the pills need the refresh."""
    triggered = []
    _stub_scheduler(monkeypatch, triggered)
    monkeypatch.setattr(
        libraries_mod, "_state",
        {"libraries": [], "recent": [{"id": "m1", "name": "Dune"}], "last_sync": 1.0},
    )
    monkeypatch.setattr(libraries_mod, "_fetch", {"status": "idle", "error": None})

    async with _client() as client:
        resp = await client.get("/api/libraries")

    assert resp.status_code == 200
    assert triggered == ["refresh_libraries"]


async def test_recent_cache_with_paths_is_not_refilled(monkeypatch, logged_in):
    triggered = []
    _stub_scheduler(monkeypatch, triggered)
    row = {"id": "m1", "name": "Dune", "path": "/Filmer/Dune.mkv"}
    monkeypatch.setattr(
        libraries_mod, "_state",
        {"libraries": [], "recent": [row], "last_sync": 1.0},
    )
    monkeypatch.setattr(libraries_mod, "_fetch", {"status": "idle", "error": None})

    async with _client() as client:
        resp = await client.get("/api/libraries")

    assert resp.status_code == 200
    assert triggered == []
    assert resp.json()["recent"] == [row]
