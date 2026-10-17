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
# Docker's environment), because load_dotenv does not override by default.
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
    radarr_url: str = ""
    sonarr_url: str = ""
    timezone: str = "Europe/Stockholm"


class Config:
    def __init__(self) -> None:
        self.host = _env("JFTOOLS_HOST", "0.0.0.0")
        self.port = int(_env("JFTOOLS_PORT", "8099"))
        self.cookie_secure = _env("JFTOOLS_COOKIE_SECURE", "false").lower() in (
            "1", "true", "yes", "on",
        )
        self.data_dir = Path(_env("JFTOOLS_DATA_DIR", "./data"))
        self.settings_file = self.data_dir / "config.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._env_username = _env("JFTOOLS_USERNAME", "admin")
        self._env_password = _env("JFTOOLS_PASSWORD", "")
        # First-run setup is skipped only when a username AND a password are both
        # supplied. An empty password (e.g. from an empty JFTOOLS_PASSWORD var in
        # Docker) must not count as "set", otherwise login would be impossible.
        self._env_creds_set = bool(self._env_username and self._env_password)

        self.secret_key = self._load_or_create_secret()
        self._settings = self._load_settings()

    # ---- secret key ---------------------------------------------------------

    def _load_or_create_secret(self) -> str:
        env_key = _env("JFTOOLS_SECRET_KEY", "")
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
            radarr_url=_env("JFTOOLS_RADARR_URL", ""),
            sonarr_url=_env("JFTOOLS_SONARR_URL", ""),
            timezone=_env("JFTOOLS_TIMEZONE", "Europe/Stockholm"),
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
                if data.get("radarr_url"):
                    settings.radarr_url = data["radarr_url"]
                if data.get("sonarr_url"):
                    settings.sonarr_url = data["sonarr_url"]
                if data.get("timezone"):
                    settings.timezone = data["timezone"]
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
            "radarr_url": s.radarr_url,
            "sonarr_url": s.sonarr_url,
            "timezone": s.timezone,
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

    @property
    def jellyfin_configured(self) -> bool:
        """True once a Jellyfin URL and API key are both set."""
        return bool(self._settings.jellyfin_url and self._settings.jellyfin_api_key)

    @property
    def needs_setup(self) -> bool:
        """True on first run until a local login exists.

        A persisted username + password hash, or both JFTOOLS_USERNAME and
        JFTOOLS_PASSWORD in the environment, count as an existing login.
        """
        if self._settings.username and self._settings.password_hash:
            return False
        return not self._env_creds_set

    def check_password(self, username: str, password: str) -> bool:
        if not hmac.compare_digest(username, self.resolved_username):
            return False
        if self._settings.password_hash:
            return verify_password(password, self._settings.password_hash)
        if self._env_password:
            return hmac.compare_digest(password, self._env_password)
        return False

    def save_settings(self, jellyfin_url: str, jellyfin_api_key: str) -> None:
        self._settings.jellyfin_url = jellyfin_url.strip()
        self._settings.jellyfin_api_key = jellyfin_api_key.strip()
        self._persist()

    def save_auth(self, username: str, new_password: str) -> None:
        self._settings.username = username.strip()
        if new_password:
            self._settings.password_hash = hash_password(new_password)
        self._persist()

    def save_integrations(self, radarr_url: str, sonarr_url: str) -> None:
        self._settings.radarr_url = radarr_url.strip()
        self._settings.sonarr_url = sonarr_url.strip()
        self._persist()

    def save_timezone(self, timezone: str) -> None:
        self._settings.timezone = timezone.strip()
        self._persist()

    # ---- backup / restore ---------------------------------------------------

    _EXPORT_FIELDS = (
        "jellyfin_url",
        "jellyfin_api_key",
        "username",
        "password_hash",
        "radarr_url",
        "sonarr_url",
        "timezone",
    )

    def export_settings(self) -> dict:
        """Return every persisted setting as a plain JSON-serialisable dict."""
        data = {"schema": 1, "app": "jftools"}
        for field in self._EXPORT_FIELDS:
            data[field] = getattr(self._settings, field)
        return data

    def import_settings(self, data: dict) -> None:
        """Apply settings from an exported dict and persist them.

        Unknown keys are ignored and missing keys leave the current value
        untouched, so partial files still work.
        """
        for field in self._EXPORT_FIELDS:
            if field in data and data[field] is not None:
                setattr(self._settings, field, str(data[field]).strip())
        self._persist()


config = Config()
