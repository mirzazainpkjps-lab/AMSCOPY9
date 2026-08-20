"""Safe storage, SQLite backup, validation, restore, and scheduling services.

Backups contain authoritative runtime data (the SQLite database and permanent
uploads), not source code, logs, caches, dependencies, or Git data.  A backup
is staged, validated, and atomically published before retention is applied.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

LOG = logging.getLogger("maintenance")
BACKUP_PREFIX = "backup_"
MANIFEST_NAME = "manifest.json"
DB_NAME = "database.sqlite3"
_LOCK_DIR_NAME = ".backup.lock"
_worker_guard = threading.Lock()
_worker_started = False


class MaintenanceError(RuntimeError):
    """A maintenance operation failed without invalidating live data."""


class BackupBusy(MaintenanceError):
    """Another process currently owns the backup lock."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_path(app) -> Path:
    uri = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise MaintenanceError("Automatic backup currently requires a file-backed SQLite database")
    raw = unquote(uri[len(prefix):].split("?", 1)[0])
    path = Path(raw)
    if not path.is_absolute():
        path = Path(app.instance_path) / path
    return path.resolve()


def _paths(app) -> tuple[Path, Path, Path]:
    backup_root = Path(app.config["BACKUP_DIR"]).resolve()
    temp_root = Path(app.config["MAINTENANCE_TEMP_DIR"]).resolve()
    upload_root = Path(app.config["UPLOAD_DIR"]).resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return backup_root, temp_root, upload_root


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def backup_lock(app) -> Iterator[None]:
    """Cross-process mkdir lock with conservative stale-owner recovery."""
    backup_root, _, _ = _paths(app)
    lock_dir = backup_root / _LOCK_DIR_NAME
    stale_seconds = max(300, int(app.config.get("BACKUP_LOCK_STALE_SECONDS", 7200)))
    payload = {"pid": os.getpid(), "created_at": _utc_now().isoformat()}
    try:
        lock_dir.mkdir(mode=0o700)
    except FileExistsError:
        owner_file = lock_dir / "owner.json"
        owner = {}
        try:
            owner = json.loads(owner_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        age = time.time() - lock_dir.stat().st_mtime
        owner_pid = int(owner.get("pid") or 0)
        if age <= stale_seconds or _is_pid_alive(owner_pid):
            raise BackupBusy(f"Backup already running (owner pid {owner_pid or 'unknown'})")
        # Only this known lock category is removed, and only after both age and
        # owner-liveness checks establish it as stale.
        shutil.rmtree(lock_dir)
        try:
            lock_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise BackupBusy("Another process recovered the stale backup lock") from exc
    try:
        (lock_dir / "owner.json").write_text(json.dumps(payload), encoding="utf-8")
        yield
    finally:
        try:
            shutil.rmtree(lock_dir)
        except FileNotFoundError:
            pass
        except OSError:
            LOG.exception("Unable to release backup lock %s", lock_dir)


def _validate_sqlite(path: Path, expected_tables: set[str] | None = None) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        raise MaintenanceError(f"Missing or empty backup database: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise MaintenanceError(f"SQLite integrity check failed: {integrity}")
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables:
                raise MaintenanceError("Backup database has no application tables")
            if expected_tables and not expected_tables.issubset(tables):
                missing = sorted(expected_tables - tables)
                raise MaintenanceError(f"Backup database is missing expected tables: {missing[:10]}")
            return {"tables": sorted(tables), "integrity": "ok", "size": path.stat().st_size}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise MaintenanceError(f"Backup database is unreadable: {exc}") from exc


def _copy_uploads(upload_root: Path, target: Path) -> list[dict]:
    records: list[dict] = []
    if not upload_root.is_dir():
        return records
    for source in sorted(upload_root.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(upload_root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        records.append({
            "path": relative.as_posix(),
            "size": destination.stat().st_size,
            "sha256": _sha256(destination),
        })
    return records


def _collision_safe_destination(root: Path, stamp: str) -> Path:
    base = root / f"{BACKUP_PREFIX}{stamp}"
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = root / f"{BACKUP_PREFIX}{stamp}_{index:02d}"
        if not candidate.exists():
            return candidate
    raise MaintenanceError("Unable to allocate a collision-safe backup name")


def list_backups(app) -> list[Path]:
    root, _, _ = _paths(app)
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith(BACKUP_PREFIX)),
        key=lambda p: p.name,
    )


def validate_backup(path: str | Path, *, verify_hashes: bool = True) -> dict:
    backup = Path(path).resolve()
    manifest_path = backup / MANIFEST_NAME
    if not backup.is_dir() or not manifest_path.is_file():
        raise MaintenanceError("Backup directory or manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MaintenanceError(f"Backup manifest is unreadable: {exc}") from exc
    if int(manifest.get("format_version") or 0) != 1:
        raise MaintenanceError("Unsupported backup format version")
    expected_tables = set(manifest.get("database", {}).get("tables") or [])
    db_path = backup / DB_NAME
    db_report = _validate_sqlite(db_path, expected_tables=expected_tables)
    if verify_hashes and _sha256(db_path) != manifest.get("database", {}).get("sha256"):
        raise MaintenanceError("Backup database checksum mismatch")
    for record in manifest.get("uploads") or []:
        rel = Path(str(record.get("path") or ""))
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise MaintenanceError("Unsafe upload path in backup manifest")
        candidate = backup / "uploads" / rel
        if not candidate.is_file() or candidate.stat().st_size != int(record.get("size") or -1):
            raise MaintenanceError(f"Missing or truncated backed-up upload: {rel}")
        if verify_hashes and _sha256(candidate) != record.get("sha256"):
            raise MaintenanceError(f"Backed-up upload checksum mismatch: {rel}")
    return {"path": str(backup), "manifest": manifest, "database": db_report, "valid": True}


def _prune_after_success(app) -> list[str]:
    retention = max(1, int(app.config.get("BACKUP_RETENTION", 3)))
    backups = list_backups(app)
    removed: list[str] = []
    # Invalid/unknown entries are deliberately not auto-deleted. Retention
    # only removes older backups that still validate as known-good.
    valid: list[Path] = []
    for candidate in backups:
        try:
            validate_backup(candidate, verify_hashes=False)
            valid.append(candidate)
        except MaintenanceError:
            LOG.error("Not pruning invalid backup automatically: %s", candidate)
    for old in valid[:-retention]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def create_backup(app, *, reason: str = "scheduled") -> dict:
    """Create and validate an online SQLite backup, then apply retention."""
    started = _utc_now()
    backup_root, temp_root, upload_root = _paths(app)
    source_db = _sqlite_path(app)
    if not source_db.is_file():
        raise MaintenanceError(f"Live database does not exist: {source_db}")
    free = shutil.disk_usage(backup_root).free
    minimum = int(app.config.get("MIN_FREE_DISK_BYTES", 100 * 1024 * 1024))
    if free < minimum:
        raise MaintenanceError(f"Insufficient free disk space ({free} bytes available; {minimum} required)")

    with backup_lock(app):
        stamp = started.strftime("%Y%m%d_%H%M%S")
        staging = temp_root / f".tmp-backup-{os.getpid()}-{time.time_ns()}"
        staging.mkdir(mode=0o700)
        published: Path | None = None
        try:
            destination_db = staging / DB_NAME
            source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
            destination = sqlite3.connect(str(destination_db), timeout=30)
            try:
                source.backup(destination, pages=256, sleep=0.05)
            finally:
                destination.close()
                source.close()
            db_report = _validate_sqlite(destination_db)
            upload_records = _copy_uploads(upload_root, staging / "uploads")
            manifest = {
                "format_version": 1,
                "created_at": started.isoformat(),
                "reason": reason,
                "database": {
                    "filename": DB_NAME,
                    "size": destination_db.stat().st_size,
                    "sha256": _sha256(destination_db),
                    "tables": db_report["tables"],
                    "integrity": "ok",
                },
                "uploads": upload_records,
            }
            manifest_path = staging / MANIFEST_NAME
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            validate_backup(staging)
            published = _collision_safe_destination(backup_root, stamp)
            os.replace(staging, published)
            # Validate the official path before old known-good backups can be removed.
            validate_backup(published)
            removed = _prune_after_success(app)
            LOG.info("Backup completed path=%s reason=%s removed=%s", published, reason, removed)
            return {"ok": True, "path": str(published), "name": published.name, "removed": removed, "manifest": manifest}
        except Exception:
            LOG.exception("Backup failed target=%s free_bytes=%s", published or staging, free)
            if published and published.exists():
                # A published-but-invalid new candidate is not a successful backup.
                shutil.rmtree(published, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def backup_due(app, *, now: datetime | None = None) -> bool:
    now = now or _utc_now()
    interval = max(60, int(app.config.get("BACKUP_INTERVAL_SECONDS", 3600)))
    valid_dates: list[datetime] = []
    for backup in list_backups(app):
        try:
            report = validate_backup(backup, verify_hashes=False)
            valid_dates.append(datetime.fromisoformat(report["manifest"]["created_at"]))
        except (MaintenanceError, ValueError):
            continue
    if not valid_dates:
        return True
    latest = max(valid_dates)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return (now - latest).total_seconds() >= interval


def run_backup_if_due(app) -> dict:
    if not backup_due(app):
        return {"ok": True, "created": False, "message": "A current valid backup already exists"}
    try:
        result = create_backup(app, reason="scheduled")
        result["created"] = True
        return result
    except BackupBusy as exc:
        return {"ok": True, "created": False, "message": str(exc)}


def cleanup_owned_temp(app, *, older_than_seconds: int | None = None) -> list[str]:
    """Delete only this subsystem's stale, categorised temp directories."""
    _, temp_root, _ = _paths(app)
    age_limit = max(3600, int(older_than_seconds or app.config.get("TEMP_RETENTION_SECONDS", 86400)))
    now = time.time()
    removed: list[str] = []
    for candidate in temp_root.iterdir():
        if not candidate.name.startswith((".tmp-backup-", ".tmp-restore-")):
            continue
        try:
            if now - candidate.stat().st_mtime < age_limit:
                continue
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            removed.append(candidate.name)
        except OSError:
            LOG.exception("Failed cleaning stale owned temp path %s", candidate)
    return removed


def restore_backup(app, backup_path: str | Path) -> dict:
    """Validate, safety-backup, restore, verify, and roll back on failure.

    This function is intended for a controlled maintenance window. It uses
    SQLite's online backup API in both directions and never swaps a partial
    database file over the live database.
    """
    source_dir = Path(backup_path).resolve()
    source_report = validate_backup(source_dir)
    _, temp_root, upload_root = _paths(app)
    live_db = _sqlite_path(app)
    # Pin the validated source in owned temporary storage. Creating the safety
    # backup may legitimately rotate an old source backup out of retention.
    staged = temp_root / f".tmp-restore-{os.getpid()}-{time.time_ns()}"
    shutil.copytree(source_dir, staged)
    validate_backup(staged)
    safety = create_backup(app, reason="pre-restore-safety")
    safety_dir = Path(safety["path"])
    safety_db = safety_dir / DB_NAME
    expected = set(source_report["database"]["tables"])
    old_uploads = temp_root / f".tmp-restore-old-uploads-{os.getpid()}-{time.time_ns()}"
    try:
        source = sqlite3.connect(f"file:{staged / DB_NAME}?mode=ro", uri=True, timeout=30)
        live = sqlite3.connect(str(live_db), timeout=30)
        try:
            source.backup(live, pages=256, sleep=0.05)
        finally:
            live.close()
            source.close()
        _validate_sqlite(live_db, expected_tables=expected)

        # Uploads are published with directory renames only after DB validation.
        incoming_uploads = staged / "uploads"
        upload_root.parent.mkdir(parents=True, exist_ok=True)
        if upload_root.exists():
            os.replace(upload_root, old_uploads)
        if incoming_uploads.exists():
            shutil.copytree(incoming_uploads, upload_root)
        else:
            upload_root.mkdir(parents=True, exist_ok=True)
        LOG.warning("Restore completed source=%s safety=%s", source_dir, safety["path"])
        shutil.rmtree(old_uploads, ignore_errors=True)
        return {"ok": True, "source": str(source_dir), "safety_backup": safety["path"]}
    except Exception as restore_error:
        LOG.exception("Restore failed; recovering from safety backup %s", safety["path"])
        try:
            previous = sqlite3.connect(f"file:{safety_db}?mode=ro", uri=True, timeout=30)
            live = sqlite3.connect(str(live_db), timeout=30)
            try:
                previous.backup(live, pages=256, sleep=0.05)
            finally:
                live.close()
                previous.close()
            _validate_sqlite(live_db)
            if old_uploads.exists():
                shutil.rmtree(upload_root, ignore_errors=True)
                os.replace(old_uploads, upload_root)
        except Exception as rollback_error:
            raise MaintenanceError(
                f"Restore failed ({restore_error}); safety recovery also failed ({rollback_error})"
            ) from restore_error
        raise MaintenanceError(f"Restore failed and live data was recovered: {restore_error}") from restore_error
    finally:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(old_uploads, ignore_errors=True)


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def maintenance_status(app) -> dict:
    backup_root, temp_root, upload_root = _paths(app)
    db_path = _sqlite_path(app)
    backups = list_backups(app)
    valid, invalid = [], []
    for backup in backups:
        try:
            valid.append(validate_backup(backup, verify_hashes=False))
        except MaintenanceError as exc:
            invalid.append({"path": str(backup), "error": str(exc)})
    interval = max(60, int(app.config.get("BACKUP_INTERVAL_SECONDS", 3600)))
    latest = valid[-1]["manifest"] if valid else None
    age = None
    if latest:
        created = datetime.fromisoformat(latest["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (_utc_now() - created).total_seconds()
    retention = max(1, int(app.config.get("BACKUP_RETENTION", 3)))
    if not valid or age is None or age > interval * 2:
        health = "CRITICAL"
    elif invalid or len(valid) != retention or age > interval * 1.25:
        health = "WARNING"
    else:
        health = "HEALTHY"
    instance = Path(app.instance_path)
    disk = shutil.disk_usage(backup_root)
    return {
        "health": health,
        "database_bytes": _size(db_path),
        "uploads_bytes": _size(upload_root),
        "backups_bytes": _size(backup_root),
        "temp_bytes": _size(temp_root),
        "logs_bytes": _size(instance / "logs"),
        "cache_bytes": _size(instance / "cache"),
        "instance_bytes": _size(instance),
        "free_disk_bytes": disk.free,
        "backup_count": len(valid),
        "invalid_backups": invalid,
        "oldest_backup": valid[0]["path"] if valid else None,
        "latest_backup": valid[-1]["path"] if valid else None,
        "latest_backup_age_seconds": age,
        "retention": retention,
        "interval_seconds": interval,
    }


def _worker(app) -> None:
    poll = max(30, min(300, int(app.config.get("BACKUP_SCHEDULER_POLL_SECONDS", 60))))
    while True:
        try:
            with app.app_context():
                cleanup_owned_temp(app)
                run_backup_if_due(app)
        except Exception:
            LOG.exception("Scheduled backup attempt failed; existing backups were preserved")
        time.sleep(poll)


def start_embedded_scheduler(app) -> bool:
    """Start the guarded fallback scheduler once per process.

    Cross-process locking makes this safe under multiple WSGI workers. An
    external hourly invocation of ``tools/maintenance.py backup-if-due`` is
    still preferred because daemon threads do not survive process downtime.
    """
    global _worker_started
    if app.config.get("TESTING") or not app.config.get("BACKUP_EMBEDDED_SCHEDULER", True):
        return False
    with _worker_guard:
        if _worker_started:
            return False
        thread = threading.Thread(target=_worker, args=(app,), daemon=True, name="ams-maintenance")
        thread.start()
        _worker_started = True
        return True
