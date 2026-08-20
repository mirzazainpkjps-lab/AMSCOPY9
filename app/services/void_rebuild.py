"""Domain service."""
from __future__ import annotations

import os, io, json, re, logging, calendar, threading, time, smtplib, shutil, sqlite3, zipfile
import urllib.request, urllib.error, secrets, importlib
from itertools import zip_longest
from urllib.parse import unquote
from contextlib import redirect_stderr
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlalchemy import func, case, text, or_, and_, exists, not_
from sqlalchemy.orm import selectinload
from types import SimpleNamespace
from flask import current_app, render_template, request, redirect, url_for, flash, jsonify
from flask import send_file, Response, make_response, send_from_directory, abort, session, g
from flask_login import login_user, login_required, logout_user, current_user

from models import *
from utils.audit import audit_log
from utils.reconciliation import run_auto_reconcile
from cash_flow_reconciliation_helpers import (
    create_reconciliation, update_reconciliation, delete_reconciliation,
    get_reconciliation_history, migrate_legacy_record,
)
from app.services import constants as C

AUTO_BILL_NS_DEFAULT = C.AUTO_BILL_NS_DEFAULT
AUTO_BILL_NAMESPACES = C.AUTO_BILL_NAMESPACES
OPEN_KHATA_CODE = C.OPEN_KHATA_CODE
OPEN_KHATA_NAME = C.OPEN_KHATA_NAME
PK_TZ = C.PK_TZ
SALE_CATEGORY_CHOICES = C.SALE_CATEGORY_CHOICES
_SALE_CATEGORY_ALIASES = C._SALE_CATEGORY_ALIASES
DOMAIN_WIPE_REGISTRY = C.DOMAIN_WIPE_REGISTRY
USER_PERMISSION_DEFAULTS = C.USER_PERMISSION_DEFAULTS
PERMISSION_LEGACY_FALLBACKS = C.PERMISSION_LEGACY_FALLBACKS
ENDPOINT_PERMISSION_MAP = C.ENDPOINT_PERMISSION_MAP
EDITABLE_USER_PERMISSION_FIELDS = C.EDITABLE_USER_PERMISSION_FIELDS
basedir = C.basedir
legacy_instance_dir = C.legacy_instance_dir
legacy_db_path = C.legacy_db_path
db_path = C.db_path
_DB_HEALTH_SNAPSHOT_PATH = C._DB_HEALTH_SNAPSHOT_PATH
_AUTO_BACKUP_ENABLED = C._AUTO_BACKUP_ENABLED
_WIPE_BACKUP_ENABLED = C._WIPE_BACKUP_ENABLED
_AUTO_RECONCILE_ENABLED = C._AUTO_RECONCILE_ENABLED
_AUTO_RECONCILE_FIX = C._AUTO_RECONCILE_FIX
_AUTO_RECONCILE_INTERVAL_SEC = C._AUTO_RECONCILE_INTERVAL_SEC
_AUTO_RECONCILE_TOL = C._AUTO_RECONCILE_TOL
_ALLOW_EMPTY_DB = C._ALLOW_EMPTY_DB
_ALLOW_DB_DROP = C._ALLOW_DB_DROP
_DB_HEALTH_DROP_RATIO = C._DB_HEALTH_DROP_RATIO
_DB_HEALTH_DROP_MIN = C._DB_HEALTH_DROP_MIN
_DB_HEALTH_MIN_BYTES = C._DB_HEALTH_MIN_BYTES

from app.services import state

# === explicit service imports ===
from app.services.accounting import (
    _reverse_account_tx_effect,
    _sync_direct_sale_accounting,
    _sync_payment_accounting,
    _sync_supplier_payment_accounting,
)
from app.services.billing import (
    _bill_no_variants,
    normalize_manual_bill,
    parse_bill_kind,
)
from app.services.grn_svc import (
    _find_grn_auto_supplier_payment,
)
from app.services.lookups import (
    get_client_by_input,
)
from app.services.sales_core import (
    _direct_sale_bill_refs,
    _direct_sale_client_identity,
    _direct_sale_default_bill_ref,
    _direct_sale_item_category,
    _entry_client_scope_filter,
    _entry_source_filter,
    _not_void,
    _pending_client_scope_filter,
    _pending_source_filter,
    _stamp_source,
    _sync_delivery_rent_for_sale,
    _void_sale_booking_allocations,
    _void_sale_grn_allocations,
    _delete_sale_grn_allocations,
    _grn_has_locked_lots,
    _refresh_grn_item_locks,
    normalize_sale_category,
)
from app.services.time_money import (
    pk_now,
)
from app.services.waive import (
    _sync_direct_sale_waive_off,
)



# --- from voiding_other.py ---
def _log_bill_repair(action, bill_no, details, severity='INFO'):
    """Log bill repair actions to errorlog.txt with standardized format."""
    try:
        timestamp = pk_now().strftime('%Y-%m-%d %H:%M:%S')
        log_msg = f"[{timestamp}] [Ahmed] BILL-REPAIR [{severity}] action={action} bill_no={bill_no} details={details}"
        logging.getLogger(__name__).info(log_msg)
    except Exception:
        pass


def _detect_orphaned_void_entries(bill_no, exclude_sale_id=None):
    """Find Entry records marked void that belong to non-void DirectSales (orphan detection)."""
    if not bill_no:
        return []
    
    candidates = _bill_no_variants(bill_no)
    if not candidates:
        return []
    
    # Find DirectSales that are NOT voided but match this bill_no
    active_sales = DirectSale.query.filter(
        _not_void(DirectSale),
        or_(
            DirectSale.manual_bill_no.in_(candidates),
            DirectSale.auto_bill_no.in_(candidates)
        )
    ).all()
    
    if not active_sales:
        return []
    
    sale_ids = [s.id for s in active_sales]
    
    # Find Entry records that are VOIDED but source to these non-voided sales
    orphaned_entries = Entry.query.filter(
        Entry.source_table == 'direct_sale',
        Entry.source_id.in_(sale_ids),
        Entry.is_void == True  # These are void but their parent is not
    ).all()
    
    return orphaned_entries


