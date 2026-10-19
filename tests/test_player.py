"""The player: streaming, subtitle conversion, and the path guards.

Playback is read-only, so these routes need a login but not the File management
setting — but they still must never serve a file outside the media roots.
"""
import json
import os
import subprocess
import time

import httpx
import pytest

from app import player
from app.config import config
from app.main import app
from app.web import require_login


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def logged_in():
    app.dependency_overrides[require_login] = lambda: "tester"
    yield
    app.dependency_overrides.pop(require_login, None)


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """One tmp dir acting as the only browsable media root."""
    monkeypatch.setattr(player, "browser_roots", lambda: [str(tmp_path)])
    monkeypatch.setattr(config.settings, "path_maps", [])
    return tmp_path


def _video(root, name="movie.mp4", data=b"RIFF....WAVEfmt "):
    path = root / name
    path.write_bytes(data)
    return path


# ---- subtitle conversion -----------------------------------------------------


def test_srt_to_vtt_rewrites_timestamps_and_adds_the_header():
    text = (
        "1\r\n"
        "00:00:01,000 --> 00:00:03,500\r\n"
        "Hello there.\r\n"
        "\r\n"
        "2\r\n"
        "01:02:03,250 --> 01:02:05,000\r\n"
        "Second cue.\r\n"
    )
    vtt = player.srt_to_vtt(text)
    assert vtt.startswith("WEBVTT\n\n")
    assert "00:00:01.000 --> 00:00:03.500" in vtt
    assert "01:02:03.250 --> 01:02:05.000" in vtt
    assert "Hello there." in vtt
    assert "," not in vtt.split("\n")[2]


