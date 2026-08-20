"""Shared helpers for the AMS legacy-data migration toolkit.

The toolkit is deliberately *pure pandas + stdlib* (no Flask/ORM import) so it
can be run in any environment that can read the legacy ALLEXPORT workbook.
The one thing it depends on is the shape of that export, which is a literal
dump of the app's tables (sheet name == table name, columns == table columns).

The three deliverable scripts build on this module:

    01_audit_legacy.py          read-only audit of the legacy workbook
    02_build_clean_export.py    produce a purged, import-ready workbook
    03_verify_clean_export.py   prove the clean workbook is leak-free

Purge contract (mirrors the requirements):
  * Every row with ``is_void == 1`` is dropped.
  * ``entry`` rows with ``type='CANCEL'`` or ``transaction_category='Cancel'``
    are dropped even when ``is_void == 0`` (they are cancelled entries).
  * Child rows that reference a purged parent are dropped too (cascade), so
    the migrated database cannot contain orphaned foreign keys.
  * ``booking_allocation`` rows whose ``booking_item_id`` no longer exists at
    all (legacy dangling references) are dropped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# --------------------------------------------------------------------------
# Source / target locations
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_XLSX = REPO_ROOT / "Realdata" / "ALLEXPORT-14-08-2026_05-51PM.xlsx"
MIGRATION_DIR = REPO_ROOT / "instance" / "migration"
META_SHEET = "__AMS_META__"

# --------------------------------------------------------------------------
# Table inventory (sheet names in the legacy export)
# --------------------------------------------------------------------------
# Transactional sheets that carry (or can carry) a legacy ``is_void`` flag.
VOID_AWARE_TABLES: List[str] = [
    "booking",
    "invoice",
    "pending_bill",
    "fbm_cash_drawer_entry",
    "account_transaction",
    "direct_sale",
    "entry",
    "grn",
    "payment",
    "supplier_payment",
    "delivery_rent",
    "material_return",
    "sale_delivery_persons",
    "waive_off",
    "grn_item",
    "booking_allocation",
    "delivery_person_payment",
    "fbm_rental_item",
]

# Master/reference sheets (no is_void semantics of their own).
MASTER_TABLES: List[str] = [
    "account",
    "account_category",
    "audit_log",
    "bill_counter",
    "client",
    "material",
    "material_category",
    "user",
    "delivery_person",
    "supplier",
    "fbm_cash_drawer_category",
    "fbm_rental_item",
    "direct_sale_draft",
    "cash_flow_difference_adjustment",
    "follow_up_reminder",
    "follow_up_contact",
    "booking_item",
    "delivery",
    "delivery_item",
    "direct_sale_item",
    "grn_item",
    "material_return_item",
    "fbm_client",
    "fbm_rental",
    "future_account_audit_log",
    "recon_basket",
    "root_backup_email_history",
    "root_backup_settings",
    "root_recovery_code",
    "schema_version",
    "settings",
    "staff_email",
    "system_lock",
    "tenant_wipe_backup_history",
    "cash_flow_reconciliation_audit",
]

# Cascade purge rules: (child_sheet, child_fk_column, parent_sheet, condition)
# ``condition`` is evaluated against the *child* row when non-None and must be
# a pandas boolean Series over the child frame (e.g. source_table == 'direct_sale').
# It is only applied for the cascade lookup; rows matching the condition whose
# FK points at a purged parent are dropped.
CASCADE_RULES: List[Tuple[str, str, str, Optional[str]]] = [
    ("booking_item", "booking_id", "booking", None),
    ("direct_sale_item", "sale_id", "direct_sale", None),
    ("booking_allocation", "sale_id", "direct_sale", None),
    ("booking_allocation", "sale_item_id", "direct_sale_item", None),
    ("booking_allocation", "booking_item_id", "booking_item", None),
    ("entry", "source_id", "direct_sale", "source_table == 'direct_sale'"),
    ("pending_bill", "source_id", "direct_sale", "source_table == 'direct_sale'"),
    ("pending_bill", "source_id", "booking", "source_table == 'booking'"),
    ("delivery_rent", "sale_id", "direct_sale", None),
    ("sale_delivery_persons", "sale_id", "direct_sale", None),
    ("waive_off", "payment_id", "payment", None),
    ("material_return", "payment_id", "payment", None),
    ("grn_item", "grn_id", "grn", None),
    ("material_return_item", "material_return_id", "material_return", None),
    ("follow_up_reminder", "pending_bill_id", "pending_bill", None),
    ("follow_up_contact", "pending_bill_id", "pending_bill", None),
    ("delivery_person_payment", "sale_id", "direct_sale", None),
    ("delivery_person_payment", "allocation_id", "sale_delivery_persons", None),
    ("delivery_person_payment", "delivery_person_id", "delivery_person", None),
    # Historical FIFO lot links: a sale line pointing at a purged GRN lot must
    # not be kept (present in the legacy export only for grn_item with qty=0,
    # count is zero for the 2026-08-14 export, kept for completeness).
    ("direct_sale_item", "grn_item_id", "grn_item", None),
]

# Child FK pairs that are audited for dangling references on the clean data.
FK_PAIRS: List[Tuple[str, str, str]] = [
    (child, col, parent) for child, col, parent, _ in CASCADE_RULES
] + [
    ("direct_sale", "invoice_id", "invoice"),
    ("entry", "invoice_id", "invoice"),
    ("account_transaction", "from_account_id", "account"),
    ("account_transaction", "to_account_id", "account"),
    ("payment", "payment_account_id", "account"),
    ("supplier_payment", "supplier_id", "supplier"),
    ("supplier_payment", "payment_account_id", "account"),
    ("grn", "supplier_id", "supplier"),
    ("material_return_item", "material_return_id", "material_return"),
    ("follow_up_contact", "reminder_id", "follow_up_reminder"),
]


def read_sheet(xls: pd.ExcelFile, name: str) -> pd.DataFrame:
    """Read one sheet and add an integer ``_void`` column (0 when absent)."""
    df = pd.read_excel(xls, name)
    if "is_void" in df.columns:
        df["_void"] = pd.to_numeric(df["is_void"], errors="coerce").fillna(0).astype(int)
    else:
        df["_void"] = 0
    return df


def _id_set(df: pd.DataFrame, col: str) -> set:
    if col not in df.columns:
        return set()
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    return set(s.astype(int).unique())


def _apply_cancel_entry_rule(df: pd.DataFrame) -> pd.Series:
    """Boolean mask over a DataFrame of entry rows that are cancelled.

    A row is "cancelled" when ``type='CANCEL'`` or
    ``transaction_category='Cancel'`` even if ``is_void`` is 0.
    """
    if "type" not in df.columns and "transaction_category" not in df.columns:
        return pd.Series(False, index=df.index)
    mask = pd.Series(False, index=df.index)
    if "type" in df.columns:
        mask |= df["type"].fillna("").astype(str).str.strip().str.upper().eq("CANCEL")
    if "transaction_category" in df.columns:
        mask |= df["transaction_category"].fillna("").astype(str).str.strip().str.upper().eq("CANCEL")
    return mask


def compute_clean_frames(xls: pd.ExcelFile):
    """Return (kept_frames, purge_report) for every sheet in the workbook.

    ``kept_frames`` maps sheet name -> cleaned DataFrame (original columns,
    without the internal ``_void`` helper column).
    ``purge_report`` maps sheet name -> {total, kept, removed_void,
    removed_cancel, removed_cascade, removed_missing_parent}.

    Purge works on *row labels* so tables with non-integer primary keys
    (e.g. ``audit_log`` UUID ids) are handled identically to integer-id tables.
    """
    sheets = [s for s in xls.sheet_names if s != META_SHEET]
    frames = {s: read_sheet(xls, s) for s in sheets}
    report: Dict[str, dict] = {}
    drop_labels: Dict[str, set] = {}
    missing_labels: Dict[str, set] = {}

    # Pass 1: direct void / cancelled purge.
    for name, df in frames.items():
        drop_mask = pd.Series(False, index=df.index)
        if "_void" in df.columns:
            drop_mask |= df["_void"] == 1
        if name == "entry":
            drop_mask |= _apply_cancel_entry_rule(df)
        drop_labels[name] = set(df.index[drop_mask].tolist())
        report[name] = {
            "total": int(len(df)),
            "kept": int(len(df) - len(drop_labels[name])),
            "removed_void": int((df["_void"] == 1).sum()) if "_void" in df.columns else 0,
            "removed_cancel": int(_apply_cancel_entry_rule(df).sum()),
            "removed_cascade": 0,
            "removed_missing_parent": 0,
        }

    # Pass 2: cascade purge (children of purged parents) plus the special
    # booking_allocation dangling-reference rule.  Counts are de-duplicated
    # per child so overlapping rules are not double-counted in the report.
    for child, col, parent, cond in CASCADE_RULES:
        if child not in frames or col not in frames[child].columns:
            continue
        df = frames[child]
        parent_kept = _kept_ids(frames, parent, drop_labels)
        if parent is None or not parent_kept:
            continue
        all_parent_ids = _id_set(frames.get(parent, pd.DataFrame()), "id")
        purged_parent_ids = all_parent_ids - parent_kept
        child_ids = pd.to_numeric(df[col], errors="coerce")
        cascade_mask = child_ids.isin(purged_parent_ids) & child_ids.notna()
        if cond:
            try:
                cascade_mask &= df.eval(cond)
            except Exception:
                cascade_mask &= pd.Series(False, index=df.index)
        cascade_labels = set(df.index[cascade_mask].tolist())

        if child == "booking_allocation" and col == "booking_item_id":
            # Allocations pointing at booking items that simply do not exist
            # in the legacy data are dropped as well (dangling FKs).
            bi_kept = _kept_ids(frames, "booking_item", drop_labels)
            bi_col = pd.to_numeric(df["booking_item_id"], errors="coerce")
            missing_mask = bi_col.notna() & ~bi_col.astype(int).isin(bi_kept)
            missing_labels[child] = set(df.index[missing_mask].tolist())
            cascade_labels -= missing_labels[child]

        drop_labels[child] |= cascade_labels

    # Build the final kept frames (original columns only).
    kept_frames: Dict[str, pd.DataFrame] = {}
    for name, df in frames.items():
        kept = df[~df.index.isin(drop_labels.get(name, set()) | missing_labels.get(name, set()))]
        kept_frames[name] = kept.drop(columns=["_void"], errors="ignore")

    # Finalise per-table counts (exact, de-duplicated).
    for name, r in report.items():
        r["removed_missing_parent"] = int(len(missing_labels.get(name, set())))
        r["removed_cascade"] = int(
            len(drop_labels.get(name, set()) - missing_labels.get(name, set()))
            - r["removed_void"] - r["removed_cancel"]
        )
        r["removed_cascade"] = max(0, r["removed_cascade"])
        r["kept"] = r["total"] - int(
            len(drop_labels.get(name, set()) | missing_labels.get(name, set()))
        )

    # Sanity: report kept counts must match the frames.
    for name, kf in kept_frames.items():
        assert len(kf) == report[name]["kept"], (name, len(kf), report[name]["kept"])
    return kept_frames, report


def _kept_ids(frames: Dict[str, pd.DataFrame], name: str, drop_labels: Dict[str, set]) -> set:
    df = frames.get(name, pd.DataFrame())
    if "id" not in df.columns:
        return set()
    ids = pd.to_numeric(df["id"], errors="coerce")
    return set(ids[~df.index.isin(drop_labels.get(name, set())) & ids.notna()].astype(int))


def load_workbook(path: Path) -> pd.ExcelFile:
    return pd.ExcelFile(path)


def sheet_names(xls: pd.ExcelFile) -> List[str]:
    return list(xls.sheet_names or [])


def sb_sequences(df: pd.DataFrame, col: str) -> Dict[str, int]:
    """Max SB-<NS>-<seq> number per namespace found in a column."""
    out: Dict[str, int] = {}
    if col not in df.columns:
        return out
    for v in df[col].dropna().astype(str):
        m = re.match(r"^SB\s*-\s*([A-Z][A-Z0-9]{1,7})\s*-\s*(\d+)$", v.strip().upper())
        if m:
            ns, n = m.group(1), int(m.group(2))
            out[ns] = max(out.get(ns, 0), n)
    return out


def money_sum(df: pd.DataFrame, col: str) -> float:
    if col not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())
