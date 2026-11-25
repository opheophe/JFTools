import httpx
import pytest

from app.config import config
from app.main import app
from app.paths import display_path, folder_of, resolve_local_path, resolve_path
from app.web import require_login


@pytest.fixture
def posix(monkeypatch):
    """Force POSIX output separators regardless of the host OS."""
    monkeypatch.setattr("app.paths._is_windows", lambda: False)


def test_no_maps_returns_input(posix):
    assert resolve_path("/Filmer/Movie/a.mkv", None) == "/Filmer/Movie/a.mkv"
    assert resolve_path("/Filmer/Movie/a.mkv", []) == "/Filmer/Movie/a.mkv"


def test_prefix_replacement(posix):
    maps = [{"from": "/Filmer", "to": "/media/Filmer"}]
    assert resolve_path("/Filmer/Movie/a.mkv", maps) == "/media/Filmer/Movie/a.mkv"
    assert resolve_path("/Filmer", maps) == "/media/Filmer"


def test_longest_prefix_wins(posix):
    maps = [
        {"from": "/Filmer", "to": "/media/all"},
        {"from": "/Filmer/Sub", "to": "/media/sub"},
    ]
    assert resolve_path("/Filmer/Sub/x.mkv", maps) == "/media/sub/x.mkv"
    assert resolve_path("/Filmer/Other/x.mkv", maps) == "/media/all/Other/x.mkv"


def test_path_boundary_is_respected(posix):
    maps = [{"from": "/Filmer", "to": "/media/Filmer"}]
    assert resolve_path("/Filmers/x.mkv", maps) == "/Filmers/x.mkv"


def test_no_match_returns_input(posix):
    maps = [{"from": "/Filmer", "to": "/media/Filmer"}]
    assert resolve_path("/Ero/x.mkv", maps) == "/Ero/x.mkv"


def test_backslash_input_is_normalised(posix):
    maps = [{"from": "/Filmer", "to": "/media/Filmer"}]
    assert resolve_path("\\Filmer\\Movie\\a.mkv", maps) == "/media/Filmer/Movie/a.mkv"


def test_skips_invalid_entries(posix):
    maps = [
        {"from": "", "to": "/x"},
        {"from": "/Filmer", "to": ""},
        "garbage",
        {"from": "/Filmer", "to": "/media/Filmer"},
    ]
    assert resolve_path("/Filmer/a.mkv", maps) == "/media/Filmer/a.mkv"


def test_local_path_requires_a_local_mapping(posix):
    maps = [{"from": "/Filmer", "to": "/media/Filmer"}]
    assert resolve_local_path("/Filmer/Movie/a.mkv", maps) == ""
    assert resolve_local_path("/Filmer/Movie/a.mkv", None) == ""


def test_local_path_uses_the_local_prefix(posix):
    maps = [{"from": "/Filmer", "to": "/media/Filmer", "local": "//NAS/Media/Filmer"}]
    assert (
        resolve_local_path("/Filmer/Movie/a.mkv", maps)
        == "//NAS/Media/Filmer/Movie/a.mkv"
    )


def test_local_path_longest_prefix_with_a_local(posix):
    maps = [
        {"from": "/Filmer", "to": "/media/all", "local": "//NAS/Media/Filmer"},
        {"from": "/Filmer/Sub", "to": "/media/sub"},
    ]
    # The more specific mapping has no local path, so the broader one maps it.
    assert (
        resolve_local_path("/Filmer/Sub/x.mkv", maps)
        == "//NAS/Media/Filmer/Sub/x.mkv"
    )


def test_local_path_windows_separators(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: True)
    maps = [
        {"from": "/Filmer", "to": "Z:\\Filmer", "local": "\\\\NAS\\Media\\Filmer"}
    ]
    assert (
        resolve_local_path("/Filmer/Movie/a.mkv", maps)
        == "\\\\NAS\\Media\\Filmer\\Movie\\a.mkv"
    )


