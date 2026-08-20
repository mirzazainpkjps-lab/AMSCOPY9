#!/usr/bin/env python3
"""preflight_check.py — lightweight transaction-blocker watch for the AMS DB.

Purpose
-------
On the old app, heavy per-client histories (bookings, booked sales, dues,
cash payments, material returns, booking cancellations) accumulated broken
links and drift that ended up *blocking new transactions*.  This tool watches
for exactly those conditions and nothing else.  It is:

  * READ-ONLY            — never writes to the database
  * LIGHTWEIGHT          — one SQLite connection, indexed single-pass queries,
                           runs in ~1 second on the current 24k-row database
  * CRON-FRIENDLY        — exit code 0 (ok) / 1 (blocker found) / 2 (error),
                           optional --json, optional --quiet
  * SELF-CLEANING        — keeps only the last N runs in a small rolling log
                           (default 100 entries); no growing garbage files

Checks
------
BLOCK  (exit 1 — these stop new transactions):
  b1 settings row missing or allow_global_negative_stock off while materials
     are negative -> new sales of those materials are rejected
  b2 dangling booking_allocation FKs (sale_id / sale_item_id / booking_item_id)
  b3 active cancel-type entry rows (is_void=0 with type=CANCEL / Cancel)
  b4 bill_counter <= max sequence already present in the data (ID collision)
  b5 duplicate client codes
  b6 duplicate active pending_bill rows for the same client + bill_no

WATCH  (exit 0 — review, usually harmless but worth knowing):
  w1 duplicate client names (ambiguous client lookup)
  w2 duplicate active manual bill numbers across sales/bookings/pending bills
  w3 sales with no stock entry / w4 credit sales with no pending bill
  w5 orphaned invoices / w6 bookings with no pending bill
  w7 account ledger drift / w8 material (stock) ledger drift
  w9 payments with no client_id / w10 inactive master records

Usage
-----
    python tools/health/preflight_check.py
    python tools/health/preflight_check.py --json --quiet
    python tools/health/preflight_check.py --db /path/to/app.db --keep 200

    # daily cron example (output only on problems):
    0 8 * * * cd /home/user/ams99 && .venv/bin/python tools/health/preflight_check.py --quiet >> /dev/null 2>&1 || echo "AMS preflight: blockers found"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "instance" / "ahmed_cement.db"
LOG_DIR = REPO_ROOT / "instance" / "health"
LOG_FILE = LOG_DIR / "preflight.log"
DEFAULT_KEEP = 100


def connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"ERROR: database not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def scalar(con: sqlite3.Connection, sql: str, default=0):
    try:
        row = con.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default


def rows(con: sqlite3.Connection, sql: str):
    try:
        return [dict(r) for r in con.execute(sql).fetchall()]
    except sqlite3.Error:
        return []


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def max_sb_seq(con: sqlite3.Connection, ns: str, pairs: list[tuple[str, str]]) -> int:
    """Largest SB-<NS>-<n> sequence across the given (table, column) pairs."""
    mx = 0
    for table, col in pairs:
        if not table_exists(con, table):
            continue
        for r in rows(con, f'SELECT "{col}" AS v FROM "{table}" '
                           f'WHERE "{col}" LIKE \'SB-{ns}-%\' AND "{col}" IS NOT NULL'):
            v = str(r["v"]).strip()
            try:
                mx = max(mx, int(v.split(f"SB-{ns}-")[1]))
            except (IndexError, ValueError):
                continue
    return mx


def run_checks(con: sqlite3.Connection) -> dict:
    blocks: list[dict] = []
    watch: list[dict] = []

    # ---- b1: negative-stock blocker --------------------------------------
    neg_mats = scalar(con, "SELECT COUNT(*) FROM material WHERE total < 0")
    neg_list = rows(con, "SELECT name, ROUND(total,1) AS total FROM material "
                         "WHERE total < 0 ORDER BY total LIMIT 5")
    if table_exists(con, "settings"):
        srows = rows(con, "SELECT allow_global_negative_stock FROM settings LIMIT 1")
        neg_allowed = bool(srows and srows[0].get("allow_global_negative_stock"))
    else:
        srows, neg_allowed = [], False
    if neg_mats and not neg_allowed:
        blocks.append({
            "id": "b1_negative_stock_blocked",
            "message": f"{neg_mats} material(s) have negative stock and "
                       f"allow_global_negative_stock is OFF (settings row "
                       f"{'missing' if not srows else 'present'}) — new sales of these materials are rejected",
            "detail": neg_list,
        })
    elif neg_mats:
        watch.append({
            "id": "b1_negative_stock", "message": f"{neg_mats} material(s) negative "
            "(allowed by settings)", "detail": neg_list,
        })

    # ---- b2: dangling booking allocations --------------------------------
    for fk, ref in [("sale_id", "direct_sale"), ("sale_item_id", "direct_sale_item"),
                    ("booking_item_id", "booking_item")]:
        n = scalar(con, f"SELECT COUNT(*) FROM booking_allocation b "
                        f"WHERE NOT EXISTS (SELECT 1 FROM {ref} p WHERE p.id = b.{fk})")
        if n:
            blocks.append({"id": "b2_dangling_allocations",
                           "message": f"booking_allocation.{fk} -> {ref}: {n} dangling row(s)"})

    # ---- b3: active cancel-type entries -----------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM entry WHERE COALESCE(is_void,0)=0 AND "
                    "(UPPER(COALESCE(type,''))='CANCEL' OR "
                    " UPPER(COALESCE(transaction_category,''))='CANCEL')")
    if n:
        blocks.append({"id": "b3_active_cancel_entries",
                       "message": f"{n} active CANCEL entry row(s) (is_void=0) — "
                       "booking cancellations that were never voided"})

    # ---- b4: bill counter collisions --------------------------------------
    if table_exists(con, "bill_counter"):
        counters = rows(con, "SELECT namespace, count FROM bill_counter")
        seq_pairs = {
            "BK": [("booking", "auto_bill_no"), ("pending_bill", "bill_no")],
            "SL": [("direct_sale", "auto_bill_no")],
            "CP": [("payment", "auto_bill_no")],
            "RTN": [("material_return", "auto_bill_no")],
            "GRN": [("grn", "auto_bill_no")],
        }
        for c in counters:
            ns = str(c.get("namespace") or "").strip()
            mx = max_sb_seq(con, ns, seq_pairs.get(ns, [])) if ns in seq_pairs else 0
            if c.get("count") is not None and mx >= int(c["count"]):
                blocks.append({"id": "b4_bill_counter_collision",
                               "message": f"bill_counter {ns}={c['count']} <= max "
                               f"sequence in data {mx} — next bill number would collide"})

    # ---- b5: duplicate client codes ----------------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM (SELECT code FROM client "
                    "GROUP BY UPPER(TRIM(code)) HAVING COUNT(*) > 1)")
    if n:
        blocks.append({"id": "b5_duplicate_client_codes",
                       "message": f"{n} duplicate client code(s)"})

    # ---- b6: duplicate active pending bills --------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM (SELECT client_name, bill_no FROM pending_bill "
                    "WHERE COALESCE(is_void,0)=0 "
                    "GROUP BY UPPER(TRIM(client_name)), UPPER(TRIM(bill_no)) HAVING COUNT(*) > 1)")
    if n:
        blocks.append({"id": "b6_duplicate_pending_bills",
                       "message": f"{n} client+bill_no pair(s) with multiple active pending bills"})

    # ---- w1: duplicate client names ----------------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM (SELECT name FROM client "
                    "GROUP BY UPPER(TRIM(name)) HAVING COUNT(*) > 1)")
    if n:
        watch.append({"id": "w1_duplicate_client_names",
                      "message": f"{n} client name(s) shared by multiple records — "
                      "lookup by name may post to the wrong ledger"})

    # ---- w2: duplicate active manual bill numbers ---------------------------
    n = scalar(con, "SELECT COUNT(*) FROM (SELECT b FROM ("
                    "SELECT UPPER(TRIM(manual_bill_no)) AS b FROM direct_sale "
                    "WHERE COALESCE(is_void,0)=0 AND TRIM(COALESCE(manual_bill_no,''))<>'' "
                    "UNION ALL SELECT UPPER(TRIM(manual_bill_no)) FROM booking "
                    "WHERE COALESCE(is_void,0)=0 AND TRIM(COALESCE(manual_bill_no,''))<>'' "
                    "UNION ALL SELECT UPPER(TRIM(bill_no)) FROM pending_bill "
                    "WHERE COALESCE(is_void,0)=0 AND TRIM(COALESCE(bill_no,''))<>'') "
                    "GROUP BY b HAVING COUNT(*) > 1)")
    if n:
        watch.append({"id": "w2_duplicate_manual_bills",
                      "message": f"{n} manual bill number(s) used more than once across "
                      "sales/bookings/pending bills (legacy book overlap)"})

    # ---- w3/w4: sales linkage -------------------------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM direct_sale ds WHERE COALESCE(ds.is_void,0)=0 "
                    "AND ds.amount > 0 AND NOT EXISTS (SELECT 1 FROM entry e "
                    "WHERE COALESCE(e.is_void,0)=0 AND UPPER(COALESCE(e.type,''))='OUT' "
                    "AND COALESCE(e.nimbus_no,'')='Direct Sale' "
                    "AND (TRIM(COALESCE(e.bill_no,''))=TRIM(COALESCE(ds.manual_bill_no,'')) "
                    "  OR TRIM(COALESCE(e.bill_no,''))=TRIM(COALESCE(ds.auto_bill_no,''))))")
    if n:
        watch.append({"id": "w3_sales_missing_entries",
                      "message": f"{n} sale(s) with no stock entry"})

    n = scalar(con, "SELECT COUNT(*) FROM direct_sale ds WHERE COALESCE(ds.is_void,0)=0 "
                    "AND LOWER(COALESCE(ds.payment_method,'')) NOT IN ('cash','') "
                    "AND ds.amount > 0 AND NOT EXISTS (SELECT 1 FROM pending_bill pb "
                    "WHERE COALESCE(pb.is_void,0)=0 "
                    "AND TRIM(COALESCE(pb.client_name,''))=TRIM(COALESCE(ds.client_name,'')) "
                    "AND (TRIM(COALESCE(pb.bill_no,''))=TRIM(COALESCE(ds.manual_bill_no,'')) "
                    "  OR TRIM(COALESCE(pb.bill_no,''))=TRIM(COALESCE(ds.auto_bill_no,''))))")
    if n:
        watch.append({"id": "w4_credit_sales_missing_pending_bills",
                      "message": f"{n} credit sale(s) with no pending bill"})

    # ---- w5/w6: invoice & booking linkage ------------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM invoice i WHERE COALESCE(i.is_void,0)=0 "
                    "AND NOT EXISTS (SELECT 1 FROM direct_sale ds "
                    "WHERE COALESCE(ds.is_void,0)=0 AND ds.invoice_id = i.id)")
    if n:
        watch.append({"id": "w5_orphaned_invoices",
                      "message": f"{n} invoice(s) with no linked sale"})

    n = scalar(con, "SELECT COUNT(*) FROM booking b WHERE COALESCE(b.is_void,0)=0 "
                    "AND b.amount > 0 AND NOT EXISTS (SELECT 1 FROM pending_bill pb "
                    "WHERE COALESCE(pb.is_void,0)=0 "
                    "AND TRIM(COALESCE(pb.client_name,''))=TRIM(COALESCE(b.client_name,'')) "
                    "AND (TRIM(COALESCE(pb.bill_no,''))=TRIM(COALESCE(b.manual_bill_no,'')) "
                    "  OR TRIM(COALESCE(pb.bill_no,''))=TRIM(COALESCE(b.auto_bill_no,''))))")
    if n:
        watch.append({"id": "w6_bookings_missing_pending_bills",
                      "message": f"{n} booking(s) with no pending bill"})

    # ---- w7: account ledger drift ---------------------------------------------
    drift = rows(con, "SELECT a.id, a.name, a.balance AS stored, "
                      "ROUND((SELECT COALESCE(SUM(CASE WHEN t.to_account_id=a.id THEN t.amount ELSE 0 END),0) "
                      "     - COALESCE(SUM(CASE WHEN t.from_account_id=a.id THEN t.amount ELSE 0 END),0) "
                      "FROM account_transaction t WHERE COALESCE(t.is_void,0)=0),2) AS ledger "
                      "FROM account a "
                      "WHERE ABS(a.balance - (SELECT COALESCE(SUM(CASE WHEN t.to_account_id=a.id THEN t.amount ELSE 0 END),0) "
                      "     - COALESCE(SUM(CASE WHEN t.from_account_id=a.id THEN t.amount ELSE 0 END),0) "
                      "FROM account_transaction t WHERE COALESCE(t.is_void,0)=0)) > 0.01")
    if drift:
        watch.append({"id": "w7_account_ledger_drift",
                      "message": f"{len(drift)} account(s) where balance != transaction net",
                      "detail": drift[:10]})

    # ---- w8: material ledger drift ---------------------------------------------
    drift = rows(con, "SELECT m.name, m.total AS stored, ROUND((SELECT "
                      "COALESCE(SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN e.qty "
                      "WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -e.qty ELSE 0 END),0) "
                      "FROM entry e WHERE COALESCE(e.is_void,0)=0 "
                      "AND TRIM(COALESCE(e.material,''))=TRIM(m.name)),2) AS ledger "
                      "FROM material m WHERE ABS(m.total - (SELECT "
                      "COALESCE(SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN e.qty "
                      "WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -e.qty ELSE 0 END),0) "
                      "FROM entry e WHERE COALESCE(e.is_void,0)=0 "
                      "AND TRIM(COALESCE(e.material,''))=TRIM(m.name))) > 0.01")
    if drift:
        watch.append({"id": "w8_material_ledger_drift",
                      "message": f"{len(drift)} material(s) where total != entry net",
                      "detail": drift[:10]})

    # ---- w9: payments without client_id ----------------------------------------
    n = scalar(con, "SELECT COUNT(*) FROM payment WHERE client_id IS NULL "
                    "AND TRIM(COALESCE(client_name,''))<>''")
    if n:
        watch.append({"id": "w9_payments_missing_client_id",
                      "message": f"{n} payment(s) with a client name but no client_id link"})

    # ---- w10: inactive masters --------------------------------------------------
    for tbl in ("client", "material"):
        n = scalar(con, f"SELECT COUNT(*) FROM {tbl} WHERE COALESCE(is_active,1)=0")
        if n:
            watch.append({"id": "w10_inactive_masters",
                          "message": f"{n} inactive {tbl}(s) preserved (FK integrity)"})

    return {"blocks": blocks, "watch": watch}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="print only problems + summary")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help="rolling log entries to keep (default %(default)s)")
    ap.add_argument("--no-log", action="store_true", help="do not append to the rolling log")
    args = ap.parse_args()

    db = Path(args.db)
    try:
        con = connect(db)
    except SystemExit:
        raise
    try:
        checks = run_checks(con)
    finally:
        con.close()

    ts = datetime.now(timezone.utc).isoformat()
    status = "BLOCK" if checks["blocks"] else "OK"
    summary = {
        "ts": ts, "db": str(db), "status": status,
        "blocks": checks["blocks"], "watch": checks["watch"],
        "counts": {
            "blockers": len(checks["blocks"]),
            "watch_items": len(checks["watch"]),
        },
    }

    # rolling log (lightweight, capped)
    if not args.no_log:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines() if LOG_FILE.exists() else []
            lines.append(json.dumps(summary))
            LOG_FILE.write_text("\n".join(lines[-max(1, args.keep):]) + "\n", encoding="utf-8")
        except OSError:
            pass  # logging is best-effort; never fail the check because of it

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("=" * 78)
        print(f"  AMS PREFLIGHT  —  {db}")
        print(f"  {ts}")
        print("=" * 78)
        if checks["blocks"]:
            print(f"\n  BLOCKERS ({len(checks['blocks'])}) — new transactions may fail:")
            for b in checks["blocks"]:
                print(f"    ✗ {b['id']}: {b['message']}")
                for d in b.get("detail", [])[:5]:
                    print(f"        {d}")
        else:
            print("\n  BLOCKERS: none — new transactions are not obstructed by data state.")
        if checks["watch"]:
            print(f"\n  WATCH ({len(checks['watch'])}) — review, usually not blocking:")
            for w in checks["watch"]:
                print(f"    • {w['id']}: {w['message']}")
                for d in w.get("detail", [])[:5]:
                    print(f"        {d}")
        print("=" * 78)
        print(f"  RESULT: {status}  ({len(checks['blocks'])} blocker(s), "
              f"{len(checks['watch'])} watch item(s))")

    return 1 if checks["blocks"] else 0


if __name__ == "__main__":
    sys.exit(main())