def test_srt_to_vtt_strips_a_byte_order_mark():
    vtt = player.srt_to_vtt("\ufeff1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    assert vtt.startswith("WEBVTT")
    assert "\ufeff" not in vtt


def test_srt_to_vtt_copes_with_out_of_spec_timestamps():
    # Hours missing, a dot separator, and a two-digit millisecond field: a
    # browser quietly drops cues it cannot parse, which looks like "no
    # subtitles" rather than an error.
    vtt = player.srt_to_vtt("1\n00:01,5 --> 00:03,25\nOdd one.\n")
    assert "00:00:01.500 --> 00:00:03.250" in vtt
    vtt = player.srt_to_vtt("1\n1:02:03.5 --> 1:02:04.75\nDotted.\n")
    assert "01:02:03.500 --> 01:02:04.750" in vtt


def test_read_text_falls_back_to_cp1252(tmp_path):
    path = tmp_path / "latin.srt"
    path.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nCaf\xe9\n".encode("cp1252"))
    assert "Café" in player._read_text(str(path))


def test_external_srt_is_served_as_vtt(tmp_path):
    (tmp_path / "movie.mp4").write_bytes(b"x")
    (tmp_path / "movie.en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8"
    )
    vtt = player._vtt_external(str(tmp_path / "movie.mp4"), "movie.en.srt")
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000" in vtt


def test_an_unknown_subtitle_file_is_refused(tmp_path):
    (tmp_path / "movie.mp4").write_bytes(b"x")
    (tmp_path / "other.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    with pytest.raises(player.SubError):
        player._vtt_external(str(tmp_path / "movie.mp4"), "other.srt")


def test_image_subtitle_files_are_refused(tmp_path):
    (tmp_path / "movie.mp4").write_bytes(b"x")
    (tmp_path / "movie.en.sup").write_bytes(b"\x00" * 4096)
    with pytest.raises(player.SubError):
        player._vtt_external(str(tmp_path / "movie.mp4"), "movie.en.sup")


# ---- track specs -------------------------------------------------------------


def test_parse_spec_splits_kind_and_key():
    assert player._parse_spec("external:movie.en.srt") == ("external", "movie.en.srt")
    assert player._parse_spec("internal:3") == ("internal", "3")
    assert player._parse_spec("") == ("", "")


def test_internal_spec_prefers_the_stream_index():
    assert player._internal_spec({"index": 4}, 0) == "internal:4"
    # A store written before the index was kept falls back to the ordinal.
    assert player._internal_spec({}, 2) == "sub:2"


def test_hms_to_seconds():
    assert player._hms_to_seconds("00:01:30.5") == 90.5
    assert player._hms_to_seconds("nonsense") == 0.0


# ---- path guard --------------------------------------------------------------


def test_video_outside_the_media_roots_is_refused(media_root, tmp_path_factory):
    inside = _video(media_root)
    assert player._local_video(str(inside)) == os.path.realpath(str(inside))

    outside = tmp_path_factory.mktemp("elsewhere") / "movie.mp4"
    outside.write_bytes(b"x")
    assert player._local_video(str(outside)) is None


def test_missing_and_empty_paths_are_refused(media_root):
    assert player._local_video(str(media_root / "gone.mp4")) is None
    assert player._local_video("") is None


def test_media_type_covers_matroska(media_root):
    assert player._video_media_type("a/b/movie.mkv") == "video/x-matroska"
    assert player._video_media_type("a/b/movie.mp4") == "video/mp4"


# ---- routes ------------------------------------------------------------------


async def test_player_page_requires_a_login():
    async with _client() as client:
        resp = await client.get("/play", params={"path": "/media/movie.mkv"})
    assert resp.status_code == 307
    assert resp.headers["location"] in ("/login", "/setup")


async def test_player_api_requires_a_login():
    async with _client() as client:
        resp = await client.get("/api/player/tracks", params={"path": "/media/movie.mkv"})
    assert resp.status_code == 401


async def test_stream_serves_the_file_and_honours_range(logged_in, media_root):
    video = _video(media_root, data=b"0123456789")
    async with _client() as client:
        full = await client.get("/api/player/stream", params={"path": str(video)})
        part = await client.get(
            "/api/player/stream",
            params={"path": str(video)},
            headers={"Range": "bytes=2-5"},
        )
    assert full.status_code == 200
    assert full.content == b"0123456789"
    assert full.headers["accept-ranges"] == "bytes"
    assert part.status_code == 206
    assert part.content == b"2345"
    assert part.headers["content-range"] == "bytes 2-5/10"


async def test_stream_refuses_a_file_outside_the_media_roots(
    logged_in, media_root, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside") / "movie.mp4"
    outside.write_bytes(b"secret")
    async with _client() as client:
        resp = await client.get("/api/player/stream", params={"path": str(outside)})
    assert resp.status_code == 404


async def test_tracks_list_embedded_and_external(logged_in, media_root, monkeypatch):
    video = _video(media_root, name="movie.mkv")
    (media_root / "movie.en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8"
    )

    async def fake_tracks(path):
        return (
            [
                {"index": 3, "lang": "Swedish", "name": "Swedish", "forced": False, "codec": "subrip"},
                {"index": 4, "lang": "English", "name": "English", "forced": False, "codec": "hdmv_pgs_subtitle"},
            ],
            [{"file": "movie.en.srt", "lang": "English", "forced": False, "ai": False, "size": 40}],
        )

    monkeypatch.setattr(player, "subtitle_tracks_for", fake_tracks)

    async with _client() as client:
        resp = await client.get("/api/player/tracks", params={"path": str(video)})
    data = resp.json()
    assert resp.status_code == 200
    assert data["file"] == "movie.mkv"

    specs = [t["spec"] for t in data["tracks"]]
    assert specs == ["internal:3", "internal:4", "external:movie.en.srt"]
    # Bitmap subtitles can be listed but not shown in a browser.
    assert data["tracks"][1]["playable"] is False
    assert data["tracks"][2]["playable"] is True
    assert data["tracks"][2]["label"] == "English — movie.en.srt"
    # The English .srt is what should be showing when the window opens.
    assert data["tracks"][2].get("default") is True
    assert "default" not in data["tracks"][0]
    assert "default" not in data["tracks"][1]


async def test_tracks_route_needs_a_reachable_video(logged_in, media_root):
    async with _client() as client:
        resp = await client.get(
            "/api/player/tracks", params={"path": str(media_root / "gone.mkv")}
        )
    assert resp.status_code == 404


async def test_tracks_route_reports_audio_the_browser_cannot_decode(
    logged_in, media_root, monkeypatch
):
    """DTS and TrueHD play with picture and no sound, so the page is told."""
    video = _video(media_root, name="movie.mkv")

    async def no_tracks(path):
        return ([], [])

    monkeypatch.setattr(player, "subtitle_tracks_for", no_tracks)
    monkeypatch.setattr(player, "_play_cache_dir", lambda: media_root / "cache")
    monkeypatch.setattr(
        player, "_audio_info",
        lambda local: {"codec": "dts", "channels": 6, "layout": "5.1(side)"},
    )

    async with _client() as client:
        resp = await client.get("/api/player/tracks", params={"path": str(video)})
    data = resp.json()
    assert data["audio"]["codec"] == "dts"
    assert data["needs_copy"] is True
    assert data["copy_reason"] == "DTS audio"
    assert data["copy_ready"] is False


async def test_tracks_route_leaves_audio_the_browser_can_decode_alone(
    logged_in, media_root, monkeypatch
):
    video = _video(media_root, name="movie.mkv")

    async def no_tracks(path):
        return ([], [])

    monkeypatch.setattr(player, "subtitle_tracks_for", no_tracks)
    monkeypatch.setattr(
        player, "_audio_info",
        lambda local: {"codec": "aac", "channels": 2, "layout": "stereo"},
    )

    async with _client() as client:
        resp = await client.get("/api/player/tracks", params={"path": str(video)})
    data = resp.json()
    assert data["needs_copy"] is False
    assert data["copy_reason"] == ""


async def test_tracks_route_reports_a_browser_copy_that_already_exists(
    logged_in, media_root, monkeypatch
):
    video = _video(media_root, name="movie.mkv")

    async def no_tracks(path):
        return ([], [])

    monkeypatch.setattr(player, "subtitle_tracks_for", no_tracks)
    monkeypatch.setattr(player, "_play_cache_dir", lambda: media_root / "cache")
    monkeypatch.setattr(
        player, "_audio_info", lambda local: {"codec": "dts", "channels": 6, "layout": ""}
    )
    cached = player._cache_file(str(video), str(video))
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"mp4")

    async with _client() as client:
        resp = await client.get("/api/player/tracks", params={"path": str(video)})
    assert resp.json()["copy_ready"] is True


def test_pick_default_prefers_the_configured_languages(monkeypatch):
    monkeypatch.setattr(
        player, "_preferred_languages", lambda: ["en", "english", "sv", "swedish"]
    )
    tracks = [
        {"kind": "internal", "lang": "Swedish", "forced": False, "playable": True},
        {"kind": "external", "lang": "Swedish", "forced": False, "playable": True},
        {"kind": "external", "lang": "English", "forced": False, "playable": True},
    ]
    assert player._pick_default(tracks) == 2


def test_pick_default_skips_unplayable_tracks_and_falls_back(monkeypatch):
    monkeypatch.setattr(player, "_preferred_languages", lambda: [])
    tracks = [
        {"kind": "internal", "lang": "English", "forced": False, "playable": False},
        {"kind": "internal", "lang": "English", "forced": False, "playable": True},
        {"kind": "external", "lang": "Korean", "forced": False, "playable": True},
    ]
    assert player._pick_default(tracks) == 2
    assert player._pick_default(tracks[:2]) == 1
    assert player._pick_default(tracks[:1]) == -1
    assert player._pick_default([]) == -1


# ---- browser copies ----------------------------------------------------------


def test_prepared_file_is_none_until_one_exists(media_root, monkeypatch):
    video = _video(media_root)
    monkeypatch.setattr(player, "_play_cache_dir", lambda: media_root / "cache")
    assert player._prepared_file(str(video)) is None

    cached = player._cache_file(str(video), str(video))
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"mp4")
    assert player._prepared_file(str(video)) == str(cached)


def test_prune_play_cache_drops_old_and_oversized_files(media_root, monkeypatch):
    cache = media_root / "cache"
    cache.mkdir()
    monkeypatch.setattr(player, "_play_cache_dir", lambda: cache)
    monkeypatch.setattr(player, "PLAY_CACHE_MAX_BYTES", 10)

    old = cache / "old.mp4"
    old.write_bytes(b"x" * 8)
    os.utime(old, (1000, 1000))
    now = time.time()
    large = cache / "large.mp4"
    large.write_bytes(b"y" * 8)
    os.utime(large, (now - 600, now - 600))
    recent = cache / "recent.mp4"
    recent.write_bytes(b"z" * 4)
    os.utime(recent, (now, now))

    player.prune_play_cache()

    assert not old.is_file()          # older than a week
    assert not large.is_file()        # kept the dir under the byte cap
    assert recent.is_file()


def test_prune_play_cache_keeps_the_newest_copy_even_over_the_cap(media_root, monkeypatch):
    cache = media_root / "cache"
    cache.mkdir()
    monkeypatch.setattr(player, "_play_cache_dir", lambda: cache)
    monkeypatch.setattr(player, "PLAY_CACHE_MAX_BYTES", 10)

    now = time.time()
    older = cache / "older.mp4"
    older.write_bytes(b"y" * 8)
    os.utime(older, (now - 600, now - 600))
    # A feature film's copy is nearly as large as the film, so the newest one can
    # exceed the whole cap on its own; it must survive or every play remuxes it.
    newest = cache / "newest.mp4"
    newest.write_bytes(b"z" * 64)
    os.utime(newest, (now, now))

    player.prune_play_cache()

    assert not older.is_file()
    assert newest.is_file()


# ---- the audio probe ---------------------------------------------------------


def test_audio_from_probe_reads_the_first_audio_stream():
    payload = json.dumps({"streams": [
        {"codec_name": "DTS", "channels": 6, "channel_layout": "5.1(side)"},
    ]})
    assert player._audio_from_probe(payload) == {
        "codec": "dts", "channels": 6, "layout": "5.1(side)",
    }


def test_audio_from_probe_copes_with_no_audio_and_broken_output():
    assert player._audio_from_probe("") == {}
    assert player._audio_from_probe("not json") == {}
    assert player._audio_from_probe(json.dumps({"streams": []})) == {}
    # A stream with no channel count is still a stream ffprobe found.
    assert player._audio_from_probe(json.dumps({"streams": [{"codec_name": "aac"}]})) == {
        "codec": "aac", "channels": 0, "layout": "",
    }


def test_probe_audio_asks_ffprobe_for_the_first_audio_stream(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"streams": [{"codec_name": "truehd"}]}), ""
        )

    monkeypatch.setattr(player.subprocess, "run", fake_run)
    assert player._probe_audio("ffprobe", "movie.mkv")["codec"] == "truehd"
    assert "-select_streams" in seen["cmd"]
    assert "a:0" in seen["cmd"]
    assert seen["cmd"][-1] == "movie.mkv"


