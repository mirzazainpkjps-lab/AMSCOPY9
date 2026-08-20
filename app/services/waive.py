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
from app.services.sales_core import (
    _direct_sale_default_bill_ref,
    normalize_sale_category,
)
from app.services.time_money import (
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

def _client_waive_off_total(client_name_norm, cutoff_dt=None):
    """Return total waive-off(loss) for a client, with legacy fallback from Payment.discount."""
    if not client_name_norm:
        return 0.0

    waive_q = WaiveOff.query.filter(
        func.lower(func.trim(WaiveOff.client_name)) == client_name_norm,
        WaiveOff.is_void == False
    )
    # DirectSale discounts are accounted from DirectSale.discount separately.
    waive_q = waive_q.filter(
        ~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%')
    )
    # Ignore orphan rows that reference a deleted payment.
    waive_q = waive_q.filter(
        or_(
            WaiveOff.payment_id.is_(None),
            exists().where(Payment.id == WaiveOff.payment_id)
        )
    )
    if cutoff_dt:
        waive_q = waive_q.filter(WaiveOff.date_posted <= cutoff_dt)
    waive_total = float(waive_q.with_entities(func.sum(WaiveOff.amount)).scalar() or 0)

    represented_payment_ids = {
        r[0] for r in waive_q.filter(WaiveOff.payment_id.isnot(None))
        .with_entities(WaiveOff.payment_id).distinct().all()
        if r and r[0] is not None
    }

    legacy_payments_q = Payment.query.filter(
        func.lower(func.trim(Payment.client_name)) == client_name_norm,
        Payment.is_void == False,
        Payment.discount > 0
    )
    if cutoff_dt:
        legacy_payments_q = legacy_payments_q.filter(Payment.date_posted <= cutoff_dt)

    legacy_total = 0.0
    for p in legacy_payments_q.all():
        if p.id in represented_payment_ids:
            continue
        legacy_total += float(p.discount or 0)

    return waive_total + legacy_total


def _sync_payment_waive_off(payment):
    """Keep dedicated waive_off rows in sync with Payment.discount for phase-2 rollout."""
    if not payment:
        return

    amount = float(getattr(payment, 'discount', 0) or 0)
    existing_rows = WaiveOff.query.filter_by(payment_id=payment.id).all()

    if amount <= 0:
        for row in existing_rows:
            db.session.delete(row)
        return

    client_obj = db.session.get(Client, payment.client_id) if getattr(payment, 'client_id', None) else get_client_by_input(payment.client_name or '')
    bill_ref = (payment.manual_bill_no or payment.auto_bill_no or f"PAY-{payment.id}")
    reason = (payment.discount_reason or '').strip() or 'Payment waive-off (loss)'
    now_dt = pk_now()
    waive_dt = payment.date_posted or now_dt

    if existing_rows:
        row = existing_rows[0]
        changed = (
            abs(float(row.amount or 0) - amount) > 0.0001
            or (row.reason or '').strip() != reason
            or bool(row.is_void) != bool(payment.is_void)
        )
        row.client_code = (client_obj.code if client_obj else row.client_code)
        row.client_name = (client_obj.name if client_obj else payment.client_name)
        row.bill_no = bill_ref
        row.amount = amount
        row.reason = reason
        row.date_posted = (now_dt if changed else (row.date_posted or waive_dt))
        row.note = payment.note
        row.is_void = bool(payment.is_void)
        for extra in existing_rows[1:]:
            db.session.delete(extra)
    else:
        db.session.add(WaiveOff(
            payment_id=payment.id,
            client_code=(client_obj.code if client_obj else None),
            client_name=(client_obj.name if client_obj else payment.client_name),
            bill_no=bill_ref,
            amount=amount,
            reason=reason,
            date_posted=waive_dt,
            created_by=(current_user.username if current_user and current_user.is_authenticated else None),
            note=payment.note,
            is_void=bool(payment.is_void)
        ))


def _direct_sale_waive_marker(sale_id):
    return f"[direct_sale_discount:{sale_id}]"


def _sync_direct_sale_waive_off(sale):
    """Keep dedicated waive_off rows in sync with DirectSale.discount."""
    if not sale:
        return

    marker = _direct_sale_waive_marker(sale.id)
    existing_rows = WaiveOff.query.filter(
        WaiveOff.payment_id.is_(None),
        WaiveOff.note == marker
    ).all()

    amount = max(0.0, float(getattr(sale, 'discount', 0) or 0))
    if amount <= 0:
        for row in existing_rows:
            db.session.delete(row)
        return

    client_name = (getattr(sale, 'client_name', '') or '').strip()
    client_obj = get_client_by_input(client_name) if client_name else None
    client_code = client_obj.code if client_obj else (OPEN_KHATA_CODE if normalize_sale_category(getattr(sale, 'category', None)) == 'Open Khata' else None)
    client_display_name = client_obj.name if client_obj else client_name
    bill_ref = _direct_sale_default_bill_ref(sale)
    reason = (getattr(sale, 'discount_reason', '') or '').strip() or 'Direct sale waive-off (loss)'

    created_by = None
    try:
        if current_user and current_user.is_authenticated:
            created_by = current_user.username
    except Exception:
        created_by = None

    if existing_rows:
        row = existing_rows[0]
        row.client_code = client_code
        row.client_name = client_display_name
        row.bill_no = bill_ref
        row.amount = amount
        row.reason = reason
        row.date_posted = sale.date_posted or pk_now()
        row.note = marker
        row.is_void = bool(sale.is_void)
        for extra in existing_rows[1:]:
            db.session.delete(extra)
    else:
        db.session.add(WaiveOff(
            payment_id=None,
            client_code=client_code,
            client_name=client_display_name,
            bill_no=bill_ref,
            amount=amount,
            reason=reason,
            date_posted=sale.date_posted or pk_now(),
            created_by=created_by,
            note=marker,
            is_void=bool(sale.is_void)
        ))


