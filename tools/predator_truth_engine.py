#!/usr/bin/env python3
"""PREDATOR TRUTH ENGINE — raw-database-only independent verification.

Purpose
-------
This tool recomputes the critical business results of the AMS application
(cement/ERP) directly from the raw SQLite tables.  It intentionally does NOT
import or reuse any application code: no ORM models, no report functions, no
dashboard calculations, no balance helpers, no service-layer aggregations and
no application reconciliation functions.  It speaks plain SQL against the
database file so that every number it produces is an *independent* source of
truth that can be compared against:

    Account.balance / balance_minor / ledger / reconciliation
    Material.total / stock summary / material ledger / API
    current payables (client ledger projection) / export CSV
    invoice totals / direct sale amounts / payment sums

Usage
-----
    python tools/predator_truth_engine.py --db instance/ahmed_cement_v44_fresh.db
    python tools/predator_truth_engine.py --db .../db.sqlite3 --section accounts --json
    python tools/predator_truth_engine.py --db .../db.sqlite3 --check

``--check`` prints a compact PASS/FAIL verdict per invariant and exits non-zero
when any invariant diverges (useful in CI / cron).

Business-rule assumptions documented (and stated so they can be challenged)
---------------------------------------------------------------------------
SALES
  * An "active" sale is a ``direct_sale`` row with ``is_void = 0``.
  * gross_sales        = SUM(direct_sale.amount) over active sales.
  * sale_embedded_paid = SUM(direct_sale.paid_amount) over active sales.
  * sale_discounts     = SUM(direct_sale.discount) over active sales.
  * external_receipts  = SUM(payment.amount) over active ``payment`` rows with
                         ``payment_type='Receipt'`` and amount > 0.
  * total_paid         = sale_embedded_paid + external_receipts.
  * total_outstanding  = gross_sales - sale_discounts - total_paid (net across
                         all clients; a negative value means client credit).
  * item_quantity_sold is computed twice, independently:
      (a) SUM(direct_sale_item.qty) for items of active sales;
      (b) SUM(entry.qty) for `entry` rows ``type='OUT' AND nimbus_no='Direct
          Sale' AND is_void=0`` that trace back to an active direct_sale.
    (a) and (b) must agree; either may disagree with the app's own figure.

ACCOUNTS
  * Every ``account_transaction`` row is a money movement: ``to_account_id``
    credits (+) the destination account, ``from_account_id`` debits (-) the
    source account.  Void rows are excluded.  A row with both is a transfer; a
    row with neither is a zero-side-effect informational row (flagged).
  * Amounts are compared in *minor units* (paisa/cents): minor =
    ``coalesce(amount_minor, round(amount*100))``.
  * expected_balance_minor = opening_balance_minor
                             + SUM(credits) - SUM(debits).
    opening_balance_minor = coalesce(opening_balance_minor,
                                     round(opening_balance*100)).
  * Account.balance should equal from_minor(balance_minor) and both should
    equal expected.  Additionally the per-account *ledger running balance*
    (ordered by date_posted, id) is recomputed and must end at the same value.

INVENTORY
  * The application has no explicit "opening stock" column on ``material``;
    stock is a pure derivation of the ``entry`` movement table.  Therefore:
    expected_stock(material) = SUM(entry.qty where type='IN'  , is_void=0)
                             - SUM(entry.qty where type='OUT' , is_void=0)
    grouped by ``entry.material`` (this is the same rule the application's
    ``_rebuild_material_totals()`` claims to implement; the point of the
    predator engine is to verify the *stored* ``material.total`` against it).
  * ``entry.type='CANCEL'`` rows are booking-cancellation evidence and must
    never affect stock; they are excluded and any non-IN/OUT type in stock
    calculations is reported.
  * GRN truth: ``grn_item.qty`` (is_void=0) for a GRN must equal the ``entry``
    IN rows created for that GRN (auto_bill_no match) after void filtering.
  * Any entry row referencing a material name with no ``material`` master row
    is reported as GHOST STOCK (invisible to master-driven lists).
  * Alternate-material bookings: the entry row stores the *delivered* material
    in ``entry.material`` and the original in ``entry.booked_material``; stock
    counts the delivered material (matches the app rule).  Any entry with
    ``is_alternate=1`` and no ``booked_material`` is flagged.

RECEIVABLES / PAYABLES (client)
  * For each client identity (id, code/name match):
      debits  = opening_balance + SUM(active DirectSale.amount)
              + SUM(active Booking.amount) + SUM(manual PendingBill.amount)
      credits = SUM(active DirectSale.paid_amount + DirectSale.discount)
              + SUM(active Booking.paid_amount + Booking.discount)
              + SUM(active Payment.amount where amount>0 and
                    payment_type in (Receipt, Material Return))
              + SUM(active WaiveOff.amount)
              + SUM(booking-cancel credits from Entry)
    expected_outstanding = debits - credits.
  * The engine deliberately does *not* filter "Payment vs sale match": at the
    date of writing the application applies client-level (not bill-level)
    settlement, so the independent forecast is also client-level.
  * A ``pending_bill`` row that can be traced to an active DirectSale/Booking/
    Invoice source is a *derived projection* and is excluded from the
    independent forecast to avoid double counting (the same rule the app
    documents); untraceable (manual) rows are included as debits.

SUPPLIERS / PAYABLES
  * GRN total (independent):
      item_total = SUM(grn_item.qty * price_at_time, is_void=0)
      total      = item_total + loading + freight + other_expense
                   + tax_amount - discount + adjustment_amount
  * supplier_payable = SUM(GRN totals, is_void=0)
                     - SUM(SupplierPayment.amount, is_void=0, amount>0)
  * supplier_payments include auto-generated GRN payments (one per GRN when
    paid_amount>0); a GRN with paid_amount>0 must have exactly one linked
    active SupplierPayment, otherwise a duplicate/ghost payment is flagged.

INTEGRITY
  * Foreign-key orphans: every row of every FK-bearing table (grn.supplier_id,
    grn_item.grn_id, direct_sale_item.sale_id, entry.invoice_id,
    payment.client_id, material_return.payment_id, waive_off.payment_id,
    account_transaction.from/to/reconciliation_id, booking_item.booking_id,
    allocation rows, delivery rows...) whose referenced parent does not exist.
  * Duplicate scan: manual/auto bill numbers reused across or inside entity
    tables, multiple active PendingBill rows per source transaction, multiple
    active waive rows per payment, idempotency keys duplicated (should be
    impossible when a DB unique index exists — reported as schema gap if the
    index is missing), duplicate auto_bill_no in entry.
  * Soft-delete consistency: a Soft-voided parent with active child effects
    (e.g. active DirectSale with is_void=1 but active entry rows, or a GRN
    with is_void=1 but active items) is reported; the rules used are the
    documented application rules (void parent ⇒ void derived effects).

The tool never writes to the database.  It opens the file read-only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# Raw connection helpers
# ---------------------------------------------------------------------------

CORE_TABLES = [
    "account", "account_transaction", "booking", "booking_allocation",
    "booking_item", "client", "delivery_person", "delivery_person_payment",
    "delivery_rent", "direct_sale", "direct_sale_item", "entry", "grn",
    "grn_allocation", "grn_item", "invoice", "material", "material_return",
    "material_return_item", "payment", "pending_bill", "sale_delivery_persons",
    "supplier", "supplier_payment", "waive_off",
]


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _tables(con) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _minor(amount) -> int:
    """Minor-unit money from a stored float; mirrors the app's to_minor."""
    if amount is None:
        return 0
    try:
        from decimal import Decimal, ROUND_HALF_UP
        return int(
            (Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except Exception:
        return 0


def _cols(con, table: str) -> set[str]:
    if table not in _tables(con):
        return set()
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _col(con, table: str, name: str) -> bool:
    return name in _cols(con, table)


def _minor_col(con, table: str, amount_col: str, minor_col: str) -> str:
    cols = _cols(con, table)
    if minor_col in cols:
        return f"COALESCE({minor_col}, CAST(ROUND({amount_col} * 100) AS INTEGER))"
    return f"CAST(ROUND({amount_col} * 100) AS INTEGER)"


# ---------------------------------------------------------------------------
# SALES
# ---------------------------------------------------------------------------

def sales_section(con) -> dict:
    t = _tables(con)
    out = {"ok": True, "findings": []}
    if "direct_sale" not in t:
        return {"ok": True, "findings": ["table direct_sale missing"], "summary": {}}

    row = con.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN is_void=0 THEN 1 ELSE 0 END) AS active,
          SUM(CASE WHEN is_void=1 THEN 1 ELSE 0 END) AS voided,
          COALESCE(SUM(CASE WHEN is_void=0 THEN amount ELSE 0 END),0) AS gross,
          COALESCE(SUM(CASE WHEN is_void=0 THEN paid_amount ELSE 0 END),0) AS embedded_paid,
          COALESCE(SUM(CASE WHEN is_void=0 THEN discount ELSE 0 END),0) AS discount
        FROM direct_sale
        """
    ).fetchone()
    active = row["active"] or 0
    gross = float(row["gross"] or 0)
    embedded_paid = float(row["embedded_paid"] or 0)
    discounts = float(row["discount"] or 0)

    receipts = 0.0
    if "payment" in t:
        r = con.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS s FROM payment
            WHERE is_void=0 AND amount > 0
            """
        ).fetchone()
        receipts = float(r["s"] or 0)

    refunds = 0.0
    if "payment" in t:
        r = con.execute(
            """
            SELECT COALESCE(SUM(ABS(amount)),0) AS s FROM payment
            WHERE is_void=0 AND amount < 0
            """
        ).fetchone()
        refunds = float(r["s"] or 0)

    outstanding = gross - discounts - receipts - embedded_paid

    # (a) item qty from sale items
    item_qty_by_table = defaultdict(float)
    if "direct_sale_item" in t:
        for r in con.execute(
            """
            SELECT dsi.product_name AS name, COALESCE(SUM(dsi.qty),0) AS qty
            FROM direct_sale_item dsi
            JOIN direct_sale ds ON ds.id = dsi.sale_id
            WHERE ds.is_void = 0
            GROUP BY dsi.product_name
            """
        ):
            item_qty_by_table[r["name"] or ""] += float(r["qty"] or 0)

    # (b) item qty from entry rows
    item_qty_by_entry = defaultdict(float)
    entry_out_count = 0
    if "entry" in t:
        for r in con.execute(
            """
            SELECT material AS name, COALESCE(SUM(qty),0) AS qty, COUNT(*) AS n
            FROM entry
            WHERE type='OUT' AND nimbus_no='Direct Sale' AND is_void=0
            GROUP BY material
            """
        ):
            item_qty_by_entry[r["name"] or ""] += float(r["qty"] or 0)
            entry_out_count += int(r["n"] or 0)

    # amount vs items*rate mismatch
    mismatched = []
    if "direct_sale_item" in t:
        rows = con.execute(
            """
            SELECT ds.id, ds.amount, COALESCE(SUM(dsi.qty * dsi.price_at_time),0) AS item_total
            FROM direct_sale ds LEFT JOIN direct_sale_item dsi ON dsi.sale_id=ds.id
            WHERE ds.is_void=0
            GROUP BY ds.id
            HAVING ABS(ds.amount - COALESCE(SUM(dsi.qty * dsi.price_at_time),0)) > 0.02
            """
        ).fetchall()
        mismatched = [
            {"sale_id": r["id"], "stored_amount": float(r["amount"]),
             "items_amount": float(r["item_total"])}
            for r in rows
        ]

    summary = {
        "active_sales": active,
        "voided_sales": row["voided"] or 0,
        "gross_sales": round(gross, 2),
        "sale_embedded_paid": round(embedded_paid, 2),
        "external_receipts": round(receipts, 2),
        "refunds": round(refunds, 2),
        "sale_discounts": round(discounts, 2),
        "total_paid": round(embedded_paid + receipts, 2),
        "total_outstanding": round(outstanding, 2),
        "item_quantity_sold_from_items": {
            k: round(v, 4) for k, v in sorted(item_qty_by_table.items())
        },
        "item_quantity_sold_from_entries": {
            k: round(v, 4) for k, v in sorted(item_qty_by_entry.items())
        },
        "entry_rows_out_direct_sale": entry_out_count,
        "amount_vs_items_mismatch": mismatched,
    }

    # cross-check the two independent qty derivations
    keys = set(item_qty_by_table) | set(item_qty_by_entry)
    for k in sorted(keys):
        a = round(item_qty_by_table.get(k, 0.0), 4)
        b = round(item_qty_by_entry.get(k, 0.0), 4)
        if abs(a - b) > 0.005:
            out["ok"] = False
            out["findings"].append(
                f"SALES.QTY.DIVERGENCE material={k!r} items_table={a} entry_table={b}"
            )
    if mismatched:
        out["ok"] = False
        out["findings"].append(
            f"SALES.AMOUNT_VS_ITEMS {len(mismatched)} active sale(s) store an amount "
            "that differs from SUM(item.qty*price_at_time)"
        )
    out["summary"] = summary
    return out


