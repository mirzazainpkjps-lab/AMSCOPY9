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
    _parse_dt_safe,
    _to_float_or_zero,
    pk_now,
)
from app.services.waive import (
    _client_waive_off_total,
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

def _resolve_opening_balance_date(date_str=None, fallback_dt=None):
    """Normalize opening-balance date from form input with stable fallback."""
    if not date_str:
        return fallback_dt or pk_now()
    try:
        return datetime.strptime(str(date_str).strip(), '%Y-%m-%d')
    except Exception:
        return fallback_dt or pk_now()


def _compute_client_financial_summary(client):
    """Return a lightweight financial summary for decision-making (balance, totals)."""
    if not client:
        return {
            'balance': 0,
            'debit_total': 0,
            'credit_total': 0,
            'cash_received_total': 0,
            'waive_off_total': 0,
            'status': 'settled'
        }
    client_name_norm = (client.name or '').strip().lower()

    b_debit = db.session.query(func.sum(Booking.amount)).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).scalar() or 0
    b_credit = db.session.query(func.sum(Booking.paid_amount)).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).scalar() or 0
    payment_party_filter = or_(
        Payment.client_id == client.id,
        and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm),
    )
    p_credit = db.session.query(func.sum(Payment.amount)).filter(
        payment_party_filter, Payment.is_void == False, Payment.amount >= 0
    ).scalar() or 0
    p_debit = db.session.query(func.sum(-Payment.amount)).filter(
        payment_party_filter, Payment.is_void == False, Payment.amount < 0
    ).scalar() or 0
    ds_debit = db.session.query(func.sum(DirectSale.amount)).filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
        DirectSale.is_void == False
    ).scalar() or 0
    ds_credit = db.session.query(func.sum(DirectSale.paid_amount)).filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
        DirectSale.is_void == False
    ).scalar() or 0
    
    b_discount = 0
    try:
        b_discount = db.session.query(func.sum(Booking.discount)).filter(
            func.lower(func.trim(Booking.client_name)) == client_name_norm,
            Booking.is_void == False
        ).scalar() or 0
    except Exception:
        pass

    p_discount = 0
    try:
        p_discount = _client_waive_off_total(client_name_norm)
    except Exception:
        pass


    ds_discount = 0
    try:
        ds_discount = db.session.query(func.sum(DirectSale.discount)).filter(
            func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
            DirectSale.is_void == False
        ).scalar() or 0
    except Exception:
        pass

    opening_balance = _to_float_or_zero(getattr(client, 'opening_balance', 0))
    opening_debit = opening_balance if opening_balance > 0 else 0
    opening_credit = abs(opening_balance) if opening_balance < 0 else 0

    debit_total = (opening_debit + b_debit + ds_debit + p_debit)
    cash_received_total = (opening_credit + b_credit + p_credit + ds_credit)
    waive_off_total = (ds_discount + b_discount + p_discount)
    credit_total = cash_received_total + waive_off_total
    balance = debit_total - credit_total
    status = 'debit' if balance > 0 else ('credit' if balance < 0 else 'settled')

    return {
        'balance': balance,
        'debit_total': debit_total,
        'credit_total': credit_total,
        'cash_received_total': cash_received_total,
        'waive_off_total': waive_off_total,
        'status': status
    }


def _parse_ledger_entry_dt(date_val, time_val=None):
    """Parse Entry(date,time) to datetime for stable ledger ordering."""
    if isinstance(date_val, datetime):
        return date_val
    if isinstance(date_val, date):
        return datetime.combine(date_val, datetime.min.time())
    s_date = str(date_val or '').strip()
    s_time = str(time_val or '').strip()
    try:
        if s_date and s_time:
            return datetime.strptime(f"{s_date} {s_time}", '%Y-%m-%d %H:%M:%S')
    except Exception:
        pass
    try:
        if s_date:
            return datetime.strptime(s_date, '%Y-%m-%d')
    except Exception:
        pass
    return datetime.min


