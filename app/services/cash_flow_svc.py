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
    _money_round,
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

def _cash_flow_net_between(start_date=None, end_date=None):
    """Net cash movement for the same transaction sources shown in Cash Flow."""
    if end_date and start_date and start_date > end_date:
        return 0.0

    def _date_filters(column):
        filters = []
        if start_date:
            filters.append(func.date(column) >= start_date.strftime('%Y-%m-%d'))
        if end_date:
            filters.append(func.date(column) <= end_date.strftime('%Y-%m-%d'))
        return filters

    cash_method_clauses = [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]
    payment_in = float(Payment.query.filter(
        Payment.is_void == False,
        or_(*cash_method_clauses),
        *_date_filters(Payment.date_posted)
    ).with_entities(func.sum(Payment.amount)).scalar() or 0)

    sale_in = float(DirectSale.query.filter(
        DirectSale.is_void == False,
        or_(
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash sale',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash sale',
        ),
        DirectSale.paid_amount > 0,
        *_date_filters(DirectSale.date_posted)
    ).with_entities(func.sum(DirectSale.paid_amount)).scalar() or 0)

    supplier_out = float(SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        *_date_filters(SupplierPayment.date_posted)
    ).with_entities(func.sum(SupplierPayment.amount)).scalar() or 0)

    account_in = 0.0
    account_out = 0.0

    account_txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment', 'Receipt']),
        *_date_filters(AccountTransaction.date_posted)
    ).all()
    account_cache = {}
    for tx in account_txs:
        amount = float(tx.amount or 0)
        note_u = (tx.note or '').upper()
        if any(marker in note_u for marker in (
            '[SRC:BOOKING:',
            '[SRC:DIRECTSALE:',
            '[SRC:PAYMENT:',
            '[SRC:SUPPLIERPAYMENT:',
            '[SRC:CLIENTREFUND:',
        )):
            continue
        if tx.transaction_type == 'Receipt' and tx.to_account_id is not None:
            if tx.to_account_id not in account_cache:
                account_cache[tx.to_account_id] = Account.query.get(tx.to_account_id)
            acc = account_cache.get(tx.to_account_id)
            if acc and (acc.category or '').lower() in ('cash', 'bank'):
                account_in += amount
            continue
        if tx.transaction_type in ['Expense', 'Payment', 'Driver Payment'] and tx.from_account_id is not None:
            if tx.from_account_id not in account_cache:
                account_cache[tx.from_account_id] = Account.query.get(tx.from_account_id)
            acc = account_cache.get(tx.from_account_id)
            if acc and (acc.category or '').lower() in ('cash', 'bank'):
                account_out += amount

    return payment_in + sale_in + account_in - supplier_out - account_out


def _legacy_adjustments_total(start_date=None, end_date=None):
    query = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.physical_cash_available.is_(None)
    )
    if start_date:
        query = query.filter(CashFlowDifferenceAdjustment.adjustment_date >= start_date)
    if end_date:
        query = query.filter(CashFlowDifferenceAdjustment.adjustment_date <= end_date)
    return float(query.with_entities(func.coalesce(func.sum(CashFlowDifferenceAdjustment.amount), 0)).scalar() or 0)


def _cash_flow_in_out_between(start_date, end_date):
    """Cash-in and cash-out totals for the history page using cash-flow sources."""
    if end_date and start_date and start_date > end_date:
        return 0.0, 0.0

    def _date_filters(column):
        filters = []
        if start_date:
            filters.append(func.date(column) >= start_date.strftime('%Y-%m-%d'))
        if end_date:
            filters.append(func.date(column) <= end_date.strftime('%Y-%m-%d'))
        return filters

    cash_method_clauses = [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]
    cash_in = float(Payment.query.filter(
        Payment.is_void == False,
        or_(*cash_method_clauses),
        *_date_filters(Payment.date_posted)
    ).with_entities(func.sum(Payment.amount)).scalar() or 0)
    cash_in += float(DirectSale.query.filter(
        DirectSale.is_void == False,
        or_(
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash sale',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash sale',
        ),
        DirectSale.paid_amount > 0,
        *_date_filters(DirectSale.date_posted)
    ).with_entities(func.sum(DirectSale.paid_amount)).scalar() or 0)
    cash_out = float(SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        *_date_filters(SupplierPayment.date_posted)
    ).with_entities(func.sum(SupplierPayment.amount)).scalar() or 0)

    account_cache = {}
    for tx in AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment', 'Receipt']),
        *_date_filters(AccountTransaction.date_posted)
    ).all():
        amount = float(tx.amount or 0)
        note_u = (tx.note or '').upper()
        if any(marker in note_u for marker in (
            '[SRC:BOOKING:',
            '[SRC:DIRECTSALE:',
            '[SRC:PAYMENT:',
            '[SRC:SUPPLIERPAYMENT:',
            '[SRC:CLIENTREFUND:',
        )):
            continue
        if tx.transaction_type == 'Receipt' and tx.to_account_id is not None:
            if tx.to_account_id not in account_cache:
                account_cache[tx.to_account_id] = Account.query.get(tx.to_account_id)
            acc = account_cache.get(tx.to_account_id)
            if acc and (acc.category or '').lower() in ('cash', 'bank'):
                cash_in += amount
            continue
        if tx.transaction_type in ['Expense', 'Payment', 'Driver Payment'] and tx.from_account_id is not None:
            if tx.from_account_id not in account_cache:
                account_cache[tx.from_account_id] = Account.query.get(tx.from_account_id)
            acc = account_cache.get(tx.from_account_id)
            if acc and (acc.category or '').lower() in ('cash', 'bank'):
                cash_out += amount

    return cash_in, cash_out


