"""Tests for the subtitle-scan classification and store helpers."""

import json
from types import SimpleNamespace

import pytest

from app.subtitles import (
    _classify_tracks,
    _external_subtitles,
    _language_from_text,
    _reconcile,
    _subtitle_lang_name,
    external_smart_marks,
    normalize_language_codes,
    preferred_language_names,
    resolve_ffprobe,
    smart_subtitle_marks,
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
        ("fil", "Filipino"),
        ("tgl", "Tagalog"),
        ("nb", "Norwegian Bokmål"),
        ("nob", "Norwegian Bokmål"),
        ("nn", "Norwegian Nynorsk"),
        ("nno", "Norwegian Nynorsk"),
        ("no", "Norwegian"),
        ("nor", "Norwegian"),
        ("und", "Unknown"),
        ("xyz", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_subtitle_lang_name(code, expected):
    assert _subtitle_lang_name(code) == expected


# ---- preferred languages / Smart mark --------------------------------------


def test_normalize_language_codes_accepts_codes_names_and_regions():
    assert normalize_language_codes("en,sv") == "en,sv"
    assert normalize_language_codes("English, Swedish") == "en,sv"
    assert normalize_language_codes("eng swe") == "en,sv"
    assert normalize_language_codes("en-US, sv_SE") == "en,sv"
    assert normalize_language_codes("English, English") == "en"
    assert normalize_language_codes("Filipino, Norwegian Bokmål") == "fil,nb"
    assert normalize_language_codes("nob, nno, tgl") == "nb,nn,tl"


def test_preferred_language_names_maps_codes_and_names():
    assert preferred_language_names("en,sv") == ["English", "Swedish"]
    assert preferred_language_names("English, Swedish") == ["English", "Swedish"]
    assert preferred_language_names("") == ["English"]
    assert preferred_language_names("nonsense") == []
    assert preferred_language_names("fil,nb") == ["Filipino", "Norwegian Bokmål"]
    assert preferred_language_names("Norwegian Nynorsk") == ["Norwegian Nynorsk"]


def test_smart_mark_marks_non_preferred_but_leaves_unknown_alone():
    subtitles = [
        {"index": 4, "lang": "English", "name": "English"},
        {"index": 5, "lang": "English", "name": "English SDH"},
        {"index": 6, "lang": "English", "name": "En SDH"},
        {"index": 7, "lang": "Unknown", "name": "Unknown (En SDH)"},
        {"index": 8, "lang": "Unknown", "name": "Unknown"},
        {"index": 9, "lang": "Swedish", "name": "Swedish"},
        {"index": 10, "lang": "French", "name": "French"},
        {"index": 11, "lang": "Unknown", "name": "Unknown (Czech czh)"},
        {"index": 12, "lang": "German", "name": "German"},
        {"index": 13, "lang": "Unknown", "name": "Unknown (fr)"},
    ]

    marks, preferred = smart_subtitle_marks(subtitles, "en,sv")

    assert preferred == ["English", "Swedish"]
    assert marks == [10, 11, 12, 13]


def test_smart_mark_includes_forced_tracks():
    subtitles = [
        {"index": 4, "lang": "English", "name": "English", "forced": False},
        {"index": 5, "lang": "English", "name": "English forced", "forced": True},
        {"index": 6, "lang": "French", "name": "French forced", "forced": True},
        {"index": 7, "lang": "German", "name": "German", "forced": False},
    ]

    marks, preferred = smart_subtitle_marks(subtitles, "en,sv")

    assert preferred == ["English", "Swedish"]
    assert marks == [5, 6, 7]


def test_smart_mark_defaults_to_english_without_a_setting():
    subtitles = [
        {"index": 4, "lang": "English", "name": "English SDH"},
        {"index": 5, "lang": "French", "name": "French"},
        {"index": 6, "lang": "Unknown", "name": "Unknown"},
    ]
    assert smart_subtitle_marks(subtitles, "") == ([5], ["English"])


# ---- language from text ------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Unknown (Català)", "Catalan"),
        ("Unknown (Euskara)", "Basque"),
        ("Unknown (Galego)", "Galician"),
        ("Unknown (ಕನ್ನಡ)", "Kannada"),
        ("Unknown (മലയാളം)", "Malayalam"),
        ("Unknown (Bahasa Melayu)", "Malay"),
        ("Unknown (Norsk Bokmål)", "Norwegian Bokmål"),
        ("Unknown (Norwegian (Bokmål))", "Norwegian Bokmål"),
        ("Unknown (Nynorsk)", "Norwegian Nynorsk"),
        ("Unknown (தமிழ்)", "Tamil"),
        ("Unknown (తెలుగు)", "Telugu"),
        ("Catalan", "Catalan"),
        ("Unknown (Catalan forced)", "Catalan"),
        ("Unknown", ""),
    ],
)
def test_language_from_text_reads_parenthesised_names(name, expected):
    assert _language_from_text(name) == expected