def _parse_cancel_amount_from_note(note):
    """Extract cancellation amount encoded in note as 'amount=<number>'."""
    text_note = str(note or '')
    m = re.search(r'amount=([-+]?\d+(?:\.\d+)?)', text_note, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _parse_cancel_rate_from_note(note):
    """Extract cancellation rate encoded in note as 'rate=<number>'."""
    text_note = str(note or '')
    m = re.search(r'rate=([-+]?\d+(?:\.\d+)?)', text_note, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _resolve_cancel_display_amount(client_name_norm, bill_ref, mat_ref, qty, note):
    """Best-effort cancel amount for ledger display.

    Priority:
    1) Matched booking item rate (case-insensitive material match).
    2) Encoded note rate.
    3) Encoded note amount (legacy fallback).
    """
    try:
        q = float(qty or 0)
    except Exception:
        q = 0
    if q <= 0:
        return None

    bill = (bill_ref or '').strip()
    mat = (mat_ref or '').strip()

    if bill and mat:
        bk = Booking.query.filter(
            func.lower(func.trim(Booking.client_name)) == client_name_norm,
            or_(Booking.manual_bill_no == bill, Booking.auto_bill_no == bill)
        ).order_by(Booking.id.desc()).first()
        if bk:
            bi = BookingItem.query.filter(
                BookingItem.booking_id == bk.id,
                func.lower(func.trim(BookingItem.material_name)) == mat.lower()
            ).order_by(BookingItem.id.desc()).first()
            if bi:
                try:
                    unit_rate = float(getattr(bi, 'price_at_time', 0) or 0)
                except Exception:
                    unit_rate = 0
                if unit_rate > 0:
                    return unit_rate * q

    parsed_rate = _parse_cancel_rate_from_note(note)
    if parsed_rate is not None:
        return float(parsed_rate) * q

    return _parse_cancel_amount_from_note(note)


def _booking_ledger_gross_due(booking, cancel_value=0.0, allow_legacy_lift=True):
    """Booking due displayed in ledger (before discount row).

    Legacy-safe rule:
    - Prefer stored booking amount.
    - Lift only clearly broken legacy rows where stored amount is lower than paid.
      This preserves historical recoveries without canceling valid discount credits.
    """
    amount = float(getattr(booking, 'amount', 0) or 0) + max(0.0, float(cancel_value or 0))
    paid = float(getattr(booking, 'paid_amount', 0) or 0)
    discount = max(0.0, float(getattr(booking, 'discount', 0) or 0))
    if allow_legacy_lift and discount > 0 and amount < paid:
        corrected_due = paid + discount
        if corrected_due > amount:
            return corrected_due
    return amount


def _pending_client_key(client_code, client_name):
    code = (client_code or '').strip()
    if code:
        return f"code:{code}"
    return f"name:{(client_name or '').strip().lower()}"


def _pending_cancel_credit_for_client(client_code, client_name):
    """Ledger-aligned cancel credit pool for a client (from active CANCEL entries)."""
    code = (client_code or '').strip()
    name_norm = (client_name or '').strip().lower()
    if not code and not name_norm:
        return 0.0

    filt = []
    if code:
        filt.append(Entry.client_code == code)
    if name_norm:
        filt.append(func.lower(func.trim(Entry.client)) == name_norm)
    if not filt:
        return 0.0

    cancel_rows = Entry.query.filter(
        or_(*filt),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).all()

    total_credit = 0.0
    for ce in cancel_rows:
        qty = float(getattr(ce, 'qty', 0) or 0)
        bill_ref = (getattr(ce, 'bill_no', None) or getattr(ce, 'auto_bill_no', None) or '').strip()
        mat_ref = (getattr(ce, 'material', None) or getattr(ce, 'booked_material', None) or '').strip()
        amt = _resolve_cancel_display_amount(
            client_name_norm=name_norm,
            bill_ref=bill_ref,
            mat_ref=mat_ref,
            qty=qty,
            note=getattr(ce, 'note', None)
        )
        if amt is not None and float(amt) > 0:
            total_credit += float(amt)
    return total_credit


def _compute_pending_effective_amount_map(pending_rows):
    """
    Compute ledger-aligned effective pending amounts by allocating booking-cancel
    credit against unpaid pending bills per client (oldest first).
    """
    if not pending_rows:
        return {}

    effective = {}
    groups = {}
    for pb in pending_rows:
        raw_amt = float(getattr(pb, 'amount', 0) or 0)
        key = _pending_client_key(getattr(pb, 'client_code', ''), getattr(pb, 'client_name', ''))
        groups.setdefault(key, []).append(pb)
        # default fallback
        effective[pb.id] = raw_amt

    for rows in groups.values():
        unpaid_rows = [
            r for r in rows
            if (not bool(getattr(r, 'is_void', False))) and (not bool(getattr(r, 'is_paid', False)))
        ]
        if not unpaid_rows:
            continue

        sample = unpaid_rows[0]
        cancel_credit_pool = _pending_cancel_credit_for_client(
            getattr(sample, 'client_code', ''),
            getattr(sample, 'client_name', '')
        )
        if cancel_credit_pool <= 0:
            continue

        # Apply to oldest unpaid first to mirror running-balance reduction.
        for pb in sorted(unpaid_rows, key=lambda x: int(getattr(x, 'id', 0) or 0)):
            amt = max(0.0, float(getattr(pb, 'amount', 0) or 0))
            if cancel_credit_pool <= 0:
                effective[pb.id] = amt
                continue
            applied = min(amt, cancel_credit_pool)
            effective[pb.id] = amt - applied
            cancel_credit_pool -= applied

    return effective


def _invoice_cutoff_dt(invoice_obj):
    if not invoice_obj:
        return None
    if getattr(invoice_obj, 'date', None):
        return datetime.combine(invoice_obj.date, datetime.max.time())
    created_at = getattr(invoice_obj, 'created_at', None)
    if created_at:
        dt = _parse_dt_safe(created_at)
        if dt:
            return dt
    inv_no = getattr(invoice_obj, 'invoice_no', None) or ''
    if isinstance(inv_no, str) and inv_no.startswith('INV-') and len(inv_no) >= 18:
        suffix = inv_no[4:]
        try:
            return datetime.strptime(suffix, '%Y%m%d%H%M%S')
        except ValueError:
            return None
    return None


def _client_balance_as_of(client_obj, cutoff_dt=None):
    """Return the unified client-ledger balance up to ``cutoff_dt``.

    The local import keeps the historical service dependency graph acyclic.
    Bill views, receipts, payables and the client ledger therefore use the same
    read-side projection rather than four independent formulas.
    """
    if not client_obj:
        return 0.0
    try:
        from app.services.financial_ledgers import build_client_financial_ledger
        ledger = build_client_financial_ledger(client_obj)
        if not cutoff_dt:
            return float(ledger.get('closing_balance') or 0)
        from decimal import Decimal
        balance = Decimal('0.00')
        for row in ledger.get('rows', []):
            row_dt = row.get('date')
            if row_dt is None or row_dt == datetime.min or row_dt <= cutoff_dt:
                balance += Decimal(str(row.get('debit') or 0)) - Decimal(str(row.get('credit') or 0))
        return float(balance)
    except Exception:
        # Preserve the legacy fallback for a partially upgraded database while
        # schema bootstrap is in progress.  It is intentionally below the
        # authoritative path and does not mutate any rows.
        pass

    opening_effect = 0.0
    opening_balance = _to_float_or_zero(getattr(client_obj, 'opening_balance', 0))
    if opening_balance != 0:
        opening_dt = (
            _parse_dt_safe(getattr(client_obj, 'opening_balance_date', None))
            or _parse_dt_safe(getattr(client_obj, 'created_at', None))
            or datetime.min
        )
        if (not cutoff_dt) or (opening_dt <= cutoff_dt):
            opening_effect = opening_balance

    client_name_norm = (client_obj.name or '').strip().lower()

    booking_q = Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    )
    payment_q = Payment.query.filter(
        or_(Payment.client_id == client_obj.id,
            and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm)),
        Payment.is_void == False
    )
    sale_q = DirectSale.query.filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
        DirectSale.is_void == False
    )

    if cutoff_dt:
        booking_q = booking_q.filter(Booking.date_posted <= cutoff_dt)
        payment_q = payment_q.filter(Payment.date_posted <= cutoff_dt)
        sale_q = sale_q.filter(DirectSale.date_posted <= cutoff_dt)

    b_debit = booking_q.with_entities(func.sum(Booking.amount)).scalar() or 0
    b_credit = booking_q.with_entities(func.sum(Booking.paid_amount)).scalar() or 0
    p_credit = payment_q.with_entities(func.sum(Payment.amount)).scalar() or 0
    # p_credit is derived from Payment rows. Do not apply summary-layer adjustments here.
    ds_debit = sale_q.with_entities(func.sum(DirectSale.amount)).scalar() or 0
    ds_credit = sale_q.with_entities(func.sum(DirectSale.paid_amount)).scalar() or 0

    b_discount = booking_q.with_entities(func.sum(Booking.discount)).scalar() or 0
    p_discount = _client_waive_off_total(client_name_norm, cutoff_dt=cutoff_dt)
    ds_discount = sale_q.with_entities(func.sum(DirectSale.discount)).scalar() or 0

    movement_effect = (b_debit + ds_debit) - (b_credit + p_credit + ds_credit + ds_discount + b_discount + p_discount)
    return float(opening_effect + movement_effect)


def _bill_cutoff_dt_for_snapshot(booking=None, payment=None, invoice=None, sale=None, pending=None):
    """Resolve bill datetime for historical pending snapshot."""
    if booking:
        return _parse_dt_safe(getattr(booking, 'date_posted', None))
    if payment:
        return _parse_dt_safe(getattr(payment, 'date_posted', None))
    if sale:
        return _parse_dt_safe(getattr(sale, 'date_posted', None))
    if invoice:
        return _invoice_cutoff_dt(invoice)
    if pending:
        return _parse_dt_safe(getattr(pending, 'created_at', None))
    return None


