"""Tests for audio-track probing, the merged scan store and audio deletion."""

import errno
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.audio as audio
import app.subtitles as sub
from app.config import config


# ---- formatting -------------------------------------------------------------


def test_audio_line_marks_the_default_track():
    track = {
        "lang": "English", "codec": "eac3", "channels": 6,
        "layout": "5.1", "default": True,
    }
    assert sub._audio_line(track) == "English (E-AC-3 5.1)*"


def test_audio_line_uses_codec_and_layout():
    track = {
        "lang": "Swedish", "codec": "aac", "channels": 2,
        "layout": "", "default": False,
    }
    assert sub._audio_line(track) == "Swedish (AAC stereo)"
    unknown = {"lang": "Unknown", "codec": "weirdcodec", "channels": 4, "layout": ""}
    assert sub._audio_line(unknown) == "Unknown (WEIRDCODEC 4ch)"


def test_audio_line_includes_container_title():
    riff = {
        "lang": "English", "title": "RiffTrax", "codec": "opus",
        "channels": 2, "layout": "stereo", "default": True,
    }
    assert sub._audio_line(riff) == "English — RiffTrax (Opus stereo)*"

    unriffed = {**riff, "title": "Unriffed feature", "default": False}
    assert sub._audio_line(unriffed) == "English — Unriffed feature (Opus stereo)"

    # Some releases repeat the language in the title.
    prefixed = {**riff, "title": "English (RiffTrax)", "default": False}
    assert sub._audio_line(prefixed) == "English (RiffTrax) (Opus stereo)"

    same = {**riff, "title": "English", "default": False}
    assert sub._audio_line(same) == "English (Opus stereo)"


def test_refresh_audio_track_updates_the_cached_title():
    track = {
        "index": 1, "lang": "English", "name": "English (Opus stereo)*",
        "code": "eng", "title": "RiffTrax", "codec": "opus",
        "channels": 2, "layout": "stereo", "default": True,
    }

    refreshed = sub._refresh_audio_track(track)

    assert refreshed["name"] == "English — RiffTrax (Opus stereo)*"
    assert refreshed["title"] == "RiffTrax"


# ---- probing ----------------------------------------------------------------


def _fake_ffprobe(monkeypatch, payload):
    result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(sub.subprocess, "run", lambda *a, **k: result)


def test_probe_streams_collects_audio_and_subtitles_in_one_pass(monkeypatch):
    _fake_ffprobe(monkeypatch, {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {
                "index": 1, "codec_type": "audio", "codec_name": "eac3",
                "channels": 6, "channel_layout": "5.1",
                "tags": {"language": "eng", "title": "RiffTrax"},
                "disposition": {"default": True},
            },
            {
                "index": 2, "codec_type": "audio", "codec_name": "aac",
                "channels": 2, "channel_layout": "stereo",
                "tags": {"language": "swe"},
                "disposition": {},
            },
            {
                "index": 3, "codec_type": "subtitle", "codec_name": "subrip",
                "tags": {"language": "eng"},
                "disposition": {"forced": True},
            },
        ],
        "format": {"duration": "5400.5"},
    })

    subs, tracks, duration, err = sub._probe_streams("ffprobe", "movie.mkv")

    assert err is None
    assert duration == 5400.5
    assert [t["name"] for t in tracks] == [
        "English — RiffTrax (E-AC-3 5.1)*",
        "Swedish (AAC stereo)",
    ]
    assert tracks[0]["index"] == 1 and tracks[0]["default"] is True
    assert tracks[0]["title"] == "RiffTrax"
    assert tracks[1]["code"] == "swe"
    assert subs == [{
        "index": 3, "lang": "English", "name": "English",
        "code": "eng", "title": "",
        "forced": True, "default": False, "codec": "subrip",
        "disposition": {"forced": True},
    }]