def _auto_repair_orphaned_entries(bill_no):
    """Auto-restore orphaned Entry records to match parent DirectSale active state."""
    orphaned = _detect_orphaned_void_entries(bill_no)
    if not orphaned:
        return 0
    
    repaired_count = 0
    for entry in orphaned:
        try:
            entry.is_void = False
            repaired_count += 1
            _log_bill_repair(
                'AUTO_REPAIR_ORPHAN',
                bill_no,
                f"restored Entry id={entry.id} from void state"
            )
        except Exception as e:
            _log_bill_repair(
                'AUTO_REPAIR_ORPHAN_FAILED',
                bill_no,
                f"Entry id={entry.id} repair failed: {str(e)}",
                severity='ERROR'
            )
    
    if repaired_count > 0:
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()
    
    return repaired_count


def _get_bill_consistency_status(bill_no):
    """Check consistency between DirectSale, Entry records, and duplicate validation."""
    if not bill_no:
        return None
    
    candidates = _bill_no_variants(bill_no)
    if not candidates:
        return None
    
    status = {
        'bill_no': bill_no,
        'direct_sales': [],
        'orphaned_entries': 0,
        'is_consistent': True,
        'issues': []
    }
    
    # Check DirectSale records
    direct_sales = DirectSale.query.filter(
        or_(
            DirectSale.manual_bill_no.in_(candidates),
            DirectSale.auto_bill_no.in_(candidates)
        )
    ).all()
    
    status['direct_sales'] = [
        {'id': s.id, 'is_void': bool(s.is_void), 'client': s.client_name}
        for s in direct_sales
    ]
    
    # Check for orphaned Entry records
    orphaned = _detect_orphaned_void_entries(bill_no)
    status['orphaned_entries'] = len(orphaned)
    
    if orphaned:
        status['is_consistent'] = False
        status['issues'].append(f"Found {len(orphaned)} orphaned void Entry records")
    
    # Check for conflicting void states
    for sale in direct_sales:
        entry_void_state = Entry.query.filter(
            Entry.source_table == 'direct_sale',
            Entry.source_id == sale.id,
            Entry.is_void == False
        ).count() > 0
        
        if bool(sale.is_void) and entry_void_state:
            status['is_consistent'] = False
            status['issues'].append(f"DirectSale {sale.id} is voided but has active Entry records")
    
    return status


def _material_return_bill_refs(ret):
    if not ret:
        return []
    refs = [f"RTN-{ret.id}"]
    if ret.manual_bill_no:
        refs.append(ret.manual_bill_no)
    if ret.auto_bill_no:
        refs.append(ret.auto_bill_no)
    return [r for r in dict.fromkeys(refs) if r]


def _set_material_return_void_state(ret, is_void):
    if not ret:
        return False
    target = bool(is_void)
    if bool(ret.is_void) == target:
        return False

    ret.is_void = target
    refs = _material_return_bill_refs(ret)
    entries = Entry.query.filter(
        Entry.bill_no.in_(refs),
        Entry.nimbus_no == 'Material Return'
    ).all()
    for e in entries:
        e.is_void = target
        mat = Material.query.filter_by(name=e.material).first()
        if not mat:
            continue
        qty = float(e.qty or 0)
        if target:
            mat.total = float(mat.total or 0) - qty
        else:
            mat.total = float(mat.total or 0) + qty

    pay = db.session.get(Payment, ret.payment_id) if ret.payment_id else None
    if pay:
        _set_payment_void_state(pay, target)
    return True


def _set_grn_void_state(grn_obj, is_void):
    if not grn_obj:
        return False
    target = bool(is_void)
    if bool(getattr(grn_obj, 'is_void', False)) == target:
        return False

    grn_obj.is_void = target

    # Void/unvoid inventory entry rows for this GRN (created with auto_bill_no)
    entries = Entry.query.filter(
        Entry.auto_bill_no == grn_obj.auto_bill_no,
        Entry.type == 'IN'
    ).all()
    for e in entries:
        e.is_void = target

    # Apply/reverse stock based on GRN items (do not hard-void items here; preserve edit history).
    for it in (grn_obj.items or []):
        if bool(getattr(it, 'is_void', False)):
            continue
        mat = Material.query.filter_by(name=it.mat_name).first()
        if not mat:
            continue
        qty = float(getattr(it, 'qty', 0) or 0)
        if target:
            mat.total = float(mat.total or 0) - qty
        else:
            mat.total = float(mat.total or 0) + qty

    auto_pay = _find_grn_auto_supplier_payment(grn_obj)
    if auto_pay:
        auto_pay.is_void = target
        _sync_supplier_payment_accounting(auto_pay)

    return True


def _booking_bill_refs(booking):
    if not booking:
        return []
    refs = [f"BK-{booking.id}"]
    if booking.manual_bill_no:
        refs.append(booking.manual_bill_no)
    if booking.auto_bill_no:
        refs.append(booking.auto_bill_no)
    return [r for r in dict.fromkeys(refs) if r]


def _payment_receipt_refs(payment):
    manual_ref = (getattr(payment, 'manual_bill_no', None) or '').strip()
    if not manual_ref:
        return []
    return _bill_no_variants(manual_ref)


def _set_payment_receipt_pending_bill_void_state(payment, is_void):
    refs = _payment_receipt_refs(payment)
    if not refs:
        return 0

    reason_filter = func.lower(func.coalesce(PendingBill.reason, '')).like('payment received%')
    bill_filter = or_(*[PendingBill.bill_no.ilike(r) for r in refs])
    client_obj = get_client_by_input(payment.client_name or '')
    if client_obj:
        client_filter = or_(
            PendingBill.client_code == client_obj.code,
            func.lower(func.coalesce(PendingBill.client_name, '')) == client_obj.name.lower(),
            func.coalesce(PendingBill.client_code, '') == ''
        )
    else:
        client_filter = func.lower(func.coalesce(PendingBill.client_name, '')) == (payment.client_name or '').strip().lower()

    q = PendingBill.query.filter(reason_filter, bill_filter, client_filter)
    if is_void:
        q = q.filter(PendingBill.is_void == False)
    rows = q.all()
    for pb in rows:
        pb.is_void = bool(is_void)
    return len(rows)


def _set_entry_void_state(entry, is_void):
    if not entry:
        return False
    target = bool(is_void)
    if entry.is_void == target:
        return False

    mat = Material.query.filter_by(name=entry.material).first()
    if mat:
        qty = float(entry.qty or 0)
        if target:
            if entry.type == 'IN':
                mat.total = (mat.total or 0) - qty
            elif entry.type == 'OUT':
                mat.total = (mat.total or 0) + qty
        else:
            if entry.type == 'IN':
                mat.total = (mat.total or 0) + qty
            elif entry.type == 'OUT':
                mat.total = (mat.total or 0) - qty

    entry.is_void = target
    return True