def _automatic_cash_opening_balance(from_date_dt):
    previous_day = from_date_dt - timedelta(days=1)
    last_physical = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.adjustment_date < from_date_dt,
        CashFlowDifferenceAdjustment.physical_cash_available.isnot(None)
    ).order_by(CashFlowDifferenceAdjustment.adjustment_date.desc()).first()

    if last_physical:
        start_date = last_physical.adjustment_date + timedelta(days=1)
        opening = float(last_physical.physical_cash_available or 0)
        opening += _cash_flow_net_between(start_date, previous_day)
        opening -= _legacy_adjustments_total(start_date, previous_day)
        return opening

    opening = _cash_flow_net_between(None, previous_day)
    opening -= _legacy_adjustments_total(None, previous_day)
    return opening


def _current_username():
    return current_user.username if current_user and current_user.is_authenticated else None


def _cash_flow_today_opening_override(today_str):
    override = session.get('cash_flow_today_opening_override') or {}
    if override.get('date') != today_str:
        return None
    try:
        return _money_round(override.get('amount', 0))
    except Exception:
        return None


def _cash_flow_fresh_start_cutoff(today_str):
    cutoff = session.get('cash_flow_fresh_start_cutoff') or {}
    if cutoff.get('date') != today_str or not cutoff.get('at'):
        cutoff = {'date': today_str, 'at': pk_now().strftime('%Y-%m-%d %H:%M:%S')}
        session['cash_flow_fresh_start_cutoff'] = cutoff
    try:
        return datetime.strptime(cutoff['at'], '%Y-%m-%d %H:%M:%S')
    except Exception:
        cutoff = {'date': today_str, 'at': pk_now().strftime('%Y-%m-%d %H:%M:%S')}
        session['cash_flow_fresh_start_cutoff'] = cutoff
        return datetime.strptime(cutoff['at'], '%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------------
# Configuration-driven Cash Flow engine
# Rules live here. Business category names do not.
# ---------------------------------------------------------------------------

CF_DIR_IN = 'in'
CF_DIR_OUT = 'out'
CF_DIR_TRANSFER = 'transfer'
CF_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER)
CF_CAT_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, 'both')

SRC_MANUAL = 'MANUAL_CASH_FLOW'
SRC_CLIENT_PAYMENT = 'CLIENT_PAYMENT'
SRC_SUPPLIER_PAYMENT = 'SUPPLIER_PAYMENT'
SRC_SALE = 'SALE'
SRC_TRANSFER = 'TRANSFER'
SRC_DRIVER_PAYMENT = 'DRIVER_PAYMENT'
SRC_ACCOUNT_RECEIPT = 'ACCOUNT_RECEIPT'
SRC_ACCOUNT_EXPENSE = 'ACCOUNT_EXPENSE'

CF_SOURCE_LABELS = {
    SRC_MANUAL: 'Manual',
    SRC_CLIENT_PAYMENT: 'Client',
    SRC_SUPPLIER_PAYMENT: 'Supplier',
    SRC_SALE: 'Sale',
    SRC_TRANSFER: 'Transfer',
    SRC_DRIVER_PAYMENT: 'Driver',
    SRC_ACCOUNT_RECEIPT: 'Other system-generated',
    SRC_ACCOUNT_EXPENSE: 'Other system-generated',
}

CF_PARTY_TYPES = [
    ('person', 'Person'),
    ('outsider', 'Outsider / other party'),
    ('loan', 'Loan (person or outside lender)'),
    ('other', 'Other'),
]

CF_SRC_MARKER = '[SRC:CashFlow]'
_MIRROR_MARKERS = (
    '[SRC:BOOKING:',
    '[SRC:DIRECTSALE:',
    '[SRC:PAYMENT:',
    '[SRC:SUPPLIERPAYMENT:',
    '[SRC:CLIENTREFUND:',
)


def _cf_actor_name(actor=None):
    if actor is not None and getattr(actor, 'username', None):
        return actor.username
    return _current_username()


def _cf_is_money_account(account):
    if account is None:
        return False
    if getattr(account, 'is_active', True) is False:
        return False
    return (account.category or '').lower() in ('cash', 'bank')


def _cf_company_accounts(active_only=True):
    q = Account.query
    if active_only:
        q = q.filter(func.coalesce(Account.is_active, True) == True)
    rows = []
    for acc in q.order_by(Account.name.asc(), Account.id.asc()).all():
        if (acc.category or '').lower() in ('cash', 'bank'):
            rows.append(acc)
    return rows


def _cf_account_label(account):
    if account is None:
        return ''
    cat = (account.category or 'cash').upper()
    bal = _money_round(account.balance or 0)
    return f'{account.name} · {cat} · Rs. {bal:,.0f}'


def _cf_normalize_direction(value):
    raw = (value or '').strip().lower()
    if raw in ('received', 'receive', 'in', 'cash_in'):
        return CF_DIR_IN
    if raw in ('spent', 'spend', 'out', 'cash_out', 'paid'):
        return CF_DIR_OUT
    if raw in ('transfer', 'xfer'):
        return CF_DIR_TRANSFER
    return raw


def _cf_type_label(direction):
    return {
        CF_DIR_IN: 'Received',
        CF_DIR_OUT: 'Spent',
        CF_DIR_TRANSFER: 'Transfer',
    }.get(direction, direction or '')


def _cf_ensure_indexes():
    try:
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_flow_entry_idempotency_key '
            'ON cash_flow_entry(idempotency_key) WHERE idempotency_key IS NOT NULL'
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _cf_snapshot(entry):
    return {
        'id': entry.id,
        'direction': entry.direction,
        'amount': float(entry.amount or 0),
        'account_id': entry.account_id,
        'destination_account_id': getattr(entry, 'destination_account_id', None),
        'category_id': entry.category_id,
        'subcategory_id': entry.subcategory_id,
        'party_id': entry.party_id,
        'party_name': entry.party_name,
        'party_type': entry.party_type,
        'description': entry.description,
        'note': entry.note,
        'reference': getattr(entry, 'reference', None),
        'date_posted': str(entry.date_posted or ''),
        'is_void': bool(entry.is_void),
        'source_type': getattr(entry, 'source_type', None) or SRC_MANUAL,
    }