def test_probe_streams_labels_filipino_and_norwegian_variants(monkeypatch):
    _fake_ffprobe(monkeypatch, {
        "streams": [
            {
                "index": 1, "codec_type": "subtitle", "codec_name": "subrip",
                "tags": {"language": "fil"}, "disposition": {},
            },
            {
                "index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                "tags": {"language": "nob"}, "disposition": {},
            },
            {
                "index": 3, "codec_type": "subtitle", "codec_name": "subrip",
                "tags": {"language": "nno"}, "disposition": {},
            },
            {
                "index": 4, "codec_type": "subtitle", "codec_name": "subrip",
                "tags": {"language": "tl"}, "disposition": {},
            },
        ],
    })

    subs, _tracks, _duration, err = sub._probe_streams("ffprobe", "movie.mkv")

    assert err is None
    assert [(t["code"], t["lang"], t["name"]) for t in subs] == [
        ("fil", "Filipino", "Filipino"),
        ("nob", "Norwegian Bokmål", "Norwegian Bokmål"),
        ("nno", "Norwegian Nynorsk", "Norwegian Nynorsk"),
        ("tl", "Tagalog", "Tagalog"),
    ]


def test_probe_streams_reads_the_subtitle_default_flag(monkeypatch):
    _fake_ffprobe(monkeypatch, {
        "streams": [{
            "index": 4, "codec_type": "subtitle", "codec_name": "subrip",
            "tags": {"language": "eng"},
            "disposition": {"default": True, "forced": True},
        }],
    })

    subs, _tracks, _duration, err = sub._probe_streams("ffprobe", "movie.mkv")

    assert err is None
    assert subs[0]["default"] is True
    assert subs[0]["forced"] is True


def test_probe_streams_reports_ffprobe_failure(monkeypatch):
    monkeypatch.setattr(
        sub.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="bad file"),
    )
    subs, tracks, duration, err = sub._probe_streams("ffprobe", "movie.mkv")
    assert subs == [] and tracks == [] and duration == 0.0
    assert err and "bad file" in err


# ---- worklists and the merged store ----------------------------------------


def test_worklist_covers_every_unscanned_file(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    monkeypatch.setattr(config._settings, "path_maps", [])
    rows = [{"path": str(video)}]
    files = {str(video): {"path": str(video), "scanned": False}}

    assert len(sub._build_worklist(rows, files)) == 1
    files[str(video)]["scanned"] = True
    assert sub._build_worklist(rows, files) == []


def test_load_state_imports_the_subtitle_only_store(tmp_path, monkeypatch):
    legacy = tmp_path / "subtitles.json"
    legacy.write_text(json.dumps({
        "version": 1,
        "full_scan_at": 1,
        "change_scan_at": 2,
        "files": {"/m.mkv": {"path": "/m.mkv", "scanned": True, "subs": ["English"]}},
        "ok": ["/m.mkv"],
    }), encoding="utf-8")
    monkeypatch.setattr(sub, "_SCANS_FILE", tmp_path / "scans.json")
    monkeypatch.setattr(sub, "_LEGACY_SUBS_FILE", legacy)

    state = sub._load_state()

    assert state["legacy"] is True
    assert state["full_scan_at"] == 1
    entry = state["files"]["/m.mkv"]
    assert entry["scanned"] is True
    assert entry["audio"] == []


def test_save_state_keeps_the_legacy_store(tmp_path, monkeypatch):
    """A successful migration writes scans.json but never deletes the old file."""
    legacy = tmp_path / "subtitles.json"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sub, "_SCANS_FILE", tmp_path / "scans.json")
    monkeypatch.setattr(sub, "_LEGACY_SUBS_FILE", legacy)
    monkeypatch.setattr(sub, "log", lambda *a, **k: None)
    monkeypatch.setitem(sub._state, "files", {"/m.mkv": {"path": "/m.mkv"}})
    monkeypatch.setitem(sub._state, "ok", [])
    monkeypatch.setitem(sub._state, "legacy", True)

    sub._save_state()

    assert (tmp_path / "scans.json").exists()
    assert legacy.exists()
    assert sub._state["legacy"] is False


def test_load_state_prefers_the_merged_store(tmp_path, monkeypatch):
    scans = tmp_path / "scans.json"
    scans.write_text(json.dumps({
        "version": 2,
        "files": {"/m.mkv": {
            "path": "/m.mkv", "scanned": True, "audio_scanned": True,
            "audio": [{"index": 1, "name": "English (AAC stereo)"}],
        }},
    }), encoding="utf-8")
    monkeypatch.setattr(sub, "_SCANS_FILE", scans)
    monkeypatch.setattr(sub, "_LEGACY_SUBS_FILE", tmp_path / "missing.json")

    state = sub._load_state()

    assert state["legacy"] is False
    entry = state["files"]["/m.mkv"]
    assert entry["audio"][0]["name"] == "English (AAC stereo)"
    # The 1.20.0-only flag is dropped; one flag covers the whole entry now.
    assert "audio_scanned" not in entry


