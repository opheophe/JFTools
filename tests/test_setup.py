from app.config import Config


def test_needs_setup_until_local_login_created(tmp_path, monkeypatch):
    monkeypatch.delenv("MEDSERV_USERNAME", raising=False)
    monkeypatch.delenv("MEDSERV_PASSWORD", raising=False)
    monkeypatch.setenv("MEDSERV_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert cfg.needs_setup

    cfg.save_auth("alice", "s3cret-pw")
    assert not cfg.needs_setup
    assert cfg.check_password("alice", "s3cret-pw")
    assert not cfg.check_password("alice", "wrong")


def test_jellyfin_configured_requires_url_and_key(tmp_path, monkeypatch):
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    monkeypatch.setenv("MEDSERV_DATA_DIR", str(tmp_path))

    cfg = Config()
    assert not cfg.jellyfin_configured

    cfg.save_settings("http://jellyfin:8096", "")
    assert not cfg.jellyfin_configured

    cfg.save_settings("http://jellyfin:8096", "abc123")
    assert cfg.jellyfin_configured


def test_env_credentials_skip_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDSERV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDSERV_USERNAME", "admin")
    monkeypatch.setenv("MEDSERV_PASSWORD", "change-me")

    cfg = Config()
    assert not cfg.needs_setup
    assert cfg.check_password("admin", "change-me")
    assert not cfg.check_password("admin", "wrong")