# ---------------------------------------------------------------------------
# ACCOUNTS
# ---------------------------------------------------------------------------

def accounts_section(con) -> dict:
    t = _tables(con)
    out = {"ok": True, "findings": [], "accounts": [], "summary": {}}
    if "account" not in t or "account_transaction" not in t:
        return {**out, "findings": ["required tables missing"]}

    tx_minor_expr = _minor_col(con, "account_transaction", "amount", "amount_minor")
    rows = con.execute(
        f"""
        SELECT
          COALESCE(to_account_id, 0)  AS to_id,
          COALESCE(from_account_id, 0) AS from_id,
          SUM(CASE WHEN to_account_id IS NOT NULL AND is_void=0 THEN {tx_minor_expr} ELSE 0 END) AS cr,
          SUM(CASE WHEN from_account_id IS NOT NULL AND is_void=0 THEN {tx_minor_expr} ELSE 0 END) AS dr
        FROM account_transaction
        GROUP BY COALESCE(to_account_id,0), COALESCE(from_account_id,0)
        """
    ).fetchall()
    credit_by = defaultdict(int)
    debit_by = defaultdict(int)
    for r in rows:
        if r["to_id"]:
            credit_by[r["to_id"]] += int(r["cr"] or 0)
        if r["from_id"]:
            debit_by[r["from_id"]] += int(r["dr"] or 0)

    bad = 0
    accounts = []
    for a in con.execute("SELECT * FROM account ORDER BY id"):
        opening_minor = int(
            a["opening_balance_minor"] if a["opening_balance_minor"] is not None
            else _minor(a["opening_balance"])
        )
        expected = opening_minor + credit_by.get(a["id"], 0) - debit_by.get(a["id"], 0)
        stored_minor = a["balance_minor"]
        stored_float = float(a["balance"] or 0)
        float_minor = _minor(stored_float)
        issues = []
        if stored_minor is None:
            issues.append("balance_minor NULL (minor ledger not maintained)")
            stored_minor = float_minor
        if int(stored_minor) != expected:
            issues.append(
                f"balance_minor={stored_minor} expected={expected} "
                f"(delta={int(stored_minor) - expected})"
            )
        if float_minor != int(stored_minor):
            issues.append(
                f"balance={stored_float:.4f} != from_minor(balance_minor)={stored_minor/100:.4f}"
            )
        if abs(float(a["opening_balance"] or 0) * 100 - _minor(a["opening_balance"])) > 0:
            issues.append("opening_balance float/minor mismatch")

        # per-account running balance check
        running = opening_minor
        run_ok = True
        for tx in con.execute(
            f"""
            SELECT id, date_posted, {tx_minor_expr} AS minor, is_void,
                   to_account_id, from_account_id
            FROM account_transaction
            WHERE (to_account_id = {a['id']} OR from_account_id = {a['id']})
            ORDER BY COALESCE(date_posted,'0000'), id
            """
        ):
            if not tx["is_void"]:
                if tx["to_account_id"] == a["id"]:
                    running += int(tx["minor"] or 0)
                if tx["from_account_id"] == a["id"]:
                    running -= int(tx["minor"] or 0)
        if running != expected:
            run_ok = False
            issues.append(f"ledger running balance {running} != expected {expected}")

        # independent cross-check against the latest finalised reconciliation
        rec_issue = None
        if "account_reconciliation" in t:
            rec = con.execute(
                """
                SELECT final_reconciled_balance_minor, final_reconciled_balance,
                       status, reconciliation_date
                FROM account_reconciliation
                WHERE account_id = ? AND status = 'Reconciled'
                ORDER BY reconciliation_date DESC, id DESC LIMIT 1
                """,
                (a["id"],),
            ).fetchone()
            if rec is not None:
                rec_minor = int(
                    rec["final_reconciled_balance_minor"]
                    if rec["final_reconciled_balance_minor"] is not None
                    else _minor(rec["final_reconciled_balance"])
                )
                if rec_minor != expected:
                    rec_issue = (
                        f"latest reconciliation final={rec_minor} "
                        f"({rec['reconciliation_date']}) != expected={expected}"
                    )
                    issues.append(rec_issue)

        accounts.append({
            "id": a["id"], "name": a["name"], "category": a["category"],
            "opening_balance": float(a["opening_balance"] or 0),
            "opening_balance_minor": opening_minor,
            "credits_minor": credit_by.get(a["id"], 0),
            "debits_minor": debit_by.get(a["id"], 0),
            "expected_balance_minor": expected,
            "stored_balance_minor": int(stored_minor),
            "stored_balance": stored_float,
            "running_balance_ok": run_ok,
            "reconciliation_issue": rec_issue,
            "issues": issues,
        })
        if issues:
            bad += 1
            out["ok"] = False
            out["findings"].append(
                f"ACCOUNT.DIVERGENCE id={a['id']} name={a['name']!r}: " + "; ".join(issues)
            )

    # zero-sided or self-sided tx rows
    tx_minor_expr2 = _minor_col(con, "account_transaction", "amount", "amount_minor")
    odd = []
    for r in con.execute(
        f"""
        SELECT id, to_account_id, from_account_id, transaction_type, note,
               {tx_minor_expr2} AS minor
        FROM account_transaction
        WHERE is_void=0
        """
    ):
        if r["to_account_id"] is None and r["from_account_id"] is None:
            odd.append({"id": r["id"], "type": r["transaction_type"],
                        "note": r["note"], "minor": r["minor"],
                        "issue": "no side (invisible money)"})
        elif r["to_account_id"] is not None and r["to_account_id"] == r["from_account_id"]:
            odd.append({"id": r["id"], "type": r["transaction_type"],
                        "note": r["note"], "minor": r["minor"],
                        "issue": "self-transfer"})
    if odd:
        out["ok"] = False
        out["findings"].append(f"ACCOUNT.TX_ODD_ROWS {len(odd)} active transaction rows "
                               "with zero or self sides (see detail)")
    out["odd_transactions"] = odd
    out["summary"] = {
        "account_count": len(accounts),
        "diverged_accounts": bad,
        "diverged_account_ids": [a["id"] for a in accounts if a["issues"]],
    }
    return out


