import pytest

from app.paths import resolve_path


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


def test_windows_output_separators(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: True)
    maps = [{"from": "/Filmer", "to": "Z:\\Filmer"}]
    assert resolve_path("/Filmer/Movie/a.mkv", maps) == "Z:\\Filmer\\Movie\\a.mkv"


def test_windows_case_insensitive(monkeypatch):
    monkeypatch.setattr("app.paths._is_windows", lambda: True)
    maps = [{"from": "/filmer", "to": "Z:\\Filmer"}]
    assert resolve_path("/Filmer/Movie/a.mkv", maps) == "Z:\\Filmer\\Movie\\a.mkv"
