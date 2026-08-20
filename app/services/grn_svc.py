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
from app.services.accounting import (
    _sync_supplier_payment_accounting,
)
from app.services.time_money import (
    pk_now,
    pk_today,
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

def calculate_grn_total(grn):
    item_total = sum(
        (item.qty or 0) * (item.price_at_time or 0)
        for item in (grn.items or [])
        if not bool(getattr(item, 'is_void', False))
    )
    expenses = (grn.loading_cost or 0) + (grn.freight_cost or 0) + (grn.other_expense or 0)
    tax = (grn.tax_amount or 0)
    discount = (grn.discount or 0)
    adjustment = (grn.adjustment_amount or 0)
    return item_total + expenses + tax - discount + adjustment


def _grn_bill_ref(grn):
    return (getattr(grn, 'manual_bill_no', None) or getattr(grn, 'auto_bill_no', None) or f"GRN-{getattr(grn, 'id', '')}").strip()


def _grn_auto_payment_note(grn):
    return f"[AUTO_GRN_PAY:{grn.id}] Auto-payment for GRN #{_grn_bill_ref(grn)}"


def _find_grn_auto_supplier_payment(grn):
    if not grn:
        return None
    marker = f"[auto_grn_pay:{grn.id}]"
    q = SupplierPayment.query.filter_by(is_void=False)
    if getattr(grn, 'supplier_id', None):
        q = q.filter(SupplierPayment.supplier_id == grn.supplier_id)
    marker_row = q.filter(
        func.lower(func.coalesce(SupplierPayment.note, '')).like(f"{marker}%")
    ).order_by(SupplierPayment.id.desc()).first()
    if marker_row:
        return marker_row

    # Legacy fallback for rows created before marker support.
    bill_ref = _grn_bill_ref(grn).lower()
    if not bill_ref:
        return None
    legacy_row = q.filter(
        func.lower(func.coalesce(SupplierPayment.note, '')).like(f"auto-payment for grn #{bill_ref}%")
    ).order_by(SupplierPayment.id.desc()).first()
    return legacy_row


def _sync_grn_auto_supplier_payment(grn, old_supplier_id=None):
    if not grn:
        return
    # If supplier changed, void old supplier's auto row (if any).
    if old_supplier_id and old_supplier_id != getattr(grn, 'supplier_id', None):
        old_marker = f"[auto_grn_pay:{grn.id}]"
        old_row = SupplierPayment.query.filter(
            SupplierPayment.is_void == False,
            SupplierPayment.supplier_id == old_supplier_id,
            func.lower(func.coalesce(SupplierPayment.note, '')).like(f"{old_marker}%")
        ).order_by(SupplierPayment.id.desc()).first()
        if old_row:
            old_row.is_void = True

    row = _find_grn_auto_supplier_payment(grn)
    paid = max(0.0, float(getattr(grn, 'paid_amount', 0) or 0))

    if not getattr(grn, 'supplier_id', None) or paid <= 0:
        if row:
            row.is_void = True
            _sync_supplier_payment_accounting(row)
        return

    if not row:
        row = SupplierPayment(
            supplier_id=grn.supplier_id,
            is_void=False
        )
        db.session.add(row)
        db.session.flush()
    row.is_void = False
    row.supplier_id = grn.supplier_id
    row.payment_type = 'Payment'
    row.source_type = 'GRN'
    row.source_id = grn.id
    row.amount = paid
    row.method = (grn.payment_type or row.method or 'Cash')
    row.date_posted = grn.date_posted or pk_now()
    row.note = _grn_auto_payment_note(grn)
    row.bank_name = grn.bank_name or ''
    row.account_name = grn.account_name or ''
    row.account_no = grn.account_no or ''
    row.payment_account_id = getattr(grn, 'payment_account_id', None)

    _sync_supplier_payment_accounting(row)


def _is_grn_backdate_restricted_user():
    if not current_user.is_authenticated:
        return False
    if current_user.role in ('admin', 'root'):
        return False
    return bool(getattr(current_user, 'restrict_backdated_edit', False))


def _enforce_grn_backdate_policy(grn_dt, action_label, redirect_endpoint='grn', **redirect_kwargs):
    if not _is_grn_backdate_restricted_user():
        return None
    if not grn_dt:
        return None
    grn_date = grn_dt.date() if isinstance(grn_dt, datetime) else grn_dt
    if grn_date < pk_today():
        flash(f'{action_label} blocked: back-dated GRN edits are restricted for your account.', 'danger')
        return redirect(url_for(redirect_endpoint, **redirect_kwargs))
    return None


