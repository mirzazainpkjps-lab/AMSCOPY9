"""Domain service module — extracted from legacy ERP core."""
from __future__ import annotations

import os
import io
import secrets
import json
import calendar
import threading
import time
import smtplib
import shutil
import sqlite3
import zipfile
import urllib.request
import urllib.error
import re
import logging
import importlib
from itertools import zip_longest
from urllib.parse import unquote
from contextlib import redirect_stderr
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlalchemy import func, case, text, or_, and_, exists, not_
from sqlalchemy.orm import selectinload
from types import SimpleNamespace
from flask import (
    current_app as app,
    render_template, request, redirect, url_for, flash, jsonify,
    send_file, Response, make_response, send_from_directory,
    got_request_exception, abort, session, g,
)
from flask_login import login_user, login_required, logout_user, current_user

from models import *
from utils.audit import audit_log
from utils.reconciliation import run_auto_reconcile
from cash_flow_reconciliation_helpers import (
    create_reconciliation, update_reconciliation, delete_reconciliation,
    get_reconciliation_history, migrate_legacy_record,
)
from app.services import constants as C
from app.services import state

# === explicit service imports ===
from app.services.lookups import (
    get_client_by_input,
)
from app.services.time_money import (
    _norm_text,
    pk_now,
)


# Rebind constants used as bare names
OPEN_KHATA_CODE = C.OPEN_KHATA_CODE
OPEN_KHATA_NAME = C.OPEN_KHATA_NAME
PK_TZ = C.PK_TZ
SALE_CATEGORY_CHOICES = C.SALE_CATEGORY_CHOICES
_SALE_CATEGORY_ALIASES = C._SALE_CATEGORY_ALIASES
DOMAIN_WIPE_REGISTRY = C.DOMAIN_WIPE_REGISTRY
USER_PERMISSION_DEFAULTS = C.USER_PERMISSION_DEFAULTS
PERMISSION_LEGACY_FALLBACKS = C.PERMISSION_LEGACY_FALLBACKS
ENDPOINT_PERMISSION_MAP = C.ENDPOINT_PERMISSION_MAP
AUTO_BILL_NS_DEFAULT = C.AUTO_BILL_NS_DEFAULT
AUTO_BILL_NAMESPACES = C.AUTO_BILL_NAMESPACES
EDITABLE_USER_PERMISSION_FIELDS = C.EDITABLE_USER_PERMISSION_FIELDS
basedir = C.basedir
legacy_instance_dir = C.legacy_instance_dir
legacy_db_path = C.legacy_db_path
db_path = C.db_path
_DB_HEALTH_SNAPSHOT_PATH = C._DB_HEALTH_SNAPSHOT_PATH
_max_upload_mb = C._max_upload_mb
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

def _cost_rate_for_material(mat_name, tx_dt=None):
    """
    Get the most recent cost rate for a material as of a given transaction date.
    Falls back to material unit price if no purchase history found.
    
    Args:
        mat_name: Material name to look up
        tx_dt: Transaction date (datetime.date object) to find rate for
        
    Returns:
        Tuple of (cost_rate, is_known) where is_known=True if rate was found in purchase history
    """
    mat_key = _norm_text(mat_name)
    if not mat_key:
        return 0.0, False
    
    # Query GRN items for this material
    try:
        grn_query = db.session.query(GRNItem, GRN).join(
            GRN, GRNItem.grn_id == GRN.id
        ).filter(
            GRN.is_void == False,
            GRNItem.is_void == False,
            GRNItem.mat_name.ilike(f'%{mat_name}%')
        ).order_by(GRN.date_posted.desc()).all()
        
        # Find the most recent GRN at or before the transaction date
        if grn_query:
            for item, grn in grn_query:
                posted_dt = grn.date_posted
                if tx_dt and posted_dt and posted_dt.date() <= tx_dt:
                    rate = float(item.price_at_time or 0)
                    if rate > 0:
                        return rate, True
            # If no match with tx_dt filter, use the most recent
            if grn_query:
                item, grn = grn_query[0]
                rate = float(item.price_at_time or 0)
                if rate > 0:
                    return rate, True
    except Exception:
        pass
    
    # Fallback to material unit price
    try:
        material = Material.query.filter(Material.name.ilike(f'%{mat_name}%')).first()
        if material:
            rate = float(material.unit_price or 0)
            if rate > 0:
                return rate, True
    except Exception:
        pass
    
    return 0.0, False