def test_smart_mark_marks_parenthesised_languages_it_does_not_prefer():
    subtitles = [
        {"index": 4, "lang": "Unknown", "name": "Unknown (Català)"},
        {"index": 5, "lang": "Unknown", "name": "Unknown (Euskara)"},
        {"index": 6, "lang": "Unknown", "name": "Unknown (Galego)"},
        {"index": 7, "lang": "Unknown", "name": "Unknown (ಕನ್ನಡ)"},
        {"index": 8, "lang": "Unknown", "name": "Unknown (മലയാളം)"},
        {"index": 9, "lang": "Unknown", "name": "Unknown (Bahasa Melayu)"},
        {"index": 10, "lang": "Unknown", "name": "Unknown (Norsk Bokmål)"},
        {"index": 11, "lang": "Unknown", "name": "Unknown (தமிழ்)"},
        {"index": 12, "lang": "Unknown", "name": "Unknown (తెలుగు)"},
        {"index": 13, "lang": "English", "name": "English"},
        {"index": 14, "lang": "Unknown", "name": "Unknown"},
    ]

    marks, preferred = smart_subtitle_marks(subtitles, "en,sv")

    assert preferred == ["English", "Swedish"]
    assert marks == [4, 5, 6, 7, 8, 9, 10, 11, 12]


# ---- external smart marks ----------------------------------------------------


def test_external_smart_marks_non_preferred_and_forced_files():
    external = [
        {"file": "Movie.English.srt", "lang": "English", "forced": False},
        {"file": "Movie.Swedish.srt", "lang": "Swedish", "forced": False},
        {"file": "Movie.French.srt", "lang": "French", "forced": False},
        {"file": "Movie.forced.srt", "lang": "English", "forced": True},
        {"file": "Movie.Unknown.srt", "lang": "Unknown", "forced": False},
    ]

    marks = external_smart_marks(external, ["English", "Swedish"])

    assert marks == ["Movie.French.srt", "Movie.forced.srt"]


def test_external_smart_marks_reads_language_from_the_filename():
    external = [
        {"file": "Movie.Norsk.Bokmål.srt", "lang": "Unknown", "forced": False},
        {"file": "Movie.TrulyUnknown.srt", "lang": "Unknown", "forced": False},
    ]

    assert external_smart_marks(external, ["English"]) == [
        "Movie.Norsk.Bokmål.srt"
    ]


# ---- classification ---------------------------------------------------------


PREFERRED = ["English", "Swedish"]


def test_classify_nothing_is_nosubs():
    flags = _classify_tracks([], [], PREFERRED)
    assert flags == {
        "preferred": False,
        "foreign": False,
        "unknown": False,
        "unknown_normal": False,
        "good": False,
        "acceptable": False,
        "acceptable_ai": False,
        "ai": False,
        "forced": False,
        "tiny": False,
        "nosubs": True,
    }


def test_classify_internal_english_acceptable():
    flags = _classify_tracks([_int("English")], [], PREFERRED)
    assert flags["acceptable"] is True
    assert flags["preferred"] is True
    assert flags["good"] is True
    assert flags["nosubs"] is False


def test_classify_external_swedish_acceptable():
    flags = _classify_tracks([], [_ext("Swedish")], PREFERRED)
    assert flags["acceptable"] is True
    assert flags["preferred"] is True


def test_classify_forced_not_acceptable():
    flags = _classify_tracks([_int("English", forced=True)], [], PREFERRED)
    assert flags["forced"] is True
    assert flags["good"] is False
    assert flags["acceptable"] is False
    # Language still counts a forced track.
    assert flags["preferred"] is True


def test_classify_norwegian_family_matches_all_variants():
    for lang in ("Norwegian", "Norwegian Bokmål", "Norwegian Nynorsk"):
        flags = _classify_tracks([_int(lang)], [], ["Norwegian"])
        assert flags["acceptable"] is True
        assert flags["preferred"] is True