def _cf_write_audit(entry, action, before=None, after=None, reason=None, actor=None):
    db.session.add(CashFlowEntryAudit(
        entry_id=entry.id,
        action=action,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        reason=(reason or '').strip() or None,
        changed_by=_cf_actor_name(actor),
        changed_at=pk_now(),
    ))


def _cf_find_category(category_id=None, name=None, active_only=True):
    if category_id:
        cat = db.session.get(CashFlowCategory, category_id)
        if cat and (not active_only or cat.is_active):
            return cat
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == name.lower())
    if active_only:
        q = q.filter(CashFlowCategory.is_active == True)
    return q.first()


def _cf_find_subcategory(category, subcategory_id=None, name=None, active_only=True):
    if not category:
        return None
    if subcategory_id:
        sub = db.session.get(CashFlowSubcategory, subcategory_id)
        if sub and sub.category_id == category.id and (not active_only or sub.is_active):
            return sub
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == category.id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
    )
    if active_only:
        q = q.filter(CashFlowSubcategory.is_active == True)
    return q.first()


def _cf_find_party(party_id=None, name=None, party_type=None, active_only=True):
    if party_id:
        party = db.session.get(CashFlowParty, party_id)
        if party and (not active_only or party.is_active):
            return party
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowParty.query.filter(func.lower(CashFlowParty.name) == name.lower())
    if party_type:
        q = q.filter(func.lower(func.coalesce(CashFlowParty.party_type, '')) == party_type.lower())
    if active_only:
        q = q.filter(CashFlowParty.is_active == True)
    return q.first()