def _cost_rate_for_grn_item(grn_item_id):
    """
    Get the cost rate for a specific GRN item.
    
    Args:
        grn_item_id: The ID of the GRN item
        
    Returns:
        Tuple of (cost_rate, is_known) where is_known=True if GRN item exists
    """
    if not grn_item_id:
        return 0.0, False
    
    try:
        grn_item = GRNItem.query.get(grn_item_id)
        if grn_item and not bool(getattr(grn_item, 'is_void', False)) and grn_item.grn and not grn_item.grn.is_void:
            rate = float(grn_item.price_at_time or 0)
            if rate > 0:
                return rate, True
    except Exception:
        pass
    
    return 0.0, False


def _frozen_cost_for_sale_item(item):
    """Prefer the rate frozen on the sale line at post time."""
    if item is None:
        return 0.0, False
    frozen = getattr(item, 'cost_rate_at_sale', None)
    if frozen is not None and float(frozen or 0) > 0:
        return float(frozen), True
    rate, known = _cost_rate_for_grn_item(getattr(item, 'grn_item_id', None))
    if known:
        return rate, True
    return 0.0, False


def _grn_consumed_qty_map(grn_item_ids, exclude_sale_id=None):
    if not grn_item_ids:
        return {}
    q = db.session.query(
        GRNAllocation.grn_item_id,
        func.sum(GRNAllocation.qty)
    ).join(DirectSale, GRNAllocation.sale_id == DirectSale.id).filter(
        GRNAllocation.grn_item_id.in_(list(grn_item_ids)),
        GRNAllocation.is_void == False,
        DirectSale.is_void == False,
    )
    if exclude_sale_id:
        q = q.filter(GRNAllocation.sale_id != int(exclude_sale_id))
    rows = q.group_by(GRNAllocation.grn_item_id).all()
    return {int(row[0]): float(row[1] or 0) for row in rows if row[0]}


def _grn_fifo_lots_for_material(mat_name, as_of_dt=None, exclude_sale_id=None):
    """Oldest open GRN lots first (date, grn id, item id)."""
    mat_key = _material_norm_key(mat_name)
    if not mat_key:
        return []
    rows = db.session.query(GRNItem, GRN).join(
        GRN, GRNItem.grn_id == GRN.id
    ).filter(
        GRN.is_void == False,
        GRNItem.is_void == False,
    ).order_by(GRN.date_posted.asc(), GRN.id.asc(), GRNItem.id.asc()).all()
    lots = []
    item_ids = []
    for item, grn in rows:
        if _material_norm_key(item.mat_name) != mat_key:
            continue
        if as_of_dt and grn.date_posted and grn.date_posted.date() > as_of_dt:
            continue
        lots.append(item)
        item_ids.append(item.id)
    consumed = _grn_consumed_qty_map(item_ids, exclude_sale_id=exclude_sale_id)
    open_lots = []
    for item in lots:
        available = max(0.0, float(item.qty or 0) - float(consumed.get(item.id, 0) or 0))
        if available <= 0:
            continue
        open_lots.append((item, available, float(item.price_at_time or 0)))
    return open_lots


def _allocate_grn_fifo_plan(mat_name, qty_needed, as_of_dt=None, exclude_sale_id=None):
    remaining = float(qty_needed or 0)
    if remaining <= 0:
        return []
    plan = []
    for item, available, rate in _grn_fifo_lots_for_material(mat_name, as_of_dt, exclude_sale_id):
        take = min(available, remaining)
        if take <= 0:
            continue
        plan.append({
            'grn_item_id': item.id,
            'qty': take,
            'cost_rate': rate,
        })
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0.0001:
        plan.append({
            'grn_item_id': None,
            'qty': remaining,
            'cost_rate': None,
        })
    return plan