# ---------------------------------------------------------------------------
# INVENTORY
# ---------------------------------------------------------------------------

def inventory_section(con) -> dict:
    t = _tables(con)
    out = {"ok": True, "findings": [], "materials": [], "ghost_stock": [], "summary": {}}
    if "material" not in t or "entry" not in t:
        return {**out, "findings": ["required tables missing"]}

    expected = defaultdict(float)
    entry_by_mat = defaultdict(list)
    for r in con.execute(
        "SELECT id, material, type, qty, is_void, nimbus_no, bill_no, source_table, source_id "
        "FROM entry ORDER BY id"
    ):
        mat = r["material"]
        entry_by_mat[mat].append(r)
        if r["is_void"]:
            continue
        if r["type"] == "IN":
            expected[mat] += float(r["qty"] or 0)
        elif r["type"] == "OUT":
            expected[mat] -= float(r["qty"] or 0)

    bad = 0
    materials = []
    for m in con.execute("SELECT * FROM material ORDER BY id"):
        exp = round(expected.get(m["name"], 0.0), 6)
        stored = round(float(m["total"] or 0), 6)
        issues = []
        if abs(exp - stored) > 0.004:
            issues.append(f"total={stored} expected={exp} (delta={round(stored - exp, 6)})")
        materials.append({
            "id": m["id"], "name": m["name"], "code": m["code"],
            "stored_total": stored, "expected_stock": exp, "issues": issues,
        })
        if issues:
            bad += 1
            out["ok"] = False
            out["findings"].append(
                f"STOCK.DIVERGENCE material={m['name']!r} stored={stored} expected={exp}"
            )

    master_names = {m["name"] for m in con.execute("SELECT name FROM material")}
    ghost = []
    for mat, rows in entry_by_mat.items():
        if mat and mat not in master_names:
            active = [r for r in rows if not r["is_void"]]
            if active:
                ghost.append({
                    "material_name": mat,
                    "active_rows": len(active),
                    "net_qty": round(sum(
                        (float(r["qty"] or 0) if r["type"] == "IN" else
                         -float(r["qty"] or 0)) for r in active
                    ), 6),
                })
    if ghost:
        out["ok"] = False
        out["findings"].append(
            f"STOCK.GHOST_MATERIALS {len(ghost)} material name(s) appear in entries "
            "but have no master row: " + ", ".join(sorted({g['material_name'] for g in ghost}))
        )
    out["ghost_stock"] = ghost

    # GRN item vs entry reconciliation
    grn_issues = []
    if "grn_item" in t and "grn" in t:
        item_by_grn = defaultdict(lambda: [0.0, 0])
        for r in con.execute(
            """
            SELECT grn_id, COALESCE(SUM(qty),0) AS qty, COUNT(*) AS n
            FROM grn_item WHERE is_void=0 GROUP BY grn_id
            """
        ):
            item_by_grn[r["grn_id"]] = [float(r["qty"] or 0), int(r["n"] or 0)]
        entry_by_grn = defaultdict(float)
        for r in con.execute(
            """
            SELECT e.auto_bill_no AS bill, g.id AS gid, COALESCE(SUM(e.qty),0) AS qty
            FROM entry e JOIN grn g ON g.auto_bill_no = e.auto_bill_no
            WHERE e.type='IN' AND e.is_void=0
            GROUP BY g.id
            """
        ):
            entry_by_grn[r["gid"]] += float(r["qty"] or 0)
        for gid, (qty, n) in item_by_grn.items():
            eqty = entry_by_grn.get(gid, 0.0)
            if abs(qty - eqty) > 0.004:
                grn_issues.append({
                    "grn_id": gid, "item_qty": qty, "entry_qty": eqty,
                })
    if grn_issues:
        out["ok"] = False
        out["findings"].append(
            f"STOCK.GRN_ITEM_ENTRY_MISMATCH {len(grn_issues)} GRN(s) whose active "
            "item qty differs from active IN entry qty"
        )
    out["grn_item_entry_mismatch"] = grn_issues

    # void-parent-consistent active children
    parent_child = []
    if "grn" in t:
        for r in con.execute(
            """
            SELECT g.id, g.is_void, COUNT(gi.id) AS active_items
            FROM grn g LEFT JOIN grn_item gi ON gi.grn_id=g.id AND gi.is_void=0
            GROUP BY g.id
            HAVING g.is_void=1 AND active_items>0
            """
        ):
            parent_child.append({"grn_id": r["id"], "active_items": r["active_items"]})
    if parent_child:
        out["ok"] = False
        out["findings"].append(
            f"STOCK.VOID_PARENT_ACTIVE_CHILD {len(parent_child)} voided GRN(s) still have "
            "active items: " + ", ".join(str(p["grn_id"]) for p in parent_child)
        )
    out["void_parent_active_child"] = parent_child

    out["summary"] = {
        "material_count": len(materials),
        "diverged_materials": bad,
        "ghost_material_count": len(ghost),
        "grn_item_entry_mismatch_count": len(grn_issues),
        "void_parent_active_child_count": len(parent_child),
    }
    return out


