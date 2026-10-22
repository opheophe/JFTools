"""Full-instance backup: settings plus runtime data, as one ZIP.

The Settings page's *Export settings* writes a portable JSON file for moving
configuration between installs. This module backs up the whole instance instead:
the config, the scheduler, the Movie Match data (including its marks and
favourites) and the media-scan results, which take hours to rebuild.

The player's remux cache is always excluded (it can be gigabytes and is
disposable). Logs are optional.

Archives are written with a manifest that records each member's size and
SHA-256, so a restore can reject a truncated or tampered file. Restores only
read allowlisted names and never extract a path from the archive.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import BinaryIO

from .config import config
from .logs import log
from .version import current_version

SCHEMA = 1
APP = "jftools"

# Files copied from the data directory. Everything here is small and either
# expensive to rebuild (scans, Movie Match) or part of the instance's identity.
DATA_FILES = (
    "config.json",
    ".secret_key",
    "schedules.json",
    "libraries.json",
    "users.json",
    "jellyfin_id.json",
    "scans.json",
)

BACKUP_GLOB = "jftools-backup-*.zip"
_NAME_RE = re.compile(r"^jftools-backup-\d{8}-\d{6}(?:-\d+)?\.zip$")
_LOG_RE = re.compile(r"^logs/[^/\\]+\.log$")

MAX_RESTORE_BYTES = 500 * 1024 * 1024  # uncompressed, guards against zip bombs

_LOCK = threading.Lock()

# Set after a restore in this process. Settings writes are refused until the
# app restarts, because the in-memory settings would otherwise overwrite the
# restored config.json on the next save.
RESTORED = False


class BackupError(Exception):
    """A backup or restore could not be completed."""


# ---- locations --------------------------------------------------------------


def backup_dir() -> Path:
    """The configured backup folder, or ``data/backups``."""
    raw = (config.settings.backup_dir or "").strip()
    if raw:
        folder = Path(raw).expanduser()
        if not folder.is_absolute():
            raise BackupError(
                "The backup folder must be an absolute path. "
                "Update it on the Settings page."
            )
        return folder
    return config.data_dir / "backups"


def ensure_backup_dir() -> Path:
    folder = backup_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Cannot create the backup folder {folder}: {exc}") from exc
    return folder


def path_for(name: str) -> Path:
    """Return the archive path for a validated backup file name."""
    if not _NAME_RE.fullmatch(name or ""):
        raise BackupError("Invalid backup name.")
    return backup_dir() / name


# ---- archive ----------------------------------------------------------------


def _log_files() -> list[tuple[str, Path]]:
    folder = config.data_dir / "logs"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.log")):
        if path.is_file():
            out.append((f"logs/{path.name}", path))
    return out


def _members() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for name in DATA_FILES:
        path = config.data_dir / name
        if path.is_file():
            out.append((name, path))
    if config.settings.backup_include_logs:
        out.extend(_log_files())
    return out


def _snapshot() -> tuple[dict, list[tuple[str, bytes]]]:
    """Read every member once and build the manifest from those bytes."""
    entries: list[tuple[str, bytes]] = []
    files: dict[str, dict] = {}
    for arcname, path in _members():
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BackupError(f"Could not read {arcname}: {exc}") from exc
        files[arcname] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        entries.append((arcname, data))
    manifest = {
        "app": APP,
        "schema": SCHEMA,
        "version": current_version(),
        "created_at": int(time.time()),
        "files": files,
    }
    return manifest, entries


def _write_zip(dest: BinaryIO) -> dict:
    manifest, entries = _snapshot()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for arcname, data in entries:
            zf.writestr(arcname, data)
    return manifest


def build_zip() -> bytes:
    """Build an archive in memory (used by the Download button)."""
    buf = io.BytesIO()
    _write_zip(buf)
    return buf.getvalue()


def create_backup() -> dict:
    """Write a new archive to the backup folder and prune old ones."""
    if not _LOCK.acquire(blocking=False):
        raise BackupError("A backup is already running.")
    try:
        folder = ensure_backup_dir()
        base = time.strftime("jftools-backup-%Y%m%d-%H%M%S")
        name = base + ".zip"
        suffix = 1
        while (folder / name).exists():
            name = f"{base}-{suffix}.zip"
            suffix += 1
        dest = folder / name
        tmp = folder / (name + ".tmp")
        try:
            with tmp.open("wb") as fh:
                manifest = _write_zip(fh)
            os.replace(tmp, dest)
            try:
                dest.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise BackupError(f"Could not write the backup: {exc}") from exc
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        removed = prune(folder, config.settings.backup_keep)
        size = dest.stat().st_size
        log("Backup", "INFO", f"Backup created: {name} ({size} bytes)")
        return {
            "name": name,
            "size": size,
            "created_at": dest.stat().st_mtime,
            "files": len(manifest["files"]),
            "removed": removed,
        }
    finally:
        _LOCK.release()


def list_backups() -> list[dict]:
    try:
        folder = backup_dir()
    except BackupError:
        return []
    try:
        paths = [p for p in folder.glob(BACKUP_GLOB) if p.is_file()]
    except OSError:
        return []
    rows = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        rows.append({"name": path.name, "size": st.st_size, "created_at": st.st_mtime})
    rows.sort(key=lambda r: r["name"], reverse=True)
    return rows


def prune(folder: Path, keep: int) -> list[str]:
    """Delete the oldest archives beyond ``keep`` (0 keeps everything)."""
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return []
    try:
        def _mtime(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        paths = sorted(
            (p for p in folder.glob(BACKUP_GLOB) if p.is_file()),
            key=lambda p: (_mtime(p), p.name),
            reverse=True,
        )
    except OSError:
        return []
    removed = []
    for path in paths[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    if removed:
        log("Backup", "INFO", f"Pruned {len(removed)} old backup(s)")
    return removed


# ---- restore ----------------------------------------------------------------


def _busy_reason() -> str | None:
    """Why a restore must wait, or None.

    Uses modules already imported by the app: importing them here would run
    their startup code in tests and touch the real data directory.
    """
    subtitles = sys.modules.get("app.subtitles")
    fetch = getattr(subtitles, "_fetch", None) if subtitles else None
    if fetch and fetch.get("status") == "running":
        return "a media scan is running"
    jellyfin_id = sys.modules.get("app.jellyfin_id")
    fetch = getattr(jellyfin_id, "_fetch", None) if jellyfin_id else None
    if fetch and fetch.get("status") == "running":
        return "a Movie Match fetch is running"
    scheduler = sys.modules.get("app.scheduler")
    running = getattr(scheduler, "_running", None) if scheduler else None
    if running:
        return "a scheduled job is running"
    return None


def _allowed(arcname: str) -> bool:
    return arcname in DATA_FILES or bool(_LOG_RE.fullmatch(arcname))


def _inside_data_dir(dest: Path) -> bool:
    try:
        dest.resolve().relative_to(config.data_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


def restore_archive(raw: bytes) -> dict:
    """Validate and apply a backup ZIP. The caller must restart afterwards."""
    global RESTORED
    if not _LOCK.acquire(blocking=False):
        raise BackupError("A backup or restore is already running.")
    try:
        result = _restore_locked(raw)
        RESTORED = True
        return result
    finally:
        _LOCK.release()


def _restore_locked(raw: bytes) -> dict:
    busy = _busy_reason()
    if busy:
        raise BackupError(f"Cannot restore while {busy}.")

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError("That is not a valid ZIP file.") from exc

    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise BackupError("The archive has no valid manifest.json.") from exc
        if not isinstance(manifest, dict):
            raise BackupError("The archive has no valid manifest.json.")
        if manifest.get("app") != APP or manifest.get("schema") != SCHEMA:
            raise BackupError("This is not a JFTools backup (or its schema is newer).")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise BackupError("The archive is empty.")

        try:
            total = sum(info.file_size for info in zf.infolist())
        except (OSError, ValueError) as exc:
            raise BackupError("The archive is damaged.") from exc
        if total > MAX_RESTORE_BYTES:
            raise BackupError("The archive is too large to restore.")

        restored: list[tuple[str, bytes]] = []
        read_total = 0
        for arcname, meta in files.items():
            if not isinstance(arcname, str) or not _allowed(arcname):
                raise BackupError(f"The archive contains an unexpected file: {arcname}")
            if not isinstance(meta, dict):
                raise BackupError(f"The archive has no checksum for {arcname}.")
            try:
                with zf.open(arcname) as fh:
                    data = fh.read(MAX_RESTORE_BYTES + 1)
            except KeyError as exc:
                raise BackupError(f"The archive is missing {arcname}.") from exc
            except (zipfile.BadZipFile, OSError) as exc:
                raise BackupError(f"The archive's {arcname} is damaged.") from exc
            if len(data) > MAX_RESTORE_BYTES:
                raise BackupError("The archive is too large to restore.")
            read_total += len(data)
            if read_total > MAX_RESTORE_BYTES:
                raise BackupError("The archive is too large to restore.")
            size = meta.get("size")
            digest = str(meta.get("sha256") or "").lower()
            if size != len(data) or digest != hashlib.sha256(data).hexdigest():
                raise BackupError(f"The archive's {arcname} is damaged or modified.")
            restored.append((arcname, data))

    staged: list[tuple[Path, Path]] = []
    try:
        for arcname, data in restored:
            dest = config.data_dir / arcname
            if not _inside_data_dir(dest):
                raise BackupError(f"The archive contains an unexpected file: {arcname}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".restore")
            tmp.write_bytes(data)
            staged.append((tmp, dest))
        for tmp, dest in staged:
            os.replace(tmp, dest)
    except OSError as exc:
        for tmp, _dest in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise BackupError(f"Could not restore the archive: {exc}") from exc
    except Exception:
        for tmp, _dest in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise

    log("Backup", "WARN", f"Backup restored ({len(restored)} file(s)); restart required")
    return {"restored": len(restored), "restart_required": True}
