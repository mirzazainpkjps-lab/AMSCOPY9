"""Disposable import upload/report files live under instance/.tmp/.

Successful imports keep their summary in the database. Files are only removed
after the import transaction has committed and a post-import integrity check
has passed. Failed or partial imports keep their files until a retention
window expires.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from flask import current_app
from sqlalchemy import text

from models import db

logger = logging.getLogger(__name__)

SUCCESS_STATUSES = frozenset({"ok", "completed", "imported", "warning"})
FAILED_STATUSES = frozenset({"failed", "partial", "cancelled"})


def tmp_root() -> Path:
    configured = current_app.config.get("IMPORT_TMP_DIR")
    if configured:
        path = Path(configured)
    else:
        path = Path(current_app.instance_path) / ".tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    raw = current_app.config.get("IMPORT_UPLOADS_DIR")
    path = Path(raw) if raw else tmp_root() / "import_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    raw = current_app.config.get("IMPORT_REPORTS_DIR")
    path = Path(raw) if raw else tmp_root() / "import_reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def verify_post_import_integrity() -> tuple[bool, str]:
    """Confirm the session is usable and SQLite (if used) is not corrupt."""
    try:
        db.session.execute(text("SELECT 1"))
        bind = db.session.get_bind()
        if bind is not None and bind.dialect.name == "sqlite":
            row = db.session.execute(text("PRAGMA integrity_check")).fetchone()
            result = (row[0] if row else "").strip().lower()
            if result != "ok":
                return False, f"sqlite integrity_check={result or 'empty'}"
        return True, "ok"
    except Exception as exc:
        logger.exception("Post-import integrity verification failed")
        return False, str(exc)


def should_discard_artifacts(status: str | None, *, failed_count: int = 0) -> bool:
    status = (status or "").strip().lower()
    if failed_count:
        return False
    return status in SUCCESS_STATUSES


def unlink_quietly(path: str | os.PathLike | None) -> None:
    if not path:
        return
    try:
        p = Path(path)
        if p.is_file():
            p.unlink()
    except OSError:
        logger.warning("Could not remove import artifact %s", path, exc_info=True)


def discard_import_artifacts(*paths: str | os.PathLike | None) -> None:
    """Remove disposable files. Call only after commit + integrity pass."""
    for path in paths:
        unlink_quietly(path)
        if path:
            meta = Path(str(path)).with_suffix(".meta.json")
            if str(path).endswith(".csv"):
                unlink_quietly(str(path).replace(".csv", ".meta.json"))
            else:
                unlink_quietly(meta)


def purge_expired_failed_artifacts(max_age_seconds: int | None = None) -> int:
    """Delete leftover failed-import files older than the retention window."""
    if max_age_seconds is None:
        max_age_seconds = int(current_app.config.get("IMPORT_ARTIFACT_RETENTION_SECONDS", 7 * 24 * 3600) or 0)
    if max_age_seconds <= 0:
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for folder in (uploads_dir(), reports_dir()):
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.stat().st_mtime > cutoff:
                    continue
                entry.unlink()
                removed += 1
            except OSError:
                continue
    return removed
