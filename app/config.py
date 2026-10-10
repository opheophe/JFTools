"""Application configuration and persisted settings.

Precedence (persisted file overrides environment defaults):
    environment defaults  ->  DATA_DIR/config.json overrides

The Jellyfin connection and the dashboard login (username + password hash) are
both editable from the Settings page and persisted to config.json. Environment
variables act as the initial/default values.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local development. No-op when env vars are already set (e.g. by
# Docker's env_file), because load_dotenv does not override by default.
load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def hash_password(password: str) -> str:
    """Hash a password with scrypt and return a self-describing string."""
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=16384, r=8, p=1, dklen=32,
    ).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a hash_password() string."""
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    calc = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=16384, r=8, p=1, dklen=32,
    ).hex()
    return hmac.compare_digest(calc, digest)


@dataclass
class Settings:
    """User-editable state persisted to config.json."""

    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    username: str = ""
    password_hash: str = ""


class Config:
    def __init__(self) -> None:
        self.host = _env("MEDSERV_HOST", "0.0.0.0")
        self.port = int(_env("MEDSERV_PORT", "8099"))
        self.cookie_secure = _env("MEDSERV_COOKIE_SECURE", "false").lower() in (
            "1", "true", "yes", "on",
        )
        self.data_dir = Path(_env("MEDSERV_DATA_DIR", "./data"))
        self.settings_file = self.data_dir / "config.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._env_username = _env("MEDSERV_USERNAME", "admin")
        self._env_password = _env("MEDSERV_PASSWORD", "admin")

        self.secret_key = self._load_or_create_secret()
        self._settings = self._load_settings()

    # ---- secret key ---------------------------------------------------------

    def _load_or_create_secret(self) -> str:
        env_key = _env("MEDSERV_SECRET_KEY", "")
        if env_key:
            return env_key
        path = self.data_dir / ".secret_key"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        key = secrets.token_hex(32)
        path.write_text(key, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key

    # ---- settings -----------------------------------------------------------

    def _load_settings(self) -> Settings:
        settings = Settings(
            jellyfin_url=_env("JELLYFIN_URL", ""),
            jellyfin_api_key=_env("JELLYFIN_API_KEY", ""),
        )
        try:
            if self.settings_file.exists():
                data = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if data.get("jellyfin_url"):
                    settings.jellyfin_url = data["jellyfin_url"]
                if data.get("jellyfin_api_key"):
                    settings.jellyfin_api_key = data["jellyfin_api_key"]
                if data.get("username"):
                    settings.username = data["username"]
                if data.get("password_hash"):
                    settings.password_hash = data["password_hash"]
        except (OSError, ValueError, TypeError):
            pass
        return settings

    def _persist(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        s = self._settings
        payload = {
            "jellyfin_url": s.jellyfin_url,
            "jellyfin_api_key": s.jellyfin_api_key,
            "username": s.username,
            "password_hash": s.password_hash,
        }
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.settings_file)

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def resolved_username(self) -> str:
        return self._settings.username or self._env_username

    def check_password(self, username: str, password: str) -> bool:
        if not hmac.compare_digest(username, self.resolved_username):
            return False
        if self._settings.password_hash:
            return verify_password(password, self._settings.password_hash)
        return hmac.compare_digest(password, self._env_password)

    def save_settings(self, jellyfin_url: str, jellyfin_api_key: str) -> None:
        self._settings.jellyfin_url = jellyfin_url.strip()
        self._settings.jellyfin_api_key = jellyfin_api_key.strip()
        self._persist()

    def save_auth(self, username: str, new_password: str) -> None:
        self._settings.username = username.strip()
        if new_password:
            self._settings.password_hash = hash_password(new_password)
        self._persist()


config = Config()
