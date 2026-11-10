import httpx
import pytest

from app.main import app
from app.paths import resolve_local_path, resolve_path
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
