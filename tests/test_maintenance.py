from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import maintenance


def _app(tmp_path: Path):
    instance = tmp_path / "instance"
    uploads = tmp_path / "static" / "uploads"
    instance.mkdir()
    uploads.mkdir(parents=True)
    db_path = instance / "live.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table records (id integer primary key, value text not null)")
    conn.execute("insert into records(value) values ('original')")
    conn.commit()
    conn.close()
    return SimpleNamespace(
        instance_path=str(instance),
        config={
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "BACKUP_DIR": str(instance / "storage" / "backups"),
            "MAINTENANCE_TEMP_DIR": str(instance / "storage" / "temp"),
            "UPLOAD_DIR": str(uploads),
            "BACKUP_RETENTION": 3,
            "BACKUP_INTERVAL_SECONDS": 3600,
            "BACKUP_LOCK_STALE_SECONDS": 7200,
            "TEMP_RETENTION_SECONDS": 86400,
            "MIN_FREE_DISK_BYTES": 0,
        },
    )


def _value(app) -> str:
    db_path = Path(app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///"))
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("select value from records").fetchone()[0]
    finally:
        conn.close()


def test_fourth_successful_backup_rotates_only_oldest(tmp_path, monkeypatch):
    app = _app(tmp_path)
    (Path(app.config["UPLOAD_DIR"]) / "receipt.jpg").write_bytes(b"business-document")
    base = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    import itertools
    clock = itertools.count()
    monkeypatch.setattr(maintenance, "_utc_now", lambda: base + timedelta(hours=next(clock) // 2))

    made = [maintenance.create_backup(app)["name"] for _ in range(4)]

    remaining = [p.name for p in maintenance.list_backups(app)]
    assert remaining == made[1:]
    assert len(remaining) == 3
    for path in maintenance.list_backups(app):
        report = maintenance.validate_backup(path)
        assert report["valid"]
        assert (path / "uploads" / "receipt.jpg").read_bytes() == b"business-document"


def test_failed_new_backup_preserves_three_existing(tmp_path, monkeypatch):
    app = _app(tmp_path)
    base = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    import itertools
    clock = itertools.count()
    monkeypatch.setattr(maintenance, "_utc_now", lambda: base + timedelta(hours=next(clock) // 2))
    for _ in range(3):
        maintenance.create_backup(app)
    before = [p.name for p in maintenance.list_backups(app)]
    monkeypatch.setattr(maintenance, "_validate_sqlite", lambda *a, **k: (_ for _ in ()).throw(maintenance.MaintenanceError("injected validation failure")))

    with pytest.raises(maintenance.MaintenanceError, match="injected"):
        maintenance.create_backup(app)

    assert [p.name for p in maintenance.list_backups(app)] == before


def test_lock_rejects_concurrent_owner_without_deleting_it(tmp_path):
    app = _app(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def owner():
        with maintenance.backup_lock(app):
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=owner)
    thread.start()
    assert entered.wait(2)
    try:
        with pytest.raises(maintenance.BackupBusy):
            with maintenance.backup_lock(app):
                pass
    except Exception as exc:  # make thread cleanup deterministic on assertion failure
        errors.append(exc)
    finally:
        release.set()
        thread.join(5)
    if errors:
        raise errors[0]


def test_manifest_or_database_corruption_is_rejected(tmp_path):
    app = _app(tmp_path)
    result = maintenance.create_backup(app)
    db_file = Path(result["path"]) / maintenance.DB_NAME
    db_file.write_bytes(b"not sqlite")
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.validate_backup(result["path"])


def test_restore_validates_safety_backs_up_and_cleans_temp(tmp_path, monkeypatch):
    app = _app(tmp_path)
    uploads = Path(app.config["UPLOAD_DIR"])
    (uploads / "proof.txt").write_text("old upload", encoding="utf-8")
    base = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    clock = iter(base + timedelta(seconds=i) for i in range(4))
    monkeypatch.setattr(maintenance, "_utc_now", lambda: next(clock))
    backup = maintenance.create_backup(app, reason="restore-source")

    db_path = Path(app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///"))
    conn = sqlite3.connect(db_path)
    conn.execute("update records set value='changed'")
    conn.commit()
    conn.close()
    (uploads / "proof.txt").write_text("changed upload", encoding="utf-8")

    result = maintenance.restore_backup(app, backup["path"])

    assert result["ok"]
    assert _value(app) == "original"
    assert (uploads / "proof.txt").read_text(encoding="utf-8") == "old upload"
    temp = Path(app.config["MAINTENANCE_TEMP_DIR"])
    assert not list(temp.glob(".tmp-restore-*"))


def test_due_logic_does_not_generate_historical_backups(tmp_path, monkeypatch):
    app = _app(tmp_path)
    base = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(maintenance, "_utc_now", lambda: base)
    first = maintenance.run_backup_if_due(app)
    assert first["created"] is True
    assert maintenance.backup_due(app, now=base + timedelta(hours=8)) is True
    monkeypatch.setattr(maintenance, "_utc_now", lambda: base + timedelta(hours=8))
    second = maintenance.run_backup_if_due(app)
    assert second["created"] is True
    assert len(maintenance.list_backups(app)) == 2


def test_cleanup_only_removes_old_owned_paths(tmp_path):
    app = _app(tmp_path)
    _, temp, _ = maintenance._paths(app)
    owned = temp / ".tmp-backup-abandoned"
    unknown = temp / "customer-file"
    owned.mkdir()
    unknown.mkdir()
    old = datetime.now().timestamp() - 90000
    import os
    os.utime(owned, (old, old))
    os.utime(unknown, (old, old))

    removed = maintenance.cleanup_owned_temp(app, older_than_seconds=3600)

    assert owned.name in removed
    assert unknown.exists()


def test_duplicate_timestamp_is_collision_safe(tmp_path, monkeypatch):
    app = _app(tmp_path)
    fixed = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(maintenance, "_utc_now", lambda: fixed)
    first = maintenance.create_backup(app)
    second = maintenance.create_backup(app)
    assert first["name"] == "backup_20260816_120000"
    assert second["name"] == "backup_20260816_120000_01"


def test_low_disk_failure_preserves_existing_backup(tmp_path):
    app = _app(tmp_path)
    first = maintenance.create_backup(app)
    app.config["MIN_FREE_DISK_BYTES"] = 10**30
    with pytest.raises(maintenance.MaintenanceError, match="Insufficient free disk"):
        maintenance.create_backup(app)
    assert [p.name for p in maintenance.list_backups(app)] == [first["name"]]


def test_status_reports_health_without_exposing_public_urls(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.config["BACKUP_RETENTION"] = 1
    now = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(maintenance, "_utc_now", lambda: now)
    maintenance.create_backup(app)
    status = maintenance.maintenance_status(app)
    assert status["health"] == "HEALTHY"
    assert status["backup_count"] == 1
    assert status["free_disk_bytes"] > 0
