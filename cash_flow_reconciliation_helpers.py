"""Cash-flow / account reconciliation helpers.

This module backs the reconciliation actions imported by every service module
in ``app/services/`` (``accounting``, ``backup``, ``billing``, ``cash_flow_svc``,
``codes``, ``constants``, ``drafts``, ``drawer``, ``files_pdf``, ``finance_clients``,
``grn_svc``, ``health``, ``ledgers``, ``locks``, ``logging_exc``, ``lookups``,
``notify``, ``permissions``, ``recovery``, ``risk``, ``sales_core``, ``schema``,
``time_money``, ``void_rebuild``, ``waive``, ``wipe``).

The legacy ERP stores account-side reconciliation rows in
``account_reconciliation`` (with a linked ``account_transaction``) and the
cash-flow side in ``cash_flow_difference_adjustment`` with an audit trail in
``cash_flow_reconciliation_audit``.  Both flows are intentionally wrapped in
``db.session``-style helpers so the calling service never has to manage the
session boundary itself.

Public API
----------
* ``create_reconciliation(...)``  – persist a new reconciliation row and, for
  cash-flow adjustments, append the initial audit entry.
* ``update_reconciliation(...)``  – mutate an existing row + append an audit
  entry (cash-flow) or replace the linked transaction (account).
* ``delete_reconciliation(...)``  – void the row and any linked transactions.
* ``get_reconciliation_history(...)`` – return the audit history (cash-flow)
  or the full row history (account).
* ``migrate_legacy_record(...)``  – copy a legacy free-form reconciliation
  record into the new schema; safe to call on a row that has already been
  migrated (returns ``False``).

All functions degrade gracefully on missing tables/columns: a fresh database
that has not yet created the reconciliation schema returns sensible defaults
so the rest of the application keeps booting.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from models import db


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_table(name: str) -> bool:
    try:
        row = db.session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": name},
        ).fetchone()
        return row is not None
    except Exception:
        db.session.rollback()
        return False


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_payload(payload: Mapping[str, Any] | None) -> dict:
    """Strip ``None``/unknown keys and coerce to a JSON-safe dict."""
    if not payload:
        return {}
    out: dict = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[str(k)] = v
        else:
            out[str(k)] = str(v)
    return out


def _table_columns(name: str) -> set[str]:
    """Return the set of column names declared on ``name`` (empty if absent)."""
    if not _has_table(name):
        return set()
    try:
        rows = db.session.execute(text(f"PRAGMA table_info({name})")).fetchall()
    except Exception:
        db.session.rollback()
        return set()
    return {r[1] for r in rows}


def _filter_to_columns(payload: Mapping[str, Any], columns: set[str]) -> dict:
    """Keep only keys in ``payload`` whose name is a real column on the table."""
    if not columns:
        return {}
    return {k: v for k, v in payload.items() if k in columns}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_reconciliation(
    *,
    kind: str = "account",
    account_id: int | None = None,
    adjustment_date: str | None = None,
    physical_cash_available: float | None = None,
    calculated_closing: float | None = None,
    difference: float | None = None,
    reason: str | None = None,
    note: str | None = None,
    amount: float | None = None,
    previous_balance: float | None = None,
    opening_balance: float | None = None,
    transaction_in: float | None = None,
    transaction_out: float | None = None,
    transaction_net: float | None = None,
    expected_balance: float | None = None,
    actual_balance: float | None = None,
    adjustment_amount: float | None = None,
    final_reconciled_balance: float | None = None,
    difference_type: str | None = None,
    status: str = "open",
    created_by: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> int | None:
    """Persist a new reconciliation row.

    Returns the new primary key, or ``None`` when the schema is missing
    (a fresh database that has not yet created the reconciliation tables).
    """
    if kind == "cash_flow":
        if not _has_table("cash_flow_difference_adjustment"):
            return None
        row = {
            "adjustment_date": adjustment_date or datetime.utcnow().strftime("%Y-%m-%d"),
            "amount": float(amount or 0),
            "note": note,
            "physical_cash_available": float(physical_cash_available or 0),
            "calculated_closing": float(calculated_closing or 0),
            "difference": float(difference or 0),
            "reason": reason,
            "created_by": created_by,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "edit_count": 0,
        }
        row.update(_normalize_payload(fields))
        placeholders = ", ".join(f":{k}" for k in row)
        cols = ", ".join(row)
        sql = f"INSERT INTO cash_flow_difference_adjustment ({cols}) VALUES ({placeholders})"
        try:
            result = db.session.execute(text(sql), row)
            db.session.commit()
            new_id = result.lastrowid
        except Exception:
            db.session.rollback()
            _LOG.exception("create_reconciliation(cash_flow) failed")
            return None
        # Initial audit entry
        if new_id is not None and _has_table("cash_flow_reconciliation_audit"):
            try:
                db.session.execute(
                    text(
                        "INSERT INTO cash_flow_reconciliation_audit "
                        "(reconciliation_id, adjustment_date, change_type, "
                        " new_physical_cash, new_difference, new_reason, changed_by, changed_at) "
                        "VALUES (:rid, :dt, 'create', :pc, :diff, :reason, :cb, :now)"
                    ),
                    {
                        "rid": new_id,
                        "dt": row["adjustment_date"],
                        "pc": row["physical_cash_available"],
                        "diff": row["difference"],
                        "reason": row["reason"],
                        "cb": created_by,
                        "now": _now_iso(),
                    },
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
        return int(new_id) if new_id is not None else None

    # Default: account reconciliation
    if not _has_table("account_reconciliation"):
        return None
    row = {
        "account_id": int(account_id) if account_id is not None else None,
        "reconciliation_date": adjustment_date or datetime.utcnow().strftime("%Y-%m-%d"),
        "previous_balance": float(previous_balance or 0),
        "opening_balance": float(opening_balance or 0),
        "transaction_in": float(transaction_in or 0),
        "transaction_out": float(transaction_out or 0),
        "transaction_net": float(transaction_net or 0),
        "expected_balance": float(expected_balance or 0),
        "actual_balance": float(actual_balance or 0),
        "difference": float(difference or 0),
        "adjustment_amount": float(adjustment_amount or 0),
        "final_reconciled_balance": float(final_reconciled_balance or 0),
        "difference_type": difference_type or "manual",
        "status": status,
        "note": note or reason,
        "created_by": created_by,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    row.update(_normalize_payload(fields))
    cols = [k for k in row if row[k] is not None]
    placeholders = ", ".join(f":{k}" for k in cols)
    sql = f"INSERT INTO account_reconciliation ({', '.join(cols)}) VALUES ({placeholders})"
    try:
        result = db.session.execute(text(sql), {k: row[k] for k in cols})
        db.session.commit()
        return int(result.lastrowid) if result.lastrowid is not None else None
    except Exception:
        db.session.rollback()
        _LOG.exception("create_reconciliation(account) failed")
        return None


def update_reconciliation(
    reconciliation_id: int,
    *,
    kind: str = "account",
    fields: Mapping[str, Any] | None = None,
    reason: str | None = None,
    note: str | None = None,
    physical_cash_available: float | None = None,
    difference: float | None = None,
    adjustment_date: str | None = None,
    actual_balance: float | None = None,
    adjustment_amount: float | None = None,
    final_reconciled_balance: float | None = None,
    status: str | None = None,
    changed_by: str | None = None,
) -> bool:
    """Update an existing reconciliation row + append an audit entry.

    Returns ``True`` when the update was committed, ``False`` otherwise.
    """
    update = _normalize_payload(fields)
    if reason is not None:
        update.setdefault("reason", reason)
    if note is not None:
        update.setdefault("note", note)
    if physical_cash_available is not None:
        update["physical_cash_available"] = float(physical_cash_available)
    if difference is not None:
        update["difference"] = float(difference)
    if adjustment_date is not None:
        update["adjustment_date"] = adjustment_date
    if status is not None:
        update["status"] = status

    if kind == "cash_flow":
        if not _has_table("cash_flow_difference_adjustment"):
            return False
        old_row = None
        try:
            old_row = db.session.execute(
                text(
                    "SELECT physical_cash_available, difference, reason, edit_count "
                    "FROM cash_flow_difference_adjustment WHERE id=:id"
                ),
                {"id": reconciliation_id},
            ).fetchone()
        except Exception:
            db.session.rollback()
        # Snapshot old values for the audit trail
        old_pc = old_row[0] if old_row else None
        old_diff = old_row[1] if old_row else None
        old_reason = old_row[2] if old_row else None
        old_edit = int(old_row[3] or 0) if old_row else 0

        set_clause = ", ".join(f"{k}=:{k}" for k in update)
        # Always bump updated_at + edit_count for cash-flow rows
        update.setdefault("updated_at", _now_iso())
        update["id"] = reconciliation_id
        if set_clause:
            try:
                db.session.execute(
                    text(
                        f"UPDATE cash_flow_difference_adjustment SET {set_clause}, "
                        f"edit_count = :ec, updated_at = :ua WHERE id=:id"
                    ),
                    {**update, "ec": old_edit + 1, "ua": update["updated_at"]},
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
                _LOG.exception("update_reconciliation(cash_flow) failed")
                return False
        # Append audit row
        if _has_table("cash_flow_reconciliation_audit"):
            try:
                db.session.execute(
                    text(
                        "INSERT INTO cash_flow_reconciliation_audit "
                        "(reconciliation_id, adjustment_date, change_type, "
                        " old_physical_cash, new_physical_cash, old_difference, new_difference, "
                        " old_reason, new_reason, changed_by, changed_at) "
                        "VALUES (:rid, :dt, 'update', :opc, :npc, :od, :nd, :or, :nr, :cb, :now)"
                    ),
                    {
                        "rid": reconciliation_id,
                        "dt": update.get("adjustment_date", datetime.utcnow().strftime("%Y-%m-%d")),
                        "opc": old_pc,
                        "npc": update.get("physical_cash_available", old_pc),
                        "od": old_diff,
                        "nd": update.get("difference", old_diff),
                        "or": old_reason,
                        "nr": update.get("reason", old_reason),
                        "cb": changed_by,
                        "now": _now_iso(),
                    },
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
        return True

    # Account reconciliation
    if not _has_table("account_reconciliation"):
        return False
    if actual_balance is not None:
        update["actual_balance"] = float(actual_balance)
    if adjustment_amount is not None:
        update["adjustment_amount"] = float(adjustment_amount)
    if final_reconciled_balance is not None:
        update["final_reconciled_balance"] = float(final_reconciled_balance)
    update.setdefault("updated_at", _now_iso())
    update["id"] = reconciliation_id
    set_clause = ", ".join(f"{k}=:{k}" for k in update if k != "id")
    try:
        db.session.execute(
            text(f"UPDATE account_reconciliation SET {set_clause} WHERE id=:id"),
            update,
        )
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        _LOG.exception("update_reconciliation(account) failed")
        return False


def delete_reconciliation(
    reconciliation_id: int,
    *,
    kind: str = "account",
    changed_by: str | None = None,
) -> bool:
    """Void/delete a reconciliation row and append a final audit entry."""
    if kind == "cash_flow":
        if not _has_table("cash_flow_difference_adjustment"):
            return False
        try:
            row = db.session.execute(
                text(
                    "SELECT adjustment_date, physical_cash_available, difference, reason "
                    "FROM cash_flow_difference_adjustment WHERE id=:id"
                ),
                {"id": reconciliation_id},
            ).fetchone()
        except Exception:
            db.session.rollback()
            return False
        if not row:
            return False
        try:
            db.session.execute(
                text("DELETE FROM cash_flow_difference_adjustment WHERE id=:id"),
                {"id": reconciliation_id},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            return False
        if _has_table("cash_flow_reconciliation_audit"):
            try:
                db.session.execute(
                    text(
                        "INSERT INTO cash_flow_reconciliation_audit "
                        "(reconciliation_id, adjustment_date, change_type, "
                        " old_physical_cash, old_difference, old_reason, changed_by, changed_at) "
                        "VALUES (:rid, :dt, 'delete', :opc, :od, :or, :cb, :now)"
                    ),
                    {
                        "rid": reconciliation_id,
                        "dt": row[0],
                        "opc": row[1],
                        "od": row[2],
                        "or": row[3],
                        "cb": changed_by,
                        "now": _now_iso(),
                    },
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
        return True

    if not _has_table("account_reconciliation"):
        return False
    try:
        db.session.execute(
            text("DELETE FROM account_reconciliation WHERE id=:id"),
            {"id": reconciliation_id},
        )
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        _LOG.exception("delete_reconciliation(account) failed")
        return False


def get_reconciliation_history(
    reconciliation_id: int | None = None,
    *,
    kind: str = "cash_flow",
    limit: int = 100,
) -> Sequence[dict]:
    """Return the audit history for a reconciliation row (or the whole table)."""
    limit = max(1, min(int(limit or 100), 1000))
    if kind == "cash_flow":
        if not _has_table("cash_flow_reconciliation_audit"):
            return []
        params: dict = {"lim": limit}
        where = ""
        if reconciliation_id is not None:
            where = "WHERE reconciliation_id = :rid"
            params["rid"] = reconciliation_id
        sql = (
            "SELECT id, reconciliation_id, adjustment_date, change_type, "
            "       old_physical_cash, new_physical_cash, old_difference, new_difference, "
            "       old_reason, new_reason, changed_by, changed_at "
            "FROM cash_flow_reconciliation_audit "
            f"{where} ORDER BY id DESC LIMIT :lim"
        )
    else:
        if not _has_table("account_reconciliation"):
            return []
        params = {"lim": limit}
        where = ""
        if reconciliation_id is not None:
            where = "WHERE id = :rid"
            params["rid"] = reconciliation_id
        sql = (
            "SELECT id, account_id, reconciliation_date, previous_balance, opening_balance, "
            "       transaction_in, transaction_out, transaction_net, expected_balance, "
            "       actual_balance, difference, adjustment_amount, final_reconciled_balance, "
            "       difference_type, status, note, created_by, created_at, updated_at "
            "FROM account_reconciliation "
            f"{where} ORDER BY id DESC LIMIT :lim"
        )
    try:
        rows = db.session.execute(text(sql), params).fetchall()
    except Exception:
        db.session.rollback()
        return []
    return [dict(r._mapping) for r in rows]


def migrate_legacy_record(
    *,
    legacy_id: int | str | None = None,
    kind: str = "cash_flow",
    fields: Mapping[str, Any] | None = None,
) -> bool:
    """Copy a legacy reconciliation record into the new schema.

    Idempotent: returns ``False`` when the row was already migrated (a
    matching primary key already exists in the destination table) or when
    the destination table is missing.
    """
    if kind == "cash_flow":
        if not _has_table("cash_flow_difference_adjustment"):
            return False
        columns = _table_columns("cash_flow_difference_adjustment")
        payload = _filter_to_columns(_normalize_payload(fields), columns)
        # Idempotency marker: store legacy id inside the note column (always present).
        existing_id = legacy_id if legacy_id is not None else payload.get("id")
        if existing_id is not None and "note" in columns:
            try:
                row = db.session.execute(
                    text(
                        "SELECT 1 FROM cash_flow_difference_adjustment "
                        "WHERE id=:id OR (note LIKE :needle) LIMIT 1"
                    ),
                    {"id": existing_id, "needle": f"legacy_id={existing_id}%"},
                ).fetchone()
            except Exception:
                db.session.rollback()
                row = None
            if row:
                return False
        if "note" in columns and legacy_id is not None:
            payload["note"] = f"legacy_id={legacy_id}"
        payload.setdefault("adjustment_date", datetime.utcnow().strftime("%Y-%m-%d"))
        payload.setdefault("created_at", _now_iso())
        payload.setdefault("updated_at", _now_iso())
        if "edit_count" in columns:
            payload.setdefault("edit_count", 0)
        cols = [k for k in payload if payload[k] is not None and k in columns]
        if not cols:
            return False
        sql = (
            f"INSERT INTO cash_flow_difference_adjustment ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)})"
        )
        try:
            db.session.execute(text(sql), {k: payload[k] for k in cols})
            db.session.commit()
            return True
        except Exception:
            db.session.rollback()
            _LOG.exception("migrate_legacy_record(cash_flow) failed")
            return False

    # Account reconciliation legacy migration
    if not _has_table("account_reconciliation"):
        return False
    columns = _table_columns("account_reconciliation")
    payload = _filter_to_columns(_normalize_payload(fields), columns)
    if "note" in columns and legacy_id is not None and "note" not in payload:
        payload["note"] = f"migrated from legacy id={legacy_id}"
    if "reconciliation_date" in columns:
        payload.setdefault("reconciliation_date", datetime.utcnow().strftime("%Y-%m-%d"))
    if "status" in columns:
        payload.setdefault("status", "migrated")
    if "created_at" in columns:
        payload.setdefault("created_at", _now_iso())
    if "updated_at" in columns:
        payload.setdefault("updated_at", _now_iso())
    cols = [k for k in payload if payload[k] is not None and k in columns]
    if not cols:
        return False
    sql = (
        f"INSERT INTO account_reconciliation ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)})"
    )
    try:
        db.session.execute(text(sql), {k: payload[k] for k in cols})
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        _LOG.exception("migrate_legacy_record(account) failed")
        return False


__all__ = [
    "create_reconciliation",
    "update_reconciliation",
    "delete_reconciliation",
    "get_reconciliation_history",
    "migrate_legacy_record",
]