def _expand_chargeable_items_fifo(items, as_of_dt=None, exclude_sale_id=None):
    """Split cash/credit lines so each line is one FIFO lot with a frozen cost."""
    expanded = []
    for item in (items or []):
        if item.get('is_booking') or float(item.get('price_at_time') or 0) <= 0:
            copied = dict(item)
            copied.setdefault('grn_item_id', None)
            copied.setdefault('cost_rate_at_sale', None)
            expanded.append(copied)
            continue
        qty = float(item.get('qty') or 0)
        plan = _allocate_grn_fifo_plan(
            item.get('product_name'),
            qty,
            as_of_dt=as_of_dt,
            exclude_sale_id=exclude_sale_id,
        )
        if not plan:
            copied = dict(item)
            copied['grn_item_id'] = None
            copied['cost_rate_at_sale'] = None
            expanded.append(copied)
            continue
        for slice_row in plan:
            copied = dict(item)
            copied['qty'] = float(slice_row['qty'])
            copied['grn_item_id'] = slice_row['grn_item_id']
            rate = slice_row['cost_rate']
            if rate is None or float(rate or 0) <= 0:
                fallback, known = _cost_rate_for_material(item.get('product_name'), as_of_dt)
                rate = fallback if known else 0.0
            copied['cost_rate_at_sale'] = float(rate or 0)
            expanded.append(copied)
    return expanded


def _refresh_grn_item_locks(grn_item_ids=None):
    q = GRNItem.query
    if grn_item_ids is not None:
        ids = [int(i) for i in grn_item_ids if i]
        if not ids:
            return 0
        q = q.filter(GRNItem.id.in_(ids))
    items = q.all()
    consumed = _grn_consumed_qty_map([it.id for it in items])
    touched = 0
    for it in items:
        locked = float(consumed.get(it.id, 0) or 0) > 0.0001
        if bool(it.is_locked) != locked:
            it.is_locked = locked
            touched += 1
    return touched


def _void_sale_grn_allocations(sale, is_void=True):
    if not sale:
        return 0
    rows = GRNAllocation.query.filter_by(sale_id=sale.id).all()
    ids = []
    for row in rows:
        row.is_void = bool(is_void)
        ids.append(row.grn_item_id)
    if ids:
        _refresh_grn_item_locks(ids)
    return len(rows)


def _delete_sale_grn_allocations(sale):
    if not sale:
        return 0
    rows = GRNAllocation.query.filter_by(sale_id=sale.id).all()
    ids = [r.grn_item_id for r in rows]
    GRNAllocation.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
    if ids:
        _refresh_grn_item_locks(ids)
    return len(rows)


def _apply_grn_allocations_for_sale(sale, sale_item_records):
    if not sale or not sale_item_records:
        return 0
    created = 0
    touched_ids = []
    for sale_item, item in sale_item_records:
        if item.get('is_booking') or float(item.get('price_at_time') or 0) <= 0:
            continue
        grn_item_id = item.get('grn_item_id')
        qty = float(item.get('qty') or 0)
        if not grn_item_id or qty <= 0:
            continue
        cost = item.get('cost_rate_at_sale')
        if cost is None:
            cost, _ = _cost_rate_for_grn_item(grn_item_id)
        db.session.add(GRNAllocation(
            sale_id=sale.id,
            sale_item_id=sale_item.id,
            grn_item_id=int(grn_item_id),
            qty=qty,
            cost_rate=float(cost or 0),
            is_void=False,
        ))
        if sale_item.cost_rate_at_sale is None:
            sale_item.cost_rate_at_sale = float(cost or 0)
        if not sale_item.grn_item_id:
            sale_item.grn_item_id = int(grn_item_id)
        touched_ids.append(int(grn_item_id))
        created += 1
    if touched_ids:
        _refresh_grn_item_locks(touched_ids)
    return created