async def test_scan_fills_both_halves(monkeypatch, tmp_path):
    import app.jellyfin_id as jid

    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"")
    track = {
        "index": 1, "lang": "English", "codec": "aac", "channels": 2,
        "layout": "stereo", "default": True, "name": "English (AAC stereo)*",
    }
    monkeypatch.setattr(sub, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(
        sub, "_probe_streams", lambda ff, local: ([], [track], 60.0, None)
    )
    monkeypatch.setattr(sub, "_external_subtitles", lambda local: [])
    monkeypatch.setattr(sub, "_save_state", lambda: None)
    monkeypatch.setattr(jid, "_state", {"rows": [{"path": str(movie), "file": movie.name}]})
    monkeypatch.setitem(
        sub._state, "files",
        {str(movie): {
            "path": str(movie), "file": movie.name,
            "scanned": False, "subs": [], "audio": [],
        }},
    )

    await sub.run_scan("scan")

    assert sub._fetch["status"] == "done"
    assert "kind" not in sub._fetch
    assert sub._fetch["scanned"] == 1
    entry = sub._state["files"][str(movie)]
    assert entry["scanned"] is True
    assert entry["audio"][0]["name"] == "English (AAC stereo)*"
    assert "audio_scanned" not in entry


# ---- delete endpoint --------------------------------------------------------


@pytest.fixture
def audio_env(tmp_path, monkeypatch):
    """A media tree, a Movie Match row and the tools; Jellyfin stays offline."""
    media = tmp_path / "Media"
    media.mkdir()
    video = media / "Movie (2020).mkv"
    video.write_bytes(b"x" * 1024)
    monkeypatch.setattr(config._settings, "path_maps", [{"from": "/Media", "to": str(media)}])
    monkeypatch.setattr(config._settings, "file_mgmt", True)

    import app.jellyfin.client as jc
    import app.jellyfin_id as jid

    monkeypatch.setattr(jid, "_state", {"rows": [{
        "id": "i1", "type": "Movie", "path": "/Media/Movie (2020).mkv",
        "file": "Movie (2020).mkv", "name": "Movie",
    }]})
    monkeypatch.setattr(audio, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(audio, "resolve_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(sub, "_save_state", lambda: None)

    class OfflineClient:
        async def scan_paths(self, paths):
            raise jc.JellyfinError("offline")

    monkeypatch.setattr(jc, "get_client", lambda: OfflineClient())
    return media, video, "/Media/Movie (2020).mkv"


def _tracks():
    return [
        {"index": 1, "lang": "English", "name": "English (E-AC-3 5.1)*", "default": True},
        {"index": 2, "lang": "Swedish", "name": "Swedish (AAC stereo)", "default": False},
    ]


async def test_delete_requires_file_management(audio_env, monkeypatch):
    monkeypatch.setattr(config._settings, "file_mgmt", False)
    resp = await audio.audio_delete(
        user="u", x_jftools="1", path=audio_env[2], index="1"
    )
    assert resp.status_code == 403


async def test_delete_requires_the_security_header(audio_env):
    resp = await audio.audio_delete(user="u", x_jftools="", path=audio_env[2], index="1")
    assert resp.status_code == 403


async def test_delete_rejects_an_unknown_library_path(audio_env):
    resp = await audio.audio_delete(
        user="u", x_jftools="1", path="/Media/Other.mkv", index="1"
    )
    assert resp.status_code == 404


async def test_delete_refuses_the_only_audio_track(audio_env, monkeypatch):
    monkeypatch.setattr(
        audio, "_probe_streams",
        lambda ff, local: ([], [_tracks()[0]], 1.0, None),
    )
    resp = await audio.audio_delete(
        user="u", x_jftools="1", path=audio_env[2], index="1"
    )
    assert resp.status_code == 400
    data = json.loads(resp.body)
    assert "only audio track" in data["error"]


async def test_delete_rejects_a_stream_that_is_not_audio(audio_env, monkeypatch):
    monkeypatch.setattr(
        audio, "_probe_streams",
        lambda ff, local: ([], _tracks(), 1.0, None),
    )
    resp = await audio.audio_delete(
        user="u", x_jftools="1", path=audio_env[2], index="9"
    )
    assert resp.status_code == 403


async def test_delete_rewrites_and_refreshes_the_store(audio_env, monkeypatch):
    _media, video, path = audio_env
    calls = {}
    remaining = [{"index": 2, "lang": "Swedish", "name": "Swedish (AAC stereo)", "default": False}]
    entry = {"path": path, "file": video.name, "audio": remaining, "scanned": True,
             "error": None}

    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: ([], _tracks(), 1.0, None)
    )

    def fake_remove(ffmpeg, local, index, set_default, ffprobe):
        calls.update(
            ffmpeg=ffmpeg, local=local, index=index,
            set_default=set_default, ffprobe=ffprobe,
        )
        return True, ""

    monkeypatch.setattr(audio, "_remove_track", fake_remove)
    monkeypatch.setattr(audio, "_probe_one", lambda ff, key, local, st: (key, entry))
    monkeypatch.setattr(audio, "_stat_path", lambda local: None)
    monkeypatch.setitem(audio._state, "files", {})

    resp = await audio.audio_delete(
        user="u", x_jftools="1", path=path, index="1"
    )
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert "English (E-AC-3 5.1)*" in data["message"]
    # The removed track was the default, so the first remaining one is promoted.
    assert calls == {
        "ffmpeg": "ffmpeg", "local": str(video), "index": 1,
        "set_default": True, "ffprobe": "ffprobe",
    }
    assert audio._state["files"][path] == entry
    assert data["entry"] == entry
    assert data["jellyfin"] == "offline"


def test_remove_track_maps_everything_but_the_stream(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    ok, err = audio._remove_track("ffmpeg", str(video), 2, True)

    assert ok and err == ""
    assert video.read_bytes() == b"new"
    cmd = seen[0]
    assert cmd[cmd.index("-map") + 1] == "0"
    assert "-0:2" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-disposition:a:0") + 1] == "default"
    assert not any(p.name.startswith(".jftools-audio-") for p in tmp_path.iterdir())


def test_remove_track_leaves_the_video_alone_on_failure(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=1, stderr="Invalid argument")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    ok, err = audio._remove_track("ffmpeg", str(video), 1, False)

    assert ok is False
    assert "Invalid argument" in err
    assert video.read_bytes() == b"old"
    assert not any(p.name.startswith(".jftools-audio-") for p in tmp_path.iterdir())


async def test_existing_route_forces_a_fresh_probe(audio_env, monkeypatch):
    _media, _video, path = audio_env
    seen = {}

    async def fake_tracks(p, force=False):
        seen.update(path=p, force=force)
        return _tracks(), None

    monkeypatch.setattr(audio, "audio_tracks_for", fake_tracks)
    resp = await audio.audio_existing(user="u", path=path)
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert data["file_mgmt"] is True
    assert [t["index"] for t in data["audio"]] == [1, 2]
    assert seen == {"path": path, "force": True}


# ---- Internal tracks --------------------------------------------------------


def _internal_streams():
    subtitles = [
        {
            "index": 4, "lang": "English", "name": "English",
            "forced": False, "default": True, "codec": "subrip",
            "disposition": {"default": True},
        },
        {
            "index": 5, "lang": "Swedish", "name": "Swedish",
            "forced": True, "default": False, "codec": "subrip",
            "disposition": {"forced": True},
        },
    ]
    tracks = [
        {
            "index": 1, "lang": "English", "name": "English (AAC stereo)",
            "default": True, "commentary": False,
            "disposition": {"default": True},
        },
        {
            "index": 2, "lang": "Swedish", "name": "Swedish (AAC stereo)",
            "default": False, "commentary": True,
            "disposition": {"comment": True},
        },
        {
            "index": 3, "lang": "Norwegian", "name": "Norwegian (AAC stereo)",
            "default": False, "commentary": False, "disposition": {},
        },
    ]
    return subtitles, tracks


async def test_internal_existing_returns_audio_and_embedded_subtitles(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    seen = {}

    async def fake_tracks(p, force=False):
        seen.update(path=p, force=force)
        return subtitles, tracks, None

    monkeypatch.setattr(audio, "internal_tracks_for", fake_tracks)
    resp = await audio.internal_existing(user="u", path=path)
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert data["file_mgmt"] is True
    assert data["subtitles"] == subtitles
    assert data["audio"] == tracks
    assert seen == {"path": path, "force": True}


async def test_internal_existing_returns_smart_marks(audio_env, monkeypatch):
    _media, _video, path = audio_env
    subtitles = [
        {"index": 4, "lang": "English", "name": "English SDH"},
        {"index": 5, "lang": "Unknown", "name": "Unknown"},
        {"index": 6, "lang": "French", "name": "French"},
        {"index": 7, "lang": "Unknown", "name": "Unknown (Czech czh)"},
    ]
    monkeypatch.setattr(config._settings, "opensubtitles_languages", "en,sv")

    async def fake_tracks(p, force=False):
        return subtitles, [], None

    monkeypatch.setattr(audio, "internal_tracks_for", fake_tracks)
    resp = await audio.internal_existing(user="u", path=path)
    data = json.loads(resp.body)

    assert data["preferred_languages"] == ["English", "Swedish"]
    assert data["smart_mark"] == [6, 7]


async def test_internal_existing_returns_external_files_and_smart_marks(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles = [
        {"index": 4, "lang": "English", "name": "English SDH"},
        {"index": 5, "lang": "French", "name": "French"},
    ]
    external = [
        {"file": "Movie (2020).English.srt", "lang": "English", "forced": False, "ai": False},
        {"file": "Movie (2020).German.srt", "lang": "German", "forced": False, "ai": False},
        {"file": "Movie (2020).Catalan.srt", "lang": "Catalan", "forced": False, "ai": False},
        {"file": "Movie (2020).forced.srt", "lang": "English", "forced": True, "ai": False},
    ]
    monkeypatch.setattr(config._settings, "opensubtitles_languages", "en,sv")

    async def fake_tracks(p, force=False):
        return subtitles, [], None

    async def fake_all(p):
        return subtitles, external

    monkeypatch.setattr(audio, "internal_tracks_for", fake_tracks)
    monkeypatch.setattr(audio, "subtitle_tracks_for", fake_all)
    resp = await audio.internal_existing(user="u", path=path)
    data = json.loads(resp.body)

    assert data["external"] == external
    assert data["smart_mark"] == [5]
    assert data["smart_mark_files"] == [
        "Movie (2020).German.srt",
        "Movie (2020).Catalan.srt",
        "Movie (2020).forced.srt",
    ]


async def test_internal_execute_deletes_multiple_and_sets_both_defaults(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    calls = {}
    entry = {
        "path": path, "file": "Movie (2020).mkv", "scanned": True,
        "audio": tracks[1:], "internal": subtitles[1:], "error": None,
    }

    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )

    def fake_rewrite(ffmpeg, local, deleted, audio_edits, subtitle_edits, ffprobe):
        calls.update(
            ffmpeg=ffmpeg, local=local, deleted=deleted,
            audio_edits=audio_edits, subtitle_edits=subtitle_edits,
        )
        return True, ""

    monkeypatch.setattr(audio, "_rewrite_tracks", fake_rewrite)
    monkeypatch.setattr(audio, "_probe_one", lambda ff, key, local, st: (key, entry))
    monkeypatch.setattr(audio, "_stat_path", lambda local: None)
    monkeypatch.setitem(audio._state, "files", {})

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path,
        delete="1,4", default_audio="2", default_subtitle="5",
    )
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert data["deleted"] == [1, 4]
    assert data["default_audio"] == 2
    assert data["default_subtitle"] == 5
    assert data["jellyfin"] == "offline"
    assert "Deleted 2 tracks." in data["message"]
    assert calls["deleted"] == {1, 4}
    # The new audio default keeps its commentary flag; the new subtitle
    # default keeps forced. Only those two dispositions changed.
    assert calls["audio_edits"] == [(0, "comment+default")]
    assert calls["subtitle_edits"] == [(0, "forced+default")]
    assert audio._state["files"][path] == entry

    from app import notifications

    items, _latest, _more = notifications.query(user="u")
    assert items[-1]["kind"] == "warning"
    assert items[-1]["title"] == "Track edit complete with warning"
    assert "Movie (2020).mkv" in items[-1]["message"]
    assert "offline" in items[-1]["message"]


async def test_internal_execute_publishes_an_error_notification(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    entry = {
        "path": path, "file": "Movie (2020).mkv", "scanned": True,
        "audio": tracks[1:], "internal": subtitles[1:], "error": None,
    }
    error = "ffmpeg error: [Errno 13] Permission denied: movie.mkv"
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )
    monkeypatch.setattr(audio, "_rewrite_tracks", lambda *args: (False, error))
    monkeypatch.setattr(audio, "_probe_one", lambda ff, key, local, st: (key, entry))
    monkeypatch.setattr(audio, "_stat_path", lambda local: None)
    monkeypatch.setitem(audio._state, "files", {})

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path,
        delete="1,4", default_audio="2", default_subtitle="5",
    )
    data = json.loads(resp.body)

    assert resp.status_code == 502
    assert data["ok"] is False
    assert data["notification_id"]

    from app import notifications

    items, _latest, _more = notifications.query(user="u")
    assert items[-1]["id"] == data["notification_id"]
    assert items[-1]["kind"] == "error"
    assert items[-1]["title"] == "Track edit failed"
    assert error in items[-1]["message"]


async def test_internal_execute_promotes_the_first_remaining_default(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles = []
    tracks = [
        {"index": 1, "name": "English", "default": True, "disposition": {"default": True}},
        {"index": 2, "name": "Swedish", "default": False, "disposition": {}},
    ]
    calls = {}
    entry = {"path": path, "file": "Movie (2020).mkv", "scanned": True,
             "audio": tracks[1:], "internal": [], "error": None}
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )

    def fake_rewrite(ffmpeg, local, deleted, audio_edits, subtitle_edits, ffprobe):
        calls.update(audio_edits=audio_edits, subtitle_edits=subtitle_edits)
        return True, ""

    monkeypatch.setattr(audio, "_rewrite_tracks", fake_rewrite)
    monkeypatch.setattr(audio, "_probe_one", lambda ff, key, local, st: (key, entry))
    monkeypatch.setattr(audio, "_stat_path", lambda local: None)
    monkeypatch.setitem(audio._state, "files", {})

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path, delete="1",
    )
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert data["default_audio"] == 2
    assert calls["audio_edits"] == [(0, "default")]
    assert "English" not in data["message"]
    assert "Swedish" in data["message"]


async def test_internal_execute_refuses_to_delete_every_audio_track(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )
    monkeypatch.setattr(
        audio, "_rewrite_tracks",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not rewrite")),
    )

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path, delete="1,2,3",
    )
    data = json.loads(resp.body)

    assert resp.status_code == 400
    assert "every audio track" in data["error"]


async def test_internal_execute_rejects_default_on_a_deleted_track(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path,
        delete="1", default_audio="1",
    )
    data = json.loads(resp.body)

    assert resp.status_code == 400
    assert "marked for deletion" in data["error"]


async def test_internal_execute_rejects_a_default_of_the_wrong_type(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path, default_audio="4",
    )
    data = json.loads(resp.body)

    assert resp.status_code == 403
    assert "not an audio track" in data["error"]


async def test_internal_execute_deletes_external_files_without_a_rewrite(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    external = [
        {"file": "Movie (2020).German.srt", "lang": "German", "forced": False, "ai": False},
    ]
    entry = {
        "path": path, "file": "Movie (2020).mkv", "scanned": True,
        "audio": tracks, "internal": subtitles, "external": [], "error": None,
    }
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )
    monkeypatch.setattr(audio, "_external_subtitles", lambda local: external)
    monkeypatch.setattr(
        audio, "_rewrite_tracks",
        lambda *a: (_ for _ in ()).throw(AssertionError("no rewrite expected")),
    )
    monkeypatch.setattr(audio, "_probe_one", lambda ff, key, local, st: (key, entry))
    monkeypatch.setattr(audio, "_stat_path", lambda local: None)
    monkeypatch.setitem(audio._state, "files", {})

    import app.files as files

    deleted_calls = {}

    def fake_delete(src, roots):
        deleted_calls["src"] = src
        return {"ok": True, "src": src}

    monkeypatch.setattr(files, "delete_entry", fake_delete)

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path,
        delete_files="Movie (2020).German.srt",
    )
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert data["deleted_files"] == ["Movie (2020).German.srt"]
    assert "Deleted 1 subtitle file." in data["message"]
    assert deleted_calls["src"].endswith("Movie (2020).German.srt")
    assert data["jellyfin"] == "offline"