def _set_booking_void_state(booking, is_void):
    if not booking:
        return False
    target = bool(is_void)
    if booking.is_void == target:
        return False
    refs = _booking_bill_refs(booking)
    booking.is_void = target
    _sync_booking_pending_bill(booking, extra_void_refs=refs)
    # Keep booking-cancel audit rows in the same lifecycle as their parent booking.
    if refs:
        Entry.query.filter(
            Entry.type == 'CANCEL',
            Entry.bill_no.in_(refs)
        ).update({'is_void': target}, synchronize_session=False)
    return True


def hard_delete_transaction(kind, obj_id):
    """Reverse effects then permanently delete the source row (no void leftovers)."""
    kind = (kind or '').strip()
    deleted = False
    if kind == 'Booking':
        booking = db.session.get(Booking, obj_id)
        if not booking:
            return False
        _set_booking_void_state(booking, True)
        marker = f'[SRC:Booking:{booking.id}]'
        for tx in AccountTransaction.query.filter(AccountTransaction.note.ilike(f'%{marker}%')).all():
            if not tx.is_void:
                _reverse_account_tx_effect(tx)
            db.session.delete(tx)
        for pay in Payment.query.filter(Payment.note.ilike(f'%{marker}%')).all():
            # Retain generated payment identity/audit history while reversing it.
            _set_payment_void_state(pay, True)
        BookingAllocation.query.filter(
            BookingAllocation.booking_item_id.in_(
                db.session.query(BookingItem.id).filter(BookingItem.booking_id == booking.id)
            )
        ).delete(synchronize_session=False)
        PendingBill.query.filter(
            PendingBill.source_table == 'booking',
            PendingBill.source_id == booking.id
        ).delete(synchronize_session=False)
        db.session.delete(booking)
        deleted = True
    elif kind == 'Payment':
        # Financial payments are never hard-deleted.  Preserve the stable source
        # identity, linked (voided) ledger entries, waive-off rows and audit
        # references while reversing their active effects.
        pay = db.session.get(Payment, obj_id)
        if not pay:
            return False
        _set_payment_void_state(pay, True)
        deleted = True
    elif kind == 'DirectSale':
        sale = db.session.get(DirectSale, obj_id)
        if not sale:
            return False
        if not sale.is_void:
            _atomic_void_direct_sale_with_tracking(sale) or _set_direct_sale_void_state(sale, True)
        Entry.query.filter(Entry.source_table == 'direct_sale', Entry.source_id == sale.id).delete(synchronize_session=False)
        DeliveryRent.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
        SaleDeliveryPerson.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
        BookingAllocation.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
        _delete_sale_grn_allocations(sale)
        PendingBill.query.filter(PendingBill.source_table == 'direct_sale', PendingBill.source_id == sale.id).delete(synchronize_session=False)

        # A credit sale may own an Invoice row.  Hard-deleting only the sale
        # leaves an active orphan invoice that still appears in bill searches
        # and consistency reports.  Remove the invoice when no other sale
        # references it; keep it intact if a legacy database shares it.
        invoice_id = getattr(sale, 'invoice_id', None)
        if invoice_id:
            other_invoice_sales = DirectSale.query.filter(
                DirectSale.invoice_id == invoice_id,
                DirectSale.id != sale.id,
                _not_void(DirectSale)
            ).count()
            if other_invoice_sales == 0:
                invoice = db.session.get(Invoice, invoice_id)
                if invoice:
                    db.session.delete(invoice)

        marker = f'[SRC:DirectSale:{sale.id}]'
        for tx in AccountTransaction.query.filter(AccountTransaction.note.ilike(f'%{marker}%')).all():
            if not tx.is_void:
                _reverse_account_tx_effect(tx)
            db.session.delete(tx)
        db.session.delete(sale)
        deleted = True
    elif kind == 'MaterialReturn':
        ret = db.session.get(MaterialReturn, obj_id)
        if not ret:
            return False
        _set_material_return_void_state(ret, True)
        refs = _material_return_bill_refs(ret)
        if refs:
            Entry.query.filter(Entry.bill_no.in_(refs), Entry.nimbus_no == 'Material Return').delete(synchronize_session=False)
        db.session.delete(ret)
        deleted = True
    elif kind == 'Entry':
        entry = db.session.get(Entry, obj_id)
        if not entry:
            return False
        _set_entry_void_state(entry, True)
        db.session.delete(entry)
        deleted = True
    elif kind == 'GRN':
        grn = db.session.get(GRN, obj_id)
        if not grn:
            return False
        if _grn_has_locked_lots(grn):
            raise ValueError('Cannot delete GRN: one or more lots are locked by cash/credit sales. Delete those sales first.')
        _set_grn_void_state(grn, True)
        db.session.delete(grn)
        deleted = True
    elif kind == 'AccountTransaction':
        tx = db.session.get(AccountTransaction, obj_id)
        if not tx:
            return False
        if not tx.is_void:
            _reverse_account_tx_effect(tx)
        db.session.delete(tx)
        deleted = True
    if deleted:
        _rebuild_material_totals()
    return deleted


def _set_payment_void_state(payment, is_void):
    if not payment:
        return False
    target = bool(is_void)
    if payment.is_void == target:
        return False
    payment.is_void = target
    _set_payment_receipt_pending_bill_void_state(payment, is_void=target)
    WaiveOff.query.filter_by(payment_id=payment.id).update({'is_void': target}, synchronize_session=False)
    _sync_payment_accounting(payment)
    return True



# --- from voiding_sales.py ---
def _track_entry_ids_for_sale(sale_id):
    """Get list of Entry IDs associated with a DirectSale before modifications."""
    if not sale_id:
        return []
    
    entries = Entry.query.filter(
        Entry.source_table == 'direct_sale',
        Entry.source_id == sale_id
    ).with_entities(Entry.id).all()
    
    return [e[0] for e in entries if e and e[0]]