def _grn_has_locked_lots(grn_obj):
    if not grn_obj:
        return False
    for it in (grn_obj.items or []):
        if bool(getattr(it, 'is_locked', False)) and not bool(getattr(it, 'is_void', False)):
            return True
        consumed = _grn_consumed_qty_map([it.id]).get(it.id, 0)
        if float(consumed or 0) > 0.0001:
            return True
    return False


def normalize_sale_category(raw_value, default='Credit Customer'):
    key = (raw_value or '').strip().lower()
    if not key:
        return default
    return _SALE_CATEGORY_ALIASES.get(key, default)


def _direct_sale_default_bill_ref(sale):
    if sale.manual_bill_no:
        return sale.manual_bill_no
    if sale.auto_bill_no:
        return sale.auto_bill_no
    if getattr(sale, 'invoice', None) and sale.invoice and sale.invoice.invoice_no:
        return sale.invoice.invoice_no
    if (sale.category or '') == 'Cash':
        return f"CSH-{sale.id}"
    return f"DS-{sale.id}"


def _direct_sale_bill_refs(sale):
    refs = {f"DS-{sale.id}", f"UNBILLED-{sale.id}", f"CSH-{sale.id}"}
    if sale.manual_bill_no:
        refs.add(sale.manual_bill_no)
    if sale.auto_bill_no:
        refs.add(sale.auto_bill_no)
    if getattr(sale, 'invoice', None) and sale.invoice and sale.invoice.invoice_no:
        refs.add(sale.invoice.invoice_no)
    return [r for r in refs if r]


def _direct_sale_client_identity(sale):
    if not sale:
        return (None, None)
    client_name = (sale.client_name or '').strip()
    if normalize_sale_category(getattr(sale, 'category', None)) == 'Open Khata':
        return (OPEN_KHATA_CODE, client_name or OPEN_KHATA_NAME)
    stored_code = (getattr(sale, 'client_code', None) or '').strip() or None
    client_obj = get_client_by_input(stored_code or client_name) if (stored_code or client_name) else None
    return (
        (client_obj.code if client_obj else stored_code),
        (client_obj.name if client_obj else client_name),
    )


def _entry_client_scope_filter(client_code=None, client_name=None):
    code = (client_code or '').strip()
    name = (client_name or '').strip()
    clauses = []
    if code:
        clauses.append(Entry.client_code == code)
    if name:
        clauses.append(func.lower(func.trim(func.coalesce(Entry.client, ''))) == name.lower())
    return or_(*clauses) if clauses else None


def _pending_client_scope_filter(client_code=None, client_name=None):
    code = (client_code or '').strip()
    name = (client_name or '').strip()
    clauses = []
    if code:
        clauses.append(PendingBill.client_code == code)
    if name:
        clauses.append(func.lower(func.trim(func.coalesce(PendingBill.client_name, ''))) == name.lower())
    return or_(*clauses) if clauses else None


def _not_void(model):
    """Treat legacy NULL is_void as active while keeping explicit void rows hidden."""
    return func.coalesce(getattr(model, 'is_void'), False) == False


def _source_payload(module, table, source_id, bill_no=None, tx_type=None):
    return {
        'source_module': module,
        'source_table': table,
        'source_id': int(source_id) if source_id else None,
        'source_bill_no': bill_no,
        'transaction_type': tx_type or module
    }


def _stamp_source(row, module, table, source_id, bill_no=None, tx_type=None):
    for key, val in _source_payload(module, table, source_id, bill_no=bill_no, tx_type=tx_type).items():
        if hasattr(row, key):
            setattr(row, key, val)
    return row


def _entry_source_filter(module, source_id, refs=None, nimbus_no=None, client_code=None, client_name=None):
    clauses = [and_(Entry.source_module == module, Entry.source_id == source_id)]
    if refs:
        fallback = Entry.bill_no.in_(list(refs))
        if nimbus_no:
            fallback = and_(fallback, Entry.nimbus_no == nimbus_no)
        client_filter = _entry_client_scope_filter(client_code, client_name)
        if client_filter is not None:
            fallback = and_(fallback, client_filter)
        clauses.append(fallback)
    return or_(*clauses)


