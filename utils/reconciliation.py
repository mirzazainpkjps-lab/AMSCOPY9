import os
import shutil
import threading
import time
from datetime import datetime

from sqlalchemy import case, func, text

from models import db, Account, AccountTransaction, DirectSale, DirectSaleItem, Entry, Material


_RECON_LOCK = threading.Lock()


def _pk_now_naive():
    # Keep it simple here: main.py already normalizes PK time elsewhere,
    # but reconciliation is internal and only needs a stable timestamp.
    return datetime.now().replace(microsecond=0)


def _safe_copy_db(db_path: str, backup_dir: str) -> str | None:
    try:
        if not db_path or not os.path.exists(db_path):
            return None
        os.makedirs(backup_dir, exist_ok=True)
        stamp = _pk_now_naive().strftime("%Y%m%d_%H%M%S")
        name = f"recon_backup_{stamp}.db"
        dest = os.path.join(backup_dir, name)
        shutil.copy2(db_path, dest)
        return dest
    except Exception:
        return None


def _cleanup_reconcile_backups(backup_dir: str, *, keep: int = 10) -> int:
    try:
        keep = int(keep or 0)
    except Exception:
        keep = 10
    keep = max(0, keep)
    try:
        if not backup_dir or not os.path.isdir(backup_dir) or keep <= 0:
            return 0
        files = []
        for name in os.listdir(backup_dir):
            if not name.startswith("recon_backup_") or not name.lower().endswith(".db"):
                continue
            path = os.path.join(backup_dir, name)
            try:
                st = os.stat(path)
            except Exception:
                continue
            files.append((st.st_mtime, path))
        files.sort(reverse=True)  # newest first
        removed = 0
        for _, path in files[keep:]:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
        return removed
    except Exception:
        return 0


def reconcile_material_totals(*, tolerance=0.01) -> dict:
    """
    Make Material.total match net stock from Entry rows:
      net = SUM(IN qty) - SUM(OUT qty) over non-void entries.
    """
    net_rows = (
        db.session.query(
            Entry.material.label("material"),
            func.sum(
                case(
                    (func.upper(func.coalesce(Entry.type, "")) == "IN", func.coalesce(Entry.qty, 0)),
                    (func.upper(func.coalesce(Entry.type, "")) == "OUT", -func.coalesce(Entry.qty, 0)),
                    else_=0,
                )
            ).label("net"),
        )
        .filter(Entry.is_void == False)
        .group_by(Entry.material)
        .all()
    )
    net_map = {(r.material or "").strip(): float(r.net or 0) for r in net_rows if (r.material or "").strip()}

    changed = 0
    checked = 0
    for mat in Material.query.all():
        name = (mat.name or "").strip()
        if not name:
            continue
        checked += 1
        expected = float(net_map.get(name, 0.0))
        actual = float(getattr(mat, "total", 0) or 0.0)
        if abs(actual - expected) <= float(tolerance or 0):
            continue
        mat.total = expected
        changed += 1

    return {"materials_checked": checked, "materials_changed": changed}