def _atomic_void_direct_sale_with_tracking(sale):
    """
    Atomically void DirectSale and ALL related Entry records together.
    Ensures transactional consistency - no partial voids.
    """
    if not sale or sale.is_void:
        return False
    
    bill_refs = _direct_sale_bill_refs(sale)
    old_client_code, old_client_name = _direct_sale_client_identity(sale)
    entry_ids_to_void = _track_entry_ids_for_sale(sale.id)
    
    try:
        # Mark DirectSale as void
        sale.is_void = True
        
        # Explicitly void ALL tracked Entry records
        if entry_ids_to_void:
            Entry.query.filter(Entry.id.in_(entry_ids_to_void)).update(
                {'is_void': True},
                synchronize_session=False
            )
        
        # Void related records
        DeliveryRent.query.filter_by(sale_id=sale.id).update({'is_void': True}, synchronize_session=False)
        SaleDeliveryPerson.query.filter_by(sale_id=sale.id).update({'is_void': True}, synchronize_session=False)
        
        # Rebuild effects to update ledger/pending
        rebuild_direct_sale_effects(
            sale,
            old_refs=bill_refs,
            old_client_code=old_client_code,
            old_client_name=old_client_name,
            rebuild_stock=True
        )
        
        _log_bill_repair(
            'VOID_SALE',
            _direct_sale_default_bill_ref(sale),
            f"sale_id={sale.id} entry_ids={entry_ids_to_void} count={len(entry_ids_to_void)}"
        )
        
        return True
    except Exception as e:
        _log_bill_repair(
            'VOID_SALE_FAILED',
            _direct_sale_default_bill_ref(sale),
            f"sale_id={sale.id} error={str(e)}",
            severity='ERROR'
        )
        return False


def _atomic_restore_direct_sale_with_tracking(sale):
    """
    Atomically restore DirectSale and ALL related Entry records together.
    Restores Entry records that were explicitly voided during void operation.
    """
    if not sale or not sale.is_void:
        return False
    
    bill_ref = _direct_sale_default_bill_ref(sale)
    old_client_code, old_client_name = _direct_sale_client_identity(sale)
    old_refs = _direct_sale_bill_refs(sale)
    
    try:
        # Mark DirectSale as NOT voided
        sale.is_void = False
        
        # Find and restore all Entry records associated with this sale
        sale_entries = Entry.query.filter(
            Entry.source_table == 'direct_sale',
            Entry.source_id == sale.id,
            Entry.is_void == True  # Restore only the ones that were voided
        ).all()
        
        for entry in sale_entries:
            entry.is_void = False
        
        # Restore related records
        DeliveryRent.query.filter_by(sale_id=sale.id).update({'is_void': False}, synchronize_session=False)
        SaleDeliveryPerson.query.filter_by(sale_id=sale.id).update({'is_void': False}, synchronize_session=False)
        
        # Rebuild effects to update ledger/pending
        rebuild_direct_sale_effects(
            sale,
            old_refs=old_refs,
            old_client_code=old_client_code,
            old_client_name=old_client_name,
            rebuild_stock=True
        )
        
        _log_bill_repair(
            'RESTORE_SALE',
            bill_ref,
            f"sale_id={sale.id} restored {len(sale_entries)} Entry records"
        )
        
        return True
    except Exception as e:
        _log_bill_repair(
            'RESTORE_SALE_FAILED',
            bill_ref,
            f"sale_id={sale.id} error={str(e)}",
            severity='ERROR'
        )
        return False


def _void_direct_sale_entries_and_restore_stock(sale, refs=None, client_code=None, client_name=None):
    refs = refs or _direct_sale_bill_refs(sale)
    if client_code is None and client_name is None:
        client_code, client_name = _direct_sale_client_identity(sale)
    q = Entry.query.filter(Entry.bill_no.in_(refs), Entry.nimbus_no == 'Direct Sale')
    client_filter = _entry_client_scope_filter(client_code, client_name)
    if client_filter is not None:
        q = q.filter(client_filter)
    entries = q.all()
    for e in entries:
        if e.is_void:
            continue
        e.is_void = True
        stock_name = e.booked_material if (e.is_alternate and e.booked_material) else e.material
        mat = Material.query.filter_by(name=stock_name).first()
        if mat:
            if e.type == 'OUT':
                mat.total = (mat.total or 0) + (e.qty or 0)
            elif e.type == 'IN':
                mat.total = (mat.total or 0) - (e.qty or 0)


def _unvoid_direct_sale_entries_and_apply_stock(sale, refs=None, client_code=None, client_name=None):
    refs = refs or _direct_sale_bill_refs(sale)
    if client_code is None and client_name is None:
        client_code, client_name = _direct_sale_client_identity(sale)
    q = Entry.query.filter(Entry.bill_no.in_(refs), Entry.nimbus_no == 'Direct Sale')
    client_filter = _entry_client_scope_filter(client_code, client_name)
    if client_filter is not None:
        q = q.filter(client_filter)
    entries = q.all()
    for e in entries:
        if not e.is_void:
            continue
        stock_name = e.booked_material if (e.is_alternate and e.booked_material) else e.material
        mat = Material.query.filter_by(name=stock_name).first()
        if mat:
            if e.type == 'OUT':
                mat.total = (mat.total or 0) - (e.qty or 0)
            elif e.type == 'IN':
                mat.total = (mat.total or 0) + (e.qty or 0)
        e.is_void = False


def _set_direct_sale_void_state(sale, is_void):
    if not sale:
        return False
    target = bool(is_void)
    if sale.is_void == target:
        return False

    old_refs = _direct_sale_bill_refs(sale)
    old_client_code, old_client_name = _direct_sale_client_identity(sale)
    sale.is_void = target
    _void_sale_booking_allocations(sale, target)
    _void_sale_grn_allocations(sale, target)
    rebuild_direct_sale_effects(
        sale,
        old_refs=old_refs,
        old_client_code=old_client_code,
        old_client_name=old_client_name,
        rebuild_stock=True
    )
    DeliveryRent.query.filter_by(sale_id=sale.id).update({'is_void': target}, synchronize_session=False)
    SaleDeliveryPerson.query.filter_by(sale_id=sale.id).update({'is_void': target}, synchronize_session=False)
    return True


