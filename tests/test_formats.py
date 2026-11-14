"""Formats tab: compact format blobs, device profiles and the items endpoint."""
import json
from urllib.parse import urlencode

import httpx

import app.formats as formats
import app.jellyfin_id as jid
from app import backup
from app.config import Config, config
from app.main import app
from app.web import require_login


# ---- compact format ---------------------------------------------------------


def _item(streams, container="mkv"):
    return {
        "MediaSources": [
            {
                "Container": container,
                "MediaStreams": streams,
            }
        ]
    }


def test_compact_format_keeps_only_what_the_verdict_needs():
    item = _item([
        {
            "Type": "Video", "Codec": "AV1", "Profile": "Main",
            "Width": 1920, "Height": 1080, "BitDepth": 10,
            "VideoRange": "SDR", "VideoRangeType": "SDR",
        },
        {"Type": "Audio", "Codec": "Opus", "Channels": 2, "Language": "eng"},
        {"Type": "Audio", "Codec": "AAC", "Channels": 6, "Language": "swe"},
        {"Type": "Subtitle", "Codec": "subrip"},
    ], container="MP4")

    assert jid._compact_format(item) == {
        "c": "mp4",
        "v": {"c": "av1", "p": "Main", "w": 1920, "h": 1080, "d": 10, "r": "SDR"},
        "a": [
            {"c": "opus", "ch": 2, "l": "eng"},
            {"c": "aac", "ch": 6, "l": "swe"},
        ],
    }


def test_compact_format_prefers_the_first_video_stream():
    item = _item([
        {"Type": "Video", "Codec": "hevc", "Height": 2160},
        {"Type": "Video", "Codec": "h264", "Height": 480},
    ])
    assert jid._compact_format(item)["v"]["c"] == "hevc"


def test_compact_format_without_a_media_source_is_none():
    assert jid._compact_format({}) is None
    assert jid._compact_format({"MediaSources": []}) is None


def test_compact_format_handles_a_source_without_streams():
    assert jid._compact_format({"MediaSources": [{"Container": "avi"}]}) == {
        "c": "avi", "v": None, "a": [],
    }


def test_compact_format_survives_odd_jellyfin_values():
    # A string dimension must not abort the whole 26k-item fetch.
    item = _item([{
        "Type": "Video", "Codec": "H264", "Width": "1920",
        "Height": "abc", "BitDepth": "8",
    }])
    fmt = jid._compact_format(item)
    assert fmt["v"]["w"] == 1920
    assert fmt["v"]["h"] == 0
    assert fmt["v"]["d"] == 8


# ---- device profiles --------------------------------------------------------


def test_format_profiles_round_trip_and_export(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_PATH_MAP", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    profile = {
        "containers": ["mkv", "mp4"],
        "video": {"h264": 8, "hevc": 10},
        "audio": ["aac", "ac3"],
    }
    cfg = Config()
    cfg.save_format_profile("Bedroom TV", profile)

    # A later settings save writes config.json as a whole; keep the profile.
    cfg.save_file_mgmt(True)
    cfg2 = Config()
    assert cfg2.settings.format_profiles["Bedroom TV"] == profile

    # It travels with the settings export.
    exported = cfg2.export_settings()
    assert exported["format_profiles"]["Bedroom TV"] == profile
    cfg3 = Config()
    cfg3.save_format_profile("Bedroom TV", None)
    assert cfg3.settings.format_profiles == {}
    cfg3.import_settings(exported)
    assert cfg3.settings.format_profiles["Bedroom TV"] == profile

    # Deleting removes only that profile.
    cfg3.save_format_profile("Other", {"containers": ["mp4"], "video": {}, "audio": []})
    cfg3.save_format_profile("Bedroom TV", None)
    assert "Bedroom TV" not in cfg3.settings.format_profiles
    assert "Other" in cfg3.settings.format_profiles


def test_format_profiles_are_rebuilt_from_known_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    cfg.save_format_profile("TV", {
        "containers": ["MKV", "mkv", ""],
        "video": {"H264": "8", "hevc": 10, "bad": "x", "zero": 0},
        "audio": ["AAC", "aac"],
        "junk": "dropped",
    })

    assert cfg.settings.format_profiles["TV"] == {
        "containers": ["mkv"],
        "video": {"h264": 8, "hevc": 10},
        "audio": ["aac"],
    }


def test_profiles_merge_saved_over_builtin(monkeypatch):
    override = {"containers": ["mp4"], "video": {"h264": 8}, "audio": ["aac"]}
    monkeypatch.setattr(config._settings, "format_profiles", {"Vardagsrum TV": override})

    profiles = formats._profiles()

    assert profiles["Vardagsrum TV"] == override
    # The other built-ins survive.
    assert "Strict" in profiles
    assert formats._saved_names() == ["Vardagsrum TV"]


# ---- endpoints --------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_formats_page_renders(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        async with _client() as client:
            response = await client.get("/formats")
        assert response.status_code == 200
        assert "/static/formats.js" in response.text
        assert 'id="fo-device"' in response.text
        assert 'id="fo-probe"' in response.text
        assert 'id="fo-corrupt"' in response.text
    finally:
        app.dependency_overrides.pop(require_login, None)