def test_classify_filipino_and_tagalog_share_a_family():
    assert _classify_tracks([_int("Filipino")], [], ["Tagalog"])["acceptable"] is True
    assert _classify_tracks([_int("Tagalog")], [], ["Filipino"])["acceptable"] is True


def test_classify_foreign():
    flags = _classify_tracks([], [_ext("French")], PREFERRED)
    assert flags["foreign"] is True
    assert flags["good"] is True
    assert flags["acceptable"] is False


def test_classify_ai_in_a_preferred_language_is_not_the_preferred_language_kind():
    flags = _classify_tracks(
        [], [_ext("English", ai=True, file="x.english_ai.srt")], PREFERRED
    )
    assert flags["ai"] is True
    assert flags["acceptable"] is False
    assert flags["acceptable_ai"] is True


def test_classify_ai_beside_a_human_preferred_subtitle_keeps_both_kinds():
    flags = _classify_tracks(
        [],
        [
            _ext("English", file="x.en.srt"),
            _ext("Swedish", ai=True, file="x.sv_ai.srt"),
        ],
        PREFERRED,
    )
    assert flags["acceptable"] is True
    assert flags["acceptable_ai"] is True


def test_classify_foreign_ai_is_not_acceptable():
    # The AI kind is about the preferred languages; a French AI subtitle does
    # not make a file acceptable when English/Swedish are wanted.
    flags = _classify_tracks(
        [], [_ext("French", ai=True, file="x.fr_ai.srt")], PREFERRED
    )
    assert flags["ai"] is True
    assert flags["acceptable"] is False
    assert flags["acceptable_ai"] is False


def test_classify_unrelated_ai_does_not_downgrade_acceptable():
    flags = _classify_tracks(
        [],
        [
            _ext("English", file="x.en.srt"),
            _ext("French", ai=True, file="x.fr_ai.srt"),
        ],
        PREFERRED,
    )
    assert flags["acceptable"] is True
    assert flags["acceptable_ai"] is False
    assert flags["foreign"] is True


def test_classify_tiny_is_not_good_or_acceptable():
    flags = _classify_tracks([], [_ext("English", tiny=True)], PREFERRED)
    assert flags["tiny"] is True
    assert flags["good"] is False
    assert flags["acceptable"] is False
    assert flags["nosubs"] is False


def test_classify_unknown_language():
    flags = _classify_tracks([], [_ext("Unknown")], PREFERRED)
    assert flags["unknown"] is True
    assert flags["unknown_normal"] is True
    assert flags["foreign"] is False
    assert flags["acceptable"] is False
    # Unknown is still a normal subtitle, so Quality's Good catches it.
    assert flags["good"] is True


def test_classify_forced_or_tiny_unknown_is_not_acceptable_unknown():
    forced = _classify_tracks([_int("Unknown", forced=True)], [], PREFERRED)
    tiny = _classify_tracks([], [_ext("Unknown", tiny=True)], PREFERRED)
    for flags in (forced, tiny):
        assert flags["unknown"] is True
        assert flags["unknown_normal"] is False
        assert flags["acceptable"] is False
        assert flags["acceptable_ai"] is False


def test_scans_map_rebuilds_flags_with_current_preferences(monkeypatch):
    import app.subtitles as sub

    entry = {
        "scanned": True,
        "internal": [{"lang": "Swedish", "forced": False}],
        "external": [],
        "flags": {"acceptable": False},
    }
    monkeypatch.setitem(sub._state, "files", {"/x.mkv": entry})

    english = sub.scans_map(["English"])
    swedish = sub.scans_map(["Swedish"])

    assert english["/x.mkv"]["flags"]["acceptable"] is False
    assert swedish["/x.mkv"]["flags"]["acceptable"] is True
    # The stored entry keeps its old flags until the next scan saves them.
    assert sub._state["files"]["/x.mkv"]["flags"]["acceptable"] is False