def _active_direct_sale_items_generate_entries(sale, refs=None, client_code=None, client_name=None):
    """Regenerate material movement rows for one active sale from DirectSaleItem source rows."""
    if not sale or sale.is_void:
        return 0

    category = normalize_sale_category(sale.category)
    amount = float(sale.amount or 0)
    # A zeroed cash/credit sale is a void-equivalent for derived material/pending effects.
    if amount <= 0 and category not in ['Booking Delivery', 'Mixed Transaction']:
        return 0

    bill_ref = _direct_sale_default_bill_ref(sale)
    now = sale.date_posted or pk_now()
    client_obj = get_client_by_input(sale.client_name or '')
    sale_client_code = client_code if client_code is not None else (client_obj.code if client_obj else (OPEN_KHATA_CODE if category == 'Open Khata' else None))
    sale_client_name = client_name or (client_obj.name if client_obj else sale.client_name)

    old_entries = Entry.query.filter(
        _entry_source_filter(
            'sales',
            sale.id,
            refs=refs or _direct_sale_bill_refs(sale),
            nimbus_no='Direct Sale',
            client_code=sale_client_code,
            client_name=sale_client_name
        )
    ).order_by(Entry.id.asc()).all()
    old_alt_candidates = []
    for e in old_entries:
        bm = (e.booked_material or '').strip()
        if bm:
            old_alt_candidates.append({
                'material': (e.material or '').strip(),
                'qty': float(e.qty or 0),
                'booked_material': bm,
                'used': False
            })

    def _take_old_booked_material(delivered_material, qty_val):
        delivered = (delivered_material or '').strip()
        q = float(qty_val or 0)
        for row in old_alt_candidates:
            if row['used']:
                continue
            if row['material'] == delivered and abs(float(row['qty'] or 0) - q) <= 0.0001:
                row['used'] = True
                return row['booked_material']
        return None

    created = 0
    for item in sale.items or []:
        qty = float(item.qty or 0)
        if qty <= 0:
            continue
        price = float(item.price_at_time or 0)
        item_category = _direct_sale_item_category(category, price)
        if item_category != 'Booking Delivery' and price <= 0:
            continue
        inferred_booked_material = None
        if item_category == 'Booking Delivery':
            inferred_booked_material = _take_old_booked_material(item.product_name, qty)
        is_alt = bool(inferred_booked_material and inferred_booked_material != item.product_name)
        row = Entry(
            date=now.strftime('%Y-%m-%d'),
            time=now.strftime('%H:%M:%S'),
            type='OUT',
            material=item.product_name,
            booked_material=(inferred_booked_material if is_alt else None),
            client=sale_client_name,
            client_code=sale_client_code,
            qty=qty,
            bill_no=bill_ref,
            nimbus_no='Direct Sale',
            created_by=(current_user.username if current_user and current_user.is_authenticated else None),
            client_category=item_category,
            transaction_category=('Unbilled' if category == 'Cash' else 'Billed'),
            driver_name=sale.driver_name,
            note=sale.note,
            is_alternate=is_alt,
            is_void=False
        )
        _stamp_source(row, 'sales', 'direct_sale', sale.id, bill_ref, item_category)
        db.session.add(row)
        created += 1
    return created



# --- from rebuild_pending.py ---
def _sync_booking_pending_bill(booking, primary_material='', extra_void_refs=None):
    if not booking:
        return None
    client_obj = get_client_by_input(booking.client_name or '')
    client_code = client_obj.code if client_obj else None
    client_name = client_obj.name if client_obj else booking.client_name
    refs = list(dict.fromkeys(_booking_bill_refs(booking) + [r for r in (extra_void_refs or []) if r]))
    bill_ref = booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
    reason_prefix = 'Booking'

    stale_rows = PendingBill.query.filter(
        _pending_source_filter(
            'booking',
            booking.id,
            refs=refs,
            reason_prefix=reason_prefix,
            client_code=client_code,
            client_name=client_name
        )
    ).order_by(PendingBill.id.desc()).all()

    pending_amount = max(0.0, float(booking.amount or 0) - float(booking.discount or 0) - float(booking.paid_amount or 0))
    reusable = None
    for pb in stale_rows:
        same_current = (
            not pb.is_void and
            (pb.bill_no or '').strip() == bill_ref and
            pending_amount > 0 and
            not booking.is_void
        )
        if same_current and reusable is None:
            reusable = pb
            continue
        pb.is_void = True
        _stamp_source(pb, 'booking', 'booking', booking.id, bill_ref, 'Booking')

    if booking.is_void:
        return None

    if pending_amount <= 0 or not client_code:
        return None

    reason = f"Booking: {primary_material}".strip()
    if reason.endswith(':'):
        reason = reason[:-1]
    pb = reusable or PendingBill(
        created_at=(booking.date_posted or pk_now()).strftime('%Y-%m-%d %H:%M'),
        created_by=(current_user.username if current_user and current_user.is_authenticated else None),
        is_void=False
    )
    pb.client_code = client_code
    pb.client_name = client_name
    pb.bill_no = bill_ref
    pb.amount = pending_amount
    pb.reason = reason
    pb.is_manual = bool(booking.manual_bill_no)
    pb.bill_kind = parse_bill_kind(bill_ref)
    pb.note = booking.note
    pb.is_paid = False
    _stamp_source(pb, 'booking', 'booking', booking.id, bill_ref, 'Booking')
    if reusable is None:
        db.session.add(pb)
    return pb