def _pending_source_filter(module, source_id, refs=None, reason_prefix=None, client_code=None, client_name=None):
    clauses = [and_(PendingBill.source_module == module, PendingBill.source_id == source_id)]
    if refs:
        fallback = PendingBill.bill_no.in_(list(refs))
        if reason_prefix:
            fallback = and_(
                fallback,
                func.lower(func.coalesce(PendingBill.reason, '')).like(f"{reason_prefix.lower()}%")
            )
        client_filter = _pending_client_scope_filter(client_code, client_name)
        if client_filter is not None:
            fallback = and_(fallback, client_filter)
        clauses.append(fallback)
    return or_(*clauses)


def _direct_sale_item_category(sale_category, price_at_time):
    cat = normalize_sale_category(sale_category)
    price = float(price_at_time or 0)
    if cat == 'Mixed Transaction':
        return 'Booking Delivery' if price <= 0 else 'Credit Customer'
    if cat == 'Booking Delivery':
        return 'Booking Delivery' if price <= 0 else 'Credit Customer'
    return cat


def _is_rent_material_name(name):
    txt = (name or '').strip().lower()
    if not txt:
        return False
    normalized = txt.replace('-', ' ').replace('_', ' ')
    return 'rent' in normalized


def _material_norm_key(v):
    txt = (v or '').strip().lower()
    return re.sub(r'[^a-z0-9]+', '', txt)


def _booking_allocated_qty_map(booking_item_ids):
    if not booking_item_ids:
        return {}
    rows = db.session.query(
        BookingAllocation.booking_item_id,
        func.sum(BookingAllocation.qty)
    ).join(DirectSaleItem).join(DirectSale).filter(
        BookingAllocation.booking_item_id.in_(booking_item_ids),
        BookingAllocation.is_void == False,
        DirectSale.is_void == False
    ).group_by(BookingAllocation.booking_item_id).all()
    return {row[0]: float(row[1] or 0) for row in rows}


def _allocate_booking_quantities_for_sale_item(client_name, booked_material, qty_needed):
    if qty_needed <= 0:
        return []
    mat_key = _material_norm_key(booked_material)
    if not mat_key:
        return []

    booking_items = BookingItem.query.join(Booking).filter(
        Booking.is_void == False,
        func.lower(func.trim(Booking.client_name)) == func.lower(func.trim(client_name))
    ).order_by(Booking.date_posted.asc(), Booking.id.asc(), BookingItem.id.asc()).all()

    booking_items = [item for item in booking_items if _material_norm_key(item.material_name) == mat_key]
    if not booking_items:
        return []

    allocated = _booking_allocated_qty_map([item.id for item in booking_items])

    # Compute total returned booked material for this client + material.
    # Booked Returns restore the available booking pool without voiding allocations.
    norm_name = func.lower(func.trim(client_name)) if client_name else ''
    client_obj = Client.query.filter(
        func.lower(func.trim(Client.name)) == norm_name
    ).first() if client_name else None
    returned_qty = 0.0
    if client_obj:
        returned_row = db.session.query(
            func.sum(Entry.qty)
        ).filter(
            Entry.type == 'IN',
            Entry.is_void == False,
            Entry.nimbus_no == 'Material Return',
            Entry.transaction_category == 'Booked Return',
            or_(
                Entry.client_code == client_obj.code,
                func.lower(func.trim(Entry.client)) == norm_name
            ),
        ).filter(
            func.lower(func.trim(Entry.material)) == func.lower(func.trim(booked_material))
        ).all()
        if returned_row and returned_row[0][0]:
            returned_qty = float(returned_row[0][0] or 0)

    # Total pool = sum(item.qty) - sum(allocated) + returned.
    total_booked = sum(float(item.qty or 0) for item in booking_items)
    total_allocated = sum(allocated.get(item.id, 0.0) for item in booking_items)
    pool_available = max(0.0, total_booked - total_allocated + returned_qty)

    if pool_available < qty_needed:
        raise ValueError(
            f'Not enough booked quantity for {booked_material}. '
            f'Requested {qty_needed}, available {pool_available:.2f}.'
        )

    remaining = float(qty_needed or 0)
    allocation_plan = []

    # First pass: allocate from items that still have original qty - alloc > 0
    for item in booking_items:
        avail = max(0.0, float(item.qty or 0) - allocated.get(item.id, 0.0))
        if avail <= 0:
            continue
        take = min(avail, remaining)
        if take <= 0:
            continue
        allocation_plan.append((item, take))
        remaining -= take
        if remaining <= 0:
            break

    # Second pass: if returns have replenished the pool, allocate from items
    # that were fully consumed but now have returned stock available.
    # We replenish items in LIFO order (most recent first) so older lots are
    # consumed first on subsequent allocations (preserving FIFO for the future).
    if remaining > 0 and returned_qty > 0:
        for item in reversed(booking_items):
            max_capacity = float(item.qty or 0)
            already_used = allocated.get(item.id, 0.0)
            # How much MORE can we allocate to this item beyond its original qty?
            # The returned pool lets us re-use the item's capacity.
            if already_used <= 0:
                continue
            take = min(remaining, max_capacity)
            if take <= 0:
                continue
            allocation_plan.append((item, take))
            remaining -= take
            if remaining <= 0:
                break

    if remaining > 0:
        raise ValueError(
            f'Not enough booked quantity for {booked_material}. '
            f'Requested {qty_needed}, available {qty_needed - remaining:.2f} (after returns).'
        )

    return allocation_plan


