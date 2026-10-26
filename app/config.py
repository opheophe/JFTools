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
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local development. No-op when env vars are already set (e.g. by
# Docker's environment), because load_dotenv does not override by default.
load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _sanitize_path_maps(value) -> list[dict]:
    """Normalise raw path-map input into a list of {"from", "to"} pairs."""
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for m in value:
        if not isinstance(m, dict):
            continue
        src = str(m.get("from") or "").strip()
        dst = str(m.get("to") or "").strip()
        if src and dst:
            out.append({"from": src, "to": dst})
    return out


def _sanitize_profiles(value) -> dict:
    """Normalise saved filter profiles: ``{page: {name: profile}}``.

    Profile names and page keys come from the browser, so anything that is not
    a non-empty string is dropped; the profile body stays opaque JSON.
    """
    out: dict[str, dict[str, dict]] = {}
    if not isinstance(value, dict):
        return out
    for page, items in value.items():
        if not isinstance(page, str) or not page or not isinstance(items, dict):
            continue
        clean: dict[str, dict] = {}
        for name, profile in items.items():
            if isinstance(name, str) and name and isinstance(profile, dict):
                clean[name] = profile
        out[page] = clean
    return out


def _parse_path_map_env(raw: str) -> list[dict]:
    """Parse ``JFTOOLS_PATH_MAP`` (``/from=/to;/from2=/to2``)."""
    if not raw:
        return []
    out: list[dict] = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        src, dst = entry.split("=", 1)
        src, dst = src.strip(), dst.strip()
        if src and dst:
            out.append({"from": src, "to": dst})
    return out


def _sanitize_api_keys(value) -> list[dict]:
    """Normalise persisted API keys: ``{name, key, created}`` entries."""
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        key = str(entry.get("key") or "").strip()
        if not name or not key:
            continue
        created = entry.get("created")
        if not isinstance(created, (int, float)):
            created = 0.0
        out.append({"name": name, "key": key, "created": created})
    return out


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
    path_maps: list[dict] = field(default_factory=list)
    file_mgmt: bool = False
    opensubtitles_api_key: str = ""
    opensubtitles_username: str = ""
    opensubtitles_password: str = ""
    # Preferred subtitle languages: the default for OpenSubtitles searches and
    # the language list used by the Internal dialog's Smart mark.
    opensubtitles_languages: str = "en"
    # Saved filter profiles per page ("search", "moviematch"), managed from that
    # page's profile bar. Kept here so they survive browser clears/backups.
    filter_profiles: dict = field(default_factory=dict)
    # Full-backup destination and retention. An empty directory means
    # data/backups; keep=0 keeps every archive.
    backup_dir: str = ""
    backup_keep: int = 7
    backup_include_logs: bool = False
    # Player cache (browser copies + converted audio) retention: files older
    # than keep_days, or a folder larger than max_gb, are pruned.
    play_cache_keep_days: int = 7
    play_cache_max_gb: int = 5
    # API keys other instances/scripts use for the read-only media export.
    # Deliberately not part of the settings export: each install keeps its own.
    api_keys: list[dict] = field(default_factory=list)
    # Remote sync: pull Movie Match data and the scan store from another
    # JFTools server (e.g. the production one) so this instance mirrors it.
    remote_sync_url: str = ""
    remote_sync_api_key: str = ""
    remote_sync_enabled: bool = False
    remote_sync_on_startup: bool = False


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
            path_maps=_parse_path_map_env(_env("JFTOOLS_PATH_MAP", "")),
            opensubtitles_api_key=_env("JFTOOLS_OPENSUBTITLES_API_KEY", ""),
            opensubtitles_username=_env("JFTOOLS_OPENSUBTITLES_USERNAME", ""),
            opensubtitles_password=_env("JFTOOLS_OPENSUBTITLES_PASSWORD", ""),
            opensubtitles_languages=_env("JFTOOLS_OPENSUBTITLES_LANGUAGES", "en"),
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
                if "path_maps" in data:
                    settings.path_maps = _sanitize_path_maps(data["path_maps"])
                if "file_mgmt" in data:
                    settings.file_mgmt = bool(data["file_mgmt"])
                if data.get("opensubtitles_api_key"):
                    settings.opensubtitles_api_key = data["opensubtitles_api_key"]
                if data.get("opensubtitles_username"):
                    settings.opensubtitles_username = data["opensubtitles_username"]
                if data.get("opensubtitles_password"):
                    settings.opensubtitles_password = data["opensubtitles_password"]
                if data.get("opensubtitles_languages"):
                    settings.opensubtitles_languages = data["opensubtitles_languages"]
                if "filter_profiles" in data:
                    settings.filter_profiles = _sanitize_profiles(data["filter_profiles"])
                if isinstance(data.get("backup_dir"), str):
                    settings.backup_dir = data["backup_dir"]
                if "backup_keep" in data:
                    try:
                        settings.backup_keep = max(0, min(100, int(data["backup_keep"])))
                    except (TypeError, ValueError):
                        pass
                if isinstance(data.get("backup_include_logs"), bool):
                    settings.backup_include_logs = data["backup_include_logs"]
                if "play_cache_keep_days" in data:
                    try:
                        settings.play_cache_keep_days = max(
                            1, min(365, int(data["play_cache_keep_days"]))
                        )
                    except (TypeError, ValueError):
                        pass
                if "play_cache_max_gb" in data:
                    try:
                        settings.play_cache_max_gb = max(
                            1, min(1024, int(data["play_cache_max_gb"]))
                        )
                    except (TypeError, ValueError):
                        pass
                if "api_keys" in data:
                    settings.api_keys = _sanitize_api_keys(data["api_keys"])
                if isinstance(data.get("remote_sync_url"), str):
                    settings.remote_sync_url = data["remote_sync_url"].strip().rstrip("/")
                if isinstance(data.get("remote_sync_api_key"), str):
                    settings.remote_sync_api_key = data["remote_sync_api_key"].strip()
                if isinstance(data.get("remote_sync_enabled"), bool):
                    settings.remote_sync_enabled = data["remote_sync_enabled"]
                if isinstance(data.get("remote_sync_on_startup"), bool):
                    settings.remote_sync_on_startup = data["remote_sync_on_startup"]
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
            "path_maps": s.path_maps,
            "file_mgmt": s.file_mgmt,
            "opensubtitles_api_key": s.opensubtitles_api_key,
            "opensubtitles_username": s.opensubtitles_username,
            "opensubtitles_password": s.opensubtitles_password,
            "opensubtitles_languages": s.opensubtitles_languages,
            "filter_profiles": s.filter_profiles,
            "backup_dir": s.backup_dir,
            "backup_keep": s.backup_keep,
            "backup_include_logs": s.backup_include_logs,
            "play_cache_keep_days": s.play_cache_keep_days,
            "play_cache_max_gb": s.play_cache_max_gb,
            "api_keys": s.api_keys,
            "remote_sync_url": s.remote_sync_url,
            "remote_sync_api_key": s.remote_sync_api_key,
            "remote_sync_enabled": s.remote_sync_enabled,
            "remote_sync_on_startup": s.remote_sync_on_startup,
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

    def save_path_maps(self, maps) -> None:
        self._settings.path_maps = _sanitize_path_maps(maps)
        self._persist()

    def save_file_mgmt(self, enabled: bool) -> None:
        self._settings.file_mgmt = bool(enabled)
        self._persist()

    def save_opensubtitles(self, api_key: str, username: str, password: str, languages: str) -> None:
        s = self._settings
        if api_key is not None:
            s.opensubtitles_api_key = api_key.strip()
        if username is not None:
            s.opensubtitles_username = username.strip()
        if password is not None:
            s.opensubtitles_password = password
        if languages is not None:
            s.opensubtitles_languages = languages.strip() or "en"
        self._persist()

    def save_subtitle_languages(self, languages: str) -> None:
        """Persist the shared preferred-subtitle-language list."""
        self._settings.opensubtitles_languages = languages.strip() or "en"
        self._persist()

    def save_filter_profiles(self, page: str, profiles: dict) -> None:
        """Replace one page's saved profiles, leaving the other page untouched."""
        current = dict(self._settings.filter_profiles or {})
        clean = _sanitize_profiles({page: profiles})
        if page in clean:
            current[page] = clean[page]
        self._settings.filter_profiles = current
        self._persist()

    def save_backup_settings(self, directory: str, keep: int, include_logs: bool) -> None:
        """Persist the automatic-backup folder, retention and log flag."""
        self._settings.backup_dir = (directory or "").strip()
        self._settings.backup_keep = max(0, min(100, int(keep)))
        self._settings.backup_include_logs = bool(include_logs)
        self._persist()

    def save_play_cache(self, keep_days: int, max_gb: int) -> None:
        """Persist the player-cache retention (age in days, size cap in GB)."""
        self._settings.play_cache_keep_days = max(1, min(365, int(keep_days)))
        self._settings.play_cache_max_gb = max(1, min(1024, int(max_gb)))
        self._persist()

    def create_api_key(self, name: str) -> str:
        """Create a named API key and return it (shown only once)."""
        key = "jft_" + secrets.token_hex(24)
        self._settings.api_keys = [
            *self._settings.api_keys,
            {"name": name.strip(), "key": key, "created": time.time()},
        ]
        self._persist()
        return key

    def revoke_api_key(self, key: str) -> bool:
        """Remove one API key; True when it existed."""
        before = len(self._settings.api_keys)
        self._settings.api_keys = [
            k for k in self._settings.api_keys
            if not hmac.compare_digest(k["key"], key)
        ]
        if len(self._settings.api_keys) == before:
            return False
        self._persist()
        return True

    def valid_api_key(self, key: str) -> dict | None:
        """The API-key entry matching ``key`` (constant-time), or None."""
        if not key:
            return None
        for entry in self._settings.api_keys:
            if hmac.compare_digest(entry["key"], key):
                return entry
        return None

    def save_remote_sync(
        self, url: str, api_key: str, enabled: bool, on_startup: bool
    ) -> None:
        """Persist the remote-sync source (the other server's URL + API key)."""
        s = self._settings
        s.remote_sync_url = (url or "").strip().rstrip("/")
        s.remote_sync_api_key = (api_key or "").strip()
        s.remote_sync_enabled = bool(enabled)
        s.remote_sync_on_startup = bool(on_startup)
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
        "path_maps",
        "file_mgmt",
        "opensubtitles_api_key",
        "opensubtitles_username",
        "opensubtitles_password",
        "opensubtitles_languages",
        "filter_profiles",
        "backup_dir",
        "backup_keep",
        "backup_include_logs",
        "play_cache_keep_days",
        "play_cache_max_gb",
        "remote_sync_url",
        "remote_sync_api_key",
        "remote_sync_enabled",
        "remote_sync_on_startup",
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
                if field == "path_maps":
                    self._settings.path_maps = _sanitize_path_maps(data[field])
                elif field == "filter_profiles":
                    self._settings.filter_profiles = _sanitize_profiles(data[field])
                elif field == "backup_keep":
                    try:
                        self._settings.backup_keep = max(0, min(100, int(data[field])))
                    except (TypeError, ValueError):
                        pass
                elif field == "play_cache_keep_days":
                    try:
                        self._settings.play_cache_keep_days = max(
                            1, min(365, int(data[field]))
                        )
                    except (TypeError, ValueError):
                        pass
                elif field == "play_cache_max_gb":
                    try:
                        self._settings.play_cache_max_gb = max(
                            1, min(1024, int(data[field]))
                        )
                    except (TypeError, ValueError):
                        pass
                elif field == "remote_sync_enabled":
                    self._settings.remote_sync_enabled = bool(data[field])
                elif field == "remote_sync_on_startup":
                    self._settings.remote_sync_on_startup = bool(data[field])
                elif field == "backup_include_logs":
                    self._settings.backup_include_logs = bool(data[field])
                elif field == "backup_dir":
                    self._settings.backup_dir = str(data[field] or "").strip()
                elif field == "file_mgmt":
                    self._settings.file_mgmt = bool(data[field])
                else:
                    setattr(self._settings, field, str(data[field]).strip())
        self._persist()


config = Config()