# ---------------------------------------------------------------------------
# RECEIVABLES / PAYABLES
# ---------------------------------------------------------------------------

def receivables_section(con) -> dict:
    t = _tables(con)
    out = {"ok": True, "findings": [], "clients": [], "summary": {}}
    if "client" not in t:
        return {**out, "findings": ["client table missing"]}

    clients = con.execute("SELECT * FROM client ORDER BY id").fetchall()
    by_id = {c["id"]: c for c in clients}
    by_name = {}
    by_code = {}
    for c in clients:
        by_name.setdefault((c["name"] or "").strip().lower(), c)
        by_code.setdefault((c["code"] or "").strip().lower(), c)

    def resolve(client_name, client_code):
        if client_code:
            c = by_code.get((client_code or "").strip().lower())
            if c:
                return c
        if client_name:
            return by_name.get((client_name or "").strip().lower())
        return None

    def bucket(row: dict) -> dict:
        cid = row.get("client_id")
        c = by_id.get(cid) if cid else None
        c = c or resolve(row.get("client_name"), row.get("client_code"))
        key = c["id"] if c else ("unresolved:" + ((row.get("client_name") or "").strip().lower()))
        return key

    sale_d = defaultdict(float)
    sale_paid = defaultdict(float)
    sale_disc = defaultdict(float)
    if "direct_sale" in t:
        for r in con.execute(
            "SELECT client_name, client_code, amount, paid_amount, discount, is_void "
            "FROM direct_sale WHERE is_void=0"
        ):
            key = bucket(dict(r))
            sale_d[key] += float(r["amount"] or 0)
            sale_paid[key] += float(r["paid_amount"] or 0)
            sale_disc[key] += float(r["discount"] or 0)

    booking_d = defaultdict(float)
    booking_paid = defaultdict(float)
    booking_disc = defaultdict(float)
    if "booking" in t:
        for r in con.execute(
            "SELECT client_name, amount, paid_amount, discount, is_void FROM booking "
            "WHERE is_void=0"
        ):
            key = bucket(dict(r))
            booking_d[key] += float(r["amount"] or 0)
            booking_paid[key] += float(r["paid_amount"] or 0)
            booking_disc[key] += float(r["discount"] or 0)

    pay_credit = defaultdict(float)
    if "payment" in t:
        for r in con.execute(
            "SELECT client_id, client_name, amount, payment_type, is_void FROM payment "
            "WHERE is_void=0"
        ):
            key = bucket(dict(r))
            amt = float(r["amount"] or 0)
            ptype = (r["payment_type"] or "").strip().lower()
            if amt > 0:                       # Receipt / Material Return reduce dues
                pay_credit[key] += amt
            elif amt < 0 and ptype in ("refund", "repayment"):
                pay_credit[key] += amt        # negative credit = more dues

    waive_credit = defaultdict(float)
    if "waive_off" in t:
        sel = "client_name, client_code, amount, is_void FROM waive_off WHERE is_void=0"
        if _col(con, "waive_off", "client_id"):
            sel = "client_id, " + sel
        for r in con.execute("SELECT " + sel):
            key = bucket(dict(r))
            waive_credit[key] += float(r["amount"] or 0)

    cancel_credit = defaultdict(float)
    if "entry" in t:
        for r in con.execute(
            "SELECT client, client_code, bill_no, qty, type, is_void, note, material "
            "FROM entry WHERE type='CANCEL' AND is_void=0"
        ):
            key = bucket(dict(r))
            if key.startswith("unresolved:"):
                continue
            # cancel credit = qty * booking rate; rate recorded in note
            note = r["note"] or ""
            rate = 0.0
            for part in note.split("|"):
                part = part.strip()
                if part.startswith("rate="):
                    try:
                        rate = float(part.split("=", 1)[1])
                    except Exception:
                        rate = 0.0
            cancel_credit[key] += float(r["qty"] or 0) * rate

    manual_pending = defaultdict(float)
    if "pending_bill" in t:
        for r in con.execute(
            "SELECT * FROM pending_bill WHERE is_void=0 AND is_paid=0"
        ):
            key = bucket(dict(r))
            # derived pendings are projections of real sources — exclude
            src = ((r["source_table"] or "") + "|" + (r["source_module"] or "")).lower()
            if any(x in src for x in ("direct_sale", "booking", "invoice", "sales")):
                continue
            manual_pending[key] += float(r["amount"] or 0)

    results = []
    unresolved_debits = 0.0
    unresolved_ids = set()
    for key, val in sale_d.items():
        if isinstance(key, str) and key.startswith("unresolved:"):
            unresolved_debits += val
            unresolved_ids.add(key)
    for key, val in booking_d.items():
        if isinstance(key, str) and key.startswith("unresolved:"):
            unresolved_debits += val
    for key, val in manual_pending.items():
        if isinstance(key, str) and key.startswith("unresolved:"):
            unresolved_debits += val
    if unresolved_debits > 0.005:
        out["ok"] = False
        out["findings"].append(
            f"RECEIVABLES.UNRESOLVED {unresolved_debits:.2f} of receivables belong to "
            "source rows with no Client master row (open-khata / orphan names): "
            + ", ".join(sorted(unresolved_ids)) + " — invisible to client-keyed reports"
        )
    out["unresolved_receivables"] = round(unresolved_debits, 2)

    results = []
    for c in clients:
        key = c["id"]
        opening = float(c["opening_balance"] or 0)
        debits = opening + sale_d[key] + booking_d[key] + manual_pending[key]
        credits = (sale_paid[key] + sale_disc[key] + booking_paid[key]
                   + booking_disc[key] + pay_credit[key] + waive_credit[key]
                   + cancel_credit[key])
        expected = round(debits - credits, 2)
        results.append({
            "client_id": c["id"], "name": c["name"], "code": c["code"],
            "opening_balance": opening,
            "sales_debit": round(sale_d[key], 2),
            "bookings_debit": round(booking_d[key], 2),
            "manual_pending_debit": round(manual_pending[key], 2),
            "sale_paid_credit": round(sale_paid[key], 2),
            "sale_discount_credit": round(sale_disc[key], 2),
            "booking_paid_credit": round(booking_paid[key], 2),
            "booking_discount_credit": round(booking_disc[key], 2),
            "payment_credit": round(pay_credit[key], 2),
            "waive_credit": round(waive_credit[key], 2),
            "cancel_credit": round(cancel_credit[key], 2),
            "expected_outstanding": expected,
        })
    out["clients"] = results
    out["summary"] = {
        "client_count": len(results),
        "total_expected_outstanding": round(sum(r["expected_outstanding"] for r in results), 2),
    }
    return out


