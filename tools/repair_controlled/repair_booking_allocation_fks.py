#!/usr/bin/env python3
"""Diagnose or transactionally repair dangling booking allocations.

Examples:
  python tools/repair_controlled/repair_booking_allocation_fks.py \
      --database /path/to/copy.db --report artifacts/fk-allocation-diagnosis.json

  python tools/repair_controlled/repair_booking_allocation_fks.py \
      --database /path/to/copy.db --report artifacts/fk-allocation-diagnosis.json \
      --confirm --expect-findings 190 --expect-fk-violations 208

The write path takes a pre-repair backup, requires exact expected counts, blocks
unsafe findings, archives each exact source row and available parent snapshots,
and commits archive/delete together. It never alters retained financial,
inventory, payment, ledger, or report source records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=REPO_ROOT / "instance" / "ahmed_cement.db",
        help="SQLite database to inspect/repair (default: supplied instance DB)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "artifacts" / "fk-allocation-diagnosis.json",
        help="Row-level JSON diagnosis output",
    )
    parser.add_argument("--confirm", action="store_true", help="Archive and remove eligible dangling rows")
    parser.add_argument("--expect-findings", type=int, help="Required exact row count for --confirm")
    parser.add_argument("--expect-fk-violations", type=int, help="Required exact FK violation count for --confirm")
    args = parser.parse_args()
    if args.confirm and (args.expect_findings is None or args.expect_fk_violations is None):
        parser.error("--confirm requires --expect-findings and --expect-fk-violations")
    return args


def _raw_checks(path: Path):
    connection = sqlite3.connect(path)
    try:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
        fk_rows = [list(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
    finally:
        connection.close()
    return integrity, fk_rows


def _table_names(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_digest(connection, table):
    digest = hashlib.sha256()
    quoted = '"' + table.replace('"', '""') + '"'
    for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _allocation_rows(connection):
    return {
        row[0]: tuple(row)
        for row in connection.execute(
            "SELECT id, sale_id, sale_item_id, booking_item_id, qty, is_void "
            "FROM booking_allocation ORDER BY id"
        )
    }


def _validate_exact_write_scope(before_path, after_path, removed_ids, repair_run_id):
    """Prove no table outside the derived rows/archive changed."""
    before = sqlite3.connect(before_path)
    after = sqlite3.connect(after_path)
    try:
        before_tables = _table_names(before)
        after_tables = _table_names(after)
        allowed_new = {"booking_allocation_repair_archive"}
        unexpected_new = sorted(after_tables - before_tables - allowed_new)
        dropped = sorted(before_tables - after_tables)
        if unexpected_new or dropped:
            raise RuntimeError(f"unexpected schema scope: new={unexpected_new}, dropped={dropped}")

        excluded = {"booking_allocation", "booking_allocation_repair_archive"}
        changed_tables = [
            table
            for table in sorted(before_tables & after_tables - excluded)
            if _table_digest(before, table) != _table_digest(after, table)
        ]
        if changed_tables:
            raise RuntimeError(f"repair changed non-target tables: {changed_tables}")

        before_allocations = _allocation_rows(before)
        after_allocations = _allocation_rows(after)
        removed = set(before_allocations) - set(after_allocations)
        added = set(after_allocations) - set(before_allocations)
        changed_survivors = [
            row_id
            for row_id in set(before_allocations) & set(after_allocations)
            if before_allocations[row_id] != after_allocations[row_id]
        ]
        if removed != set(removed_ids) or added or changed_survivors:
            raise RuntimeError(
                "booking allocation write scope mismatch: "
                f"removed={len(removed)}, added={sorted(added)}, changed={changed_survivors[:20]}"
            )

        archives = after.execute(
            "SELECT original_allocation_id, source_row_json "
            "FROM booking_allocation_repair_archive WHERE repair_run_id = ?",
            (repair_run_id,),
        ).fetchall()
        archived_ids = {row[0] for row in archives}
        if archived_ids != removed:
            raise RuntimeError(
                f"archive identity mismatch: removed={len(removed)}, archived={len(archived_ids)}"
            )
        for original_id, source_json in archives:
            source = json.loads(source_json)
            original = before_allocations[original_id]
            reconstructed = (
                source["id"], source["sale_id"], source["sale_item_id"],
                source["booking_item_id"], source["qty"], source["is_void"],
            )
            # SQLite booleans are integers while JSON booleans are bool; tuple
            # equality intentionally treats those values as equivalent.
            if reconstructed != original:
                raise RuntimeError(f"archive snapshot mismatch for allocation {original_id}")

        return {
            "unchanged_business_tables": len((before_tables & after_tables) - excluded),
            "removed_allocation_rows": len(removed),
            "unchanged_allocation_rows": len(after_allocations),
            "archived_rows": len(archives),
            "unexpected_new_tables": unexpected_new,
            "dropped_tables": dropped,
            "changed_business_tables": changed_tables,
        }
    finally:
        before.close()
        after.close()


def _summary(findings, fk_rows):
    return {
        "finding_rows": len(findings),
        "foreign_key_violations": len(fk_rows),
        "repair_eligible_rows": sum(1 for row in findings if row["repair_eligible"]),
        "blocked_rows": sum(1 for row in findings if not row["repair_eligible"]),
        "by_classification": dict(sorted(Counter(row["classification"] for row in findings).items())),
        "by_criticality": dict(sorted(Counter(row["criticality"] for row in findings).items())),
        "by_violating_field": dict(sorted(Counter(field for row in findings for field in row["violating_fields"]).items())),
        "active_rows": sum(1 for row in findings if not row["is_void"]),
        "void_rows": sum(1 for row in findings if row["is_void"]),
    }


def main() -> int:
    args = _args()
    database = args.database.expanduser().resolve()
    report = args.report.expanduser().resolve()
    if not database.is_file():
        print(f"ERROR: database not found: {database}", file=sys.stderr)
        return 2

    backup_path = None
    if args.confirm:
        from tools.repair_controlled.repair_guard import preflight

        backup_path = preflight(
            script_name=__file__,
            description="Archive and remove only transactionally verified dangling booking allocations",
            db_path=database,
            backup_dir=database.parent / "reconcile_backups",
        )

    # Bind only the ORM needed by this one-shot command. Importing the full web
    # app would run broad startup schema/bootstrap work and would weaken this
    # command's exact write-scope guarantee.
    from flask import Flask
    from models import BookingAllocationRepairArchive, db

    app = Flask("booking-allocation-fk-repair")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    from app.services.allocation_integrity import (
        audit_booking_allocation_integrity,
        repair_dangling_booking_allocations,
    )

    integrity_before, fk_before = _raw_checks(database)
    with app.app_context():
        if args.confirm:
            BookingAllocationRepairArchive.__table__.create(bind=db.engine, checkfirst=True)
        findings = audit_booking_allocation_integrity(db.session)
        summary = _summary(findings, fk_before)
        document = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "database": str(database),
            "integrity_check_before": integrity_before,
            "foreign_key_check_before": fk_before,
            "fk_id_mapping": {
                "booking_allocation.0": "booking_item_id -> booking_item.id",
                "booking_allocation.1": "sale_item_id -> direct_sale_item.id",
                "booking_allocation.2": "sale_id -> direct_sale.id",
            },
            "summary": summary,
            "repair_policy": {
                "write_scope": "booking_allocation and booking_allocation_repair_archive only",
                "method": "archive exact row and available parent snapshots, then delete derived row in one transaction",
                "blocked_cases": [
                    "missing direct sale",
                    "active allocation missing sale item",
                    "sale-item ownership mismatch",
                    "booking item whose booking is missing",
                ],
            },
            "findings": findings,
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"Diagnosis: {report}")

        if not args.confirm:
            db.session.rollback()
            return 0 if integrity_before == ["ok"] else 3

        if len(findings) != args.expect_findings:
            db.session.rollback()
            print(
                f"ERROR: expected {args.expect_findings} findings, observed {len(findings)}; no repair committed",
                file=sys.stderr,
            )
            return 4
        if len(fk_before) != args.expect_fk_violations:
            db.session.rollback()
            print(
                f"ERROR: expected {args.expect_fk_violations} FK violations, observed {len(fk_before)}; no repair committed",
                file=sys.stderr,
            )
            return 4

        try:
            result = repair_dangling_booking_allocations()
            archived = BookingAllocationRepairArchive.query.filter_by(
                repair_run_id=result["run_id"]
            ).count()
            if archived != result["archived_and_removed"]:
                raise RuntimeError(
                    f"archive count mismatch: expected {result['archived_and_removed']}, observed {archived}"
                )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    integrity_after, fk_after = _raw_checks(database)
    try:
        write_scope = _validate_exact_write_scope(
            Path(backup_path), database, result["row_ids"], result["run_id"]
        )
    except Exception as exc:
        print(
            f"ERROR: exact write-scope validation failed: {exc}. Restore from {backup_path}",
            file=sys.stderr,
        )
        return 6
    if integrity_after != ["ok"] or fk_after:
        print(
            f"ERROR: post-repair validation failed; restore from {backup_path}. "
            f"integrity={integrity_after!r}, FK rows={len(fk_after)}",
            file=sys.stderr,
        )
        return 5

    print(json.dumps({
        "repair_run_id": result["run_id"],
        "archived_and_removed": result["archived_and_removed"],
        "integrity_check_after": integrity_after,
        "foreign_key_violations_after": len(fk_after),
        "exact_write_scope": write_scope,
        "pre_repair_backup": backup_path,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