def test_load_state_marks_old_unknown_languages_for_one_reprobe(
    tmp_path, monkeypatch
):
    import app.subtitles as sub

    store = {
        "version": 3,
        "files": {
            "/new-code.mkv": {
                "scanned": True,
                "internal": [{"lang": "Unknown", "code": "fil"}],
                "audio": [],
                "external": [],
            },
            "/old-no-code.mkv": {
                "scanned": True,
                "internal": [{"lang": "Unknown"}],
                "audio": [],
                "external": [],
            },
            "/genuinely-und.mkv": {
                "scanned": True,
                "internal": [{"lang": "Unknown", "code": "und"}],
                "audio": [],
                "external": [],
            },
            "/audio-fil.mkv": {
                "scanned": True,
                "internal": [],
                "audio": [{"lang": "Unknown", "code": "fil"}],
                "external": [],
            },
            "/already-good.mkv": {
                "scanned": True,
                "internal": [{"lang": "English", "code": "eng"}],
                "audio": [],
                "external": [],
            },
        },
    }
    scans = tmp_path / "scans.json"
    scans.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(sub, "_SCANS_FILE", scans)

    state = sub._load_state()["files"]

    assert state["/new-code.mkv"]["scanned"] is False
    assert state["/old-no-code.mkv"]["scanned"] is False
    assert state["/audio-fil.mkv"]["scanned"] is False
    assert state["/genuinely-und.mkv"]["scanned"] is True
    assert state["/already-good.mkv"]["scanned"] is True


