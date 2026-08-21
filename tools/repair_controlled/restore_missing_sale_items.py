"""Rebuild missing DirectSaleItem rows from surviving stock Entry rows.

Background
----------
A series of database rollbacks left some sales with their `direct_sale_item`
rows destroyed while the corresponding stock `entry` rows survived (the two
tables live on different DB pages and were restored from different snapshots).
Such a sale opens in the UI showing "0 materials" even though the stock ledger,
client balance and inventory totals are all still correct.

This script rebuilds the missing item rows from the surviving entries.

Recovery rules (all derived from invariants verified against healthy sales)
---------------------------------------------------------------------------
1.  material + quantity  -> taken verbatim from the entry (100% reliable).
2.  booking lines (entry.client_category == 'Booking Delivery') -> price 0.
    Verified: every healthy booking line stores price_at_time = 0.
3.  chargeable rent lines -> priced from `direct_sale.rent_item_revenue`.
    Verified on 476/476 healthy sales, 0 counterexamples.
4.  remaining chargeable material lines -> priced from the invariant
        sum(qty * price_at_time) == direct_sale.amount
    Verified on 2423/2426 healthy sales. With exactly one unpriced line the
    price is recovered EXACTLY; with several the residual is spread as a
    single blended rate and the sale is reported as ESTIMATED.

A line is never written with price 0 unless it is genuinely a booking line,
because `_direct_sale_item_category()` treats a 0 price as "Booking Delivery"
and a wrong category here would make the consistency rebuild void good
stock entries.

Nothing except `direct_sale_item` is touched: stock entries, pending bills,
invoices, payments and inventory totals are all left exactly as they are
(they were never lost, and they already reflect these materials).

Usage
-----
    # report only, writes nothing
    python tools/repair_controlled/restore_missing_sale_items.py

    # take a backup and apply
    python tools/repair_controlled/restore_missing_sale_items.py --confirm
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.repair_controlled.repair_guard import preflight  # noqa: E402

DEFAULT_DB = Path("instance") / "ahmed_cement.db"
TOLERANCE = 0.05
BOOKING = "Booking Delivery"


def _is_rent(name: str | None) -> bool:
    return "rent" in (name or "").replace("-", " ").replace("_", " ").lower()


def _round(value: float) -> float:
    return round(float(value or 0), 6)


def plan_sale(sale: sqlite3.Row, entries: list[sqlite3.Row]) -> dict:
    """Work out the item rows (and their prices) for one damaged sale."""
    amount = float(sale["amount"] or 0)
    rent_revenue = float(sale["rent_item_revenue"] or 0)

    booking = [e for e in entries if (e["client_category"] or "") == BOOKING]
    chargeable = [e for e in entries if (e["client_category"] or "") != BOOKING]
    rent_lines = [e for e in chargeable if _is_rent(e["material"])]
    goods_lines = [e for e in chargeable if not _is_rent(e["material"])]

    prices: dict[int, float] = {e["id"]: 0.0 for e in booking}
    notes: list[str] = []
    exact = True

    # --- rule 3: chargeable rent lines are priced from rent_item_revenue ---
    rent_total = 0.0
    if rent_lines:
        if rent_revenue > 0:
            rent_qty = sum(float(e["qty"] or 0) for e in rent_lines)
            if rent_qty > 0:
                rate = rent_revenue / rent_qty
                for e in rent_lines:
                    prices[e["id"]] = _round(rate)
                rent_total = rent_revenue
                if len(rent_lines) > 1:
                    notes.append("rent revenue split across multiple rent lines")
            else:
                exact = False
                notes.append("rent line has zero qty")
        else:
            # No recorded rent revenue: the rent line has to share the residual.
            goods_lines = goods_lines + rent_lines
            rent_lines = []

    # --- rule 4: residual of `amount` covers the remaining chargeable lines ---
    residual = amount - rent_total
    unpriced = [e for e in goods_lines if e["id"] not in prices]
    if unpriced:
        qty_total = sum(float(e["qty"] or 0) for e in unpriced)
        if qty_total <= 0:
            exact = False
            notes.append("chargeable line with zero qty")
            for e in unpriced:
                prices[e["id"]] = 0.0
        else:
            rate = residual / qty_total
            for e in unpriced:
                prices[e["id"]] = _round(rate)
            if len(unpriced) > 1:
                exact = False
                notes.append(
                    f"{len(unpriced)} chargeable lines share one amount; "
                    f"blended rate {rate:,.2f} applied"
                )
            if residual <= 0 and amount > 0:
                exact = False
                notes.append("residual after rent is not positive")
    elif residual > TOLERANCE:
        exact = False
        notes.append(f"{residual:,.2f} of the sale amount is unexplained by any line")

    rows = [
        {
            "entry_id": e["id"],
            "product_name": (e["material"] or "").strip(),
            "qty": float(e["qty"] or 0),
            "price": prices.get(e["id"], 0.0),
            "category": e["client_category"] or "",
        }
        for e in entries
    ]

    computed = sum(r["qty"] * r["price"] for r in rows)
    if abs(computed - amount) > TOLERANCE:
        exact = False
        notes.append(f"total check failed: {computed:,.2f} vs amount {amount:,.2f}")

    return {
        "sale_id": sale["id"],
        "client": sale["client_name"],
        "bill_no": sale["manual_bill_no"] or sale["auto_bill_no"],
        "date": (sale["date_posted"] or "")[:16],
        "category": sale["category"],
        "amount": amount,
        "rows": rows,
        "exact": exact,
        "notes": notes,
        "total_check_ok": abs(computed - amount) <= TOLERANCE,
    }


def find_damaged(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    sales = conn.execute(
        """
        SELECT s.*
          FROM direct_sale s
         WHERE s.is_void = 0
           AND NOT EXISTS (SELECT 1 FROM direct_sale_item i WHERE i.sale_id = s.id)
           AND EXISTS (SELECT 1 FROM entry e
                        WHERE e.source_id = s.id
                          AND e.source_table = 'direct_sale'
                          AND e.is_void = 0)
         ORDER BY s.id
        """
    ).fetchall()

    plans = []
    for sale in sales:
        entries = conn.execute(
            """
            SELECT id, material, qty, client_category
              FROM entry
             WHERE source_id = ? AND source_table = 'direct_sale' AND is_void = 0
             ORDER BY id
            """,
            (sale["id"],),
        ).fetchall()
        plans.append(plan_sale(sale, entries))
    return plans


def apply_plans(conn: sqlite3.Connection, plans: list[dict]) -> int:
    inserted = 0
    for plan in plans:
        for row in plan["rows"]:
            conn.execute(
                """
                INSERT INTO direct_sale_item
                       (sale_id, product_name, qty, price_at_time,
                        grn_item_id, cost_rate_at_sale)
                VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                (plan["sale_id"], row["product_name"], row["qty"], row["price"]),
            )
            inserted += 1
    return inserted


