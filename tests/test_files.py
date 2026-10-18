"""File browser: root confinement, listing, traversal and symlink escapes."""

import json
import os

import pytest

import app.libraries as libraries_mod
from app.config import config
from app.files import (
    api_files_delete,
    api_files_list,
    api_files_roots,
    browser_roots,
    delete_entry,
    mkdir_entry,
    move_entry,
    rename_entry,
)


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
    monkeypatch.setattr(config._settings, "file_mgmt", False)
    monkeypatch.setattr(libraries_mod, "_state", {"libraries": [{"locations": ["/Media"]}], "recent": []})
    return media


@pytest.fixture
def mgmt_env(media_env, monkeypatch):
    """The media fixture with file management enabled."""
    monkeypatch.setattr(config._settings, "file_mgmt", True)
    return media_env


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


async def test_list_includes_mgmt_flag(media_env, monkeypatch):
    monkeypatch.setattr(config._settings, "file_mgmt", True)
    data = _json(await api_files_list(path=str(media_env / "Movies")))
    assert data["mgmt"] is True


# ---- mutations ---------------------------------------------------------------


def test_rename_file(mgmt_env):
    src = str(mgmt_env / "Movies" / "notes.txt")
    result = rename_entry(src, "notes2.txt", browser_roots())
    assert result["ok"] is True
    assert (mgmt_env / "Movies" / "notes2.txt").exists()
    assert not (mgmt_env / "Movies" / "notes.txt").exists()


def test_rename_invalid_name(mgmt_env):
    src = str(mgmt_env / "Movies" / "notes.txt")
    assert rename_entry(src, "../evil", browser_roots())["ok"] is False
    assert rename_entry(src, "a/b", browser_roots())["ok"] is False


def test_rename_collision(mgmt_env):
    src = str(mgmt_env / "Movies" / "notes.txt")
    assert rename_entry(src, "A (2020).mkv", browser_roots())["ok"] is False


def test_move_file(mgmt_env):
    src = str(mgmt_env / "Movies" / "notes.txt")
    result = move_entry(src, str(mgmt_env / "docs"), browser_roots())
    assert result["ok"] is True
    assert (mgmt_env / "docs" / "notes.txt").exists()
    assert not (mgmt_env / "Movies" / "notes.txt").exists()


def test_move_folder_into_itself_refused(mgmt_env):
    (mgmt_env / "docs" / "sub").mkdir()
    folder = str(mgmt_env / "docs")
    assert move_entry(folder, folder, browser_roots())["ok"] is False
    assert move_entry(folder, str(mgmt_env / "docs" / "sub"), browser_roots())["ok"] is False


def test_delete_file_and_folder(mgmt_env):
    assert delete_entry(str(mgmt_env / "Movies" / "notes.txt"), browser_roots())["ok"] is True
    assert not (mgmt_env / "Movies" / "notes.txt").exists()
    assert delete_entry(str(mgmt_env / "docs"), browser_roots())["ok"] is True
    assert not (mgmt_env / "docs").exists()


def test_delete_root_refused(mgmt_env):
    assert delete_entry(str(mgmt_env), browser_roots())["ok"] is False
    assert mgmt_env.exists()


def test_mkdir(mgmt_env):
    result = mkdir_entry(str(mgmt_env / "Movies"), "Subs", browser_roots())
    assert result["ok"] is True
    assert (mgmt_env / "Movies" / "Subs").is_dir()


def test_mkdir_bad_name(mgmt_env):
    assert mkdir_entry(str(mgmt_env / "Movies"), "a/b", browser_roots())["ok"] is False
    assert mkdir_entry(str(mgmt_env / "Movies"), ".", browser_roots())["ok"] is False


def test_mutation_outside_root_refused(mgmt_env, tmp_path):
    roots = browser_roots()
    assert delete_entry(str(tmp_path), roots)["ok"] is False
    assert rename_entry(str(tmp_path / "x"), "y", roots)["ok"] is False
    assert mkdir_entry(str(tmp_path), "x", roots)["ok"] is False


def test_symlinked_parent_escape_refused(mgmt_env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    link = mgmt_env / "Movies" / "link"
    try:
        os.symlink(str(outside), str(link))
    except OSError:
        pytest.skip("symlinks not supported here")
    # the file's parent is a symlink pointing outside -> refused
    result = delete_entry(str(link / "secret.txt"), browser_roots())
    assert result["ok"] is False
    assert (outside / "secret.txt").exists()


# ---- endpoint gates ----------------------------------------------------------


def test_mutation_gate_disabled(media_env):
    resp = api_files_delete(path=str(media_env / "Movies" / "notes.txt"), x_jftools="1")
    assert resp.status_code == 403
    assert (media_env / "Movies" / "notes.txt").exists()


def test_mutation_requires_csrf_header(mgmt_env):
    resp = api_files_delete(path=str(mgmt_env / "Movies" / "notes.txt"))
    assert resp.status_code == 403
    assert (mgmt_env / "Movies" / "notes.txt").exists()


def test_mutation_endpoint_ok(mgmt_env):
    resp = api_files_delete(path=str(mgmt_env / "Movies" / "notes.txt"), x_jftools="1")
    assert resp.status_code == 200
    assert json.loads(resp.body)["ok"] is True
    assert not (mgmt_env / "Movies" / "notes.txt").exists()