async def test_internal_execute_rejects_an_unknown_external_file(
    audio_env, monkeypatch
):
    _media, _video, path = audio_env
    subtitles, tracks = _internal_streams()
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )
    monkeypatch.setattr(audio, "_external_subtitles", lambda local: [])
    monkeypatch.setattr(
        audio, "_rewrite_tracks",
        lambda *a: (_ for _ in ()).throw(AssertionError("no rewrite expected")),
    )

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path, delete_files="Nope.srt",
    )
    data = json.loads(resp.body)

    assert resp.status_code == 403
    assert "external subtitle file" in data["error"]


async def test_internal_execute_requires_file_management(audio_env, monkeypatch):
    monkeypatch.setattr(config._settings, "file_mgmt", False)
    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=audio_env[2], delete="1"
    )
    assert resp.status_code == 403


async def test_internal_execute_requires_the_security_header(audio_env):
    resp = await audio.internal_execute(
        user="u", x_jftools="", path=audio_env[2], delete="1"
    )
    assert resp.status_code == 403


def test_probe_one_stores_subtitle_default_and_drops_dispositions(
    tmp_path, monkeypatch
):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"")
    subtitles = [{
        "index": 3, "lang": "English", "name": "English",
        "forced": True, "default": True, "codec": "subrip",
        "disposition": {"default": True, "forced": True},
    }]
    tracks = [{
        "index": 1, "lang": "English", "name": "English (AAC stereo)",
        "default": True, "commentary": False, "disposition": {"default": True},
    }]
    monkeypatch.setattr(
        sub, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )
    monkeypatch.setattr(sub, "_external_subtitles", lambda local: [])

    _key, entry = sub._probe_one(
        "ffprobe", str(video), str(video), video.stat()
    )

    assert entry["internal"] == [{
        "index": 3, "lang": "English", "name": "English",
        "code": "", "title": "",
        "forced": True, "default": True, "codec": "subrip",
    }]
    assert "disposition" not in entry["audio"][0]
    assert entry["audio"][0]["default"] is True