def test_load_state_does_not_reprobe_after_version_four(tmp_path, monkeypatch):
    import app.subtitles as sub

    scans = tmp_path / "scans.json"
    scans.write_text(
        json.dumps({
            "version": 4,
            "files": {
                "/x.mkv": {
                    "scanned": True,
                    "internal": [{"lang": "Unknown", "code": "und"}],
                    "audio": [],
                    "external": [],
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(sub, "_SCANS_FILE", scans)

    assert sub._load_state()["files"]["/x.mkv"]["scanned"] is True


def test_scans_map_hides_stale_lines_for_unscanned_entries(monkeypatch):
    import app.subtitles as sub

    monkeypatch.setitem(sub._state, "files", {
        "/x.mkv": {
            "scanned": False,
            "internal": [{"lang": "Unknown", "name": "Unknown"}],
            "external": [],
            "subs": ["Unknown"],
            "flags": {"unknown": True},
        },
    })

    entry = sub.scans_map(["English"])["/x.mkv"]

    assert entry["subs"] == []
    assert entry["flags"] is None


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


def test_external_subtitles_ignore_codec_tokens(tmp_path):
    movie = tmp_path / "Movie.mkv"
    movie.write_bytes(b"")
    (tmp_path / "Movie.English.DTS.srt").write_bytes(b"x" * 2000)
    (tmp_path / "Movie.aac.srt").write_bytes(b"x" * 2000)

    entries = {e["file"]: e for e in _external_subtitles(str(movie))}

    # The last token is an audio codec, not the ISO 639-3 code for Toro So
    # Dogon; keep looking and find the real language.
    assert entries["Movie.English.DTS.srt"]["lang"] == "English"
    # "aac" is the ISO 639-3 code for Ari, but in a filename it is a codec.
    assert entries["Movie.aac.srt"]["lang"] == "Unknown"


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


# ---- external subtitle change check -----------------------------------------


def _scanned_entry(path, external=None, folder=False):
    return {
        "path": path,
        "file": "movie.mkv",
        "scanned": True,
        "folder": folder,
        "external": list(external or []),
    }


def test_changed_entries_flags_new_sidecar(tmp_path):
    import app.subtitles as sub

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    (tmp_path / "movie.en.srt").write_bytes(b"x" * 2000)
    files = {str(movie): _scanned_entry(str(movie))}

    # The store knows nothing about the new file, so it must be re-probed.
    assert sub._changed_entries([{"path": str(movie)}], files) == [str(movie)]

    # Once the store has it, the same listing is not a change.
    files[str(movie)]["external"] = sub._external_subtitles(str(movie))
    assert sub._changed_entries([{"path": str(movie)}], files) == []


def test_changed_entries_flags_removed_sidecar(tmp_path):
    import app.subtitles as sub

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    files = {str(movie): _scanned_entry(str(movie), [_ext(file="movie.en.srt")])}
    assert sub._changed_entries([{"path": str(movie)}], files) == [str(movie)]


def test_changed_entries_ignores_order(tmp_path):
    import app.subtitles as sub

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    (tmp_path / "movie.en.srt").write_bytes(b"x" * 2000)
    (tmp_path / "movie.sv.srt").write_bytes(b"x" * 2000)
    files = {
        str(movie): _scanned_entry(
            str(movie),
            [_ext("Swedish", file="movie.sv.srt"), _ext("English", file="movie.en.srt")],
        )
    }
    assert sub._changed_entries([{"path": str(movie)}], files) == []


def test_changed_entries_skips_unscanned_and_folders(tmp_path):
    import app.subtitles as sub

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    (tmp_path / "movie.en.srt").write_bytes(b"x" * 2000)
    show = tmp_path / "Show"
    show.mkdir()

    unscanned = _scanned_entry(str(movie))
    unscanned["scanned"] = False
    rows = [{"path": str(movie)}, {"path": str(show)}]
    files = {str(movie): unscanned, str(show): _scanned_entry(str(show), folder=True)}
    # Unscanned items are queued for probing anyway; a series "folder" row has
    # no file to check. Neither should be reported as a change.
    assert sub._changed_entries(rows, files) == []


def test_changed_entries_keeps_data_when_folder_unreachable(tmp_path):
    import app.subtitles as sub

    missing = tmp_path / "gone" / "movie.mkv"
    files = {str(missing): _scanned_entry(str(missing), [_ext(file="movie.en.srt")])}
    # An unreachable folder says nothing about the files: never invalidate on it.
    assert sub._changed_entries([{"path": str(missing)}], files) == []


def test_changed_entries_lists_each_folder_once(tmp_path, monkeypatch):
    """A season folder holds many episodes: one listing must serve them all."""
    import app.subtitles as sub

    rows = []
    files = {}
    for i in range(5):
        movie = tmp_path / f"ep{i}.mkv"
        movie.write_bytes(b"")
        (tmp_path / f"ep{i}.en.srt").write_bytes(b"x" * 2000)
        rows.append({"path": str(movie)})
        files[str(movie)] = _scanned_entry(str(movie))

    calls = []
    real = sub._list_dir

    def counting(folder):
        calls.append(folder)
        return real(folder)

    monkeypatch.setattr(sub, "_list_dir", counting)
    flagged = sub._changed_entries(rows, files)
    assert len(calls) == 1
    assert sorted(flagged) == sorted(r["path"] for r in rows)


async def test_scan_reprobes_an_item_whose_subtitle_appeared(monkeypatch, tmp_path):
    """The whole point: an already-scanned file with a new sidecar is probed
    again by the normal media scan, without a full scan."""
    import app.subtitles as sub
    from app.jellyfin_id import _state as id_state

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    (tmp_path / "movie.en.srt").write_bytes(b"x" * 2000)

    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(sub, "_probe_streams", lambda ff, local: ([], [], 60.0, None))
    monkeypatch.setattr(sub, "_save_state", lambda: None)

    files_before = dict(sub._state["files"])
    ok_before = list(sub._state["ok"])
    rows_before = id_state["rows"]
    try:
        # Scanned before the subtitle was added, and manually marked Ok.
        sub._state["files"] = {str(movie): _scanned_entry(str(movie))}
        sub._state["ok"] = [str(movie)]
        id_state["rows"] = [{"path": str(movie), "file": movie.name}]

        await sub.run_scan("scan")

        assert sub._fetch["status"] == "done"
        entry = sub.scans_map()[str(movie)]
        assert entry["scanned"] is True
        assert [e["file"] for e in entry["external"]] == ["movie.en.srt"]
        assert entry["flags"]["acceptable"] is True
        # The manual mark was for the old state, so it is dropped.
        assert sub._state["ok"] == []
    finally:
        sub._state["files"] = files_before
        sub._state["ok"] = ok_before
        id_state["rows"] = rows_before


async def test_full_scan_skips_the_check(monkeypatch, tmp_path):
    """Every file is re-probed anyway, so the check would be wasted I/O."""
    import app.subtitles as sub
    from app.jellyfin_id import _state as id_state

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(sub, "_probe_streams", lambda ff, local: ([], [], 60.0, None))
    monkeypatch.setattr(sub, "_save_state", lambda: None)

    calls = []
    monkeypatch.setattr(
        sub, "_changed_entries", lambda rows, files: calls.append(True) or []
    )
    files_before = dict(sub._state["files"])
    rows_before = id_state["rows"]
    try:
        sub._state["files"] = {str(movie): _scanned_entry(str(movie))}
        id_state["rows"] = [{"path": str(movie), "file": movie.name}]
        await sub.run_scan("full")
        assert calls == []
    finally:
        sub._state["files"] = files_before
        id_state["rows"] = rows_before


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


def test_probe_one_folder_is_scanned_not_failed():
    """Jellyfin lists a series with its folder as the Path: nothing to probe."""
    import stat as statmod

    from app.subtitles import _probe_one

    st = SimpleNamespace(st_size=0, st_mtime=0.0, st_mode=statmod.S_IFDIR)
    key, entry = _probe_one("ffprobe", "/x", "/x", st)
    assert entry["scanned"] is True
    assert entry["folder"] is True
    assert entry["error"] is None


def test_probe_one_rejects_other_special_files():
    import stat as statmod

    from app.subtitles import _probe_one

    st = SimpleNamespace(st_size=0, st_mtime=0.0, st_mode=statmod.S_IFIFO)
    key, entry = _probe_one("ffprobe", "/x", "/x", st)
    assert entry["scanned"] is False
    assert entry["error"] == "not a file"


def test_probe_one_success_marks_scanned(monkeypatch, tmp_path):
    import app.subtitles as sub

    f = tmp_path / "movie.mkv"
    f.write_bytes(b"")
    monkeypatch.setattr(sub, "_probe_streams", lambda ff, local: ([], [], 0.0, None))
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
    from app.subtitles import scans_map, subtitles_ok

    assert isinstance(subtitles_ok(), list)
    assert isinstance(scans_map(), dict)


def test_prune_to_paths_drops_orphaned_entries_and_marks(monkeypatch):
    import app.subtitles as sub

    monkeypatch.setattr(sub, "_save_state", lambda: None)
    files_before = dict(sub._state["files"])
    ok_before = list(sub._state["ok"])
    try:
        sub._state["files"] = {"keep.mkv": {"path": "keep.mkv"}, "gone": {"path": "gone"}}
        sub._state["ok"] = ["keep.mkv", "gone"]

        assert sub.prune_to_paths({"keep.mkv"}) == 1
        assert list(sub._state["files"]) == ["keep.mkv"]
        assert sub._state["ok"] == ["keep.mkv"]
    finally:
        sub._state["files"] = files_before
        sub._state["ok"] = ok_before


async def test_rescan_one_without_ffprobe(monkeypatch):
    import app.subtitles as sub

    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: None)
    entry = await sub.rescan_one("/x/y.mkv")
    assert entry["scanned"] is False
    assert entry["error"]


async def test_rescan_one_returns_the_stored_entry(monkeypatch, tmp_path):
    """The Search table applies the rescan response in place, so the entry it
    gets back has to be the one scans_map() serves, keyed by the path."""
    import app.subtitles as sub

    f = tmp_path / "movie.mkv"
    f.write_bytes(b"")
    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(sub, "_probe_streams", lambda ff, local: ([], [], 60.0, None))
    monkeypatch.setattr(sub, "_external_subtitles", lambda local: [])
    monkeypatch.setattr(sub, "_save_state", lambda: None)

    before = dict(sub._state["files"])
    try:
        entry = await sub.rescan_one(str(f))
        assert entry["path"] == str(f)
        assert sub.scans_map()[str(f)] == entry
    finally:
        sub._state["files"] = before


async def test_scan_clears_not_scanned_for_series_folders(monkeypatch, tmp_path):
    """A series row's Path is its folder, which can never be probed. It used to
    fail as "not a file" on every scan, so the Search tab kept listing it as
    Not scanned forever; it must come out scanned instead."""
    import app.subtitles as sub
    from app.jellyfin_id import _state as id_state

    show = tmp_path / "Some Show"
    show.mkdir()
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    rows = [{"path": str(show), "file": show.name}, {"path": str(movie), "file": movie.name}]

    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(sub, "_probe_streams", lambda ff, local: ([], [], 60.0, None))
    monkeypatch.setattr(sub, "_external_subtitles", lambda local: [])
    monkeypatch.setattr(sub, "_save_state", lambda: None)
    monkeypatch.setattr(sub, "_reconcile", lambda files, ok, paths: (dict(files), list(ok)))

    files_before = dict(sub._state["files"])
    rows_before = id_state["rows"]
    try:
        id_state["rows"] = rows
        sub._state["files"] = {}
        await sub.run_scan("scan")

        assert sub._fetch["status"] == "done"
        assert sub._fetch["total"] == 2
        entry = sub.scans_map()[str(show)]
        assert entry["scanned"] is True
        assert entry["folder"] is True
        assert sub.scans_map()[str(movie)]["scanned"] is True
        assert sub.scans_map()[str(movie)]["audio"] == []
    finally:
        sub._state["files"] = files_before
        id_state["rows"] = rows_before