def _void_sale_booking_allocations(sale, is_void):
    if not sale:
        return 0
    sale_item_ids = [item.id for item in (sale.items or []) if item.id]
    if not sale_item_ids:
        return 0
    return BookingAllocation.query.filter(
        BookingAllocation.sale_item_id.in_(sale_item_ids)
    ).update({'is_void': bool(is_void)}, synchronize_session=False)


def _apply_booking_allocations_for_sale(sale, sale_item_records):
    if not sale or not sale_item_records:
        return 0
    created = 0
    for sale_item, item in sale_item_records:
        if not item.get('is_booking'):
            continue
        booked_material = item.get('booked_material') or item.get('product_name')
        qty_needed = float(item.get('qty') or 0)
        if qty_needed <= 0:
            continue
        allocation_plan = _allocate_booking_quantities_for_sale_item(sale.client_name, booked_material, qty_needed)
        for booking_item, alloc_qty in allocation_plan:
            db.session.add(BookingAllocation(
                sale_id=sale.id,
                sale_item_id=sale_item.id,
                booking_item_id=booking_item.id,
                qty=alloc_qty,
                is_void=False
            ))
            created += 1
    return created


def _merge_delivery_maps(primary_map, secondary_map):
    """
    Merge per-material delivered quantities without double counting overlaps.

    The same OUT rows can appear in both name-based and code-based aggregates.
    For each material, keep the larger aggregate instead of summing both sides.
    """
    merged = dict(primary_map or {})
    for mat_name, qty in (secondary_map or {}).items():
        merged[mat_name] = max(
            float(merged.get(mat_name, 0) or 0),
            float(qty or 0),
        )
    return merged