def _sync_direct_sale_pending_bill(sale, primary_material='', extra_void_refs=None):
    category = normalize_sale_category(sale.category)
    sale.category = category
    discount = float(getattr(sale, 'discount', 0) or 0)
    pending_amount = max(0.0, float(sale.amount or 0) - discount - float(sale.paid_amount or 0))

    client_obj = get_client_by_input((sale.client_name or '').strip())
    client_code = client_obj.code if client_obj else None
    client_name = client_obj.name if client_obj else sale.client_name

    if category == 'Open Khata':
        client_code = OPEN_KHATA_CODE
        if not client_name:
            client_name = OPEN_KHATA_NAME

    refs = set(_direct_sale_bill_refs(sale))
    bill_ref = _direct_sale_default_bill_ref(sale)
    stale_refs = {r for r in (extra_void_refs or []) if r and r != bill_ref}
    source_filter = _pending_source_filter(
        'sales',
        sale.id,
        refs=list(refs.union(stale_refs)),
        reason_prefix='Direct Sale',
        client_code=client_code,
        client_name=client_name
    )
    if stale_refs:
        stale_q = PendingBill.query.filter(
            PendingBill.is_void == False,
            PendingBill.bill_no.in_(list(stale_refs)),
            func.lower(func.coalesce(PendingBill.reason, '')).like('direct sale%')
        )
        client_filter = _pending_client_scope_filter(client_code, client_name)
        if client_filter is not None:
            stale_q = stale_q.filter(client_filter)
        stale_q.update({'is_void': True}, synchronize_session=False)

    should_track = (
        (not sale.is_void) and (
            pending_amount > 0 or
            (category in ['Cash', 'Mixed Transaction', 'Credit Customer', 'Open Khata'] and float(sale.amount or 0) > 0)
        )
    )
    if not should_track:
        # If this sale no longer generates a tracked pending bill, void any existing direct-sale pending rows.
        void_q = PendingBill.query.filter(
            source_filter
        )
        void_q.update({'is_void': True}, synchronize_session=False)
        return

    reason = f"Direct Sale ({category}): {primary_material}".strip()
    if reason.endswith(':'):
        reason = reason[:-1]
    is_paid_status = (pending_amount <= 0 and float(sale.amount or 0) > 0)

    active_q = PendingBill.query.filter(
        source_filter,
        PendingBill.is_void == False,
    )
    active_rows = active_q.order_by(PendingBill.id.desc()).all()
    existing_pb = active_rows[0] if active_rows else None

    if existing_pb:
        for duplicate_pb in active_rows[1:]:
            duplicate_pb.is_void = True
        existing_pb.client_name = client_name
        existing_pb.client_code = client_code
        existing_pb.amount = pending_amount
        existing_pb.reason = reason
        existing_pb.is_cash = (category == 'Cash')
        existing_pb.is_manual = bool(sale.manual_bill_no)
        existing_pb.bill_kind = parse_bill_kind(bill_ref)
        existing_pb.is_paid = is_paid_status
        existing_pb.note = sale.note
        _stamp_source(existing_pb, 'sales', 'direct_sale', sale.id, bill_ref, category)
    else:
        pb = PendingBill(
            client_code=client_code,
            client_name=client_name,
            bill_no=bill_ref,
            bill_kind=parse_bill_kind(bill_ref),
            amount=pending_amount,
            reason=reason,
            is_cash=(category == 'Cash'),
            is_manual=bool(sale.manual_bill_no),
            is_paid=is_paid_status,
            created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
            created_by=(current_user.username if current_user and current_user.is_authenticated else None),
            note=sale.note
        )
        _stamp_source(pb, 'sales', 'direct_sale', sale.id, bill_ref, category)
        db.session.add(pb)


def _rebuild_direct_sale_pending_bills():
    """Rebuild all active direct sale pending bill rows from current non-void sales."""
    PendingBill.query.filter(
        PendingBill.is_void == False,
        func.lower(func.coalesce(PendingBill.reason, '')).like('direct sale%')
    ).update({'is_void': True}, synchronize_session=False)

    for sale in DirectSale.query.filter_by(is_void=False).all():
        _sync_direct_sale_pending_bill(sale)


def _apply_settlement_to_pending_bills_for_client(client, paid_amount, discount_amount=0):
    if not client:
        return 0
    remaining = max(0.0, float(paid_amount or 0)) + max(0.0, float(discount_amount or 0))
    if remaining <= 0:
        return 0
    client_name_norm = (client.name or '').strip().lower()
    filters = [func.lower(func.trim(func.coalesce(PendingBill.client_name, ''))) == client_name_norm]
    if client.code:
        filters.append(func.lower(func.trim(func.coalesce(PendingBill.client_code, ''))) == client.code.strip().lower())
    rows = PendingBill.query.filter(
        PendingBill.is_void == False,
        PendingBill.is_paid == False,
        PendingBill.amount > 0,
        or_(*filters)
    ).order_by(PendingBill.created_at.asc(), PendingBill.id.asc()).all()
    touched = 0
    for pb in rows:
        if remaining <= 0:
            break
        amount = float(pb.amount or 0)
        if amount <= 0:
            pb.is_paid = True
            continue
        settle = min(amount, remaining)
        pb.amount = max(0.0, amount - settle)
        pb.client_name = client.name
        pb.client_code = client.code
        pb.is_paid = pb.amount <= 0.00001
        remaining -= settle
        touched += 1
    return touched


def rebuild_pending_bills(client_id=None):
    """Rebuild derived booking/direct-sale pending rows, then replay active payments."""
    if client_id:
        client = db.session.get(Client, client_id)
        if not client:
            return {'pending_voided': 0, 'pending_created': 0, 'payments_replayed': 0}
        client_name_norm = (client.name or '').strip().lower()
        sales = DirectSale.query.filter(func.lower(func.trim(DirectSale.client_name)) == client_name_norm).all()
        bookings = Booking.query.filter(func.lower(func.trim(Booking.client_name)) == client_name_norm).all()
        payments = Payment.query.filter(
            or_(Payment.client_id == client.id,
                and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm)),
            Payment.is_void == False,
        ).order_by(Payment.date_posted.asc(), Payment.id.asc()).all()
    else:
        sales = DirectSale.query.all()
        bookings = Booking.query.all()
        payments = Payment.query.filter(Payment.is_void == False).order_by(Payment.date_posted.asc(), Payment.id.asc()).all()

    before_active = PendingBill.query.filter(PendingBill.is_void == False).count()
    for sale in sales:
        _sync_direct_sale_pending_bill(sale, (sale.items[0].product_name if sale.items else ''), extra_void_refs=_direct_sale_bill_refs(sale))
    for booking in bookings:
        _sync_booking_pending_bill(booking, (booking.items[0].material_name if booking.items else ''), extra_void_refs=_booking_bill_refs(booking))

    replayed = 0
    for p in payments:
        client = db.session.get(Client, p.client_id) if getattr(p, 'client_id', None) else get_client_by_input(p.client_name or '')
        if not client:
            continue
        replayed += _apply_settlement_to_pending_bills_for_client(
            client,
            float(p.amount or 0),
            float(getattr(p, 'discount', 0) or 0)
        )
    after_active = PendingBill.query.filter(PendingBill.is_void == False).count()
    return {
        'pending_active_before': before_active,
        'pending_active_after': after_active,
        'payments_replayed': replayed
    }



