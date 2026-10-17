"""Tests for the subtitle-scan classification and store helpers."""

from types import SimpleNamespace

import pytest

from app.subtitles import (
    _classify_tracks,
    _external_subtitles,
    _reconcile,
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


# ---- reconcile --------------------------------------------------------------


def test_reconcile_adds_new_as_unscanned_and_drops_stale():
    files = {"/a": {"path": "/a", "scanned": True}, "/gone": {"path": "/gone", "scanned": True}}
    ok = ["/a", "/gone"]
    files, ok = _reconcile(files, ok, {"/a", "/b"})
    assert set(files) == {"/a", "/b"}
    assert files["/a"]["scanned"] is True
    assert files["/b"]["scanned"] is False
    assert files["/b"]["file"] == "b"
    assert ok == ["/a"]


def test_reconcile_preserves_existing_scanned():
    files = {"/a": {"path": "/a", "scanned": False}}
    files, ok = _reconcile(files, [], {"/a"})
    assert files["/a"]["scanned"] is False


# ---- worklist (flag-based) --------------------------------------------------


def test_build_worklist_probes_only_unscanned(tmp_path):
    from app.subtitles import _build_worklist

    f = tmp_path / "movie.mkv"
    f.write_bytes(b"")
    g = tmp_path / "new.mkv"
    g.write_bytes(b"")
    rows = [{"path": str(f)}, {"path": str(g)}]
    files = {
        str(f): {"path": str(f), "scanned": True},
        str(g): {"path": str(g), "scanned": False},
    }
    work = _build_worklist(rows, files)
    assert [w[0] for w in work] == [str(g)]

    # A missing entry (shouldn't happen post-reconcile) is also probed.
    assert [w[0] for w in _build_worklist([{"path": str(f)}], {})] == [str(f)]


# ---- probe scanned flag -----------------------------------------------------


def test_probe_one_unreachable_not_scanned():
    from app.subtitles import _probe_one

    key, entry = _probe_one("ffprobe", "/x/y.mkv", "/x/y.mkv", None)
    assert entry["scanned"] is False
    assert entry["error"] == "unreachable"


def test_probe_one_not_a_file_not_scanned():
    import stat as statmod

    from app.subtitles import _probe_one

    st = SimpleNamespace(st_size=0, st_mtime=0.0, st_mode=statmod.S_IFDIR)
    key, entry = _probe_one("ffprobe", "/x", "/x", st)
    assert entry["scanned"] is False
    assert entry["error"] == "not a file"


def test_probe_one_success_marks_scanned(monkeypatch, tmp_path):
    import app.subtitles as sub

    f = tmp_path / "movie.mkv"
    f.write_bytes(b"")
    monkeypatch.setattr(sub, "_probe_subtitles", lambda ff, local: ([], 0.0, None))
    monkeypatch.setattr(sub, "_external_subtitles", lambda local: [])
    key, entry = sub._probe_one("ffprobe", str(f), str(f), f.stat())
    assert entry["scanned"] is True
    assert entry["flags"]["nosubs"] is True


# ---- ffprobe resolution -----------------------------------------------------


def test_resolve_ffprobe_env(tmp_path, monkeypatch):
    exe = tmp_path / "ffprobe.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("JFTOOLS_FFPROBE", str(exe))
    assert resolve_ffprobe() == str(exe)


def test_subtitles_ok_getter_is_callable():
    # Regression: the getter must not be shadowed by the /api/subtitles/ok route.
    from app.subtitles import subtitles_map, subtitles_ok

    assert isinstance(subtitles_ok(), list)
    assert isinstance(subtitles_map(), dict)


async def test_rescan_one_without_ffprobe(monkeypatch):
    import app.subtitles as sub

    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: None)
    entry = await sub.rescan_one("/x/y.mkv")
    assert entry["scanned"] is False
    assert entry["error"]