def test_internal_rewrite_maps_deletions_and_dispositions(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    ok, err = audio._rewrite_tracks(
        "ffmpeg", str(video), {1, 3},
        [(0, "comment+default")], [(0, "forced+default")],
    )

    assert ok and err == ""
    assert video.read_bytes() == b"new"
    cmd = seen[0]
    assert cmd[cmd.index("-map") + 1] == "0"
    assert "-0:1" in cmd and "-0:3" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-disposition:a:0") + 1] == "comment+default"
    assert cmd[cmd.index("-disposition:s:0") + 1] == "forced+default"
    assert not any(p.name.startswith(".jftools-internal-") for p in tmp_path.iterdir())


def test_internal_rewrite_leaves_the_video_alone_on_failure(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=1, stderr="Invalid argument")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    ok, err = audio._rewrite_tracks("ffmpeg", str(video), {1}, [], [])

    assert ok is False
    assert "Invalid argument" in err
    assert video.read_bytes() == b"old"
    assert not any(p.name.startswith(".jftools-internal-") for p in tmp_path.iterdir())


def _side_files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith(".jftools-"))


def test_internal_rewrite_restores_the_original_when_the_swap_fails(
    tmp_path, monkeypatch
):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    real_rename = audio._retry_rename
    calls = []

    def flaky_rename(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError(errno.EACCES, "Permission denied", src, dst)
        return real_rename(src, dst)

    monkeypatch.setattr(audio, "_retry_rename", flaky_rename)

    ok, err = audio._rewrite_tracks("ffmpeg", str(video), {1}, [], [])

    assert ok is False
    assert "original was restored" in err
    assert video.read_bytes() == b"old"
    assert _side_files(tmp_path) == []


def test_internal_rewrite_keeps_a_backup_when_restore_also_fails(
    tmp_path, monkeypatch
):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    real_rename = audio._retry_rename
    calls = []

    def flaky_rename(src, dst):
        calls.append((src, dst))
        if len(calls) in (2, 3):
            raise OSError(errno.EACCES, "Permission denied", src, dst)
        return real_rename(src, dst)

    monkeypatch.setattr(audio, "_retry_rename", flaky_rename)

    ok, err = audio._rewrite_tracks("ffmpeg", str(video), {1}, [], [])

    backup = Path(audio._backup_path(str(video)))
    assert ok is False
    assert str(backup) in err
    assert "Restoring it also failed" in err
    assert not video.exists()
    assert backup.read_bytes() == b"old"


def test_internal_rewrite_rejects_invalid_output_before_the_swap(
    tmp_path, monkeypatch
):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    monkeypatch.setattr(
        audio, "_probe_streams",
        lambda ff, path: ([], [], 0.0, "Invalid data found"),
    )

    ok, err = audio._rewrite_tracks(
        "ffmpeg", str(video), {1}, [], [], "ffprobe"
    )

    assert ok is False
    assert "not valid" in err
    assert "Invalid data found" in err
    assert video.read_bytes() == b"old"
    assert _side_files(tmp_path) == []


def test_internal_rewrite_preserves_mode_and_mtime(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    video.chmod(0o640)
    before = video.stat()
    os.utime(video, ns=(before.st_atime_ns, 1_600_000_000_000_000_000))

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"new")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    ok, err = audio._rewrite_tracks("ffmpeg", str(video), {1}, [], [])

    after = video.stat()
    assert ok and err == ""
    assert video.read_bytes() == b"new"
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_mtime_ns == 1_600_000_000_000_000_000
    assert _side_files(tmp_path) == []


def test_retry_rename_retries_a_transient_lock(tmp_path, monkeypatch):
    src = tmp_path / "source.mkv"
    dst = tmp_path / "target.mkv"
    src.write_bytes(b"x")
    real_rename = os.rename
    attempts = []

    def flaky_rename(a, b):
        attempts.append((a, b))
        if len(attempts) == 1:
            raise OSError(errno.EACCES, "Permission denied", a, b)
        return real_rename(a, b)

    monkeypatch.setattr(audio.os, "rename", flaky_rename)
    monkeypatch.setattr(audio.time, "sleep", lambda _seconds: None)

    audio._retry_rename(str(src), str(dst))

    assert len(attempts) == 2
    assert dst.read_bytes() == b"x"


def test_recover_previous_swap_puts_the_original_back(tmp_path):
    video = tmp_path / "movie.mkv"
    backup = Path(audio._backup_path(str(video)))
    backup.write_bytes(b"original")

    error = audio._recover_previous_swap(str(video), None)

    assert error is None
    assert video.read_bytes() == b"original"
    assert not backup.exists()


def test_recover_previous_swap_discards_a_bad_replacement(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"bad")
    backup = Path(audio._backup_path(str(video)))
    backup.write_bytes(b"original")
    monkeypatch.setattr(
        audio, "_validate_media",
        lambda ffprobe, path: "bad" if path == str(video) else None,
    )

    error = audio._recover_previous_swap(str(video), "ffprobe")

    assert error is None
    assert video.read_bytes() == b"original"
    assert not backup.exists()
    assert _side_files(tmp_path) == []


async def test_internal_execute_recovers_an_interrupted_swap(audio_env, monkeypatch):
    _media, video, path = audio_env
    video.unlink()
    backup = Path(audio._backup_path(str(video)))
    backup.write_bytes(b"original")
    subtitles, tracks = _internal_streams()
    entry = {
        "path": path, "file": video.name, "scanned": True,
        "audio": tracks, "internal": subtitles, "error": None,
    }
    monkeypatch.setattr(
        audio, "_probe_streams", lambda ff, local: (subtitles, tracks, 1.0, None)
    )
    monkeypatch.setattr(audio, "_rewrite_tracks", lambda *args: (True, ""))
    monkeypatch.setattr(audio, "_probe_one", lambda ff, key, local, st: (key, entry))
    monkeypatch.setattr(audio, "_stat_path", lambda local: None)
    monkeypatch.setitem(audio._state, "files", {})

    resp = await audio.internal_execute(
        user="u", x_jftools="1", path=path, default_audio="2",
    )
    data = json.loads(resp.body)

    assert data["ok"] is True
    assert video.read_bytes() == b"original"
    assert not backup.exists()
