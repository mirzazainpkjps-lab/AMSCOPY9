"""Remove exact duplicate DirectSaleItems and void duplicate Entry/PendingBill rows.

Usage:
    python tools/repair_controlled/repair_exact_bill_duplicates.py --confirm

WARNING: Uses raw sqlite3 — bypasses ORM hooks. Run only when exact duplicates are confirmed.
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.repair_controlled.repair_guard import preflight
preflight(
    script_name=__file__,
    description="Remove exact duplicate DirectSaleItem rows and void duplicate entries",
)

import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


DB_PATH = Path("instance/ahmed_cement.db")
BACKUP_DIR = Path("instance/reconcile_backups")


def norm(value):
    return (value or "").strip().lower()


def rounded(value):
    try:
        return round(float(value or 0), 6)
    except Exception:
        return 0.0


def sale_category(value):
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
    return aliases.get((value or "").strip(), (value or "").strip() or "Credit Customer")


def sale_bill_ref(sale):
    if sale["manual_bill_no"]:
        return sale["manual_bill_no"]
    if sale["auto_bill_no"]:
        return sale["auto_bill_no"]
    if sale_category(sale["category"]) == "Cash":
        return f"CSH-{sale['id']}"
    return f"DS-{sale['id']}"


def client_identity(cur, sale):
    name = (sale["client_name"] or "").strip()
    if sale_category(sale["category"]) == "Open Khata":
        return "OPEN-KHATA", name or "OPEN KHATA"
    row = cur.execute(
        "select code, name from client where lower(trim(name)) = ? limit 1",
        (norm(name),),
    ).fetchone()
    if row:
        return row["code"], row["name"]
    return None, name


def restore_stock_for_voided_entry(cur, entry):
    if entry["is_void"]:
        return
    stock_name = entry["booked_material"] if entry["is_alternate"] and entry["booked_material"] else entry["material"]
    if not stock_name:
        return
    qty = float(entry["qty"] or 0)
    if entry["type"] == "OUT":
        cur.execute("update material set total = coalesce(total, 0) + ? where name = ?", (qty, stock_name))
    elif entry["type"] == "IN":
        cur.execute("update material set total = coalesce(total, 0) - ? where name = ?", (qty, stock_name))


def main():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"before_exact_duplicate_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(DB_PATH, backup_path)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    source_items_deleted = 0
    sale_amounts_updated = 0
    entry_rows_voided = 0
    pending_rows_voided = 0
    pending_rows_updated = 0

    # 1) Remove exact duplicate source item rows within a sale.
    sales_with_source_changes = set()
    item_rows = cur.execute(
        "select id, sale_id, product_name, qty, price_at_time, grn_item_id "
        "from direct_sale_item order by sale_id, id"
    ).fetchall()
    seen = {}
    for row in item_rows:
        key = (
            row["sale_id"],
            (row["product_name"] or "").strip(),
            rounded(row["qty"]),
            rounded(row["price_at_time"]),
            row["grn_item_id"],
        )
        if key in seen:
            cur.execute("delete from direct_sale_item where id = ?", (row["id"],))
            source_items_deleted += 1
            sales_with_source_changes.add(row["sale_id"])
        else:
            seen[key] = row["id"]

    for sale_id in sorted(sales_with_source_changes):
        amount = cur.execute(
            "select coalesce(sum(coalesce(qty, 0) * coalesce(price_at_time, 0)), 0) "
            "from direct_sale_item where sale_id = ?",
            (sale_id,),
        ).fetchone()[0]
        cur.execute("update direct_sale set amount = ? where id = ?", (float(amount or 0), sale_id))
        sale_amounts_updated += 1

    # 2) Void exact duplicate active direct-sale ledger rows; keep the latest active row.
    active_entries = cur.execute(
        "select id, type, material, booked_material, is_alternate, client, client_code, "
        "client_category, transaction_category, qty, bill_no, nimbus_no, is_void "
        "from entry where coalesce(is_void, 0) = 0 and nimbus_no = 'Direct Sale' and coalesce(bill_no, '') <> '' "
        "order by id desc"
    ).fetchall()
    groups = defaultdict(list)
    for row in active_entries:
        key = (
            row["bill_no"],
            row["client_code"] or "",
            norm(row["client"]),
            row["material"] or "",
            row["booked_material"] or "",
            int(row["is_alternate"] or 0),
            row["client_category"] or "",
            row["transaction_category"] or "",
            rounded(row["qty"]),
        )
        groups[key].append(row)

    for rows in groups.values():
        for duplicate in rows[1:]:
            restore_stock_for_voided_entry(cur, duplicate)
            cur.execute("update entry set is_void = 1 where id = ?", (duplicate["id"],))
            entry_rows_voided += 1

    # 3) Keep one active pending row per direct sale/client/bill and sync amount.
    active_sales = cur.execute(
        "select id, client_name, category, amount, paid_amount, discount, manual_bill_no, auto_bill_no, is_void "
        "from direct_sale where coalesce(is_void, 0) = 0"
    ).fetchall()
    for sale in active_sales:
        bill_ref = sale_bill_ref(sale)
        if not bill_ref:
            continue
        client_code, client_name = client_identity(cur, sale)
        clauses = ["bill_no = ?", "coalesce(is_void, 0) = 0", "lower(coalesce(reason, '')) like 'direct sale%'"]
        params = [bill_ref]
        if client_code or client_name:
            inner = []
            if client_code:
                inner.append("client_code = ?")
                params.append(client_code)
            if client_name:
                inner.append("lower(trim(coalesce(client_name, ''))) = ?")
                params.append(norm(client_name))
            clauses.append("(" + " or ".join(inner) + ")")
        pending_rows = cur.execute(
            "select id from pending_bill where " + " and ".join(clauses) + " order by id desc",
            params,
        ).fetchall()
        if not pending_rows:
            continue
        first_item = cur.execute(
            "select product_name from direct_sale_item where sale_id = ? order by id limit 1",
            (sale["id"],),
        ).fetchone()
        primary_material = first_item["product_name"] if first_item else ""
        pending_amount = max(
            0.0,
            float(sale["amount"] or 0) - float(sale["discount"] or 0) - float(sale["paid_amount"] or 0),
        )
        is_paid = 1 if pending_amount <= 0 and float(sale["amount"] or 0) > 0 else 0
        reason = f"Direct Sale ({sale_category(sale['category'])}): {primary_material}".rstrip(": ")
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
                pending_rows[0]["id"],
            ),
        )
        pending_rows_updated += 1
        for row in pending_rows[1:]:
            cur.execute("update pending_bill set is_void = 1 where id = ?", (row["id"],))
            pending_rows_voided += 1

    con.commit()
    con.close()

    print(f"Backup: {backup_path}")
    print(f"Exact duplicate source sale items deleted: {source_items_deleted}")
    print(f"Sale amounts recalculated: {sale_amounts_updated}")
    print(f"Exact duplicate direct-sale entry rows voided: {entry_rows_voided}")
    print(f"Pending rows updated: {pending_rows_updated}")
    print(f"Duplicate pending rows voided: {pending_rows_voided}")


if __name__ == "__main__":
    main()
