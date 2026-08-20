#!/usr/bin/env python3
"""Compare authoritative business state across two SQLite databases.

This validator is read-only. It records exact row-content hashes plus selected
counts/sums for financial, payment, inventory, and ledger source tables. The
only permitted differences for booking-allocation FK remediation are removed
``booking_allocation`` rows and the new immutable archive table.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BUSINESS_TABLES = (
    "account", "account_transaction", "booking", "booking_item", "client",
    "cash_flow_entry", "direct_sale", "direct_sale_item", "entry", "grn",
    "grn_allocation", "grn_item", "invoice", "material", "material_return",
    "material_return_item", "payment", "pending_bill", "supplier",
    "supplier_payment", "waive_off",
)
SUM_FIELDS = {
    "account": ("balance",),
    "account_transaction": ("amount",),
    "booking": ("amount", "paid_amount", "discount"),
    "booking_item": ("qty",),
    "cash_flow_entry": ("amount",),
    "direct_sale": ("amount", "paid_amount", "discount"),
    "direct_sale_item": ("qty",),
    "entry": ("qty",),
    "grn": ("total_amount", "paid_amount"),
    "grn_allocation": ("qty", "cost_rate"),
    "grn_item": ("qty",),
    "invoice": ("amount",),
    "material": ("stored",),
    "material_return": ("total_amount",),
    "material_return_item": ("qty",),
    "payment": ("amount", "discount"),
    "pending_bill": ("amount",),
    "supplier": ("opening_balance",),
    "supplier_payment": ("amount",),
    "waive_off": ("amount",),
}


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})")}


def _digest(connection, table):
    digest = hashlib.sha256()
    for row in connection.execute(f"SELECT * FROM {_quote(table)} ORDER BY rowid"):
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _metrics(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        metrics = {}
        for table in BUSINESS_TABLES:
            if table not in tables:
                metrics[table] = {"missing": True}
                continue
            columns = _columns(connection, table)
            sums = {}
            for field in SUM_FIELDS.get(table, ()):
                if field in columns:
                    sums[field] = connection.execute(
                        f"SELECT ROUND(COALESCE(SUM({_quote(field)}), 0), 6) FROM {_quote(table)}"
                    ).fetchone()[0]
            metrics[table] = {
                "rows": connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0],
                "sha256": _digest(connection, table),
                "sums": sums,
            }
        return {
            "integrity_check": [row[0] for row in connection.execute("PRAGMA integrity_check")],
            "foreign_key_violations": [list(row) for row in connection.execute("PRAGMA foreign_key_check")],
            "booking_allocation_rows": connection.execute("SELECT COUNT(*) FROM booking_allocation").fetchone()[0],
            "archive_rows": (
                connection.execute("SELECT COUNT(*) FROM booking_allocation_repair_archive").fetchone()[0]
                if "booking_allocation_repair_archive" in tables else 0
            ),
            "business_tables": metrics,
        }
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    before = _metrics(args.before.resolve())
    after = _metrics(args.after.resolve())
    changed = [
        table for table in BUSINESS_TABLES
        if before["business_tables"][table] != after["business_tables"][table]
    ]
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "before": str(args.before.resolve()),
        "after": str(args.after.resolve()),
        "business_tables_compared": len(BUSINESS_TABLES),
        "changed_business_tables": changed,
        "authoritative_business_state_unchanged": not changed,
        "before_metrics": before,
        "after_metrics": after,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(args.output)
    else:
        print(payload, end="")
    return 0 if not changed and after["integrity_check"] == ["ok"] and not after["foreign_key_violations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
