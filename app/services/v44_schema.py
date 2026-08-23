"""Fresh v4.4 database bootstrap.

The v4.4 design is intentionally kept independent from the legacy ORM tables.
A new installation starts from the checked-in SQL schema and never imports the
old/live database.  The legacy ORM tables are created afterwards as a temporary
compatibility surface so the existing Flask screens remain usable while their
queries are moved to the v4.4 names.
"""
from __future__ import annotations

import logging
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


def has_any_table(connection: sqlite3.Connection) -> bool:
    """True when the SQLite file contains at least one user table.

    A file created implicitly by a connection (or by a failed bootstrap) is a
    valid but *empty* SQLite database.  It is not a legacy database and must
    not be treated as one, otherwise startup refuses to bootstrap forever and
    every request that touches a table returns HTTP 500.
    """
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return bool(row)


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
    schema_file = schema_path()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if existed:
            if is_v44_database(conn):
                return False
            if has_any_table(conn):
                # A database that already has tables (the ORM/legacy schema)
                # is left completely untouched — the v4.4 SQL bundle is only
                # ever applied to a brand new file.  This used to raise, which
                # aborted the whole startup bootstrap and left the instance
                # serving HTTP 500 on every database-backed page.
                logging.getLogger(__name__).info(
                    "Existing non-v4.4 database at %s left untouched; "
                    "skipping the v4.4 schema bundle.",
                    path,
                )
                return False
            # An empty SQLite file (e.g. created by a connection before the
            # bootstrap ran, or left behind by a previously failed bootstrap).
            # Treat it as a fresh install instead of bricking every boot.
            existed = False
        if not schema_file.exists():
            # The v4.4 SQL bundle is optional; the ORM bootstrap
            # (``db.create_all()`` + default admin) creates a fully usable
            # database on its own.  Refusing to start here used to leave the
            # instance with zero tables and a 500 on every login.
            logging.getLogger(__name__).warning(
                "v4.4 schema file not found at %s; falling back to the ORM "
                "schema bootstrap.",
                schema_file,
            )
            return False
        sql = schema_file.read_text(encoding="utf-8")
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
