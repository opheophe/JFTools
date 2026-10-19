from app.config import Config


def test_needs_setup_until_local_login_created(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_USERNAME", raising=False)
    monkeypatch.delenv("JFTOOLS_PASSWORD", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert cfg.needs_setup

    cfg.save_auth("alice", "s3cret-pw")
    assert not cfg.needs_setup
    assert cfg.check_password("alice", "s3cret-pw")
    assert not cfg.check_password("alice", "wrong")


def test_jellyfin_configured_requires_url_and_key(tmp_path, monkeypatch):
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert not cfg.jellyfin_configured

    cfg.save_settings("http://jellyfin:8096", "")
    assert not cfg.jellyfin_configured

    cfg.save_settings("http://jellyfin:8096", "abc123")
    assert cfg.jellyfin_configured


def test_env_credentials_skip_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JFTOOLS_USERNAME", "admin")
    monkeypatch.setenv("JFTOOLS_PASSWORD", "change-me")

    cfg = Config()
    assert not cfg.needs_setup
    assert cfg.check_password("admin", "change-me")
    assert not cfg.check_password("admin", "wrong")


def test_path_maps_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_PATH_MAP", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    cfg.save_path_maps([{"from": "/Filmer", "to": "Z:\\Filmer"}])
    assert cfg.settings.path_maps == [{"from": "/Filmer", "to": "Z:\\Filmer"}]

    # Reloaded from disk.
    cfg2 = Config()
    assert cfg2.settings.path_maps == [{"from": "/Filmer", "to": "Z:\\Filmer"}]

    # Export -> import round trip.
    exported = cfg.export_settings()
    cfg3 = Config()
    cfg3.save_path_maps([])
    cfg3.import_settings(exported)
    assert cfg3.settings.path_maps == [{"from": "/Filmer", "to": "Z:\\Filmer"}]


def test_filter_profiles_round_trip_and_export(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_PATH_MAP", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    profile = {
        "filters": {"type": ["Movie"], "audioRe": "swe"},
        "sort": {"col": "file", "dir": 1},
    }
    cfg = Config()
    cfg.save_filter_profiles("search", {"Mine": profile})

    # Another settings save must keep them: config.json is written as a whole.
    cfg.save_file_mgmt(True)
    cfg2 = Config()
    assert cfg2.settings.filter_profiles["search"]["Mine"] == profile

    # They travel with the settings export.
    exported = cfg2.export_settings()
    assert exported["filter_profiles"]["search"]["Mine"] == profile
    cfg3 = Config()
    cfg3.save_filter_profiles("search", {})
    cfg3.import_settings(exported)
    assert cfg3.settings.filter_profiles["search"]["Mine"] == profile

    # An older export without the key leaves the current profiles alone.
    cfg3.save_filter_profiles("moviematch", {"Other": {"filters": {}}})
    cfg3.import_settings({"timezone": "UTC"})
    assert cfg3.settings.filter_profiles["moviematch"]["Other"] == {"filters": {}}
    assert cfg3.settings.filter_profiles["search"]["Mine"] == profile


def test_filter_profiles_drop_invalid_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    cfg.save_filter_profiles("search", {"ok": {"filters": {}}, "bad": "nope", "": {}})

    assert cfg.settings.filter_profiles["search"] == {"ok": {"filters": {}}}


def test_backup_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_PATH_MAP", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert cfg.settings.backup_keep == 7
    assert cfg.settings.backup_dir == ""
    cfg.save_backup_settings(str(tmp_path / "backups"), 3, True)

    cfg2 = Config()
    assert cfg2.settings.backup_dir == str(tmp_path / "backups")
    assert cfg2.settings.backup_keep == 3
    assert cfg2.settings.backup_include_logs is True

    # Another settings save must not drop the backup fields.
    cfg2.save_file_mgmt(True)
    cfg3 = Config()
    assert cfg3.settings.backup_keep == 3

    # Export -> import round trip.
    cfg3.save_backup_settings("", 9, False)
    cfg3.import_settings(cfg2.export_settings())
    assert cfg3.settings.backup_dir == str(tmp_path / "backups")
    assert cfg3.settings.backup_keep == 3
    assert cfg3.settings.backup_include_logs is True


def test_backup_keep_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    cfg.save_backup_settings("", 500, False)
    assert cfg.settings.backup_keep == 100
    cfg.save_backup_settings("", -5, False)
    assert cfg.settings.backup_keep == 0


def test_path_map_env_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JFTOOLS_PATH_MAP", r"/Filmer=\\NAS\Filmer;/Ero=Z:\Ero")

    cfg = Config()
    assert cfg.settings.path_maps == [
        {"from": "/Filmer", "to": "\\\\NAS\\Filmer"},
        {"from": "/Ero", "to": "Z:\\Ero"},
    ]


def test_path_maps_sanitized_on_save(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_PATH_MAP", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    cfg.save_path_maps(
        [
            {"from": "/Filmer", "to": "Z:\\Filmer"},
            {"from": "", "to": "/x"},
            {"from": "/Ero", "to": ""},
            "junk",
        ]
    )
    assert cfg.settings.path_maps == [{"from": "/Filmer", "to": "Z:\\Filmer"}]
