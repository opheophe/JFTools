"""Tests for the Tracks-pill Remux action."""

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import app.remux as remux
import app.audio as audio


def _allow_validation(monkeypatch, error=None):
    """Skip the real ffprobe validation in both commit helpers."""
    fake = lambda ffprobe, path: error  # noqa: E731
    monkeypatch.setattr(remux, "_validate_media", fake)
    monkeypatch.setattr(audio, "_validate_media", fake)


def _fake_ffmpeg(monkeypatch, payload=b"new", returncode=0, stderr=""):
    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(payload)
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(remux.subprocess, "run", fake_run)


def _layout(monkeypatch, streams, duration=60.0):
    monkeypatch.setattr(
        remux, "_probe_layout",
        lambda ffprobe, path: (streams, duration, None),
    )


def _side_files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith(".jftools-"))


def test_layout_error_accepts_an_identical_copy():
    streams = [("video", "h264"), ("audio", "aac")]
    assert remux._layout_error(streams, streams, 60.0, 60.2) is None


def test_layout_error_rejects_a_lost_stream():
    before = [("video", "h264"), ("audio", "aac")]
    after = [("video", "h264")]
    assert "streams went in" in remux._layout_error(before, after, 60.0, 60.0)


def test_layout_error_rejects_a_changed_stream_type():
    before = [("video", "h264"), ("audio", "aac")]
    after = [("video", "h264"), ("subtitle", "subrip")]
    assert "types changed" in remux._layout_error(before, after, 60.0, 60.0)


def test_layout_error_rejects_a_short_copy():
    streams = [("video", "h264")]
    assert "duration changed" in remux._layout_error(
        streams, streams, 600.0, 30.0
    )


def test_subtitle_codec_args_converts_only_text_formats():
    streams = [
        ("video", "h264"),
        ("audio", "aac"),
        ("subtitle", "mov_text"),
        ("subtitle", "hdmv_pgs_subtitle"),
        ("subtitle", "eia_608"),
        ("subtitle", "subrip"),
    ]
    assert remux._subtitle_codec_args(streams) == [
        "-c:s:0", "srt",
        "-c:s:2", "srt",
    ]


def test_remux_in_place_replaces_the_file_after_validation(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    _layout(monkeypatch, [("video", "h264"), ("audio", "aac")])
    _allow_validation(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    ok, err, final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok and err == ""
    assert final == str(video)
    assert video.read_bytes() == b"new"
    assert _side_files(tmp_path) == []


def test_remux_keeps_the_original_when_ffmpeg_fails(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    _layout(monkeypatch, [("video", "h264")])
    _allow_validation(monkeypatch)
    _fake_ffmpeg(monkeypatch, payload=b"partial", returncode=1, stderr="No space left on device")

    ok, err, _final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok is False
    assert "No space left on device" in err
    assert video.read_bytes() == b"old"
    assert _side_files(tmp_path) == []


def test_remux_keeps_the_original_when_a_stream_is_lost(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    before = [("video", "h264"), ("audio", "aac")]

    def fake_layout(ffprobe, path):
        if os.path.basename(path).startswith(".jftools-remux-"):
            return [("video", "h264")], 60.0, None
        return before, 60.0, None

    monkeypatch.setattr(remux, "_probe_layout", fake_layout)
    _allow_validation(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    ok, err, _final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok is False
    assert "did not verify" in err
    assert video.read_bytes() == b"old"
    assert _side_files(tmp_path) == []


def test_remux_rejects_an_invalid_copy_before_the_swap(tmp_path, monkeypatch):
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"old")
    _layout(monkeypatch, [("video", "h264")])
    _allow_validation(monkeypatch, "the new file is empty.")
    _fake_ffmpeg(monkeypatch)

    ok, err, _final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok is False
    assert "not valid" in err
    assert video.read_bytes() == b"old"
    assert _side_files(tmp_path) == []


def test_remux_to_mkv_renames_and_keeps_every_stream(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"old")
    _layout(monkeypatch, [("video", "h264"), ("audio", "aac")])
    _allow_validation(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    ok, err, final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok and err == ""
    assert final == str(tmp_path / "movie.mkv")
    assert Path(final).read_bytes() == b"new"
    assert not video.exists()
    assert _side_files(tmp_path) == []


def test_remux_restores_the_original_when_the_rename_to_mkv_fails(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"old")
    _layout(monkeypatch, [("video", "h264")])
    _allow_validation(monkeypatch)
    _fake_ffmpeg(monkeypatch)

    real_rename = remux._retry_rename
    calls = []

    def flaky_rename(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError(errno.EACCES, "Permission denied", src, dst)
        return real_rename(src, dst)

    monkeypatch.setattr(remux, "_retry_rename", flaky_rename)

    ok, err, _final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok is False
    assert "original was restored" in err
    assert video.read_bytes() == b"old"
    assert not (tmp_path / "movie.mkv").exists()
    assert _side_files(tmp_path) == []


def test_remux_refuses_to_overwrite_an_existing_mkv(tmp_path, monkeypatch):
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"old")
    existing = tmp_path / "movie.mkv"
    existing.write_bytes(b"keep")
    _layout(monkeypatch, [("video", "h264")])
    _fake_ffmpeg(monkeypatch)

    ok, err, _final = remux._remux_video("ffmpeg", "ffprobe", str(video))

    assert ok is False
    assert "already exists" in err
    assert video.read_bytes() == b"old"
    assert existing.read_bytes() == b"keep"