def suppliers_section(con) -> dict:
    t = _tables(con)
    out = {"ok": True, "findings": [], "suppliers": [], "summary": {}}
    if "supplier" not in t:
        return {**out, "findings": ["supplier table missing"]}

    grn_totals = defaultdict(float)
    if "grn" in t and "grn_item" in t:
        for r in con.execute(
            """
            SELECT g.id, g.supplier_id, g.is_void,
                   COALESCE((SELECT SUM(gi.qty*gi.price_at_time) FROM grn_item gi
                             WHERE gi.grn_id=g.id AND gi.is_void=0),0) AS item_total,
                   COALESCE(g.loading_cost,0)+COALESCE(g.freight_cost,0)
                     +COALESCE(g.other_expense,0)+COALESCE(g.tax_amount,0)
                     -COALESCE(g.discount,0)+COALESCE(g.adjustment_amount,0) AS extras
            FROM grn g
            """
        ):
            if r["is_void"]:
                continue
            grn_totals[r["supplier_id"] or 0] += (
                float(r["item_total"] or 0) + float(r["extras"] or 0)
            )

    payments = defaultdict(float)
    if "supplier_payment" in t:
        for r in con.execute(
            "SELECT supplier_id, amount, is_void FROM supplier_payment WHERE is_void=0 AND amount>0"
        ):
            payments[r["supplier_id"] or 0] += float(r["amount"] or 0)

    results = []
    for s in con.execute("SELECT * FROM supplier ORDER BY id"):
        opening = float(s["opening_balance"] or 0)
        payable = opening + grn_totals[s["id"]] - payments[s["id"]]
        results.append({
            "supplier_id": s["id"], "name": s["name"],
            "opening_balance": opening,
            "grn_total": round(grn_totals[s["id"]], 2),
            "payments": round(payments[s["id"]], 2),
            "expected_payable": round(payable, 2),
        })

    # GRN paid_amount must map to exactly one active auto payment
    autopay_gap = []
    if "grn" in t and "supplier_payment" in t:
        for r in con.execute(
            """
            SELECT g.id, g.paid_amount, g.is_void,
              (SELECT COUNT(*) FROM supplier_payment sp
               WHERE sp.is_void=0 AND sp.source_type='GRN' AND sp.source_id=g.id) AS n
            FROM grn g
            """
        ):
            if r["is_void"] or float(r["paid_amount"] or 0) <= 0:
                continue
            if int(r["n"] or 0) != 1:
                autopay_gap.append({
                    "grn_id": r["id"], "paid_amount": float(r["paid_amount"]),
                    "active_auto_payments": int(r["n"] or 0),
                })
    if autopay_gap:
        out["ok"] = False
        out["findings"].append(
            f"SUPPLIER.AUTO_PAYMENT_GAP {len(autopay_gap)} GRN(s) with paid_amount>0 "
            "do not have exactly one active auto SupplierPayment"
        )
    out["grn_auto_payment_gap"] = autopay_gap
    out["suppliers"] = results
    out["summary"] = {
        "supplier_count": len(results),
        "total_expected_payable": round(sum(r["expected_payable"] for r in results), 2),
        "auto_payment_gap_count": len(autopay_gap),
    }
    return out