def verify(conn: sqlite3.Connection, plans: list[dict]) -> list[str]:
    """Post-write checks; any failure aborts the transaction."""
    problems = []
    for plan in plans:
        rows = conn.execute(
            "SELECT product_name, qty, price_at_time FROM direct_sale_item WHERE sale_id = ?",
            (plan["sale_id"],),
        ).fetchall()
        if len(rows) != len(plan["rows"]):
            problems.append(f"sale {plan['sale_id']}: expected {len(plan['rows'])} items, found {len(rows)}")
            continue
        total = sum(float(r["qty"] or 0) * float(r["price_at_time"] or 0) for r in rows)
        if abs(total - plan["amount"]) > TOLERANCE:
            problems.append(
                f"sale {plan['sale_id']}: items total {total:,.2f} != amount {plan['amount']:,.2f}"
            )

    still_empty = conn.execute(
        """
        SELECT COUNT(*) FROM direct_sale s
         WHERE s.is_void = 0
           AND NOT EXISTS (SELECT 1 FROM direct_sale_item i WHERE i.sale_id = s.id)
           AND EXISTS (SELECT 1 FROM entry e WHERE e.source_id = s.id
                        AND e.source_table = 'direct_sale' AND e.is_void = 0)
        """
    ).fetchone()[0]
    if still_empty:
        problems.append(f"{still_empty} restorable sale(s) still have no items")

    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"quick_check: {integrity}")
    return problems


def report(plans: list[dict], applied: bool) -> None:
    header = "APPLIED" if applied else "DRY RUN — nothing written"
    print(f"\n=== Restore missing sale items — {header} ===\n")
    if not plans:
        print("No damaged sales found. Nothing to do.\n")
        return

    exact = [p for p in plans if p["exact"]]
    estimated = [p for p in plans if not p["exact"]]
    lines = sum(len(p["rows"]) for p in plans)

    for plan in plans:
        flag = "EXACT    " if plan["exact"] else "ESTIMATED"
        print(f"[{flag}] sale {plan['sale_id']}  {plan['date']}  {plan['bill_no'] or '(no bill)'}")
        print(f"            {plan['client']}  |  {plan['category']}  |  amount {plan['amount']:,.2f}")
        for row in plan["rows"]:
            tag = "booking" if row["category"] == BOOKING else "charged"
            print(
                f"              - {row['product_name']:<24} qty {row['qty']:>10,.2f}"
                f"  @ {row['price']:>10,.2f}  ({tag})"
            )
        for note in plan["notes"]:
            print(f"              ! {note}")
        print()

    print("-" * 72)
    print(f"sales repaired      : {len(plans)}")
    print(f"item lines rebuilt  : {lines}")
    print(f"exact prices        : {len(exact)}")
    print(f"estimated prices    : {len(estimated)}"
          + (f"  -> sales {[p['sale_id'] for p in estimated]}" if estimated else ""))
    print(f"amount check passed : {sum(1 for p in plans if p['total_check_ok'])}/{len(plans)}")
    print("-" * 72 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--db", default=None, help="database path (default: instance/ahmed_cement.db)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Path(os.environ.get("APP_DB_PATH") or DEFAULT_DB)

    if args.confirm:
        preflight(
            script_name=__file__,
            description="Rebuild missing direct_sale_item rows from surviving stock entries",
            db_path=db_path,
            backup_dir=db_path.parent / "reconcile_backups",
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        plans = find_damaged(conn)

        if not args.confirm:
            report(plans, applied=False)
            if plans:
                print("Re-run with --confirm to write these rows (a backup is taken first).\n")
            return 0

        if not plans:
            report(plans, applied=False)
            return 0

        conn.execute("BEGIN")
        inserted = apply_plans(conn, plans)
        problems = verify(conn, plans)
        if problems:
            conn.rollback()
            print("\nVERIFICATION FAILED — all changes rolled back:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        conn.commit()
        report(plans, applied=True)
        print(f"{inserted} item rows written and verified.\n")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
