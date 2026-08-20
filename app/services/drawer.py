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

def _drawer_method_normalized(method):
    return (method or '').strip().lower()


def _drawer_is_cash_method(method):
    return _drawer_method_normalized(method) in ['cash', 'cash sale']


def _drawer_parse_date(raw):
    txt = str(raw or '').strip()
    if not txt:
        return None
    try:
        return datetime.strptime(txt, '%Y-%m-%d').date()
    except Exception:
        return None


def _drawer_upsert_category(name):
    clean = str(name or '').strip()
    if not clean:
        return None
    existing = FbmCashDrawerCategory.query.filter(
        func.lower(func.trim(FbmCashDrawerCategory.name)) == clean.lower()
    ).first()
    if existing:
        return existing
    row = FbmCashDrawerCategory(name=clean)
    db.session.add(row)
    db.session.flush()
    return row


def _drawer_filtered_queries(date_from=None, date_to=None):
    cash_method_clauses = [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]
    client_cash_q = Payment.query.filter(
        Payment.is_void == False,
        or_(*cash_method_clauses)
    )
    cash_sale_q = DirectSale.query.filter(
        DirectSale.is_void == False,
        or_(
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash sale',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash sale',
        )
    )
    drawer_entries_q = FbmCashDrawerEntry.query.filter(FbmCashDrawerEntry.is_void == False)

    if date_from:
        client_cash_q = client_cash_q.filter(func.date(Payment.date_posted) >= date_from)
        cash_sale_q = cash_sale_q.filter(func.date(DirectSale.date_posted) >= date_from)
        drawer_entries_q = drawer_entries_q.filter(func.date(FbmCashDrawerEntry.date_posted) >= date_from)
    if date_to:
        client_cash_q = client_cash_q.filter(func.date(Payment.date_posted) <= date_to)
        cash_sale_q = cash_sale_q.filter(func.date(DirectSale.date_posted) <= date_to)
        drawer_entries_q = drawer_entries_q.filter(func.date(FbmCashDrawerEntry.date_posted) <= date_to)
    return client_cash_q, cash_sale_q, drawer_entries_q


def _drawer_kpis(date_from=None, date_to=None):
    client_cash_q, cash_sale_q, drawer_entries_q = _drawer_filtered_queries(date_from=date_from, date_to=date_to)
    sale_paid_expr = case((DirectSale.paid_amount > 0, DirectSale.paid_amount), else_=DirectSale.amount)

    client_cash_received = float(client_cash_q.with_entities(func.sum(Payment.amount)).scalar() or 0)
    sale_cash_received = float(cash_sale_q.with_entities(func.sum(sale_paid_expr)).scalar() or 0)

    manual_cash_in = 0.0
    manual_cash_out = 0.0
    ignored_non_cash_entries = 0
    for row in drawer_entries_q.all():
        if not _drawer_is_cash_method(row.method):
            ignored_non_cash_entries += 1
            continue
        amount = float(row.amount or 0)
        if (row.entry_type or '').strip().lower() == 'in':
            manual_cash_in += amount
        else:
            manual_cash_out += amount

    total_cash_in = client_cash_received + sale_cash_received + manual_cash_in
    total_cash_out = manual_cash_out
    return {
        'client_cash_received': client_cash_received,
        'sale_cash_received': sale_cash_received,
        'manual_cash_in': manual_cash_in,
        'manual_cash_out': manual_cash_out,
        'total_cash_in': total_cash_in,
        'total_cash_out': total_cash_out,
        'current_balance': total_cash_in - total_cash_out,
        'ignored_non_cash_entries': ignored_non_cash_entries,
    }


def _drawer_unified_rows(date_from=None, date_to=None, source='all', flow='all', category_filter=''):
    client_cash_q, cash_sale_q, drawer_entries_q = _drawer_filtered_queries(date_from=date_from, date_to=date_to)
    source = (source or 'all').strip().lower()
    flow = (flow or 'all').strip().lower()
    category_filter = str(category_filter or '').strip().lower()
    rows = []

    wants_client = source in ['all', 'client_payments']
    wants_sales = source in ['all', 'material_sales']
    wants_manual = source in ['all', 'manual']
    wants_in = flow in ['all', 'in']
    wants_out = flow in ['all', 'out']

    if wants_client and wants_in:
        for p in client_cash_q.all():
            category = 'Client Payment'
            if category_filter and category.lower() != category_filter:
                continue
            bill_ref = (p.manual_bill_no or p.auto_bill_no or f'PAY-{p.id}')
            rows.append({
                'source': 'client_payments',
                'entry_type': 'in',
                'method': p.method or 'Cash',
                'category': category,
                'amount': float(p.amount or 0),
                'note': p.note or '',
                'date_posted': p.date_posted,
                'label': p.client_name or '',
                'ref': bill_ref,
                'editable': False,
                'id': p.id,
            })

    if wants_sales and wants_in:
        for s in cash_sale_q.all():
            category = 'Material Sold Cash'
            if category_filter and category.lower() != category_filter:
                continue
            paid = float(s.paid_amount or 0)
            amount = paid if paid > 0 else float(s.amount or 0)
            bill_ref = (s.manual_bill_no or s.auto_bill_no or f'SALE-{s.id}')
            rows.append({
                'source': 'material_sales',
                'entry_type': 'in',
                'method': 'Cash',
                'category': category,
                'amount': amount,
                'note': s.note or '',
                'date_posted': s.date_posted,
                'label': s.client_name or '',
                'ref': bill_ref,
                'editable': False,
                'id': s.id,
            })

    if wants_manual:
        if flow in ['in', 'out']:
            drawer_entries_q = drawer_entries_q.filter(
                func.lower(func.trim(func.coalesce(FbmCashDrawerEntry.entry_type, ''))) == flow
            )
        if category_filter:
            drawer_entries_q = drawer_entries_q.filter(
                func.lower(func.trim(func.coalesce(FbmCashDrawerEntry.category, ''))) == category_filter
            )
        for r in drawer_entries_q.all():
            r_type = (r.entry_type or 'out').strip().lower()
            if r_type == 'in' and not wants_in:
                continue
            if r_type == 'out' and not wants_out:
                continue
            rows.append({
                'source': 'manual',
                'entry_type': r_type,
                'method': r.method or 'Cash',
                'category': r.category or '',
                'amount': float(r.amount or 0),
                'note': r.note or '',
                'date_posted': r.date_posted,
                'label': r.created_by or '',
                'ref': f'MAN-{r.id}',
                'editable': True,
                'id': r.id,
                'row': r,
            })

    rows.sort(key=lambda x: ((x.get('date_posted') or datetime.min), x.get('id') or 0), reverse=True)
    return rows