def _client_booking_unit_price_map(client_name=None, client_code=None):
    client = get_client_by_input((client_code or '').strip() or (client_name or '').strip())
    if not client and client_name:
        norm = (client_name or '').strip().lower()
        if norm:
            client = Client.query.filter(func.lower(func.trim(Client.name)) == norm).first()
    if not client:
        return {}

    bookings = Booking.query.filter_by(client_name=client.name, is_void=False).all()
    booking_ids = [b.id for b in bookings]
    if not booking_ids:
        return {}

    latest_price = {}
    latest_price_dt = {}
    for item in BookingItem.query.filter(BookingItem.booking_id.in_(booking_ids)).all():
        raw_mat = (item.material_name or '').strip()
        key = _material_norm_key(raw_mat)
        if not key:
            continue
        bk = item.booking
        bk_dt = bk.date_posted if bk and getattr(bk, 'date_posted', None) else None
        if key not in latest_price_dt or (bk_dt and latest_price_dt[key] and bk_dt > latest_price_dt[key]) or (bk_dt and not latest_price_dt[key]):
            latest_price_dt[key] = bk_dt
            latest_price[key] = float(item.price_at_time or 0)
        elif key not in latest_price:
            latest_price[key] = float(item.price_at_time or 0)
    return latest_price


def _rent_reconciliation_from_items(items, delivery_rent_cost=0, client_name=None, client_code=None):
    booking_rate_map = _client_booking_unit_price_map(client_name=client_name, client_code=client_code)
    rent_revenue = 0.0
    for item in (items or []):
        mat_name = (item.get('product_name') or item.get('name') or '').strip()
        if not _is_rent_material_name(mat_name):
            continue
        qty = float(item.get('qty') or 0)
        rate = float(item.get('price_at_time') or 0)
        if rate <= 0:
            lookup_name = (item.get('booked_material') or mat_name or '').strip()
            rate = float(booking_rate_map.get(_material_norm_key(lookup_name), 0) or 0)
        if qty <= 0 or rate <= 0:
            continue
        rent_revenue += qty * rate
    delivery_cost = max(0.0, float(delivery_rent_cost or 0))
    variance_loss = max(0.0, delivery_cost - rent_revenue)
    return {
        'rent_item_revenue': float(rent_revenue),
        'delivery_rent_cost': float(delivery_cost),
        'rent_variance_loss': float(variance_loss),
    }


def _dedupe_direct_sale_items(items):
    """Drop exact repeated sale lines before writing derived ledgers."""
    merged = []
    seen = set()
    for item in (items or []):
        product_name = (item.get('product_name') or item.get('name') or '').strip()
        if not product_name:
            continue
        qty = float(item.get('qty') or 0)
        if qty <= 0:
            continue
        booked_material = (item.get('booked_material') or '').strip() or None
        price_at_time = float(item.get('price_at_time') or 0)
        key = (
            product_name,
            booked_material or '',
            bool(item.get('is_booking')),
            bool(item.get('is_alternate')),
            round(price_at_time, 6),
            item.get('grn_item_id') or None,
            round(qty, 6),
        )
        if key in seen:
            continue
        copied = dict(item)
        copied['product_name'] = product_name
        copied['booked_material'] = booked_material
        copied['price_at_time'] = price_at_time
        copied['qty'] = qty
        seen.add(key)
        merged.append(copied)
    return merged


def _sync_delivery_rent_for_sale(sale, include_in_bill=False, rent_amount=0, rent_note=''):
    """Upsert delivery-rent ledger row for a sale when an actual rent amount is provided."""
    if not sale:
        return
    include = float(rent_amount or 0) > 0
    row = DeliveryRent.query.filter_by(sale_id=sale.id).order_by(DeliveryRent.id.desc()).first()

    if not include:
        if row:
            db.session.delete(row)
        return

    if not (sale.driver_name or '').strip():
        return

    bill_ref = _direct_sale_default_bill_ref(sale)
    created_by = None
    try:
        if current_user and current_user.is_authenticated:
            created_by = current_user.username
    except Exception:
        created_by = None

    if not row:
        row = DeliveryRent(sale_id=sale.id, created_by=created_by)
        db.session.add(row)
    elif not row.created_by and created_by:
        row.created_by = created_by

    row.delivery_person_name = (sale.driver_name or '').strip()
    row.bill_no = bill_ref
    row.amount = float(rent_amount or 0)
    row.note = (rent_note or '').strip()
    row.date_posted = sale.date_posted or pk_now()
    row.is_void = bool(sale.is_void)


