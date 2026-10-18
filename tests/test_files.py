"""File browser: root confinement, listing, traversal and symlink escapes."""

import json
import os

import pytest

import app.libraries as libraries_mod
from app.config import config
from app.files import api_files_list, api_files_roots, browser_roots


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    """A fake media tree exposed through path maps and a library location."""
    media = tmp_path / "Media"
    movies = media / "Movies"
    movies.mkdir(parents=True)
    (movies / "A (2020).mkv").write_bytes(b"x" * 100)
    (movies / "notes.txt").write_text("hi", encoding="utf-8")
    (media / "docs").mkdir()
    (media / "docs" / "readme.txt").write_text("r", encoding="utf-8")
    monkeypatch.setattr(config._settings, "path_maps", [{"from": "/Media", "to": str(media)}])
    monkeypatch.setattr(libraries_mod, "_state", {"libraries": [{"locations": ["/Media"]}], "recent": []})
    return media


def _json(resp):
    return json.loads(resp.body)


def test_browser_roots_combine_maps_and_libraries(media_env):
    roots = browser_roots()
    assert os.path.realpath(str(media_env)) in roots
    # the app dir and data dir must never be roots
    from app.web import BASE_DIR

    assert os.path.realpath(str(BASE_DIR)) not in roots


async def test_list_directory(media_env):
    resp = await api_files_list(path=str(media_env / "Movies"))
    data = _json(resp)
    assert data["ok"] is True
    assert [e["name"] for e in data["entries"]] == ["A (2020).mkv", "notes.txt"]
    assert data["entries"][0]["dir"] is False
    assert data["parent"] == os.path.realpath(str(media_env))
    assert data["usage"]["total"] > 0
    assert isinstance(data["writable"], bool)
    assert data["truncated"] is False


async def test_dirs_first_and_crumb(media_env):
    resp = await api_files_list(path=str(media_env))
    data = _json(resp)
    assert [e["dir"] for e in data["entries"]] == [True, True]
    assert data["entries"][0]["name"] == "docs"
    assert data["entries"][1]["name"] == "Movies"
    assert data["crumb"][-1][1] == "Media"
    # the parent (the pytest temp dir) is outside the roots
    assert data["parent"] is None


async def test_list_file_highlights_in_parent(media_env):
    resp = await api_files_list(path=str(media_env / "Movies" / "A (2020).mkv"))
    data = _json(resp)
    assert data["ok"] is True
    assert data["highlight"] == "A (2020).mkv"
    assert "A (2020).mkv" in [e["name"] for e in data["entries"]]


async def test_jellyfin_path_resolved_through_maps(media_env):
    resp = await api_files_list(path="/Media/Movies")
    data = _json(resp)
    assert data["ok"] is True
    assert data["entries"][0]["name"] == "A (2020).mkv"


async def test_outside_root_is_forbidden(media_env, tmp_path):
    resp = await api_files_list(path=str(tmp_path))
    assert resp.status_code == 403
    resp = await api_files_list(path=str(media_env / ".."))
    assert resp.status_code == 403


async def test_missing_path_404(media_env):
    resp = await api_files_list(path=str(media_env / "nope"))
    assert resp.status_code == 404


async def test_symlink_escape_is_forbidden(media_env, tmp_path):
    link = media_env / "Movies" / "escape"
    try:
        os.symlink(str(tmp_path / "secret"), str(link))
    except OSError:
        pytest.skip("symlinks not supported here")
    resp = await api_files_list(path=str(link))
    assert resp.status_code == 403


async def test_symlink_shows_as_plain_entry(media_env):
    link = media_env / "Movies" / "escape"
    try:
        os.symlink(str(media_env / "docs"), str(link))
    except OSError:
        pytest.skip("symlinks not supported here")
    resp = await api_files_list(path=str(media_env / "Movies"))
    data = _json(resp)
    entry = next(e for e in data["entries"] if e["name"] == "escape")
    assert entry["dir"] is False


async def test_no_roots_returns_pick(tmp_path, monkeypatch):
    monkeypatch.setattr(config._settings, "path_maps", [])
    monkeypatch.setattr(libraries_mod, "_state", {"libraries": [{"locations": []}], "recent": []})
    resp = await api_files_list(path=str(tmp_path))
    data = _json(resp)
    assert data["ok"] is True
    assert data.get("pick") is True
    assert data["roots"] == []


async def test_roots_endpoint(media_env):
    resp = await api_files_roots()
    data = _json(resp)
    assert any(r["path"] == os.path.realpath(str(media_env)) for r in data["roots"])


def test_app_dir_never_a_root(media_env, monkeypatch):
    from app.web import BASE_DIR

    monkeypatch.setattr(config._settings, "path_maps", [{"from": "/x", "to": str(BASE_DIR)}])
    roots = browser_roots()
    assert os.path.realpath(str(BASE_DIR)) not in roots
