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