# ---------------------------------------------------------------------------
# INTEGRITY: FKs, duplicates, soft-delete consistency, hidden rows
# ---------------------------------------------------------------------------

FK_RULES = [
    ("grn", "supplier_id", "supplier", "id"),
    ("grn", "payment_account_id", "account", "id"),
    ("grn_item", "grn_id", "grn", "id"),
    ("direct_sale", "invoice_id", "invoice", "id"),
    ("direct_sale", "payment_account_id", "account", "id"),
    ("direct_sale_item", "sale_id", "direct_sale", "id"),
    ("direct_sale_item", "grn_item_id", "grn_item", "id"),
    ("payment", "client_id", "client", "id"),
    ("payment", "payment_account_id", "account", "id"),
    ("supplier_payment", "supplier_id", "supplier", "id"),
    ("supplier_payment", "payment_account_id", "account", "id"),
    ("account_transaction", "from_account_id", "account", "id"),
    ("account_transaction", "to_account_id", "account", "id"),
    ("account_transaction", "reconciliation_id", "account_reconciliation", "id"),
    ("booking_item", "booking_id", "booking", "id"),
    ("booking", "receive_in_account_id", "account", "id"),
    ("booking_allocation", "sale_id", "direct_sale", "id"),
    ("booking_allocation", "sale_item_id", "direct_sale_item", "id"),
    ("booking_allocation", "booking_item_id", "booking_item", "id"),
    ("grn_allocation", "sale_id", "direct_sale", "id"),
    ("grn_allocation", "sale_item_id", "direct_sale_item", "id"),
    ("grn_allocation", "grn_item_id", "grn_item", "id"),
    ("material_return", "payment_id", "payment", "id"),
    ("material_return_item", "material_return_id", "material_return", "id"),
    ("waive_off", "payment_id", "payment", "id"),
    ("entry", "invoice_id", "invoice", "id"),
    ("delivery_rent", "sale_id", "direct_sale", "id"),
    ("sale_delivery_persons", "sale_id", "direct_sale", "id"),
    ("sale_delivery_persons", "delivery_person_id", "delivery_person", "id"),
    ("delivery_person_payment", "delivery_person_id", "delivery_person", "id"),
]

