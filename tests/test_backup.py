import hashlib
import io
import json
import zipfile

import pytest

import app.backup as backup
from app.config import Settings

DATA = {
    "config.json": '{"app":"jftools"}',
    "scans.json": '{"files":{}}',
    "jellyfin_id.json": '{"rows":[],"marks":[]}',
    "libraries.json": "{}",
    "users.json": "{}",
    "schedules.json": '{"jobs":[]}',
    ".secret_key": "k" * 64,
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    for name, content in DATA.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    settings = Settings(backup_dir=str(tmp_path / "backups"), backup_keep=7)
    monkeypatch.setattr(backup.config, "data_dir", tmp_path)
    monkeypatch.setattr(backup.config, "_settings", settings)
    monkeypatch.setattr(backup, "RESTORED", False)
    return tmp_path


def _make_zip(files: dict[str, bytes], manifest_files: dict) -> bytes:
    manifest = {"app": "jftools", "schema": 1, "files": manifest_files}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _rebuild(path, replace: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, replace.get(item.filename, src.read(item.filename)))
    return buf.getvalue()


def test_create_backup_writes_allowlisted_files(store):
    info = backup.create_backup()
    path = store / "backups" / info["name"]

    assert path.is_file()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))

    assert {"config.json", "scans.json", "jellyfin_id.json", ".secret_key"} <= names
    assert "manifest.json" not in manifest["files"]
    assert manifest["app"] == "jftools"
    assert manifest["schema"] == 1
    assert manifest["files"]["config.json"]["sha256"] == hashlib.sha256(
        (store / "config.json").read_bytes()
    ).hexdigest()
    assert not any(name.startswith("logs/") for name in names)
    assert not any("play_cache" in name for name in names)


def test_create_backup_works_without_config(store):
    (store / "config.json").unlink()

    info = backup.create_backup()

    with zipfile.ZipFile(store / "backups" / info["name"]) as zf:
        assert "config.json" not in zf.namelist()
        assert "scans.json" in zf.namelist()


def test_relative_backup_dir_is_rejected(store):
    backup.config.settings.backup_dir = "relative/backups"

    with pytest.raises(backup.BackupError, match="absolute path"):
        backup.create_backup()
    assert backup.list_backups() == []


def test_logs_are_optional(store):
    (store / "logs" / "2026-10-19 Search.log").write_text("hello", encoding="utf-8")
    backup.config.settings.backup_include_logs = True

    info = backup.create_backup()

    with zipfile.ZipFile(store / "backups" / info["name"]) as zf:
        assert "logs/2026-10-19 Search.log" in zf.namelist()


def test_restore_round_trip(store):
    info = backup.create_backup()
    raw = (store / "backups" / info["name"]).read_bytes()

    (store / "config.json").write_text('{"changed":true}', encoding="utf-8")
    (store / "scans.json").write_text('{"files":{"x":1}}', encoding="utf-8")

    result = backup.restore_archive(raw)

    assert result["restored"] == len(DATA)
    assert (store / "config.json").read_text(encoding="utf-8") == DATA["config.json"]
    assert (store / "scans.json").read_text(encoding="utf-8") == DATA["scans.json"]


def test_restore_rejects_tampered_file(store):
    info = backup.create_backup()
    raw = _rebuild(store / "backups" / info["name"], {"config.json": b'{"changed":true}'})

    with pytest.raises(backup.BackupError, match="damaged or modified"):
        backup.restore_archive(raw)


def test_restore_rejects_unexpected_member(store):
    payload = b"{}"
    raw = _make_zip(
        {"config.json": payload, "../evil": b"xx"},
        {
            "config.json": {"size": 2, "sha256": hashlib.sha256(payload).hexdigest()},
            "../evil": {"size": 2, "sha256": hashlib.sha256(b"xx").hexdigest()},
        },
    )

    with pytest.raises(backup.BackupError, match="unexpected file"):
        backup.restore_archive(raw)


def test_restore_requires_manifest(store):
    with pytest.raises(backup.BackupError, match="valid ZIP"):
        backup.restore_archive(b"not a zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("config.json", "{}")
    with pytest.raises(backup.BackupError, match="manifest"):
        backup.restore_archive(buf.getvalue())


def test_restore_refuses_while_busy(store, monkeypatch):
    monkeypatch.setattr(backup, "_busy_reason", lambda: "a media scan is running")

    with pytest.raises(backup.BackupError, match="media scan"):
        backup.restore_archive(b"not a zip")


def test_prune_keeps_newest_only(store):
    folder = store / "backups"
    folder.mkdir()
    for i in range(5):
        (folder / f"jftools-backup-20260101-00000{i}.zip").write_bytes(b"x")
    notes = folder / "notes.txt"
    notes.write_text("keep", encoding="utf-8")

    removed = backup.prune(folder, 2)

    assert len(removed) == 3
    assert sorted(p.name for p in folder.glob("jftools-backup-*.zip")) == [
        "jftools-backup-20260101-000003.zip",
        "jftools-backup-20260101-000004.zip",
    ]
    assert notes.exists()


def test_prune_zero_keeps_everything(store):
    folder = store / "backups"
    folder.mkdir()
    for i in range(3):
        (folder / f"jftools-backup-20260101-00000{i}.zip").write_bytes(b"x")

    assert backup.prune(folder, 0) == []
    assert len(list(folder.glob("jftools-backup-*.zip"))) == 3


def test_path_for_rejects_traversal(store):
    with pytest.raises(backup.BackupError):
        backup.path_for("../evil.zip")


def test_list_backups_newest_first(store):
    folder = store / "backups"
    folder.mkdir()
    (folder / "jftools-backup-20260101-000001.zip").write_bytes(b"a")
    (folder / "jftools-backup-20260102-000001.zip").write_bytes(b"b")

    rows = backup.list_backups()

    assert [r["name"] for r in rows] == [
        "jftools-backup-20260102-000001.zip",
        "jftools-backup-20260101-000001.zip",
    ]