def test_probe_audio_gives_none_when_ffprobe_fails(monkeypatch):
    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(player.subprocess, "run", failing)
    assert player._probe_audio("ffprobe", "movie.mkv") is None

    def missing(cmd, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(player.subprocess, "run", missing)
    assert player._probe_audio("ffprobe", "movie.mkv") is None


def test_audio_info_probes_a_file_once(media_root, monkeypatch):
    video = _video(media_root)
    calls = []

    def fake_probe(ffprobe, local):
        calls.append(local)
        return {"codec": "dts", "channels": 6, "layout": "5.1(side)"}

    monkeypatch.setattr(player, "_AUDIO_CACHE", {})
    monkeypatch.setattr(player, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(player, "_probe_audio", fake_probe)

    assert player._audio_info(str(video))["codec"] == "dts"
    assert player._audio_info(str(video))["codec"] == "dts"
    assert len(calls) == 1


def test_audio_info_does_not_remember_a_failed_probe(media_root, monkeypatch):
    video = _video(media_root)
    monkeypatch.setattr(player, "_AUDIO_CACHE", {})
    monkeypatch.setattr(player, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(player, "_probe_audio", lambda ffprobe, local: None)
    assert player._audio_info(str(video)) is None
    assert player._AUDIO_CACHE == {}


def test_audio_info_without_ffprobe_is_unknown(media_root, monkeypatch):
    video = _video(media_root)
    monkeypatch.setattr(player, "_AUDIO_CACHE", {})
    monkeypatch.setattr(player, "resolve_ffprobe", lambda: None)
    assert player._audio_info(str(video)) is None


def test_audio_copy_reason_flags_only_what_no_browser_can_decode():
    assert player.audio_copy_reason({"codec": "dts"}) == "DTS audio"
    assert player.audio_copy_reason({"codec": "truehd"}) == "TrueHD audio"
    assert player.audio_copy_reason({"codec": "mlp"}) == "MLP audio"
    # Everything else gets a chance to play: the page watches for silence. Some
    # browsers decode AC-3 and E-AC-3 through the operating system and some do
    # not, which is not something ffprobe can answer.
    for codec in ("aac", "ac3", "eac3", "opus", "flac", "pcm_s16le", "wmav2", ""):
        assert player.audio_copy_reason({"codec": codec}) == ""
    assert player.audio_copy_reason({}) == ""
    assert player.audio_copy_reason(None) == ""
