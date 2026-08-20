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
from app.services.time_money import (
    _to_float_or_zero,
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

def _collect_direct_sale_form_draft(form_data, mode='add', sale_id=None):
    delivery_rows = []
    delivery_primary_name = (form_data.get('driver_name') or '').strip()
    person_ids = form_data.getlist('delivery_person_id[]')
    bags_list = form_data.getlist('delivery_bags[]')
    rent_list = form_data.getlist('delivery_rent_amount[]')
    for idx, raw_id in enumerate(person_ids):
        pid = _safe_int(raw_id)
        bags = (bags_list[idx] if idx < len(bags_list) else '').strip()
        rent = (rent_list[idx] if idx < len(rent_list) else '').strip()
        has_payload = bool(str(raw_id or '').strip() or bags or rent)
        if not has_payload:
            continue
        if pid and not delivery_primary_name:
            dp = db.session.get(DeliveryPerson, pid)
            if dp and dp.name:
                delivery_primary_name = dp.name
        delivery_rows.append({
            'delivery_person_id': raw_id,
            'bags_delivered': bags,
            'rent_amount': rent
        })
    return {
        'mode': mode,
        'sale_id': sale_id,
        'category': (form_data.get('category') or '').strip(),
        'client_code': (form_data.get('client_code') or '').strip(),
        'client_name': (form_data.get('client_name') or '').strip(),
        'manual_client_name': (form_data.get('manual_client_name') or '').strip(),
        'driver_name': delivery_primary_name,
        'sale_date': (form_data.get('sale_date') or '').strip(),
        'manual_bill_no': (form_data.get('manual_bill_no') or '').strip(),
        'note': (form_data.get('note') or '').strip(),
        'photo_url': (form_data.get('photo_url') or '').strip(),
        'amount': (form_data.get('amount') or '').strip(),
        'paid_amount': (form_data.get('paid_amount') or '').strip(),
        'discount': (form_data.get('discount') or '').strip(),
        'discount_reason': (form_data.get('discount_reason') or '').strip(),
        'delivery_rent': (form_data.get('delivery_rent') or '').strip(),
        'delivery_persons': delivery_rows,
        'allow_negative_stock': str(form_data.get('allow_negative_stock') or '').strip().lower() in ['1', 'true', 'on', 'yes'],
        'has_bill': str(form_data.get('has_bill') or '').strip().lower() in ['1', 'true', 'on', 'yes'],
        'create_invoice': str(form_data.get('create_invoice') or '').strip().lower() in ['1', 'true', 'on', 'yes'],
        'track_as_cash': str(form_data.get('track_as_cash') or '').strip().lower() in ['1', 'true', 'on', 'yes'],
        'items': [
            {
                'product_name': (p or '').strip(),
                'alternate_material': (a or '').strip(),
                'qty': (q or '').strip(),
                'unit_rate': (r or '').strip(),
                'ignore_booking': str(ib or '').strip().lower() in ['1', 'true', 'on', 'yes']
            }
            for p, a, q, r, ib in zip_longest(
                form_data.getlist('product_name[]'),
                form_data.getlist('alternate_material[]'),
                form_data.getlist('qty[]'),
                form_data.getlist('unit_rate[]'),
                form_data.getlist('ignore_booking_item[]'),
                fillvalue=''
            )
            if (p or '').strip() or (q or '').strip() or (r or '').strip()
        ]
    }


def _stash_direct_sale_form_draft(form_data, mode='add', sale_id=None):
    session['direct_sale_form_draft'] = _collect_direct_sale_form_draft(
        form_data,
        mode=mode,
        sale_id=sale_id
    )


def _safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _parse_delivery_allocations(form_data):
    person_ids = form_data.getlist('delivery_person_id[]')
    bags_list = form_data.getlist('delivery_bags[]')
    rent_list = form_data.getlist('delivery_rent_amount[]')
    allocations = []
    total_rent = 0.0
    total_bags = 0.0
    primary_name = ''

    for idx, raw_id in enumerate(person_ids):
        pid = _safe_int(raw_id)
        bags = _to_float_or_zero(bags_list[idx] if idx < len(bags_list) else 0)
        rent = _to_float_or_zero(rent_list[idx] if idx < len(rent_list) else 0)
        has_payload = (pid is not None) or (bags > 0) or (rent > 0)
        if not has_payload:
            continue
        if pid is None:
            return None, "Delivery person is required for all delivery rows.", 0.0, 0.0, ''
        if bags < 0 or rent < 0:
            return None, "Delivery bags and rent cannot be negative.", 0.0, 0.0, ''
        dp = db.session.get(DeliveryPerson, pid)
        if not dp:
            return None, "Selected delivery person is invalid.", 0.0, 0.0, ''
        allocations.append({
            'delivery_person': dp,
            'bags_delivered': bags,
            'rent_amount': rent
        })
        total_rent += rent
        total_bags += bags
        if not primary_name:
            primary_name = (dp.name or '').strip()

    return allocations, None, total_rent, total_bags, primary_name


def _summarize_direct_sale_draft(draft):
    items = draft.get('items') or []
    item_count = len(items)
    total_qty = sum(_to_float_or_zero(i.get('qty')) for i in items)
    total_amount = _to_float_or_zero(draft.get('amount'))
    return {
        'item_count': item_count,
        'total_qty': total_qty,
        'total_amount': total_amount
    }


def _client_material_returnable_qty_map(client_obj):
    if not client_obj:
        return {}
    norm_name = (client_obj.name or '').strip().lower()
    out_rows = db.session.query(
        Entry.material,
        func.sum(Entry.qty)
    ).filter(
        Entry.type == 'OUT',
        Entry.is_void == False,
        Entry.nimbus_no == 'Direct Sale',
        # Cash/credit/open-khata only — never consume booking-delivery qty as a normal return.
        or_(
            Entry.client_category.is_(None),
            func.trim(Entry.client_category) == '',
            ~Entry.client_category.in_(['Booking Delivery']),
        ),
        or_(
            Entry.client_code == client_obj.code,
            func.lower(func.trim(Entry.client)) == norm_name
        )
    ).group_by(Entry.material).all()
    in_rows = db.session.query(
        Entry.material,
        func.sum(Entry.qty)
    ).filter(
        Entry.type == 'IN',
        Entry.is_void == False,
        Entry.nimbus_no == 'Material Return',
        or_(
            Entry.transaction_category == 'Return',
            Entry.transaction_category.is_(None),
            func.trim(Entry.transaction_category) == ''
        ),
        or_(
            Entry.client_code == client_obj.code,
            func.lower(func.trim(Entry.client)) == norm_name
        )
    ).group_by(Entry.material).all()
    delivered = {str(m or '').strip(): float(q or 0) for m, q in out_rows if str(m or '').strip()}
    returned = {str(m or '').strip(): float(q or 0) for m, q in in_rows if str(m or '').strip()}
    return {
        k: max(0.0, float(v or 0) - float(returned.get(k, 0) or 0))
        for k, v in delivered.items()
    }


def _client_booked_material_returnable_qty_map(client_obj):
    """
    Returnable qty map for booked deliveries.

    Uses OUT entries recorded as booking deliveries and subtracts only booked returns
    (material return entries tagged as transaction_category='Booked Return').
    """
    if not client_obj:
        return {}
    norm_name = (client_obj.name or '').strip().lower()

    out_rows = db.session.query(
        func.trim(Entry.material),
        func.sum(Entry.qty)
    ).filter(
        Entry.type == 'OUT',
        Entry.is_void == False,
        or_(Entry.nimbus_no == 'Booking Delivery', Entry.client_category == 'Booking Delivery'),
        or_(
            Entry.client_code == client_obj.code,
            func.lower(func.trim(Entry.client)) == norm_name
        )
    ).group_by(func.trim(Entry.material)).all()

    in_rows = db.session.query(
        func.trim(Entry.material),
        func.sum(Entry.qty)
    ).filter(
        Entry.type == 'IN',
        Entry.is_void == False,
        Entry.nimbus_no == 'Material Return',
        Entry.transaction_category == 'Booked Return',
        or_(
            Entry.client_code == client_obj.code,
            func.lower(func.trim(Entry.client)) == norm_name
        )
    ).group_by(func.trim(Entry.material)).all()

    delivered = {str(m or '').strip(): float(q or 0) for m, q in out_rows if str(m or '').strip()}
    returned = {str(m or '').strip(): float(q or 0) for m, q in in_rows if str(m or '').strip()}
    return {
        k: max(0.0, float(v or 0) - float(returned.get(k, 0) or 0))
        for k, v in delivered.items()
    }


def _returnable_qty_for_name(qty_map, name):
    """Look up returnable qty using exact name, then case, then normalized key.

    The public maps stay keyed by the stored Entry.material text so existing
    callers are unchanged. Validation uses this helper so the same Material
    Master record is not split across spelling variants.
    """
    if not qty_map:
        return 0.0
    exact = str(name or '').strip()
    if not exact:
        return 0.0
    if exact in qty_map:
        return float(qty_map.get(exact) or 0)
    lower = exact.lower()
    for key, qty in qty_map.items():
        if str(key or '').strip().lower() == lower:
            return float(qty or 0)

    def _norm(value):
        txt = (value or '').strip().lower()
        return re.sub(r'[^a-z0-9]+', '', txt)

    needle = _norm(exact)
    if not needle:
        return 0.0
    total = 0.0
    matched = False
    for key, qty in qty_map.items():
        if _norm(key) == needle:
            total += float(qty or 0)
            matched = True
    return total if matched else 0.0


def _parse_transaction_return_type(form, default='normal'):
    """Read the transaction-level Return Type. Per-item values cannot diverge."""
    submitted = [
        str(v or '').strip().lower()
        for v in form.getlist('return_type')
        if str(v or '').strip()
    ]
    if submitted:
        unique = set(submitted)
        if len(unique) > 1:
            raise ValueError(
                'Return Type must be the same for every selected item in this return.'
            )
        raw = submitted[0]
    else:
        raw = (form.get('return_type') or default or 'normal').strip().lower()
    if not raw:
        raw = default or 'normal'
    if raw not in ('normal', 'booked'):
        raise ValueError('Select a valid Return Type (Normal or Booked).')
    return raw


def _infer_driver_name_from_refs(refs, allow_booking=False):
    if not refs:
        return ''
    q = Entry.query.filter(
        Entry.bill_no.in_(refs),
        Entry.is_void == False,
        Entry.driver_name.isnot(None)
    )
    if allow_booking:
        q = q.filter(or_(
            Entry.nimbus_no == 'Direct Sale',
            Entry.nimbus_no == 'Booking Delivery',
            Entry.client_category == 'Booking Delivery'
        ))
    else:
        q = q.filter(Entry.nimbus_no == 'Direct Sale')
    row = q.order_by(Entry.id.desc()).first()
    return (row.driver_name or '').strip() if row else ''


