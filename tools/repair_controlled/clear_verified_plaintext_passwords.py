#!/usr/bin/env python3
"""Clear legacy plaintext passwords only after each value verifies its own hash.

No password value is printed, logged, written to a report, or copied outside the
guarded database backup. The command is all-or-nothing and preserves every
existing password hash, username, role, status, and permission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import check_password_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path,
        default=REPO_ROOT / "instance" / "ahmed_cement.db",
    )
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expect-count", type=int)
    parser.add_argument(
        "--report", type=Path,
        default=REPO_ROOT / "artifacts" / "plaintext-password-remediation.json",
    )
    args = parser.parse_args()
    if args.confirm and args.expect_count is None:
        parser.error("--confirm requires --expect-count")
    return args


def _table_digest(connection, table):
    digest = hashlib.sha256()
    quoted = '"' + table.replace('"', '""') + '"'
    for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _non_user_digests(connection):
    tables = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'user' ORDER BY name"
        )
    ]
    return {table: _table_digest(connection, table) for table in tables}


def _inspect(connection):
    rows = connection.execute(
        "SELECT id, password_hash, password_plain FROM user "
        "WHERE password_plain IS NOT NULL AND trim(password_plain) <> '' ORDER BY id"
    ).fetchall()
    invalid_ids = []
    for user_id, password_hash, password_plain in rows:
        valid = False
        if password_hash and password_plain:
            try:
                valid = check_password_hash(password_hash, password_plain)
            except Exception:
                valid = False
        if not valid:
            invalid_ids.append(user_id)
    return rows, invalid_ids


def _write_report(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    args = _args()
    database = args.database.expanduser().resolve()
    report = args.report.expanduser().resolve()
    if not database.is_file():
        print(f"ERROR: database not found: {database}", file=sys.stderr)
        return 2

    connection = sqlite3.connect(database)
    try:
        integrity_before = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        rows, invalid_ids = _inspect(connection)
    finally:
        connection.close()

    base_report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "integrity_check_before": integrity_before,
        "plaintext_rows_before": len(rows),
        "verified_against_own_hash": len(rows) - len(invalid_ids),
        "blocked_unverified_row_ids": invalid_ids,
        "password_values_recorded": False,
    }
    if not args.confirm:
        _write_report(report, base_report)
        print(json.dumps(base_report, indent=2, sort_keys=True))
        return 0 if integrity_before == ["ok"] and not invalid_ids else 3

    from tools.repair_controlled.repair_guard import preflight
    backup_path = preflight(
        script_name=__file__,
        description="Clear only plaintext passwords that verify against their existing hashes",
        db_path=database,
        backup_dir=database.parent / "reconcile_backups",
    )
    if len(rows) != args.expect_count:
        print(
            f"ERROR: expected {args.expect_count} plaintext rows, observed {len(rows)}; no cleanup performed",
            file=sys.stderr,
        )
        return 4
    if invalid_ids:
        print(
            f"ERROR: {len(invalid_ids)} plaintext rows do not verify against their hashes; no cleanup performed",
            file=sys.stderr,
        )
        return 5

    before_connection = sqlite3.connect(backup_path)
    try:
        non_user_before = _non_user_digests(before_connection)
        hashes_before = dict(before_connection.execute("SELECT id, password_hash FROM user ORDER BY id"))
    finally:
        before_connection.close()

    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cleared = 0
        for user_id, expected_hash, expected_plain in rows:
            current = connection.execute(
                "SELECT password_hash, password_plain FROM user WHERE id = ?", (user_id,)
            ).fetchone()
            if current != (expected_hash, expected_plain):
                raise RuntimeError(f"user row {user_id} changed during cleanup")
            if not check_password_hash(expected_hash, expected_plain):
                raise RuntimeError(f"user row {user_id} no longer verifies")
            cursor = connection.execute(
                "UPDATE user SET password_plain = NULL "
                "WHERE id = ? AND password_hash = ? AND password_plain = ?",
                (user_id, expected_hash, expected_plain),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"atomic update failed for user row {user_id}")
            cleared += 1
        remaining = connection.execute(
            "SELECT COUNT(*) FROM user WHERE password_plain IS NOT NULL AND trim(password_plain) <> ''"
        ).fetchone()[0]
        if remaining != 0:
            raise RuntimeError(f"plaintext rows remain after staged cleanup: {remaining}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    connection = sqlite3.connect(database)
    try:
        integrity_after = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        hashes_after = dict(connection.execute("SELECT id, password_hash FROM user ORDER BY id"))
        remaining_after = connection.execute(
            "SELECT COUNT(*) FROM user WHERE password_plain IS NOT NULL AND trim(password_plain) <> ''"
        ).fetchone()[0]
        non_user_after = _non_user_digests(connection)
    finally:
        connection.close()

    if hashes_before != hashes_after:
        print(f"ERROR: password hashes changed; restore from {backup_path}", file=sys.stderr)
        return 6
    changed_non_user = [
        table for table in sorted(non_user_before)
        if non_user_before[table] != non_user_after.get(table)
    ]
    if changed_non_user or integrity_after != ["ok"] or remaining_after:
        print(
            f"ERROR: post-cleanup validation failed; restore from {backup_path}",
            file=sys.stderr,
        )
        return 7

    result = {
        **base_report,
        "cleared_rows": cleared,
        "plaintext_rows_after": remaining_after,
        "all_password_hashes_unchanged": True,
        "changed_non_user_tables": changed_non_user,
        "integrity_check_after": integrity_after,
        "pre_cleanup_backup": backup_path,
    }
    _write_report(report, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
