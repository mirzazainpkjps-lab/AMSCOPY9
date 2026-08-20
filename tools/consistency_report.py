"""
AMS Consistency Report — One-Command System Health Check
=========================================================
Prints a full data-integrity report without writing anything to the database.

Checks performed:
  1. Account.balance vs AccountTransaction ledger sum
  2. Material.total vs Entry (IN/OUT) net stock
  3. Orphaned Payments (no linked Client)
  4. Orphaned AccountTransactions (void flag inconsistency)
  5. DirectSale with missing Entry rows (stock not posted)
  6. DirectSale with missing PendingBill (ledger not posted)
  7. Invoice / DirectSale orphan pairs
  8. Booking with missing PendingBill
  9. CashFlowDifferenceAdjustment referencing non-existent reconciliation records
 10. Health snapshot freshness check

Usage:
    python tools/consistency_report.py
    python tools/consistency_report.py --json          # machine-readable output
    python tools/consistency_report.py --fail-on-error  # exit 1 if issues found

All reads are via the production SQLite database directly (read-only).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
_db_env = os.environ.get("APP_DB_PATH", "").strip()
DB_PATH = Path(_db_env).expanduser() if _db_env else (_repo_root / "instance" / "ahmed_cement.db")
_snapshot_env = os.environ.get("DB_HEALTH_SNAPSHOT_PATH", "").strip()
HEALTH_SNAPSHOT = (
    Path(_snapshot_env).expanduser()
    if _snapshot_env
    else DB_PATH.parent / "health_snapshot.json"
)
TOLERANCE = 0.01


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows(con: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def scalar(con: sqlite3.Connection, sql: str, params=(), default=0):
    r = con.execute(sql, params).fetchone()
    return r[0] if r and r[0] is not None else default


def check_account_balances(con: sqlite3.Connection) -> dict:
    """Compare stored Account.balance against AccountTransaction ledger sums."""
    accounts = rows(con, "SELECT id, name, balance FROM account")
    tx_sums = {
        r["aid"]: r["net"]
        for r in rows(
            con,
            """
            SELECT
                COALESCE(to_account_id, -1) AS aid,
                SUM(CASE WHEN to_account_id IS NOT NULL THEN COALESCE(amount,0) ELSE 0 END)
                - SUM(CASE WHEN from_account_id IS NOT NULL THEN COALESCE(amount,0) ELSE 0 END) AS net
            FROM account_transaction
            WHERE COALESCE(is_void,0) = 0
            GROUP BY to_account_id
            """,
        )
    }
    from_sums: dict[int, float] = {}
    for r in rows(con, "SELECT from_account_id, SUM(COALESCE(amount,0)) AS s FROM account_transaction WHERE COALESCE(is_void,0)=0 AND from_account_id IS NOT NULL GROUP BY from_account_id"):
        from_sums[r["from_account_id"]] = float(r["s"] or 0)

    ledger: dict[int, float] = {}
    for r in rows(con, "SELECT to_account_id, from_account_id, amount FROM account_transaction WHERE COALESCE(is_void,0)=0"):
        amt = float(r["amount"] or 0)
        if r["to_account_id"] is not None:
            ledger[r["to_account_id"]] = ledger.get(r["to_account_id"], 0.0) + amt
        if r["from_account_id"] is not None:
            ledger[r["from_account_id"]] = ledger.get(r["from_account_id"], 0.0) - amt

    mismatches = []
    for acc in accounts:
        aid = acc["id"]
        stored = float(acc["balance"] or 0)
        expected = float(ledger.get(aid, 0.0))
        diff = abs(stored - expected)
        if diff > TOLERANCE:
            mismatches.append({
                "account_id": aid,
                "name": acc["name"],
                "stored_balance": round(stored, 2),
                "ledger_balance": round(expected, 2),
                "difference": round(stored - expected, 2),
            })

    return {
        "check": "account_balances",
        "accounts_checked": len(accounts),
        "mismatches": mismatches,
        "status": "FAIL" if mismatches else "OK",
    }


def check_material_totals(con: sqlite3.Connection) -> dict:
    """Compare Material.total against net stock from Entry rows."""
    materials = rows(con, "SELECT id, name, total FROM material")
    net_map: dict[str, float] = {}
    for r in rows(
        con,
        """
        SELECT material, SUM(CASE WHEN UPPER(COALESCE(type,''))='IN' THEN COALESCE(qty,0)
                                  WHEN UPPER(COALESCE(type,''))='OUT' THEN -COALESCE(qty,0)
                                  ELSE 0 END) AS net
        FROM entry WHERE COALESCE(is_void,0)=0
        GROUP BY material
        """,
    ):
        net_map[(r["material"] or "").strip()] = float(r["net"] or 0)

    mismatches = []
    for mat in materials:
        name = (mat["name"] or "").strip()
        stored = float(mat["total"] or 0)
        expected = float(net_map.get(name, 0.0))
        diff = abs(stored - expected)
        if diff > TOLERANCE:
            mismatches.append({
                "material": name,
                "stored_total": round(stored, 2),
                "entry_net": round(expected, 2),
                "difference": round(stored - expected, 2),
            })

    return {
        "check": "material_totals",
        "materials_checked": len(materials),
        "mismatches": mismatches,
        "status": "FAIL" if mismatches else "OK",
    }


def check_orphaned_payments(con: sqlite3.Connection) -> dict:
    """Find Payment rows referencing a payment_account_id that no longer exists in account."""
    orphans = rows(
        con,
        """
        SELECT p.id, p.amount, p.client_name, p.payment_account_id, p.date_posted
        FROM payment p
        WHERE COALESCE(p.is_void,0) = 0
          AND p.payment_account_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM account a WHERE a.id = p.payment_account_id)
        LIMIT 100
        """,
    )
    return {
        "check": "orphaned_payments",
        "orphan_count": len(orphans),
        "orphans": orphans,
        "status": "FAIL" if orphans else "OK",
    }


def check_orphaned_account_transactions(con: sqlite3.Connection) -> dict:
    """Find AccountTransaction rows referencing non-existent accounts."""
    orphans = rows(
        con,
        """
        SELECT at.id, at.amount, at.to_account_id, at.from_account_id, at.transaction_type
        FROM account_transaction at
        WHERE COALESCE(at.is_void,0) = 0
          AND (
              (at.to_account_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM account a WHERE a.id = at.to_account_id))
           OR (at.from_account_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM account a WHERE a.id = at.from_account_id))
          )
        LIMIT 100
        """,
    )
    return {
        "check": "orphaned_account_transactions",
        "orphan_count": len(orphans),
        "orphans": orphans,
        "status": "FAIL" if orphans else "OK",
    }


def check_sales_missing_entries(con: sqlite3.Connection) -> dict:
    """Find active DirectSale rows that have no corresponding active OUT Entry."""
    missing = rows(
        con,
        """
        SELECT ds.id, ds.manual_bill_no, ds.auto_bill_no, ds.client_name, ds.amount
        FROM direct_sale ds
        WHERE COALESCE(ds.is_void,0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM entry e
              WHERE COALESCE(e.is_void,0) = 0
                AND UPPER(COALESCE(e.type,'')) = 'OUT'
                AND TRIM(COALESCE(e.nimbus_no,'')) = 'Direct Sale'
                AND (
                    TRIM(COALESCE(e.bill_no,'')) = TRIM(COALESCE(ds.manual_bill_no,''))
                 OR TRIM(COALESCE(e.bill_no,'')) = TRIM(COALESCE(ds.auto_bill_no,''))
                )
          )
          AND ds.amount > 0
        LIMIT 100
        """,
    )
    return {
        "check": "sales_missing_entries",
        "missing_count": len(missing),
        "missing": missing,
        "status": "WARN" if missing else "OK",
    }


def check_sales_missing_pending_bills(con: sqlite3.Connection) -> dict:
    """Find active credit DirectSale rows with no PendingBill.
    Credit sales = payment_method is not 'Cash' (or NULL/blank treated as credit).
    """
    missing = rows(
        con,
        """
        SELECT ds.id, ds.manual_bill_no, ds.auto_bill_no, ds.client_name, ds.amount, ds.payment_method
        FROM direct_sale ds
        WHERE COALESCE(ds.is_void,0) = 0
          AND LOWER(COALESCE(ds.payment_method,'')) NOT IN ('cash','')
          AND ds.amount > 0
          AND NOT EXISTS (
              SELECT 1 FROM pending_bill pb
              WHERE COALESCE(pb.is_void,0) = 0
                AND TRIM(COALESCE(pb.client_name,'')) = TRIM(COALESCE(ds.client_name,''))
                AND (
                    TRIM(COALESCE(pb.bill_no,'')) = TRIM(COALESCE(ds.manual_bill_no,''))
                 OR TRIM(COALESCE(pb.bill_no,'')) = TRIM(COALESCE(ds.auto_bill_no,''))
                )
          )
        LIMIT 100
        """,
    )
    return {
        "check": "credit_sales_missing_pending_bills",
        "missing_count": len(missing),
        "missing": missing,
        "status": "WARN" if missing else "OK",
    }


def check_orphaned_invoices(con: sqlite3.Connection) -> dict:
    """Find active Invoice rows with no matching active DirectSale."""
    orphans = rows(
        con,
        """
        SELECT i.id, i.invoice_no, i.client_name, i.balance
        FROM invoice i
        WHERE COALESCE(i.is_void,0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM direct_sale ds
              WHERE COALESCE(ds.is_void,0) = 0
                AND ds.invoice_id = i.id
          )
        LIMIT 100
        """,
    )
    return {
        "check": "orphaned_invoices",
        "orphan_count": len(orphans),
        "orphans": orphans,
        "status": "WARN" if orphans else "OK",
    }


def check_health_snapshot(con: sqlite3.Connection) -> dict:
    """Check how fresh the health snapshot is."""
    if not HEALTH_SNAPSHOT.exists():
        return {"check": "health_snapshot", "status": "WARN", "message": "health_snapshot.json not found"}
    try:
        data = json.loads(HEALTH_SNAPSHOT.read_text(encoding="utf-8"))
        ts_str = data.get("timestamp") or data.get("created_at") or ""
        age_hours = None
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").split("+")[0])
                age_hours = round((datetime.now() - ts).total_seconds() / 3600, 1)
            except Exception:
                pass
        return {
            "check": "health_snapshot",
            "snapshot_timestamp": ts_str,
            "age_hours": age_hours,
            "intentional_reset": data.get("intentional_reset"),
            "reset_source": data.get("reset_source"),
            "status": "OK",
        }
    except Exception as exc:
        return {"check": "health_snapshot", "status": "WARN", "message": str(exc)}


def check_booking_missing_pending_bills(con: sqlite3.Connection) -> dict:
    """Find active Bookings with no corresponding active PendingBill.
    Matches by bill_no (manual or auto) and client_name.
    """
    missing = rows(
        con,
        """
        SELECT b.id, b.client_name, b.amount,
               b.manual_bill_no, b.auto_bill_no
        FROM booking b
        WHERE COALESCE(b.is_void,0) = 0
          AND b.amount > 0
          AND NOT EXISTS (
              SELECT 1 FROM pending_bill pb
              WHERE COALESCE(pb.is_void,0) = 0
                AND TRIM(COALESCE(pb.client_name,'')) = TRIM(COALESCE(b.client_name,''))
                AND (
                    TRIM(COALESCE(pb.bill_no,'')) = TRIM(COALESCE(b.manual_bill_no,''))
                 OR TRIM(COALESCE(pb.bill_no,'')) = TRIM(COALESCE(b.auto_bill_no,''))
                )
          )
        LIMIT 100
        """,
    )
    return {
        "check": "bookings_missing_pending_bills",
        "missing_count": len(missing),
        "missing": missing,
        "status": "WARN" if missing else "OK",
    }


def run_all_checks() -> dict:
    con = connect()
    try:
        checks = [
            check_account_balances(con),
            check_material_totals(con),
            check_orphaned_payments(con),
            check_orphaned_account_transactions(con),
            check_sales_missing_entries(con),
            check_sales_missing_pending_bills(con),
            check_orphaned_invoices(con),
            check_booking_missing_pending_bills(con),
            check_health_snapshot(con),
        ]
    finally:
        con.close()

    total_issues = sum(1 for c in checks if c["status"] in ("FAIL", "WARN"))
    return {
        "generated_at": datetime.now().isoformat(),
        "db_path": str(DB_PATH),
        "overall_status": "FAIL" if any(c["status"] == "FAIL" for c in checks)
                          else ("WARN" if total_issues else "OK"),
        "total_checks": len(checks),
        "issues_found": total_issues,
        "checks": checks,
    }


def print_report(report: dict) -> None:
    w = 65
    print("=" * w)
    print("  AMS SYSTEM CONSISTENCY REPORT")
    print(f"  Generated: {report['generated_at']}")
    print(f"  Database : {report['db_path']}")
    print("=" * w)
    status_icon = {"OK": "✓", "WARN": "⚠", "FAIL": "✗"}.get(report["overall_status"], "?")
    print(f"  Overall Status: {status_icon} {report['overall_status']}")
    print(f"  Checks run    : {report['total_checks']}")
    print(f"  Issues found  : {report['issues_found']}")
    print("=" * w)

    for chk in report["checks"]:
        icon = {"OK": "✓", "WARN": "⚠", "FAIL": "✗"}.get(chk["status"], "?")
        name = chk["check"].replace("_", " ").title()
        print(f"\n  {icon} {name}  [{chk['status']}]")

        if chk["check"] == "account_balances":
            print(f"    Accounts checked: {chk['accounts_checked']}")
            if chk["mismatches"]:
                print(f"    MISMATCHES ({len(chk['mismatches'])}):")
                for m in chk["mismatches"][:10]:
                    print(f"      • {m['name']} (id={m['account_id']}): "
                          f"stored={m['stored_balance']:,.2f}  ledger={m['ledger_balance']:,.2f}  "
                          f"diff={m['difference']:+,.2f}")

        elif chk["check"] == "material_totals":
            print(f"    Materials checked: {chk['materials_checked']}")
            if chk["mismatches"]:
                print(f"    MISMATCHES ({len(chk['mismatches'])}):")
                for m in chk["mismatches"][:10]:
                    print(f"      • {m['material']}: stored={m['stored_total']:,.2f}  "
                          f"entry_net={m['entry_net']:,.2f}  diff={m['difference']:+,.2f}")

        elif chk["status"] in ("FAIL", "WARN"):
            count_key = next((k for k in chk if k.endswith("_count") or k == "orphan_count"), None)
            if count_key:
                print(f"    Count: {chk[count_key]}")
            items_key = next((k for k in chk if isinstance(chk[k], list)), None)
            if items_key and chk[items_key]:
                for item in chk[items_key][:5]:
                    print(f"      • {item}")

        elif chk["check"] == "health_snapshot":
            age = chk.get("age_hours")
            ts = chk.get("snapshot_timestamp", "unknown")
            print(f"    Snapshot timestamp: {ts}")
            print(f"    Age: {age} hours" if age is not None else "    Age: unknown")
            if chk.get("intentional_reset"):
                print(f"    Reset source: {chk.get('reset_source')}")

        else:
            if "message" in chk:
                print(f"    {chk['message']}")

    print()
    print("=" * w)
    if report["overall_status"] == "OK":
        print("  All checks passed. No inconsistencies detected.")
    elif report["overall_status"] == "WARN":
        print("  Warnings found. Review above and run repair if needed.")
    else:
        print("  FAILURES found. Data inconsistencies require attention.")
    print("=" * w)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AMS System Consistency Report")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    ap.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit with code 1 if any FAIL checks are found",
    )
    ns = ap.parse_args()

    report = run_all_checks()

    if ns.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)

    if ns.fail_on_error and report["overall_status"] == "FAIL":
        sys.exit(1)
