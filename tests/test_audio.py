"""Tests for audio-track probing, the merged scan store and audio deletion."""

import json
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
                "tags": {"language": "eng", "title": "Surround"},
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
        "English (E-AC-3 5.1)*",
        "Swedish (AAC stereo)",
    ]
    assert tracks[0]["index"] == 1 and tracks[0]["default"] is True
    assert tracks[1]["code"] == "swe"
    assert subs == [{
        "index": 3, "lang": "English", "name": "English",
        "forced": True, "codec": "subrip",
    }]


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

    def fake_remove(ffmpeg, local, index, set_default):
        calls.update(ffmpeg=ffmpeg, local=local, index=index, set_default=set_default)
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
        "ffmpeg": "ffmpeg", "local": str(video), "index": 1, "set_default": True,
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
