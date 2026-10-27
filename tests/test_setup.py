import json

from app.config import Config


def test_subtitle_language_preferences_default_to_english(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_OPENSUBTITLES_LANGUAGES", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert cfg.settings.opensubtitles_languages == "en"

    cfg.save_subtitle_languages("")
    assert cfg.settings.opensubtitles_languages == "en"

    cfg.save_subtitle_languages("English, Swedish")
    assert cfg.settings.opensubtitles_languages == "English, Swedish"


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


def test_play_cache_settings_round_trip_and_clamp(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert cfg.settings.play_cache_keep_days == 7
    assert cfg.settings.play_cache_max_gb == 5
    cfg.save_play_cache(3, 20)

    cfg2 = Config()
    assert cfg2.settings.play_cache_keep_days == 3
    assert cfg2.settings.play_cache_max_gb == 20

    # Another settings save must not drop the fields.
    cfg2.save_file_mgmt(True)
    cfg3 = Config()
    assert cfg3.settings.play_cache_keep_days == 3
    assert cfg3.settings.play_cache_max_gb == 20

    # Export -> import round trip.
    cfg3.save_play_cache(1, 1)
    cfg3.import_settings(cfg2.export_settings())
    assert cfg3.settings.play_cache_keep_days == 3
    assert cfg3.settings.play_cache_max_gb == 20

    # Out-of-range values are clamped.
    cfg.save_play_cache(0, 99999)
    assert cfg.settings.play_cache_keep_days == 1
    assert cfg.settings.play_cache_max_gb == 1024


def test_api_keys_create_revoke_and_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    key = cfg.create_api_key("Local dev")
    assert key.startswith("jft_")
    assert cfg.valid_api_key(key)["name"] == "Local dev"
    assert cfg.valid_api_key("nope") is None

    # Persisted, so a restart keeps them.
    cfg2 = Config()
    assert cfg2.valid_api_key(key)["name"] == "Local dev"
    assert cfg2.revoke_api_key(key) is True
    assert cfg2.valid_api_key(key) is None
    assert cfg2.revoke_api_key(key) is False

    # API keys are per-install secrets: they stay out of the settings export.
    cfg2.create_api_key("Other")
    assert "api_keys" not in cfg2.export_settings()


def test_opensubtitles_accounts_add_remove_and_persist(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_OPENSUBTITLES_USERNAME", raising=False)
    monkeypatch.delenv("JFTOOLS_OPENSUBTITLES_PASSWORD", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert cfg.settings.opensubtitles_accounts == []
    assert cfg.add_opensubtitles_account("alice", "pw1") is True
    assert cfg.add_opensubtitles_account("bob", "pw2") is True
    # Adding an existing name updates that account instead of duplicating it.
    assert cfg.add_opensubtitles_account("Alice", "new-pw") is False
    assert [a["username"] for a in cfg.settings.opensubtitles_accounts] == ["alice", "bob"]
    assert cfg.settings.opensubtitles_accounts[0]["password"] == "new-pw"

    # Persisted, in order, so a restart keeps the priority.
    cfg2 = Config()
    assert [a["username"] for a in cfg2.settings.opensubtitles_accounts] == ["alice", "bob"]

    assert cfg2.remove_opensubtitles_account("nobody") is False
    assert cfg2.remove_opensubtitles_account("ALICE") is True
    assert [a["username"] for a in cfg2.settings.opensubtitles_accounts] == ["bob"]

    # They travel with the settings export (and are sanitised on import).
    cfg3 = Config()
    exported = cfg2.export_settings()
    assert exported["opensubtitles_accounts"] == [{"username": "bob", "password": "pw2"}]
    cfg3.import_settings(
        {**exported, "opensubtitles_accounts": [
            {"username": "x", "password": "y"},
            {"username": "x", "password": "dup"},
            {"username": "", "password": "skip"},
        ]}
    )
    assert cfg3.settings.opensubtitles_accounts == [{"username": "x", "password": "y"}]


def test_opensubtitles_legacy_single_login_is_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("JFTOOLS_OPENSUBTITLES_USERNAME", raising=False)
    monkeypatch.delenv("JFTOOLS_OPENSUBTITLES_PASSWORD", raising=False)
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"opensubtitles_username": "old", "opensubtitles_password": "secret"}
        ),
        encoding="utf-8",
    )

    cfg = Config()
    assert cfg.settings.opensubtitles_accounts == [
        {"username": "old", "password": "secret"}
    ]

    cfg.add_opensubtitles_account("second", "pw")
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "opensubtitles_username" not in saved
    assert [a["username"] for a in saved["opensubtitles_accounts"]] == ["old", "second"]


def test_opensubtitles_removing_every_account_sticks(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))
    cfg = Config()
    cfg.add_opensubtitles_account("only", "pw")
    cfg.remove_opensubtitles_account("only")
    assert Config().settings.opensubtitles_accounts == []


def test_remote_sync_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("JFTOOLS_DATA_DIR", str(tmp_path))

    cfg = Config()
    cfg.save_remote_sync("http://192.168.1.114:8099/", "jft_abc", True, True)

    cfg2 = Config()
    assert cfg2.settings.remote_sync_url == "http://192.168.1.114:8099"
    assert cfg2.settings.remote_sync_api_key == "jft_abc"
    assert cfg2.settings.remote_sync_enabled is True
    assert cfg2.settings.remote_sync_on_startup is True

    # They travel with the settings export (a full backup).
    cfg2.save_remote_sync("", "", False, False)
    cfg2.import_settings(cfg.export_settings())
    assert cfg2.settings.remote_sync_url == "http://192.168.1.114:8099"
    assert cfg2.settings.remote_sync_api_key == "jft_abc"
    assert cfg2.settings.remote_sync_enabled is True
    assert cfg2.settings.remote_sync_on_startup is True


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