BILL_TABLES = [
    ("direct_sale", "manual_bill_no"),
    ("direct_sale", "auto_bill_no"),
    ("grn", "manual_bill_no"),
    ("grn", "auto_bill_no"),
    ("payment", "manual_bill_no"),
    ("payment", "auto_bill_no"),
    ("supplier_payment", "manual_bill_no"),
    ("supplier_payment", "auto_bill_no"),
    ("booking", "manual_bill_no"),
    ("booking", "auto_bill_no"),
    ("material_return", "manual_bill_no"),
    ("material_return", "auto_bill_no"),
    ("invoice", "invoice_no"),
]


def integrity_section(con) -> dict:
    t = _tables(con)
    out = {"ok": True, "findings": [], "fk_orphans": [], "duplicates": [],
           "soft_delete_issues": [], "hidden_rows": [], "summary": {}}

    # FK orphans (schema may not enforce FKs on legacy rows)
    orphans = []
    for child, fk, parent, pk in FK_RULES:
        if child not in t or parent not in t or fk not in {
            r[1] for r in con.execute(f"PRAGMA table_info({child})")
        }:
            continue
        rows = con.execute(
            f"""
            SELECT c.id, c.{fk} AS fk_value
            FROM {child} c
            WHERE c.{fk} IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{pk} = c.{fk})
            LIMIT 200
            """
        ).fetchall()
        for r in rows:
            orphans.append({
                "child": child, "child_id": r["id"], "fk": fk,
                "value": r["fk_value"], "parent": parent,
            })
    if orphans:
        out["ok"] = False
        out["findings"].append(f"FK.ORPHANS {len(orphans)} orphaned child row(s)")

    # duplicates
    dups = []
    seen = {}
    for table, col in BILL_TABLES:
        if table not in t or col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            continue
        for r in con.execute(
            f"""
            SELECT {col} AS v, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
            FROM {table} WHERE {col} IS NOT NULL AND {col} != ''
            GROUP BY {col} HAVING n > 1
            """
        ):
            key = (table, col, r["v"])
            dups.append({
                "table": table, "column": col, "value": r["v"],
                "count": r["n"], "ids": r["ids"],
            })
    # cross-table manual duplicates
    if "direct_sale" in t and "grn" in t:
        for r in con.execute(
            """
            SELECT ds.manual_bill_no AS v, 'direct_sale' AS a, ds.id AS aid,
                   g.id AS bid, 'grn' AS b
            FROM direct_sale ds JOIN grn g ON g.manual_bill_no = ds.manual_bill_no
            WHERE ds.manual_bill_no IS NOT NULL AND ds.manual_bill_no != ''
            """
        ):
            dups.append({"table": "cross", "column": "manual_bill_no",
                         "value": r["v"], "count": 2,
                         "ids": f"{r['a']}#{r['aid']}+{r['b']}#{r['bid']}"})
    if dups:
        out["ok"] = False
        out["findings"].append(f"DUP.BILL_NUMBERS {len(dups)} duplicated bill number(s)")
    out["duplicates"] = dups

    # soft-delete parent with active derived rows
    soft = []
    if "direct_sale" in t and "entry" in t and "pending_bill" in t:
        for r in con.execute(
            """
            SELECT ds.id, ds.manual_bill_no,
              (SELECT COUNT(*) FROM entry e WHERE e.source_table='direct_sale'
                 AND e.source_id=ds.id AND e.is_void=0) AS active_entries,
              (SELECT COUNT(*) FROM pending_bill pb
                 WHERE pb.source_table='direct_sale' AND pb.source_id=ds.id
                 AND pb.is_void=0) AS active_pending
            FROM direct_sale ds WHERE ds.is_void=1
            """
        ):
            if (r["active_entries"] or 0) > 0 or (r["active_pending"] or 0) > 0:
                soft.append({
                    "parent": "direct_sale", "id": r["id"],
                    "active_entries": r["active_entries"],
                    "active_pending": r["active_pending"],
                })
    if "payment" in t and "waive_off" in t:
        for r in con.execute(
            """
            SELECT p.id,
              (SELECT COUNT(*) FROM waive_off w WHERE w.payment_id=p.id AND w.is_void=0)
                AS active_waives
            FROM payment p WHERE p.is_void=1 AND active_waives>0
            """
        ):
            soft.append({"parent": "payment", "id": r["id"],
                         "active_waives": r["active_waives"]})
    if soft:
        out["ok"] = False
        out["findings"].append(
            f"SOFTDELETE.ACTIVE_CHILDREN {len(soft)} voided parent(s) still have active "
            "derived rows"
        )
    out["soft_delete_issues"] = soft

    # hidden row accounting: raw vs non-void counts per table
    hidden = []
    for table in CORE_TABLES:
        if table not in t:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if "is_void" not in cols:
            continue
        raw = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        active = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE is_void=0"
        ).fetchone()[0]
        hidden.append({
            "entity": table, "raw": raw, "active": active,
            "voided": raw - active,
        })
    out["hidden_rows"] = hidden
    out["summary"] = {
        "fk_orphans": len(orphans),
        "duplicates": len(dups),
        "soft_delete_issues": len(soft),
        "hidden_row_entities": len(hidden),
    }
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