def save_cf_category(name, direction='both', notes=None, actor=None):
    name = (name or '').strip()
    direction = (direction or 'both').strip().lower()
    if not name:
        raise ValueError('Category name is required.')
    if direction not in CF_CAT_DIRECTIONS:
        raise ValueError('Category direction must be Received, Spent, or Both.')
    existing = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == name.lower()).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        existing.direction = direction
        if notes is not None:
            existing.notes = (notes or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    cat = CashFlowCategory(
        name=name, direction=direction, is_active=True,
        notes=(notes or '').strip() or None, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(cat)
    db.session.flush()
    return cat, True


def save_cf_subcategory(category_id, name, notes=None, actor=None):
    name = (name or '').strip()
    cat = db.session.get(CashFlowCategory, category_id) if category_id else None
    if not cat:
        raise ValueError('Pick a parent category.')
    if not name:
        raise ValueError('Sub-category name is required.')
    existing = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == cat.id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        if notes is not None:
            existing.notes = (notes or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    sub = CashFlowSubcategory(
        category_id=cat.id, name=name, is_active=True,
        notes=(notes or '').strip() or None, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(sub)
    db.session.flush()
    return sub, True


def save_cf_party(name, party_type='person', note=None, actor=None):
    name = (name or '').strip()
    party_type = (party_type or 'other').strip().lower() or 'other'
    if not name:
        raise ValueError('Name is required.')
    existing = _cf_find_party(name=name, party_type=party_type, active_only=False)
    if existing:
        existing.is_active = True
        if note is not None:
            existing.note = (note or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    party = CashFlowParty(
        name=name, party_type=party_type, note=(note or '').strip() or None,
        is_active=True, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(party)
    db.session.flush()
    return party, True


def disable_cf_category(category_id):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    cat.is_active = False
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def enable_cf_category(category_id):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    cat.is_active = True
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def disable_cf_subcategory(subcategory_id):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    sub.is_active = False
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def enable_cf_subcategory(subcategory_id):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    sub.is_active = True
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def disable_cf_party(party_id):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    party.is_active = False
    party.updated_at = pk_now()
    db.session.flush()
    return party


def enable_cf_party(party_id):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    party.is_active = True
    party.updated_at = pk_now()
    db.session.flush()
    return party


def update_cf_party(party_id, name, party_type=None, note=None):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Name is required.')
    party.name = name
    if party_type:
        party.party_type = (party_type or 'other').strip().lower() or 'other'
    if note is not None:
        party.note = (note or '').strip() or None
    party.updated_at = pk_now()
    db.session.flush()
    return party


def rename_cf_category(category_id, name, direction=None, notes=None):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Category name is required.')
    clash = CashFlowCategory.query.filter(
        func.lower(CashFlowCategory.name) == name.lower(),
        CashFlowCategory.id != cat.id,
    ).first()
    if clash:
        raise ValueError('Another category already uses that name.')
    cat.name = name
    if direction in CF_CAT_DIRECTIONS:
        cat.direction = direction
    if notes is not None:
        cat.notes = (notes or '').strip() or None
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def rename_cf_subcategory(subcategory_id, name, notes=None):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Sub-category name is required.')
    clash = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == sub.category_id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
        CashFlowSubcategory.id != sub.id,
    ).first()
    if clash:
        raise ValueError('Another sub-category already uses that name under this category.')
    sub.name = name
    if notes is not None:
        sub.notes = (notes or '').strip() or None
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def cf_used_category_ids():
    rows = db.session.query(CashFlowEntry.category_id).filter(
        CashFlowEntry.category_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def cf_used_subcategory_ids():
    rows = db.session.query(CashFlowEntry.subcategory_id).filter(
        CashFlowEntry.subcategory_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def cf_used_party_ids():
    rows = db.session.query(CashFlowEntry.party_id).filter(
        CashFlowEntry.party_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def _cf_category_is_used(category_id):
    if CashFlowEntry.query.filter(CashFlowEntry.category_id == category_id).first():
        return True
    sub_ids = [
        row[0] for row in db.session.query(CashFlowSubcategory.id).filter(
            CashFlowSubcategory.category_id == category_id
        ).all()
    ]
    if sub_ids and CashFlowEntry.query.filter(CashFlowEntry.subcategory_id.in_(sub_ids)).first():
        return True
    return False


def delete_cf_category(category_id):
    """Hard-delete an unused category. Historically used categories must be disabled."""
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    if _cf_category_is_used(cat.id):
        raise ValueError(
            'This category is used by historical transactions and cannot be deleted. Disable it instead.'
        )
    CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == cat.id).delete(
        synchronize_session=False
    )
    db.session.delete(cat)
    db.session.flush()
    return True


def delete_cf_subcategory(subcategory_id):
    """Hard-delete an unused sub-category. Historically used ones must be disabled."""
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    if CashFlowEntry.query.filter(CashFlowEntry.subcategory_id == sub.id).first():
        raise ValueError(
            'This sub-category is used by historical transactions and cannot be deleted. Disable it instead.'
        )
    db.session.delete(sub)
    db.session.flush()
    return True


def delete_cf_party(party_id):
    """Hard-delete an unused party. Historically used parties must be disabled."""
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    if CashFlowEntry.query.filter(CashFlowEntry.party_id == party.id).first():
        raise ValueError(
            'This party is used by historical transactions and cannot be deleted. Disable it instead.'
        )
    db.session.delete(party)
    db.session.flush()
    return True


def parse_physical_cash_amount(raw):
    """Parse Physical Cash Available.

    Zero is valid. Empty / missing is invalid. Invalid text is rejected.
    Do not treat 0 as missing.
    """
    if raw is None:
        raise ValueError('Physical Cash Available is required. Difference is calculated by the system.')
    text = str(raw).strip().replace(',', '')
    for prefix in ('Rs.', 'rs.', 'RS.', 'PKR', 'pkr'):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text == '':
        raise ValueError('Physical Cash Available is required. Difference is calculated by the system.')
    try:
        amount = Decimal(text)
    except Exception:
        raise ValueError('Physical Cash Available must be a valid number.')
    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amount == Decimal('-0.00'):
        amount = Decimal('0.00')
    return float(amount)


def compute_physical_cash_difference(physical_cash, calculated_closing):
    """Difference = Physical Cash Available - System Calculated Closing."""
    return _money_round(physical_cash) - _money_round(calculated_closing)


def _cf_resolve_category(direction, category_id, category_name, *, required, create_if_missing):
    if direction == CF_DIR_TRANSFER:
        return None
    cat = _cf_find_category(category_id=category_id, name=category_name, active_only=True)
    if cat:
        allowed = (cat.direction or 'both').lower()
        if allowed not in ('both', direction):
            raise ValueError('Selected category is not allowed for this transaction type.')
        return cat
    name = (category_name or '').strip()
    if required and not name and not category_id:
        raise ValueError('Category is required for Received and Spent.')
    if category_id and not cat:
        raise ValueError('Selected category is missing or inactive.')
    if name and create_if_missing:
        cat, _ = save_cf_category(name, direction=direction)
        return cat
    if required:
        raise ValueError('Category must exist and be active. Create it in Categories first.')
    return None


def _cf_resolve_subcategory(category, subcategory_id, subcategory_name, *, create_if_missing):
    if not category:
        if subcategory_id or (subcategory_name or '').strip():
            raise ValueError('Sub-category requires a category.')
        return None
    sub = _cf_find_subcategory(category, subcategory_id=subcategory_id, name=subcategory_name, active_only=True)
    if sub:
        return sub
    if subcategory_id:
        raise ValueError('Sub-category does not belong to the selected category.')
    name = (subcategory_name or '').strip()
    if name and create_if_missing:
        sub, _ = save_cf_subcategory(category.id, name)
        return sub
    if name:
        raise ValueError('Sub-category must belong to the selected category.')
    return None


def _cf_build_tx_fields(direction, amount, account, destination, description, note, posted, entry_id=None):
    from utils.money import to_minor
    marker = CF_SRC_MARKER if not entry_id else f'[SRC:CashFlow:{int(entry_id)}]'
    note_txt = ' '.join(x for x in [(note or '').strip(), marker] if x).strip()
    amount = _money_round(amount)
    fields = {
        'amount': amount,
        'amount_minor': to_minor(amount),
        'description': description,
        'note': note_txt,
        'date_posted': posted,
        'source_type': 'CashFlowEntry',
        'source_id': entry_id,
        'is_void': False,
    }
    if direction == CF_DIR_IN:
        fields.update(from_account_id=None, to_account_id=account.id, transaction_type='Receipt')
    elif direction == CF_DIR_OUT:
        fields.update(from_account_id=account.id, to_account_id=None, transaction_type='Expense')
    else:
        fields.update(
            from_account_id=account.id,
            to_account_id=destination.id,
            transaction_type='Transfer',
        )
    return fields


def validate_manual_cash_flow(
    *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    require_category=True, create_missing=False, check_balance=True,
):
    direction = _cf_normalize_direction(direction)
    if direction not in CF_DIRECTIONS:
        raise ValueError('Choose Received, Spent, or Transfer.')
    amount = _money_round(amount)
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')
    account = db.session.get(Account, account_id) if account_id else None
    if not _cf_is_money_account(account):
        raise ValueError('Select a valid company cash or bank account.')
    destination = None
    if direction == CF_DIR_TRANSFER:
        destination = db.session.get(Account, destination_account_id) if destination_account_id else None
        if not _cf_is_money_account(destination):
            raise ValueError('Select a valid destination account.')
        if destination.id == account.id:
            raise ValueError('Source and destination accounts cannot be the same.')
    else:
        cat = _cf_resolve_category(
            direction, category_id, category_name,
            required=require_category, create_if_missing=create_missing,
        )
        _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    if check_balance and direction in (CF_DIR_OUT, CF_DIR_TRANSFER) and float(account.balance or 0) < amount:
        raise ValueError(f'Insufficient balance in {account.name}.')
    return direction, amount, account, destination


def save_manual_cash_flow_entry(
    *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    party_id=None, party_name=None, party_type=None, description=None, note=None,
    reference=None, date_posted=None, idempotency_key=None, actor=None,
    create_missing=True,
):
    from app.services.accounting import _apply_account_tx_effect

    _cf_ensure_indexes()
    key = (idempotency_key or '').strip() or None
    if key:
        existing = CashFlowEntry.query.filter(CashFlowEntry.idempotency_key == key).first()
        if existing:
            return existing, False

    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=True,
        create_missing=create_missing,
    )
    cat = _cf_resolve_category(
        direction, category_id, category_name, required=(direction != CF_DIR_TRANSFER),
        create_if_missing=create_missing,
    ) if direction != CF_DIR_TRANSFER else None
    sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    ptype = (party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype, active_only=True)
    if not party and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party:
        party_name = party.name
        ptype = party.party_type or ptype

    posted = date_posted or pk_now()
    desc = (description or '').strip()
    if not desc:
        desc = (cat.name if cat else _cf_type_label(direction))
        if (party_name or '').strip():
            desc = f'{desc} — {party_name.strip()}'
    note = (note or '').strip() or None
    reference = (reference or '').strip() or None
    actor_name = _cf_actor_name(actor)

    from utils.money import to_minor
    entry = CashFlowEntry(
        direction=direction, amount=amount, amount_minor=to_minor(amount),
        account_id=account.id,
        destination_account_id=(destination.id if destination else None),
        category_id=cat.id if cat else None,
        subcategory_id=sub.id if sub else None,
        party_id=party.id if party else None,
        party_name=(party_name or '').strip() or None,
        party_type=ptype,
        description=desc, note=note, reference=reference,
        date_posted=posted, created_by=actor_name, updated_by=actor_name,
        source_type=SRC_MANUAL, is_void=False, revision=1,
        idempotency_key=key, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(entry)
    db.session.flush()

    tx_fields = _cf_build_tx_fields(direction, amount, account, destination, desc, note, posted, entry.id)
    tx_fields['created_by'] = actor_name
    tx = AccountTransaction(**tx_fields)
    db.session.add(tx)
    db.session.flush()
    _apply_account_tx_effect(tx)
    entry.account_tx_id = tx.id
    _cf_write_audit(entry, 'Created', after=_cf_snapshot(entry), actor=actor)
    db.session.flush()
    return entry, True


def update_manual_cash_flow_entry(
    entry, *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    party_id=None, party_name=None, party_type=None, description=None, note=None,
    reference=None, date_posted=None, reason=None, actor=None, create_missing=True,
):
    from app.services.accounting import _apply_account_tx_effect, _reverse_account_tx_effect

    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('A voided transaction cannot be edited. Restore it first.')

    before = _cf_snapshot(entry)
    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=True,
        create_missing=create_missing, check_balance=False,
    )
    # Reverse first so the insufficient-balance check sees the pre-transaction balance.
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and not tx.is_void:
        _reverse_account_tx_effect(tx)

    if direction in (CF_DIR_OUT, CF_DIR_TRANSFER) and float(account.balance or 0) < amount:
        if tx and not tx.is_void:
            _apply_account_tx_effect(tx)
        raise ValueError(f'Insufficient balance in {account.name}.')

    cat = _cf_resolve_category(
        direction, category_id, category_name, required=(direction != CF_DIR_TRANSFER),
        create_if_missing=create_missing,
    ) if direction != CF_DIR_TRANSFER else None
    sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    ptype = (party_type or entry.party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype, active_only=True)
    if not party and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party:
        party_name = party.name
        ptype = party.party_type or ptype

    posted = date_posted or entry.date_posted or pk_now()
    desc = (description or '').strip() or entry.description or _cf_type_label(direction)
    note = (note or '').strip() or None
    reference = (reference if reference is not None else entry.reference)
    reference = (reference or '').strip() or None
    actor_name = _cf_actor_name(actor)

    from utils.money import to_minor
    entry.direction = direction
    entry.amount = amount
    entry.amount_minor = to_minor(amount)
    entry.account_id = account.id
    entry.destination_account_id = destination.id if destination else None
    entry.category_id = cat.id if cat else None
    entry.subcategory_id = sub.id if sub else None
    entry.party_id = party.id if party else None
    entry.party_name = (party_name or '').strip() or None
    entry.party_type = ptype
    entry.description = desc
    entry.note = note
    entry.reference = reference
    entry.date_posted = posted
    entry.updated_by = actor_name
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1

    tx_fields = _cf_build_tx_fields(direction, amount, account, destination, desc, note, posted, entry.id)
    if tx is None:
        tx = AccountTransaction(created_by=actor_name, **tx_fields)
        db.session.add(tx)
        db.session.flush()
        entry.account_tx_id = tx.id
    else:
        for key, value in tx_fields.items():
            setattr(tx, key, value)
    _apply_account_tx_effect(tx)
    _cf_write_audit(entry, 'Edited', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def void_manual_cash_flow_entry(entry, reason=None, actor=None):
    from app.services.accounting import _void_account_tx

    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('This transaction is already voided.')
    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and not tx.is_void:
        _void_account_tx(tx)
        tx.voided_by = _cf_actor_name(actor)
        tx.voided_at = pk_now()
    entry.is_void = True
    entry.voided_at = pk_now()
    entry.voided_by = _cf_actor_name(actor)
    entry.void_reason = (reason or '').strip() or None
    entry.updated_by = _cf_actor_name(actor)
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1
    _cf_write_audit(entry, 'Voided', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def restore_manual_cash_flow_entry(entry, reason=None, actor=None):
    from app.services.accounting import _unvoid_account_tx

    if entry is None:
        raise ValueError('Entry not found.')
    if not entry.is_void:
        raise ValueError('This transaction is already active.')
    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and tx.is_void:
        _unvoid_account_tx(tx)
    entry.is_void = False
    entry.voided_at = None
    entry.voided_by = None
    entry.void_reason = None
    entry.updated_by = _cf_actor_name(actor)
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1
    _cf_write_audit(entry, 'Restored', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def _cf_row(
    *, sort_dt, direction, amount, reference, description, note='',
    category='', subcategory='', party_name='', party_type='',
    account_id=None, account_name='', dest_account_id=None, dest_account_name='',
    source_type=SRC_MANUAL, origin='recorded', origin_label='',
    created_by='', status='active', entry_id=None, tx_id=None,
):
    amount = float(amount or 0)
    cash_in = amount if direction == CF_DIR_IN else 0.0
    cash_out = amount if direction == CF_DIR_OUT else 0.0
    transfer_amount = amount if direction == CF_DIR_TRANSFER else 0.0
    if dest_account_name and direction == CF_DIR_TRANSFER:
        account_display = f'{account_name or "—"} → {dest_account_name}'
    else:
        account_display = account_name or '—'
    dt = sort_dt
    date_val = dt.date() if hasattr(dt, 'date') else dt
    return {
        'date': date_val,
        'sort_dt': dt,
        'type': direction,
        'tx_type_label': _cf_type_label(direction),
        'cash_in': cash_in,
        'cash_out': cash_out,
        'transfer_amount': transfer_amount,
        'amount': amount,
        'account_id': account_id,
        'account_name': account_name or '',
        'account_to_id': dest_account_id,
        'account_to_name': dest_account_name or '',
        'account_display': account_display,
        'category': category or '',
        'subcategory': subcategory or '',
        'party_name': party_name or '',
        'party_type': party_type or '',
        'description': description or '',
        'note': note or '',
        'reference': reference or '',
        'source': 'MANUAL' if source_type == SRC_MANUAL else 'SYSTEM',
        'source_type': source_type,
        'origin': origin,
        'origin_label': origin_label or CF_SOURCE_LABELS.get(source_type, source_type),
        'created_by': created_by or '',
        'status': status,
        'entry_id': entry_id,
        'tx_id': tx_id,
    }


def _cf_is_mirror_tx(tx):
    note_u = (tx.note or '').upper()
    if CF_SRC_MARKER.upper() in note_u or '[SRC:CASHFLOW' in note_u:
        return True
    if (getattr(tx, 'source_type', None) or '') == 'CashFlowEntry':
        return True
    return any(marker in note_u for marker in _MIRROR_MARKERS)


def collect_cash_flow_rows(from_date, to_date, *, posted_after=None, include_voided=True):
    """Collect manual + system cash-flow rows for a date window."""
    rows = []
    account_by_id = {a.id: a for a in Account.query.all()}

    recorded = CashFlowEntry.query.filter(
        func.date(CashFlowEntry.date_posted) >= from_date,
        func.date(CashFlowEntry.date_posted) <= to_date,
    )
    if posted_after is not None:
        recorded = recorded.filter(CashFlowEntry.date_posted > posted_after)
    if not include_voided:
        recorded = recorded.filter(CashFlowEntry.is_void == False)
    for e in recorded.all():
        acc = e.account or account_by_id.get(e.account_id)
        dest = getattr(e, 'destination_account', None) or account_by_id.get(getattr(e, 'destination_account_id', None))
        rows.append(_cf_row(
            sort_dt=e.date_posted,
            direction=e.direction,
            amount=e.amount,
            reference=(getattr(e, 'reference', None) or f'CF-{e.id}'),
            description=e.description or '',
            note=e.note or '',
            category=e.category.name if e.category else '',
            subcategory=e.subcategory.name if e.subcategory else '',
            party_name=e.party_name or (e.party.name if e.party else ''),
            party_type=e.party_type or (e.party.party_type if e.party else ''),
            account_id=e.account_id,
            account_name=acc.name if acc else '',
            dest_account_id=getattr(e, 'destination_account_id', None),
            dest_account_name=dest.name if dest else '',
            source_type=getattr(e, 'source_type', None) or SRC_MANUAL,
            origin='recorded',
            origin_label='Recorded on Cash Flow',
            created_by=e.created_by or '',
            status='voided' if e.is_void else 'active',
            entry_id=e.id,
            tx_id=e.account_tx_id,
        ))

    cash_method_clauses = [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]
    pay_q = Payment.query.filter(
        Payment.is_void == False,
        or_(*cash_method_clauses),
        func.date(Payment.date_posted) >= from_date,
        func.date(Payment.date_posted) <= to_date,
    )
    if posted_after is not None:
        pay_q = pay_q.filter(Payment.date_posted > posted_after)
    for p in pay_q.all():
        acc = account_by_id.get(getattr(p, 'payment_account_id', None))
        amt = float(p.amount or 0)
        direction = CF_DIR_OUT if amt < 0 else CF_DIR_IN
        rows.append(_cf_row(
            sort_dt=p.date_posted, direction=direction, amount=abs(amt),
            reference=p.manual_bill_no or p.auto_bill_no or f'PAY-{p.id}',
            description=f'Client Payment — {p.client_name or ""}',
            note=p.note or '',
            party_name=p.client_name or '', party_type='client',
            account_id=getattr(p, 'payment_account_id', None),
            account_name=acc.name if acc else '',
            source_type=SRC_CLIENT_PAYMENT, origin='derived',
            origin_label='From Accounts · Client Payments',
            created_by=getattr(p, 'created_by', None) or '',
        ))

    sale_q = DirectSale.query.filter(
        DirectSale.is_void == False,
        or_(
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash sale',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash sale',
        ),
        DirectSale.paid_amount > 0,
        func.date(DirectSale.date_posted) >= from_date,
        func.date(DirectSale.date_posted) <= to_date,
    )
    if posted_after is not None:
        sale_q = sale_q.filter(DirectSale.date_posted > posted_after)
    for s in sale_q.all():
        acc = account_by_id.get(getattr(s, 'payment_account_id', None))
        rows.append(_cf_row(
            sort_dt=s.date_posted, direction=CF_DIR_IN, amount=s.paid_amount,
            reference=s.manual_bill_no or s.auto_bill_no or f'DS-{s.id}',
            description=f'Cash Sale — {s.client_name or ""}',
            note=s.note or '',
            party_name=s.client_name or '', party_type='client',
            account_id=getattr(s, 'payment_account_id', None),
            account_name=acc.name if acc else '',
            source_type=SRC_SALE, origin='derived',
            origin_label='From Sales',
            created_by=getattr(s, 'created_by', None) or '',
        ))

    sp_q = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) >= from_date,
        func.date(SupplierPayment.date_posted) <= to_date,
    )
    if posted_after is not None:
        sp_q = sp_q.filter(SupplierPayment.date_posted > posted_after)
    for sp in sp_q.all():
        supplier_name = sp.supplier.name if getattr(sp, 'supplier', None) else ''
        acc = account_by_id.get(getattr(sp, 'payment_account_id', None))
        rows.append(_cf_row(
            sort_dt=sp.date_posted, direction=CF_DIR_OUT, amount=sp.amount,
            reference=sp.manual_bill_no or sp.auto_bill_no or f'SUP-{sp.id}',
            description=f'Supplier Payment — {supplier_name}',
            note=sp.note or '',
            party_name=supplier_name, party_type='supplier',
            account_id=getattr(sp, 'payment_account_id', None),
            account_name=acc.name if acc else '',
            source_type=SRC_SUPPLIER_PAYMENT, origin='derived',
            origin_label='From Accounts · Supplier Payments',
            created_by=getattr(sp, 'created_by', None) or '',
        ))

    tx_q = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment', 'Transfer', 'Receipt']),
        func.date(AccountTransaction.date_posted) >= from_date,
        func.date(AccountTransaction.date_posted) <= to_date,
    )
    if posted_after is not None:
        tx_q = tx_q.filter(AccountTransaction.date_posted > posted_after)
    for tx in tx_q.all():
        if _cf_is_mirror_tx(tx):
            continue
        from_acc = account_by_id.get(tx.from_account_id)
        to_acc = account_by_id.get(tx.to_account_id)
        if tx.transaction_type == 'Transfer':
            from_ok = from_acc and (from_acc.category or '').lower() in ('cash', 'bank')
            to_ok = to_acc and (to_acc.category or '').lower() in ('cash', 'bank')
            if not (from_ok or to_ok):
                continue
            rows.append(_cf_row(
                sort_dt=tx.date_posted, direction=CF_DIR_TRANSFER, amount=tx.amount,
                reference=f'TX-{tx.id}',
                description=tx.description or 'Account transfer',
                note=tx.note or '',
                account_id=tx.from_account_id,
                account_name=from_acc.name if from_acc else '',
                dest_account_id=tx.to_account_id,
                dest_account_name=to_acc.name if to_acc else '',
                source_type=SRC_TRANSFER, origin='derived',
                origin_label='From Accounts · Transfer',
                created_by=tx.created_by or '',
                tx_id=tx.id,
            ))
            continue
        if tx.transaction_type == 'Receipt' and to_acc and (to_acc.category or '').lower() in ('cash', 'bank'):
            rows.append(_cf_row(
                sort_dt=tx.date_posted, direction=CF_DIR_IN, amount=tx.amount,
                reference=f'TX-{tx.id}',
                description=tx.description or 'Cash received',
                note=tx.note or '',
                account_id=tx.to_account_id, account_name=to_acc.name,
                source_type=SRC_ACCOUNT_RECEIPT, origin='derived',
                origin_label='From Accounts · Other receive',
                created_by=tx.created_by or '',
                tx_id=tx.id,
            ))
            continue
        if tx.transaction_type in ('Expense', 'Payment', 'Driver Payment') and from_acc and (from_acc.category or '').lower() in ('cash', 'bank'):
            is_driver = tx.transaction_type == 'Driver Payment'
            rows.append(_cf_row(
                sort_dt=tx.date_posted, direction=CF_DIR_OUT, amount=tx.amount,
                reference=f'TX-{tx.id}',
                description=tx.description or ('Driver service payment' if is_driver else 'Expense'),
                note=tx.note or '',
                party_type=('delivery_person' if is_driver else ''),
                account_id=tx.from_account_id, account_name=from_acc.name,
                source_type=SRC_DRIVER_PAYMENT if is_driver else SRC_ACCOUNT_EXPENSE,
                origin='derived',
                origin_label=('From Accounts · Driver Services' if is_driver else 'From Accounts · Expense'),
                created_by=tx.created_by or '',
                tx_id=tx.id,
            ))
    return rows


def _cf_sort_key(row):
    d = row.get('sort_dt') or row.get('date')
    ref = row.get('reference') or ''
    if hasattr(d, 'timestamp'):
        return (d.timestamp(), ref)
    try:
        return (datetime.combine(d, datetime.min.time()).timestamp(), ref)
    except Exception:
        return (0, ref)


def filter_cash_flow_rows(rows, filters):
    out = list(rows)
    ftype = (filters.get('filter_type') or 'all').strip().lower()
    if ftype in ('cash_in', 'received', 'in'):
        out = [r for r in out if r['type'] == CF_DIR_IN]
    elif ftype in ('cash_out', 'spent', 'out'):
        out = [r for r in out if r['type'] == CF_DIR_OUT]
    elif ftype == 'transfer':
        out = [r for r in out if r['type'] == CF_DIR_TRANSFER]

    origin = (filters.get('origin') or 'all').strip().lower()
    if origin in ('derived', 'recorded'):
        out = [r for r in out if r.get('origin') == origin]
    elif origin == 'manual':
        out = [r for r in out if r.get('source') == 'MANUAL']
    elif origin == 'system':
        out = [r for r in out if r.get('source') == 'SYSTEM']
    elif origin and origin not in ('all', ''):
        out = [r for r in out if (r.get('source_type') or '').lower() == origin.lower()]

    category = (filters.get('category') or '').strip().lower()
    if category:
        out = [r for r in out if category in (r.get('category') or '').lower()]
    subcategory = (filters.get('subcategory') or '').strip().lower()
    if subcategory:
        out = [r for r in out if subcategory in (r.get('subcategory') or '').lower()]
    party_type = (filters.get('party_type') or '').strip().lower()
    if party_type:
        out = [r for r in out if (r.get('party_type') or '').lower() == party_type]
    party = (filters.get('party') or '').strip().lower()
    if party:
        out = [r for r in out if party in (r.get('party_name') or '').lower()]
    account_id = filters.get('account_id')
    if account_id:
        out = [r for r in out if r.get('account_id') == account_id or r.get('account_to_id') == account_id]
    notes = (filters.get('notes') or '').strip().lower()
    if notes:
        out = [r for r in out if notes in (r.get('note') or '').lower()]
    reference = (filters.get('reference') or '').strip().lower()
    if reference:
        out = [r for r in out if reference in (r.get('reference') or '').lower()]
    description = (filters.get('description') or '').strip().lower()
    if description:
        out = [r for r in out if description in (r.get('description') or '').lower()]
    created_by = (filters.get('created_by') or '').strip().lower()
    if created_by:
        out = [r for r in out if created_by in (r.get('created_by') or '').lower()]
    status = (filters.get('status') or 'active').strip().lower()
    if status in ('active', 'voided'):
        out = [r for r in out if (r.get('status') or 'active') == status]
    try:
        amin = filters.get('amount_min')
        if amin not in (None, ''):
            amin = float(amin)
            out = [r for r in out if float(r.get('amount') or 0) >= amin]
    except (TypeError, ValueError):
        pass
    try:
        amax = filters.get('amount_max')
        if amax not in (None, ''):
            amax = float(amax)
            out = [r for r in out if float(r.get('amount') or 0) <= amax]
    except (TypeError, ValueError):
        pass
    q = (filters.get('q') or '').strip().lower()
    if q:
        def _blob(r):
            return ' '.join([
                str(r.get('entry_id') or ''),
                str(r.get('reference') or ''),
                str(r.get('description') or ''),
                str(r.get('note') or ''),
                str(r.get('party_name') or ''),
                str(r.get('category') or ''),
                str(r.get('subcategory') or ''),
                str(r.get('account_display') or ''),
            ]).lower()
        out = [r for r in out if q in _blob(r)]
    return out


def apply_running_balance(rows, opening_balance, account_id=None):
    running = float(opening_balance or 0)
    for row in rows:
        if (row.get('status') or 'active') == 'voided':
            row['running_balance'] = running
            continue
        if account_id:
            if row['type'] == CF_DIR_IN and row.get('account_id') == account_id:
                running += float(row.get('cash_in') or 0)
            elif row['type'] == CF_DIR_OUT and row.get('account_id') == account_id:
                running -= float(row.get('cash_out') or 0)
            elif row['type'] == CF_DIR_TRANSFER:
                if row.get('account_to_id') == account_id:
                    running += float(row.get('transfer_amount') or 0)
                if row.get('account_id') == account_id:
                    running -= float(row.get('transfer_amount') or 0)
        else:
            running += float(row.get('cash_in') or 0) - float(row.get('cash_out') or 0)
        row['running_balance'] = running
    return running


def summarize_cash_flow_rows(rows, account_id=None):
    active = [r for r in rows if (r.get('status') or 'active') == 'active']
    total_in = sum(float(r.get('cash_in') or 0) for r in active)
    total_out = sum(float(r.get('cash_out') or 0) for r in active)
    transfer_in = 0.0
    transfer_out = 0.0
    for r in active:
        if r.get('type') != CF_DIR_TRANSFER:
            continue
        amt = float(r.get('transfer_amount') or 0)
        if account_id:
            if r.get('account_to_id') == account_id:
                transfer_in += amt
            if r.get('account_id') == account_id:
                transfer_out += amt
        else:
            transfer_in += amt
            transfer_out += amt
    breakdown_cat, breakdown_party, breakdown_account = {}, {}, {}
    for r in active:
        ck = (r.get('category') or '—').strip() or '—'
        breakdown_cat.setdefault(ck, {'in': 0.0, 'out': 0.0, 'transfer': 0.0})
        breakdown_cat[ck]['in'] += float(r.get('cash_in') or 0)
        breakdown_cat[ck]['out'] += float(r.get('cash_out') or 0)
        breakdown_cat[ck]['transfer'] += float(r.get('transfer_amount') or 0)
        pk = ((r.get('party_type') or '') + ' · ' + (r.get('party_name') or '—')).strip(' ·')
        breakdown_party.setdefault(pk, {'in': 0.0, 'out': 0.0, 'transfer': 0.0})
        breakdown_party[pk]['in'] += float(r.get('cash_in') or 0)
        breakdown_party[pk]['out'] += float(r.get('cash_out') or 0)
        breakdown_party[pk]['transfer'] += float(r.get('transfer_amount') or 0)
        ak = (r.get('account_display') or r.get('account_name') or '—').strip() or '—'
        breakdown_account.setdefault(ak, {'in': 0.0, 'out': 0.0, 'transfer': 0.0})
        breakdown_account[ak]['in'] += float(r.get('cash_in') or 0)
        breakdown_account[ak]['out'] += float(r.get('cash_out') or 0)
        breakdown_account[ak]['transfer'] += float(r.get('transfer_amount') or 0)
    return {
        'total_cash_in': total_in,
        'total_cash_out': total_out,
        'total_transfer_in': transfer_in,
        'total_transfer_out': transfer_out,
        'breakdown_cat': breakdown_cat,
        'breakdown_party': breakdown_party,
        'breakdown_account': breakdown_account,
    }


def categories_for_direction(direction, include_inactive=False):
    direction = _cf_normalize_direction(direction)
    q = CashFlowCategory.query
    if not include_inactive:
        q = q.filter(CashFlowCategory.is_active == True)
    rows = q.order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all()
    if direction in (CF_DIR_IN, CF_DIR_OUT):
        rows = [c for c in rows if (c.direction or 'both') in (direction, 'both')]
    return rows


def subcategories_for_category(category_id, include_inactive=False):
    q = CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == category_id)
    if not include_inactive:
        q = q.filter(CashFlowSubcategory.is_active == True)
    return q.order_by(CashFlowSubcategory.name).all()