# --- from rebuild_core.py ---
def rebuild_direct_sale_effects(sale, *, old_refs=None, old_client_code=None, old_client_name=None, rebuild_stock=True):
    """Void stale direct-sale derived rows, rebuild valid rows, refresh pending and stock."""
    if not sale:
        return {'entries_voided': 0, 'entries_created': 0}
    refs = list(dict.fromkeys((old_refs or []) + _direct_sale_bill_refs(sale)))
    client_code, client_name = _direct_sale_client_identity(sale)
    scope_code = old_client_code if old_client_code is not None else client_code
    scope_name = old_client_name if old_client_name is not None else client_name

    stale_entries = Entry.query.filter(
        _entry_source_filter(
            'sales',
            sale.id,
            refs=refs,
            nimbus_no='Direct Sale',
            client_code=scope_code,
            client_name=scope_name
        ),
        Entry.is_void == False
    ).order_by(Entry.id.asc()).all()

    category = normalize_sale_category(sale.category)
    active_source = bool(
        sale and
        (not sale.is_void) and
        (float(sale.amount or 0) > 0 or category in ['Booking Delivery', 'Mixed Transaction'])
    )

    def _entry_sig(row):
        return (
            (row.material or '').strip(),
            round(float(row.qty or 0), 6),
            _direct_sale_item_category(category, getattr(row, 'price_at_time', 0) if hasattr(row, 'price_at_time') else 0)
        )

    desired = []
    if active_source:
        for item in sale.items or []:
            qty = float(item.qty or 0)
            if qty <= 0:
                continue
            price = float(item.price_at_time or 0)
            item_category = _direct_sale_item_category(category, price)
            if item_category != 'Booking Delivery' and price <= 0:
                continue
            desired.append(((item.product_name or '').strip(), round(qty, 6), item_category))

    existing_sig = [
        ((e.material or '').strip(), round(float(e.qty or 0), 6), e.client_category or '')
        for e in stale_entries
    ]
    if active_source and sorted(existing_sig) == sorted(desired):
        bill_ref = _direct_sale_default_bill_ref(sale)
        now = sale.date_posted or pk_now()
        client_obj = get_client_by_input(sale.client_name or '')
        for e in stale_entries:
            e.date = now.strftime('%Y-%m-%d')
            e.time = now.strftime('%H:%M:%S')
            e.bill_no = bill_ref
            e.client = client_name
            e.client_code = client_code if client_code is not None else (client_obj.code if client_obj else (OPEN_KHATA_CODE if category == 'Open Khata' else None))
            e.nimbus_no = 'Direct Sale'
            e.transaction_category = 'Unbilled' if category == 'Cash' else 'Billed'
            e.driver_name = sale.driver_name
            e.note = sale.note
            _stamp_source(e, 'sales', 'direct_sale', sale.id, bill_ref, e.client_category or category)
        _sync_direct_sale_pending_bill(sale, (sale.items[0].product_name if sale.items else ''), extra_void_refs=refs)
        _sync_delivery_rent_for_sale(sale, rent_amount=float(getattr(sale, 'delivery_rent_cost', 0) or 0), rent_note='')
        _sync_direct_sale_waive_off(sale)
        _sync_direct_sale_accounting(sale)
        if rebuild_stock:
            _rebuild_material_totals()
        return {'entries_voided': 0, 'entries_created': 0}

    for e in stale_entries:
        e.is_void = True
        _stamp_source(e, 'sales', 'direct_sale', sale.id, _direct_sale_default_bill_ref(sale), 'Direct Sale')

    created = _active_direct_sale_items_generate_entries(
        sale,
        refs=refs,
        client_code=client_code,
        client_name=client_name
    )
    _sync_direct_sale_pending_bill(sale, (sale.items[0].product_name if sale.items else ''), extra_void_refs=refs)
    _sync_delivery_rent_for_sale(sale, rent_amount=float(getattr(sale, 'delivery_rent_cost', 0) or 0), rent_note='')
    _sync_direct_sale_waive_off(sale)
    _sync_direct_sale_accounting(sale)
    if rebuild_stock:
        _rebuild_material_totals()
    return {'entries_voided': len(stale_entries), 'entries_created': created}


def rebuild_material_ledger(client_id=None):
    """Rebuild direct-sale material movement rows from source sales and recalc stock totals."""
    if client_id:
        client = db.session.get(Client, client_id)
        if not client:
            return {'sales_checked': 0, 'entries_voided': 0, 'entries_created': 0}
        client_name_norm = (client.name or '').strip().lower()
        sales = DirectSale.query.filter(func.lower(func.trim(DirectSale.client_name)) == client_name_norm).all()
    else:
        sales = DirectSale.query.all()
    result = {'sales_checked': 0, 'entries_voided': 0, 'entries_created': 0}
    for sale in sales:
        stats = rebuild_direct_sale_effects(sale, old_refs=_direct_sale_bill_refs(sale), rebuild_stock=False)
        result['sales_checked'] += 1
        result['entries_voided'] += int(stats.get('entries_voided') or 0)
        result['entries_created'] += int(stats.get('entries_created') or 0)
    _rebuild_material_totals()
    return result


def rebuild_invoice_orphan_effects(client_id=None):
    """Void legacy material/pending effects whose invoice source is void or zeroed."""
    if client_id:
        client = db.session.get(Client, client_id)
        if not client:
            return {'invoices_checked': 0, 'entries_voided': 0, 'pending_voided': 0}
        invoices = Invoice.query.filter(or_(Invoice.client_code == client.code, func.lower(func.trim(Invoice.client_name)) == client.name.strip().lower())).all()
    else:
        invoices = Invoice.query.all()

    stats = {'invoices_checked': 0, 'entries_voided': 0, 'pending_voided': 0}
    for inv in invoices:
        stats['invoices_checked'] += 1
        invoice_no = (inv.invoice_no or '').strip()
        if not invoice_no:
            continue
        should_void_effects = bool(inv.is_void) or float(inv.total_amount or 0) <= 0
        if not should_void_effects:
            continue
        active_sale = DirectSale.query.filter(
            _not_void(DirectSale),
            DirectSale.invoice_id == inv.id
        ).first()
        if active_sale:
            continue
        entry_rows = Entry.query.filter(
            Entry.bill_no == invoice_no,
            _not_void(Entry),
            or_(Entry.invoice_id == inv.id, Entry.nimbus_no == 'Direct Sale')
        ).all()
        for e in entry_rows:
            e.is_void = True
            _stamp_source(e, 'invoice', 'invoice', inv.id, invoice_no, 'Invoice')
            stats['entries_voided'] += 1
        pending_rows = PendingBill.query.filter(
            PendingBill.bill_no == invoice_no,
            _not_void(PendingBill),
            or_(
                PendingBill.source_module == 'invoice',
                func.lower(func.coalesce(PendingBill.reason, '')).like('direct sale%')
            )
        ).all()
        for pb in pending_rows:
            pb.is_void = True
            _stamp_source(pb, 'invoice', 'invoice', inv.id, invoice_no, 'Invoice')
            stats['pending_voided'] += 1
    return stats


