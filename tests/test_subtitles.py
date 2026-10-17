"""Tests for the subtitle-scan classification and store helpers."""

from types import SimpleNamespace

import pytest

from app.subtitles import (
    _classify_tracks,
    _external_subtitles,
    _needs_scan,
    _prune_files,
    _subtitle_lang_name,
    resolve_ffprobe,
)


def _int(lang, forced=False, codec="subrip"):
    return {"lang": lang, "name": lang, "forced": forced, "codec": codec}


def _ext(lang="English", forced=False, ai=False, tiny=False, file="x.srt"):
    return {"file": file, "lang": lang, "forced": forced, "ai": ai, "tiny": tiny}


# ---- language names ---------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("en", "English"),
        ("eng", "English"),
        ("english", "English"),
        ("swe", "Swedish"),
        ("und", "Unknown"),
        ("xyz", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_subtitle_lang_name(code, expected):
    assert _subtitle_lang_name(code) == expected


# ---- classification ---------------------------------------------------------


def test_classify_nothing_is_nosubs():
    flags = _classify_tracks([], [])
    assert flags == {
        "acceptable": False,
        "ai": False,
        "forced": False,
        "foreign": False,
        "nosubs": True,
    }


def test_classify_internal_english_acceptable():
    flags = _classify_tracks([_int("English")], [])
    assert flags["acceptable"] is True
    assert flags["nosubs"] is False


def test_classify_external_english_acceptable():
    flags = _classify_tracks([], [_ext("English")])
    assert flags["acceptable"] is True


def test_classify_forced_not_acceptable():
    flags = _classify_tracks([_int("English", forced=True)], [])
    assert flags["forced"] is True
    assert flags["acceptable"] is False


def test_classify_foreign():
    flags = _classify_tracks([], [_ext("French")])
    assert flags["foreign"] is True
    assert flags["acceptable"] is False


def test_classify_ai_is_not_acceptable():
    flags = _classify_tracks([], [_ext("English", ai=True, file="x.english_ai.srt")])
    assert flags["ai"] is True
    assert flags["acceptable"] is False


def test_classify_tiny_ignored():
    flags = _classify_tracks([], [_ext("English", tiny=True)])
    assert flags["nosubs"] is True


# ---- external subtitle detection -------------------------------------------


def test_external_subtitles(tmp_path):
    movie = tmp_path / "Movie (2020).mkv"
    movie.write_bytes(b"")
    (tmp_path / "Movie (2020).en.srt").write_bytes(b"x" * 2000)
    (tmp_path / "Movie (2020).english_ai.srt").write_bytes(b"x" * 2000)
    (tmp_path / "Movie (2020).forced.srt").write_bytes(b"x" * 2000)
    (tmp_path / "Movie (2020).sv.srt").write_bytes(b"x" * 2000)
    (tmp_path / "Movie (2020).tiny.srt").write_bytes(b"x" * 10)
    # A different movie in the same folder must be ignored.
    (tmp_path / "Other.mkv").write_bytes(b"")
    (tmp_path / "Other.en.srt").write_bytes(b"x" * 2000)

    entries = {e["file"]: e for e in _external_subtitles(str(movie))}
    assert set(entries) == {
        "Movie (2020).en.srt",
        "Movie (2020).english_ai.srt",
        "Movie (2020).forced.srt",
        "Movie (2020).sv.srt",
        "Movie (2020).tiny.srt",
    }
    assert entries["Movie (2020).en.srt"]["lang"] == "English"
    assert entries["Movie (2020).english_ai.srt"]["ai"] is True
    assert entries["Movie (2020).english_ai.srt"]["lang"] == "English"
    assert entries["Movie (2020).forced.srt"]["forced"] is True
    assert entries["Movie (2020).sv.srt"]["lang"] == "Swedish"
    assert entries["Movie (2020).tiny.srt"]["tiny"] is True


# ---- change-scan skip rule --------------------------------------------------


def _st(size=100, mtime=1.0):
    return SimpleNamespace(st_size=size, st_mtime=mtime)


def test_needs_scan_full_always():
    entry = {"size": 100, "mtime": 1.0}
    assert _needs_scan(entry, _st(100, 1.0), "full") is True


def test_needs_scan_changed_missing():
    assert _needs_scan(None, _st(), "changed") is True


def test_needs_scan_changed_with_error():
    assert _needs_scan({"error": "unreachable"}, _st(), "changed") is True


def test_needs_scan_changed_unchanged_skips():
    entry = {"size": 100, "mtime": 1.0}
    assert _needs_scan(entry, _st(100, 1.0), "changed") is False


def test_needs_scan_changed_size_differs():
    entry = {"size": 100, "mtime": 1.0}
    assert _needs_scan(entry, _st(200, 1.0), "changed") is True


# ---- prune ------------------------------------------------------------------


def test_prune_removes_stale_entries_and_ok():
    files = {"/a": {"path": "/a"}, "/b": {"path": "/b"}}
    ok = ["/a", "/b"]
    files, ok = _prune_files(files, ok, {"/a"})
    assert files == {"/a": {"path": "/a"}}
    assert ok == ["/a"]


# ---- ffprobe resolution -----------------------------------------------------


def test_resolve_ffprobe_env(tmp_path, monkeypatch):
    exe = tmp_path / "ffprobe.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("JFTOOLS_FFPROBE", str(exe))
    assert resolve_ffprobe() == str(exe)