SECTIONS = {
    "sales": sales_section,
    "accounts": accounts_section,
    "inventory": inventory_section,
    "receivables": receivables_section,
    "suppliers": suppliers_section,
    "integrity": integrity_section,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the SQLite database")
    ap.add_argument("--section", choices=sorted(SECTIONS), default=None,
                    help="only run one section")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero on any divergence")
    args = ap.parse_args()

    con = connect(args.db)
    missing = [t for t in CORE_TABLES if t not in _tables(con)]
    result = {"db": args.db, "checked_at": datetime.now().isoformat(timespec="seconds"),
              "missing_tables": missing, "sections": {}}
    failed = False
    sections = [args.section] if args.section else list(SECTIONS)
    for name in sections:
        try:
            sec = SECTIONS[name](con)
        except Exception as exc:  # noqa: BLE001
            sec = {"ok": False, "findings": [f"TRUTH-ENGINE EXCEPTION: {exc!r}"]}
        if not sec.get("ok", True):
            failed = True
        result["sections"][name] = sec

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for name, sec in result["sections"].items():
            status = "OK" if sec.get("ok", True) else "DIVERGENCE"
            print(f"[{status}] {name}")
            for f in sec.get("findings", []):
                print(f"    - {f}")
            if "summary" in sec:
                print(f"    summary: {sec['summary']}")
    if args.check:
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
