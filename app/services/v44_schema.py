"""Fresh v4.4 database bootstrap.

The v4.4 design is intentionally kept independent from the legacy ORM tables.
A new installation starts from the checked-in SQL schema and never imports the
old/live database.  The legacy ORM tables are created afterwards as a temporary
compatibility surface so the existing Flask screens remain usable while their
queries are moved to the v4.4 names.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash


RETIRED_DB_NAMES = (
    "ahmed_cement.db",
    "ahmed_cement.db-wal",
    "ahmed_cement.db-shm",
    "ahmed_cement_v44.db",
    "ahmed_cement_v44.db-wal",
    "ahmed_cement_v44.db-shm",
)


def schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "v44" / "SCHEMA_v4_4.sql"


def retire_legacy_database_files(instance_dir, extra_dirs=None) -> list[str]:
    """Permanently remove retired live/migrated SQLite files.

    v4.4 is a clean install. Historical business data is not imported.
    """
    removed: list[str] = []
    roots = [Path(instance_dir)]
    for extra in extra_dirs or []:
        roots.append(Path(extra))
    for root in roots:
        if not root.exists():
            continue
        for name in RETIRED_DB_NAMES:
            path = root / name
            if path.exists() or path.is_symlink():
                path.unlink()
                removed.append(str(path))
    return removed


def is_v44_database(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    # The legacy schema also has schema_version, so identify the v4.4 role table.
    return bool(row and connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='roles'"
    ).fetchone())


def initialize_v44_database(db_path: str, *, default_user: str = "Admin",
                            default_password: str = "Admin@fbm12345") -> bool:
    """Create a pristine v4.4 database if *db_path* does not exist.

    Returns True when the v4.4 schema was created, False when an existing
    database was left untouched.  This function never reads or copies the
    legacy database and never runs a destructive migration.
    """
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() and path.stat().st_size > 0
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if existed:
            if not is_v44_database(conn):
                raise RuntimeError(
                    f"Refusing to use non-v4.4 database at {path}; "
                    "set APP_DB_PATH to a new file or run the explicit migration."
                )
            return False
        sql = schema_path().read_text(encoding="utf-8")
        conn.executescript(sql)
        # The SQL bundle seeds roles, permissions and wipe scopes, but users are
        # deliberately not seeded.  A fresh install gets exactly one usable
        # administrator; no business/master/transaction data is fabricated.
        conn.execute(
            """INSERT INTO users
               (username,password_hash,full_name,role_id,status,active,created_at,updated_at)
               VALUES (?,?,?,(SELECT id FROM roles WHERE name='Admin'),
                       'active',1,datetime('now'),datetime('now'))""",
            (default_user, generate_password_hash(default_password), default_user),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        if not existed:
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()
        raise
    finally:
        conn.close()