def rebuild_client_ledger(client_id=None):
    """Client ledger is a live view; rebuilding means refreshing derived pending/material effects."""
    material_stats = rebuild_material_ledger(client_id=client_id)
    invoice_stats = rebuild_invoice_orphan_effects(client_id=client_id)
    pending_stats = rebuild_pending_bills(client_id=client_id)
    invoice_stats_after_pending = rebuild_invoice_orphan_effects(client_id=client_id)
    return {
        'pending': pending_stats,
        'material': material_stats,
        'invoice_orphans': invoice_stats,
        'invoice_orphans_after_pending': invoice_stats_after_pending
    }


def rebuild_all_erp_consistency(client_id=None):
    stats = rebuild_client_ledger(client_id=client_id)
    _rebuild_material_totals()
    db.session.flush()
    return stats


def _log_transaction_consistency_failure(bill_no, source_module, exc):
    logging.exception(
        'Transaction consistency failure bill_no=%s source_module=%s',
        bill_no,
        source_module
    )


def validate_transaction_consistency(source_module, source_id):
    """Small source-level validation for derived rows. Raises on hard failure."""
    module = (source_module or '').strip().lower()
    if module in ['sales', 'direct_sale']:
        sale = db.session.get(DirectSale, source_id)
        if not sale or sale.is_void:
            return True
        category = normalize_sale_category(sale.category)
        bill_ref = _direct_sale_default_bill_ref(sale)
        if category in ['Booking Delivery', 'Mixed Transaction']:
            active_items = [it for it in (sale.items or []) if float(it.qty or 0) > 0 and float(it.price_at_time or 0) <= 0]
            if active_items:
                active_entries = Entry.query.filter(
                    Entry.source_module == 'sales',
                    Entry.source_id == sale.id,
                    _not_void(Entry),
                    Entry.type == 'OUT'
                ).count()
                if active_entries <= 0:
                    raise ValueError(f'Material ledger effects missing for {bill_ref}')
        pending_amount = max(0.0, float(sale.amount or 0) - float(getattr(sale, 'discount', 0) or 0) - float(sale.paid_amount or 0))
        if pending_amount > 0:
            pb = PendingBill.query.filter(
                PendingBill.source_module == 'sales',
                PendingBill.source_id == sale.id,
                _not_void(PendingBill)
            ).first()
            if not pb:
                raise ValueError(f'Pending bill effect missing for {bill_ref}')
    return True


def finalize_transaction(source_module, source_id):
    """Rebuild derived effects for one source and validate before commit."""
    module = (source_module or '').strip().lower()
    if module in ['sales', 'direct_sale']:
        sale = db.session.get(DirectSale, source_id)
        if not sale:
            return {'ok': False, 'reason': 'source not found'}
        stats = rebuild_direct_sale_effects(sale, old_refs=_direct_sale_bill_refs(sale), rebuild_stock=True)
        validate_transaction_consistency('sales', sale.id)
        return {'ok': True, 'source_module': 'sales', 'source_id': sale.id, 'stats': stats}
    if module == 'booking':
        booking = db.session.get(Booking, source_id)
        if not booking:
            return {'ok': False, 'reason': 'source not found'}
        _sync_booking_pending_bill(booking, (booking.items[0].material_name if booking.items else ''), extra_void_refs=_booking_bill_refs(booking))
        return {'ok': True, 'source_module': 'booking', 'source_id': booking.id}
    return {'ok': False, 'reason': f'unsupported source module {source_module}'}


def repair_transaction_by_bill_no(bill_no):
    """Targeted repair for one bill; rebuilds only the owning source transaction."""
    base = normalize_manual_bill(bill_no) if bill_no else ''
    candidates = _bill_no_variants(base or bill_no)
    sale = DirectSale.query.filter(
        or_(DirectSale.manual_bill_no.in_(candidates), DirectSale.auto_bill_no.in_(candidates))
    ).order_by(DirectSale.id.desc()).first()
    if sale:
        result = finalize_transaction('sales', sale.id)
        db.session.flush()
        return result
    booking = Booking.query.filter(
        or_(Booking.manual_bill_no.in_(candidates), Booking.auto_bill_no.in_(candidates))
    ).order_by(Booking.id.desc()).first()
    if booking:
        result = finalize_transaction('booking', booking.id)
        db.session.flush()
        return result
    invoice = Invoice.query.filter(Invoice.invoice_no.in_(candidates)).order_by(Invoice.id.desc()).first()
    if invoice:
        linked_sale = DirectSale.query.filter_by(invoice_id=invoice.id).order_by(DirectSale.id.desc()).first()
        if linked_sale:
            result = finalize_transaction('sales', linked_sale.id)
            db.session.flush()
            return result
    return {'ok': False, 'reason': 'source transaction not found', 'bill_no': bill_no}


def _rebuild_material_totals():
    """Recalculate material stock totals from active entry rows.

    Single-pass aggregation: the previous implementation issued two SUM
    queries per material (N materials = 2N queries).  Every sale submit,
    void, edit and delete calls this, so on a large catalogue the rebuild
    alone could add hundreds of queries to one transaction.  The grouped
    form below is two queries total and is behaviour-identical.
    """
    in_rows = db.session.query(
        Entry.material, func.sum(Entry.qty)
    ).filter(
        Entry.type == 'IN',
        Entry.is_void == False,
    ).group_by(Entry.material).all()
    out_rows = db.session.query(
        Entry.material, func.sum(Entry.qty)
    ).filter(
        Entry.type == 'OUT',
        Entry.is_void == False,
    ).group_by(Entry.material).all()

    totals = {}
    for material, qty in in_rows:
        if material:
            totals[str(material)] = float(qty or 0)
    for material, qty in out_rows:
        if material:
            key = str(material)
            totals[key] = float(totals.get(key, 0) or 0) - float(qty or 0)

    for mat in Material.query.all():
        mat.total = totals.get(mat.name, 0)