def reconcile_account_balances(*, tolerance=0.01, create_adjustment=True) -> dict:
    """
    Keep Account.balance consistent with AccountTransaction history.

    Strategy:
    - Compute ledger-based balance per account.
    - If stored balance differs:
        - If create_adjustment=True: create an 'Adjustment' transaction that bridges the gap
          so ledger == stored, then keep stored balance unchanged.
        - Else: overwrite stored Account.balance to ledger value.
    """
    tx_rows = AccountTransaction.query.filter(AccountTransaction.is_void == False).all()
    ledger = {}
    for tx in tx_rows:
        amt = float(getattr(tx, "amount", 0) or 0.0)
        to_id = getattr(tx, "to_account_id", None)
        from_id = getattr(tx, "from_account_id", None)
        if to_id:
            ledger[int(to_id)] = float(ledger.get(int(to_id), 0.0)) + amt
        if from_id:
            ledger[int(from_id)] = float(ledger.get(int(from_id), 0.0)) - amt

    changed = 0
    adjustments = 0
    checked = 0
    for acc in Account.query.all():
        acc_id = int(acc.id)
        checked += 1
        expected = float(ledger.get(acc_id, 0.0))
        stored = float(getattr(acc, "balance", 0) or 0.0)
        diff = stored - expected
        if abs(diff) <= float(tolerance or 0):
            continue

        if not create_adjustment:
            acc.balance = expected
            changed += 1
            continue

        # Bridge the gap with a reconciliation adjustment, so the ledger matches the stored balance.
        marker = f"[RECON:ACCOUNT:{acc_id}]"
        description = "Balance reconciliation (auto)"
        note = f"{marker} Stored balance={stored:.2f}, ledger={expected:.2f}, diff={diff:.2f}"

        if diff > 0:
            tx = AccountTransaction(
                from_account_id=None,
                to_account_id=acc_id,
                amount=abs(diff),
                description=description,
                note=note,
                transaction_type="Adjustment",
                date_posted=_pk_now_naive(),
                is_void=False,
            )
        else:
            tx = AccountTransaction(
                from_account_id=acc_id,
                to_account_id=None,
                amount=abs(diff),
                description=description,
                note=note,
                transaction_type="Adjustment",
                date_posted=_pk_now_naive(),
                is_void=False,
            )
        db.session.add(tx)
        adjustments += 1
        changed += 1

    return {"accounts_checked": checked, "accounts_adjusted": adjustments, "accounts_changed": changed}


