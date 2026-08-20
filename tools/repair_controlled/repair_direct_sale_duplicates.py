"""Void duplicate DirectSale, Entry, and PendingBill rows.

Usage:
    python tools/repair_controlled/repair_direct_sale_duplicates.py --confirm

WARNING: Uses raw sqlite3 — bypasses ORM hooks. Run only when duplicates are confirmed.
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.repair_controlled.repair_guard import preflight
preflight(
    script_name=__file__,
    description="Void duplicate DirectSale / Entry / PendingBill rows via raw sqlite3",
)

import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DB_PATH = Path("instance/ahmed_cement.db")
BACKUP_DIR = Path("instance/reconcile_backups")


def norm_name(value):
    return (value or "").strip().lower()


def qty_key(value):
    try:
        return round(float(value or 0), 4)
    except Exception:
        return 0.0


def sale_category(value):
    txt = (value or "").strip()
    aliases = {
        "Booking": "Booking Delivery",
        "Booked": "Booking Delivery",
        "Booked Sale": "Booking Delivery",
        "Booking Delivery": "Booking Delivery",
        "Mixed": "Mixed Transaction",
        "Booked + Due": "Mixed Transaction",
        "Mixed Transaction": "Mixed Transaction",
        "Credit": "Credit Customer",
        "Credit Customer": "Credit Customer",
        "Cash": "Cash",
        "Open Khata": "Open Khata",
    }
    return aliases.get(txt, txt or "Credit Customer")


def direct_sale_bill_ref(row):
    if row["manual_bill_no"]:
        return row["manual_bill_no"]
    if row["auto_bill_no"]:
        return row["auto_bill_no"]
    if sale_category(row["category"]) == "Cash":
        return f"CSH-{row['id']}"
    return f"DS-{row['id']}"


def client_identity(cur, row):
    name = (row["client_name"] or "").strip()
    if sale_category(row["category"]) == "Open Khata":
        return "OPEN-KHATA", name or "OPEN KHATA"
    client = cur.execute(
        "select code, name from client where lower(trim(name)) = ? limit 1",
        (norm_name(name),),
    ).fetchone()
    if client:
        return client["code"], client["name"]
    return None, name


def scoped_clause(client_code, client_name):
    clauses = []
    params = []
    if client_code:
        clauses.append("client_code = ?")
        params.append(client_code)
    if client_name:
        clauses.append("lower(trim(coalesce(client, ''))) = ?")
        params.append(norm_name(client_name))
    if not clauses:
        return "", []
    return " and (" + " or ".join(clauses) + ")", params


def pending_scoped_clause(client_code, client_name):
    clauses = []
    params = []
    if client_code:
        clauses.append("client_code = ?")
        params.append(client_code)
    if client_name:
        clauses.append("lower(trim(coalesce(client_name, ''))) = ?")
        params.append(norm_name(client_name))
    if not clauses:
        return "", []
    return " and (" + " or ".join(clauses) + ")", params


def restore_stock_for_voided_entry(cur, entry):
    if entry["is_void"]:
        return
    stock_name = entry["booked_material"] if entry["is_alternate"] and entry["booked_material"] else entry["material"]
    if not stock_name:
        return
    qty = float(entry["qty"] or 0)
    if entry["type"] == "OUT":
        cur.execute(
            "update material set total = coalesce(total, 0) + ? where name = ?",
            (qty, stock_name),
        )
    elif entry["type"] == "IN":
        cur.execute(
            "update material set total = coalesce(total, 0) - ? where name = ?",
            (qty, stock_name),
        )


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"before_direct_sale_duplicate_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(DB_PATH, backup_path)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    entries_voided = 0
    pending_voided = 0
    pending_updated = 0
    sales_seen = 0

    sales = cur.execute(
        "select id, client_name, category, amount, paid_amount, discount, manual_bill_no, auto_bill_no, is_void "
        "from direct_sale where coalesce(is_void, 0) = 0 order by id"
    ).fetchall()

    for sale in sales:
        bill_ref = direct_sale_bill_ref(sale)
        if not bill_ref:
            continue
        items = cur.execute(
            "select product_name, qty from direct_sale_item where sale_id = ? order by id",
            (sale["id"],),
        ).fetchall()
        if not items:
            continue

        client_code, client_name = client_identity(cur, sale)
        scope_sql, scope_params = scoped_clause(client_code, client_name)
        entries = cur.execute(
            "select id, type, material, booked_material, is_alternate, qty, is_void "
            "from entry where bill_no = ? and nimbus_no = 'Direct Sale' and coalesce(is_void, 0) = 0"
            + scope_sql
            + " order by id desc",
            [bill_ref] + scope_params,
        ).fetchall()
        if not entries:
            continue

        sales_seen += 1
        desired = Counter((row["product_name"], qty_key(row["qty"])) for row in items)
        kept = Counter()
        keep_ids = set()
        for entry in entries:
            key = (entry["material"], qty_key(entry["qty"]))
            if kept[key] < desired[key]:
                keep_ids.add(entry["id"])
                kept[key] += 1

        for entry in entries:
            if entry["id"] in keep_ids:
                continue
            restore_stock_for_voided_entry(cur, entry)
            cur.execute("update entry set is_void = 1 where id = ?", (entry["id"],))
            entries_voided += 1

        pending_scope_sql, pending_scope_params = pending_scoped_clause(client_code, client_name)
        pending_rows = cur.execute(
            "select id from pending_bill where bill_no = ? and coalesce(is_void, 0) = 0 "
            "and lower(coalesce(reason, '')) like 'direct sale%'"
            + pending_scope_sql
            + " order by id desc",
            [bill_ref] + pending_scope_params,
        ).fetchall()
        pending_amount = max(
            0.0,
            float(sale["amount"] or 0) - float(sale["discount"] or 0) - float(sale["paid_amount"] or 0),
        )
        reason = f"Direct Sale ({sale_category(sale['category'])}): {items[0]['product_name']}".rstrip(": ")
        is_paid = 1 if pending_amount <= 0 and float(sale["amount"] or 0) > 0 else 0

        if pending_rows:
            keep_pending_id = pending_rows[0]["id"]
            cur.execute(
                "update pending_bill set client_code = ?, client_name = ?, amount = ?, reason = ?, "
                "is_paid = ?, is_cash = ?, is_manual = ? where id = ?",
                (
                    client_code,
                    client_name,
                    pending_amount,
                    reason,
                    is_paid,
                    1 if sale_category(sale["category"]) == "Cash" else 0,
                    1 if sale["manual_bill_no"] else 0,
                    keep_pending_id,
                ),
            )
            pending_updated += 1
            for row in pending_rows[1:]:
                cur.execute("update pending_bill set is_void = 1 where id = ?", (row["id"],))
                pending_voided += 1

    con.commit()
    con.close()

    print(f"Backup: {backup_path}")
    print(f"Sales checked with active rows: {sales_seen}")
    print(f"Duplicate direct-sale entry rows voided: {entries_voided}")
    print(f"Pending bills updated: {pending_updated}")
    print(f"Duplicate pending bills voided: {pending_voided}")


if __name__ == "__main__":
    main()