async def test_settings_test_button_probes_the_local_path(tmp_path):
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/paths/test",
                json={"path": "/Filmer", "to": str(tmp_path), "local": str(tmp_path)},
            )
    finally:
        app.dependency_overrides.pop(require_login, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["local"]["resolved"] == str(tmp_path)
    assert data["local"]["exists"] is True


def test_windows_output_separators(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: True)
    maps = [{"from": "/Filmer", "to": "Z:\\Filmer"}]
    assert resolve_path("/Filmer/Movie/a.mkv", maps) == "Z:\\Filmer\\Movie\\a.mkv"


def test_windows_case_insensitive(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: True)
    maps = [{"from": "/filmer", "to": "Z:\\Filmer"}]
    assert resolve_path("/Filmer/Movie/a.mkv", maps) == "Z:\\Filmer\\Movie\\a.mkv"


def test_folder_of_strips_the_file():
    assert folder_of("/Filmer/Movie/a.mkv") == "/Filmer/Movie"
    assert folder_of("/a.mkv") == "/"
    assert folder_of("C:\\Filmer\\Movie\\a.mkv") == "C:\\Filmer\\Movie"
    assert folder_of("C:\\a.mkv") == "C:\\"
    assert folder_of("\\\\NAS\\Media\\Movie\\a.mkv") == "\\\\NAS\\Media\\Movie"


def test_folder_of_keeps_a_folder():
    assert folder_of("/Filmer/Show/", is_dir=True) == "/Filmer/Show"
    assert folder_of("\\\\NAS\\Media\\Show", is_dir=True) == "\\\\NAS\\Media\\Show"


def test_display_path_uses_windows_separators_for_windows_paths():
    assert display_path("//NAS/Media/Movie") == "\\\\NAS\\Media\\Movie"
    assert display_path("Z:/Filmer/Movie") == "Z:\\Filmer\\Movie"
    assert display_path("/media/Filmer/Movie") == "/media/Filmer/Movie"
    assert display_path("") == ""


async def test_folder_endpoint_returns_a_files_parent(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: False)
    monkeypatch.setattr(
        config._settings, "path_maps", [{"from": "/Filmer", "to": "/media/Filmer"}]
    )
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/paths/folder", params={"path": "/Filmer/Movie/a.mkv"}
            )
    finally:
        app.dependency_overrides.pop(require_login, None)

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "folder": "/media/Filmer/Movie",
        "local_folder": "",
        "mapped": True,
    }


async def test_folder_endpoint_keeps_a_series_folder(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: False)
    monkeypatch.setattr(
        config._settings, "path_maps", [{"from": "/Filmer", "to": "/media/Filmer"}]
    )
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/paths/folder",
                params={"path": "/Filmer/Show", "is_dir": "true"},
            )
    finally:
        app.dependency_overrides.pop(require_login, None)

    assert resp.status_code == 200
    assert resp.json()["folder"] == "/media/Filmer/Show"


async def test_folder_endpoint_returns_the_local_path(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: False)
    monkeypatch.setattr(
        config._settings,
        "path_maps",
        [
            {
                "from": "/Filmer",
                "to": "/media/Filmer",
                "local": "\\\\NAS\\Media\\Filmer",
            }
        ],
    )
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/paths/folder", params={"path": "/Filmer/Movie/a.mkv"}
            )
    finally:
        app.dependency_overrides.pop(require_login, None)

    data = resp.json()
    assert data["folder"] == "/media/Filmer/Movie"
    assert data["local_folder"] == "\\\\NAS\\Media\\Filmer\\Movie"
    assert data["mapped"] is True


async def test_folder_endpoint_flags_an_unmapped_path(monkeypatch):
    monkeypatch.setattr(config._settings, "path_maps", [])
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/paths/folder", params={"path": "/Filmer/Movie/a.mkv"}
            )
    finally:
        app.dependency_overrides.pop(require_login, None)

    data = resp.json()
    assert data["folder"] == "/Filmer/Movie"
    assert data["mapped"] is False


async def test_folder_endpoint_rejects_an_empty_path():
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/paths/folder", params={"path": "  "})
    finally:
        app.dependency_overrides.pop(require_login, None)

    assert resp.status_code == 400
    assert resp.json()["ok"] is False


async def test_folder_endpoint_requires_login():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/paths/folder", params={"path": "/Filmer"})

    assert resp.status_code == 401