def reconcile_direct_sale_item_names(*, tolerance=0.01) -> dict:
    """
    Fix mismatches where DirectSaleItem.product_name doesn't match Entry.material for the same sale bill_ref.

    Only adjusts item.product_name when it can be matched to an entry material by quantity (within tolerance).
    This helps keep reports consistent and avoids stock drift when other code resolves materials by name.
    """
    # Map invoice_id -> invoice_no (best effort, avoids importing Invoice model here).
    invoice_no_by_id = {}
    if db.engine and db.engine.dialect.name == "sqlite":
        try:
            rows = db.session.execute(text("SELECT id, invoice_no FROM invoice WHERE COALESCE(is_void,0)=0")).fetchall()
            for r in rows:
                invoice_no_by_id[int(r[0])] = (r[1] or "").strip()
        except Exception:
            invoice_no_by_id = {}

    fixed_items = 0
    checked_sales = 0
    migrated_bills = 0

    sales = DirectSale.query.filter(DirectSale.is_void == False).all()
    for sale in sales:
        checked_sales += 1

        manual = (getattr(sale, "manual_bill_no", "") or "").strip()
        auto = (getattr(sale, "auto_bill_no", "") or "").strip()
        inv_no = ""
        inv_id = getattr(sale, "invoice_id", None)
        if inv_id:
            inv_no = (invoice_no_by_id.get(int(inv_id)) or "").strip()

        # Expected ref under current logic (invoice takes precedence when present).
        expected_bill_ref = manual or inv_no or auto or f"UNBILLED-{sale.id}"

        # Legacy fix: if sale has invoice_no but entries were posted under auto bill ref, migrate bill_no.
        if inv_no and expected_bill_ref == inv_no and auto:
            has_inv = (
                Entry.query.filter(
                    Entry.is_void == False,
                    func.upper(func.coalesce(Entry.type, "")) == "OUT",
                    func.trim(func.coalesce(Entry.nimbus_no, "")) == "Direct Sale",
                    func.trim(func.coalesce(Entry.bill_no, "")) == inv_no,
                )
                .limit(1)
                .first()
            )
            if not has_inv:
                legacy = Entry.query.filter(
                    Entry.is_void == False,
                    func.upper(func.coalesce(Entry.type, "")) == "OUT",
                    func.trim(func.coalesce(Entry.nimbus_no, "")) == "Direct Sale",
                    func.trim(func.coalesce(Entry.bill_no, "")) == auto,
                ).all()
                if legacy:
                    for e in legacy:
                        e.bill_no = inv_no
                    migrated_bills += 1

        # Work with the (possibly migrated) expected bill ref.
        bill_ref = expected_bill_ref

        entry_rows = (
            Entry.query.filter(
                Entry.is_void == False,
                func.upper(func.coalesce(Entry.type, "")) == "OUT",
                func.trim(func.coalesce(Entry.nimbus_no, "")) == "Direct Sale",
                func.trim(func.coalesce(Entry.bill_no, "")) == bill_ref,
            )
            .order_by(Entry.id.asc())
            .all()
        )
        if not entry_rows:
            continue

        item_rows = (
            DirectSaleItem.query.filter(DirectSaleItem.sale_id == sale.id)
            .order_by(DirectSaleItem.id.asc())
            .all()
        )
        if not item_rows:
            continue

        def _qty(v):
            try:
                return float(v or 0)
            except Exception:
                return 0.0

        unmatched_items = set(int(r.id) for r in item_rows)
        items_by_id = {int(r.id): r for r in item_rows}

        for e in entry_rows:
            eq = _qty(getattr(e, "qty", 0))
            emat = (getattr(e, "material", "") or "").strip()
            if not emat:
                continue

            # Prefer exact name matches for this qty.
            chosen_id = None
            for it_id in list(unmatched_items):
                it = items_by_id[it_id]
                if abs(_qty(getattr(it, "qty", 0)) - eq) <= tolerance and (getattr(it, "product_name", "") or "").strip() == emat:
                    chosen_id = it_id
                    break

            # Otherwise match any item with same qty.
            if chosen_id is None:
                for it_id in list(unmatched_items):
                    it = items_by_id[it_id]
                    if abs(_qty(getattr(it, "qty", 0)) - eq) <= tolerance:
                        chosen_id = it_id
                        break

            if chosen_id is None:
                continue
            unmatched_items.discard(chosen_id)
            it = items_by_id[chosen_id]
            cur_name = (getattr(it, "product_name", "") or "").strip()
            if cur_name != emat and emat:
                it.product_name = emat
                fixed_items += 1

    return {
        "sales_checked": checked_sales,
        "items_renamed": fixed_items,
        "bill_refs_migrated": migrated_bills,
    }


def run_auto_reconcile(app, *, interval_seconds=600, tolerance=0.01, fix=True) -> None:
    """
    Background loop. Uses a global lock so it won't overlap with itself.
    """
    interval_seconds = max(10, int(interval_seconds or 600))
    while True:
        if not fix:
            time.sleep(interval_seconds)
            continue
        if not _RECON_LOCK.acquire(blocking=False):
            time.sleep(interval_seconds)
            continue
        try:
            with app.app_context():
                # Optional DB file backup
                enable_backup = os.environ.get("AUTO_RECONCILE_DB_BACKUP", "1").strip() != "0"
                keep_backups = os.environ.get("AUTO_RECONCILE_BACKUP_KEEP", "10").strip()
                db_uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
                db_path = ""
                if db_uri.startswith("sqlite:///"):
                    db_path = db_uri.replace("sqlite:///", "", 1)
                if enable_backup and db_path:
                    backup_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "instance", "reconcile_backups"))
                    _safe_copy_db(db_path, backup_dir)
                    _cleanup_reconcile_backups(backup_dir, keep=int(keep_backups or 10))

                reconcile_direct_sale_item_names(tolerance=tolerance)
                reconcile_material_totals(tolerance=tolerance)
                reconcile_account_balances(tolerance=tolerance, create_adjustment=True)
                db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
        finally:
            _RECON_LOCK.release()
        time.sleep(interval_seconds)