async def test_items_endpoint_returns_formats_without_scans(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        monkeypatch.setattr(jid, "_state", {
            "rows": [{
                "id": "m1", "type": "Movie", "path": "/Filmer/Dune/Dune.mkv",
                "file": "Dune.mkv", "name": "Dune", "series": "",
                "display": "Dune (2021)", "year": 2021, "library": "Filmer",
                "fmt": {"c": "mkv", "v": {"c": "av1", "p": "Main",
                                          "w": 1920, "h": 1080, "d": 8, "r": "SDR"},
                        "a": [{"c": "opus", "ch": 2, "l": "eng"}]},
            }],
            "last_sync": 1234.0,
        })
        monkeypatch.setattr(config._settings, "path_maps", [])
        monkeypatch.setattr(formats, "scans_map", lambda: {})

        async with _client() as client:
            response = await client.get("/api/formats/items")

        assert response.status_code == 200
        data = response.json()
        assert data["fetched"] is True
        assert data["last_sync"] == 1234.0
        assert data["rows"][0]["fmt"]["v"]["c"] == "av1"
        # The full scan store is megabytes; Formats only gets a tiny summary.
        assert "scans" not in data
        assert "scan" not in data["rows"][0]
        assert "nosrc" not in data["rows"][0]
        assert "Vardagsrum TV" in data["profiles"]
        assert data["builtin"]
    finally:
        app.dependency_overrides.pop(require_login, None)


async def test_items_endpoint_sends_only_the_scan_summary(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    try:
        monkeypatch.setattr(jid, "_state", {
            "rows": [{
                "id": "m1", "type": "Movie", "path": "/Filmer/Dune/Dune.mkv",
                "file": "Dune.mkv", "name": "Dune", "library": "Filmer",
                "fmt": {"c": "mkv", "v": None, "a": []},
                "nosrc": True,
            }],
            "last_sync": None,
        })
        monkeypatch.setattr(config._settings, "path_maps", [])
        monkeypatch.setattr(formats, "scans_map", lambda: {
            "/Filmer/Dune/Dune.mkv": {
                "error": "ffprobe exit 1: moov atom not found",
                "scanned": False,
                "duration": 0,
                "size": 0,
                # A real entry also carries subtitle and audio tracks; the
                # Formats payload must not inherit them.
                "internal": [{"lang": "eng"}],
                "audio": [{"c": "aac"}],
            },
        })

        async with _client() as client:
            response = await client.get("/api/formats/items")

        row = response.json()["rows"][0]
        assert row["scan"] == {
            "e": "ffprobe exit 1: moov atom not found",
            "s": False,
            "d": 0.0,
            "z": 0,
        }
        assert row["nosrc"] is True
    finally:
        app.dependency_overrides.pop(require_login, None)


def test_scan_summary_tolerates_missing_and_odd_values():
    assert formats._scan_summary(None) is None
    assert formats._scan_summary("not an entry") is None
    assert formats._scan_summary({"duration": "abc"}) == {
        "e": "", "s": False, "d": 0.0, "z": None,
    }


# ---- probing ---------------------------------------------------------------


def test_bit_depth_from_explicit_fields_and_pixel_format():
    assert formats._bit_depth({"bit_depth": 10}) == 10
    assert formats._bit_depth({"bits_per_raw_sample": "12"}) == 12
    assert formats._bit_depth({"pix_fmt": "yuv420p10le"}) == 10
    assert formats._bit_depth({"pix_fmt": "p010le"}) == 10
    assert formats._bit_depth({"pix_fmt": "gray10le"}) == 10
    assert formats._bit_depth({"pix_fmt": "yuv420p"}) == 8
    assert formats._bit_depth({}) == 0


def test_video_range_from_colour_metadata():
    assert formats._video_range({"color_transfer": "smpte2084"}) == "HDR10"
    assert formats._video_range({"color_transfer": "arib-std-b67"}) == "HLG"
    assert formats._video_range(
        {"side_data_list": [{"side_data_type": "DOVI configuration record"}]}
    ) == "DOVI"
    assert formats._video_range({"pix_fmt": "yuv420p"}) == "SDR"


def test_needs_probe_only_when_no_stream_info():
    # Missing blob (older cache) and an empty stream list both need a probe.
    assert formats._needs_probe({"path": "/a.mkv"}) is True
    assert formats._needs_probe({"fmt": None}) is True
    assert formats._needs_probe({"fmt": {"c": "mkv", "v": None, "a": []}}) is True
    # Genuine audio-only and normal files do not.
    assert formats._needs_probe(
        {"fmt": {"c": "mkv", "v": None, "a": [{"c": "opus", "ch": 2, "l": ""}]}}
    ) is False
    assert formats._needs_probe(
        {"fmt": {"c": "mkv", "v": {"c": "h264"}, "a": []}}
    ) is False


def test_probe_pending_caps_work_and_keeps_failures(monkeypatch):
    rows = [
        {"path": "/a.mkv", "fmt": {"c": "mkv", "v": None, "a": []}},
        {"path": "/b.mkv", "fmt": None},
        {"path": "/c.mkv", "fmt": {"c": "mkv", "v": None, "a": []}},
        {"path": "/ok.mkv", "fmt": {"c": "mkv", "v": {"c": "h264"}, "a": []}},
    ]
    monkeypatch.setattr(jid, "_state", {"rows": rows})
    monkeypatch.setattr(formats, "resolve_path", lambda path, maps: path)
    monkeypatch.setattr(formats.os.path, "isfile", lambda path: path != "/b.mkv")

    def fake_probe(ffprobe, local):
        return {"c": "mkv", "v": {"c": "h264"}, "a": [{"c": "aac", "ch": 2, "l": ""}]}

    monkeypatch.setattr(formats, "_ffprobe_format", fake_probe)

    result = formats._probe_pending("ffprobe", [], limit=1)

    assert result == {"probed": 1, "failed": 0, "remaining": 2}
    assert rows[0]["fmt"]["v"]["c"] == "h264"
    # The capped rows and the untouched good row keep their old data.
    assert rows[2]["fmt"]["v"] is None
    assert rows[3]["fmt"] == {"c": "mkv", "v": {"c": "h264"}, "a": []}


async def test_probe_endpoint_probes_and_saves(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    rows = [
        {"path": "/Serier/A.mkv", "fmt": {"c": "mkv", "v": None, "a": []}},
        {"path": "/Serier/B.mkv", "fmt": {"c": "mkv", "v": {"c": "h264"}, "a": []}},
        {"path": "/Serier/C.mkv"},
    ]
    monkeypatch.setattr(jid, "_state", {"rows": rows})
    monkeypatch.setattr(jid, "_fetch", {"status": "idle"})
    monkeypatch.setattr(formats, "resolve_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(formats, "resolve_path", lambda path, maps: path)
    monkeypatch.setattr(formats.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(
        formats, "_ffprobe_format",
        lambda ffprobe, local: {
            "c": "mkv",
            "v": {"c": "h264", "p": "High", "w": 720, "h": 576, "d": 8, "r": "SDR"},
            "a": [{"c": "ac3", "ch": 2, "l": ""}],
        },
    )
    saved = []
    monkeypatch.setattr(jid, "_save_state", lambda: saved.append(True))
    formats._probe["running"] = False
    try:
        async with _client() as client:
            response = await client.post("/api/formats/probe")

        assert response.status_code == 200
        assert response.json() == {
            "ok": True, "probed": 2, "failed": 0, "remaining": 0,
        }
        assert rows[0]["fmt"]["v"]["c"] == "h264"
        assert rows[2]["fmt"]["v"]["c"] == "h264"
        assert saved == [True]
    finally:
        formats._probe["running"] = False
        app.dependency_overrides.pop(require_login, None)


async def test_probe_blocked_after_restore(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    monkeypatch.setattr(backup, "RESTORED", True)
    try:
        async with _client() as client:
            response = await client.post("/api/formats/probe")
        assert response.status_code == 409
        assert "Restart" in response.json()["error"]
    finally:
        monkeypatch.setattr(backup, "RESTORED", False)
        app.dependency_overrides.pop(require_login, None)


async def test_probe_endpoint_refuses_while_fetching_or_without_ffprobe(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    monkeypatch.setattr(jid, "_fetch", {"status": "running"})
    try:
        async with _client() as client:
            response = await client.post("/api/formats/probe")
        assert response.status_code == 409

        monkeypatch.setattr(jid, "_fetch", {"status": "idle"})
        monkeypatch.setattr(formats, "resolve_ffprobe", lambda: None)
        async with _client() as client:
            response = await client.post("/api/formats/probe")
        assert response.status_code == 400
        assert "ffprobe" in response.json()["error"]
    finally:
        app.dependency_overrides.pop(require_login, None)


async def test_profiles_endpoint_saves_and_deletes(monkeypatch):
    app.dependency_overrides[require_login] = lambda: "tester"
    calls = []
    saved: dict = {}

    def fake_save(name, profile):
        calls.append((name, profile))
        if profile is None:
            saved.pop(name, None)
        else:
            saved[name] = profile

    monkeypatch.setattr(config, "save_format_profile", fake_save)
    monkeypatch.setattr(formats, "_profiles", lambda: dict(saved))
    monkeypatch.setattr(formats, "_saved_names", lambda: sorted(saved))
    try:
        async with _client() as client:
            body = urlencode({
                "name": "Bedroom TV",
                "profile": json.dumps(
                    {"containers": ["mp4"], "video": {"h264": 8}, "audio": ["aac"]}
                ),
            })
            response = await client.post("/api/formats/profiles", content=body,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert response.status_code == 200
            assert response.json()["ok"] is True
            assert calls[-1][0] == "Bedroom TV"

            response = await client.post("/api/formats/profiles", content=urlencode({"name": "Bedroom TV"}),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert response.status_code == 200
            assert calls[-1] == ("Bedroom TV", None)

            response = await client.post("/api/formats/profiles", content=urlencode({"profile": "{}"}),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(require_login, None)
