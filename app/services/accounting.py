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
from app.services.sales_core import (
    _direct_sale_default_bill_ref,
)
from app.services.time_money import (
    pk_now,
)
from utils.money import from_minor, to_minor


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

def _payment_expected_account_category(method):
    m = (method or '').strip().lower()
    if m in ['cash', 'cash sale']:
        return 'cash'
    if m in ['bank', 'bank transfer', 'check', 'cheque', 'card', 'online']:
        return 'bank'
    return None


def _src_marker(kind, src_id, suffix=None):
    base = f"[SRC:{kind}:{int(src_id)}]"
    if suffix:
        return f"{base}:{suffix}"
    return base


def _account_minor(account):
    if account is None:
        return 0
    current = getattr(account, 'balance_minor', None)
    return int(current) if current is not None else to_minor(account.balance or 0)


def _tx_minor(tx):
    current = getattr(tx, 'amount_minor', None)
    return int(current) if current is not None else to_minor(tx.amount or 0)


def _set_account_minor(account, minor):
    account.balance_minor = int(minor)
    account.balance = float(from_minor(minor))


def _reverse_account_tx_effect(tx):
    if not tx or bool(getattr(tx, 'is_void', False)):
        return
    amount_minor = _tx_minor(tx)
    if getattr(tx, 'to_account_id', None):
        acc = db.session.get(Account, tx.to_account_id)
        if acc:
            _set_account_minor(acc, _account_minor(acc) - amount_minor)
    if getattr(tx, 'from_account_id', None):
        acc = db.session.get(Account, tx.from_account_id)
        if acc:
            _set_account_minor(acc, _account_minor(acc) + amount_minor)


def _apply_account_tx_effect(tx):
    if not tx or bool(getattr(tx, 'is_void', False)):
        return
    amount_minor = _tx_minor(tx)
    if getattr(tx, 'to_account_id', None):
        acc = db.session.get(Account, tx.to_account_id)
        if acc:
            _set_account_minor(acc, _account_minor(acc) + amount_minor)
    if getattr(tx, 'from_account_id', None):
        acc = db.session.get(Account, tx.from_account_id)
        if acc:
            _set_account_minor(acc, _account_minor(acc) - amount_minor)


def _void_account_tx(tx):
    if not tx or bool(getattr(tx, 'is_void', False)):
        return False
    _reverse_account_tx_effect(tx)
    tx.is_void = True
    return True


def _unvoid_account_tx(tx):
    if not tx or not bool(getattr(tx, 'is_void', False)):
        return False
    tx.is_void = False
    _apply_account_tx_effect(tx)
    return True


def _sync_linked_receipt_tx(kind, src_id, to_account_id, amount, date_posted, description, note, is_void):
    marker = _src_marker(kind, src_id)
    existing = AccountTransaction.query.filter(
        AccountTransaction.transaction_type == 'Receipt',
        AccountTransaction.note.ilike(f"%{marker}%")
    ).order_by(AccountTransaction.id.desc()).all()

    primary = existing[0] if existing else None
    for extra in existing[1:]:
        _void_account_tx(extra)

    desired_ok = (not bool(is_void)) and bool(to_account_id) and float(amount or 0) > 0
    if not desired_ok:
        if primary:
            _void_account_tx(primary)
        return

    # If the existing tx doesn't match the new intent, void and recreate to keep balances correct.
    if primary and (not primary.is_void) and (
        primary.to_account_id != to_account_id or _tx_minor(primary) != to_minor(amount or 0)
    ):
        _void_account_tx(primary)
        primary = None

    if primary and primary.is_void:
        primary = None

    if not primary:
        tx = AccountTransaction(
            from_account_id=None,
            to_account_id=to_account_id,
            amount=float(from_minor(to_minor(amount or 0))),
            amount_minor=to_minor(amount or 0),
            description=description,
            note=(note or '').strip(),
            transaction_type='Receipt',
            source_type=kind,
            source_id=src_id,
            date_posted=date_posted or pk_now()
        )
        db.session.add(tx)
        db.session.flush()
        _apply_account_tx_effect(tx)
        return

    primary.source_type = kind
    primary.source_id = src_id
    primary.description = description
    primary.note = (note or '').strip()
    if date_posted:
        primary.date_posted = date_posted


def _sync_linked_supplier_payment_tx(kind, src_id, from_account_id, amount, date_posted, description, note, is_void):
    marker = _src_marker(kind, src_id)
    existing = AccountTransaction.query.filter(
        AccountTransaction.transaction_type == 'Supplier Payment',
        AccountTransaction.note.ilike(f"%{marker}%")
    ).order_by(AccountTransaction.id.desc()).all()

    primary = existing[0] if existing else None
    for extra in existing[1:]:
        _void_account_tx(extra)

    desired_ok = (not bool(is_void)) and bool(from_account_id) and float(amount or 0) > 0
    if not desired_ok:
        if primary:
            _void_account_tx(primary)
        return

    if primary and (not primary.is_void) and (
        primary.from_account_id != from_account_id or _tx_minor(primary) != to_minor(amount or 0)
    ):
        _void_account_tx(primary)
        primary = None

    if primary and primary.is_void:
        primary = None

    if not primary:
        tx = AccountTransaction(
            from_account_id=from_account_id,
            to_account_id=None,
            amount=float(from_minor(to_minor(amount or 0))),
            amount_minor=to_minor(amount or 0),
            description=description,
            note=(note or '').strip(),
            transaction_type='Supplier Payment',
            source_type=kind,
            source_id=src_id,
            date_posted=date_posted or pk_now()
        )
        db.session.add(tx)
        db.session.flush()
        _apply_account_tx_effect(tx)
        return

    primary.source_type = kind
    primary.source_id = src_id
    primary.description = description
    primary.note = (note or '').strip()
    if date_posted:
        primary.date_posted = date_posted


def _sync_linked_client_refund_tx(src_id, from_account_id, amount, date_posted, description, note, is_void):
    """Synchronise the cash/bank outflow for a negative client Payment row."""
    canonical = _src_marker('Payment', src_id)
    legacy = f"[SRC:ClientRefund:{int(src_id)}]"
    existing = AccountTransaction.query.filter(
        AccountTransaction.transaction_type.in_(['Refund', 'Payment']),
        or_(AccountTransaction.note.ilike(f"%{canonical}%"),
            AccountTransaction.note.ilike(f"%{legacy}%"))
    ).order_by(AccountTransaction.id.desc()).all()
    primary = existing[0] if existing else None
    for extra in existing[1:]:
        _void_account_tx(extra)

    refund_minor = abs(to_minor(amount or 0))
    desired_ok = (not bool(is_void)) and bool(from_account_id) and refund_minor > 0
    if not desired_ok:
        if primary:
            _void_account_tx(primary)
        return

    if primary and not primary.is_void and (
        primary.from_account_id != from_account_id or _tx_minor(primary) != refund_minor
    ):
        _void_account_tx(primary)
        primary = None
    if primary and primary.is_void:
        primary = None

    if not primary:
        primary = AccountTransaction(
            from_account_id=from_account_id,
            to_account_id=None,
            amount=float(from_minor(refund_minor)),
            amount_minor=refund_minor,
            description=description,
            note=(note or '').strip(),
            transaction_type='Refund',
            source_type='Payment',
            source_id=src_id,
            date_posted=date_posted or pk_now(),
        )
        db.session.add(primary)
        db.session.flush()
        _apply_account_tx_effect(primary)
        return

    primary.transaction_type = 'Refund'
    primary.source_type = 'Payment'
    primary.source_id = src_id
    primary.description = description
    primary.note = (note or '').strip()
    if date_posted:
        primary.date_posted = date_posted


def _sync_linked_driver_payment_tx(kind, src_id, from_account_id, amount, date_posted, description, note, is_void):
    """Keep exactly one money-out ledger row for a driver service payment.

    Mirrors ``_sync_linked_supplier_payment_tx``: the source row is authoritative
    for *who/why*, this single ``AccountTransaction`` is authoritative for the
    *financial effect*.  Amount/account changes void-and-recreate so an edit can
    never apply the old and the new effect at the same time.
    """
    marker = _src_marker(kind, src_id)
    existing = AccountTransaction.query.filter(
        AccountTransaction.transaction_type == 'Driver Payment',
        or_(AccountTransaction.note.ilike(f"%{marker}%"),
            and_(AccountTransaction.source_type == kind,
                 AccountTransaction.source_id == src_id))
    ).order_by(AccountTransaction.id.desc()).all()

    primary = existing[0] if existing else None
    for extra in existing[1:]:
        _void_account_tx(extra)

    desired_ok = (not bool(is_void)) and bool(from_account_id) and float(amount or 0) > 0
    if not desired_ok:
        if primary:
            _void_account_tx(primary)
        return

    if primary and (not primary.is_void) and (
        primary.from_account_id != from_account_id or _tx_minor(primary) != to_minor(amount or 0)
    ):
        _void_account_tx(primary)
        primary = None

    if primary and primary.is_void:
        primary = None

    if not primary:
        tx = AccountTransaction(
            from_account_id=from_account_id,
            to_account_id=None,
            amount=float(from_minor(to_minor(amount or 0))),
            amount_minor=to_minor(amount or 0),
            description=description,
            note=(note or '').strip(),
            transaction_type='Driver Payment',
            source_type=kind,
            source_id=src_id,
            date_posted=date_posted or pk_now()
        )
        db.session.add(tx)
        db.session.flush()
        _apply_account_tx_effect(tx)
        return

    primary.source_type = kind
    primary.source_id = src_id
    primary.description = description
    primary.note = (note or '').strip()
    if date_posted:
        primary.date_posted = date_posted


def _sync_linked_loss_tx(kind, src_id, amount, date_posted, description, note, is_void):
    marker = _src_marker(kind, src_id, suffix='LOSS')
    existing = AccountTransaction.query.filter(
        AccountTransaction.transaction_type == 'Loss',
        AccountTransaction.note.ilike(f"%{marker}%")
    ).order_by(AccountTransaction.id.desc()).all()
    primary = existing[0] if existing else None
    for extra in existing[1:]:
        extra.is_void = True

    desired_ok = (not bool(is_void)) and float(amount or 0) > 0
    if not desired_ok:
        if primary:
            primary.is_void = True
        return

    if primary and (not primary.is_void) and _tx_minor(primary) != to_minor(amount or 0):
        primary.is_void = True
        primary = None
    if primary and primary.is_void:
        primary = None

    if not primary:
        tx = AccountTransaction(
            from_account_id=None,
            to_account_id=None,
            amount=float(from_minor(to_minor(amount or 0))),
            amount_minor=to_minor(amount or 0),
            description=description,
            note=(note or '').strip(),
            transaction_type='Loss',
            source_type=kind,
            source_id=src_id,
            date_posted=date_posted or pk_now()
        )
        db.session.add(tx)
        return

    primary.description = description
    primary.note = (note or '').strip()
    if date_posted:
        primary.date_posted = date_posted


def _sync_payment_accounting(payment):
    if not payment:
        return
    marker = _src_marker('Payment', payment.id)
    bill = (payment.manual_bill_no or payment.auto_bill_no or f"PAY-{payment.id}").strip()
    desc = f"Client payment received from {payment.client_name or 'Client'} ({bill})"
    note = " ".join([x for x in [(payment.note or '').strip(), marker] if x]).strip()
    amount = float(getattr(payment, 'amount', 0) or 0)
    payment_type = (getattr(payment, 'payment_type', None) or '').strip().lower()
    is_non_cash_credit = payment_type == 'material return' or (payment.method or '').strip().lower() == 'material return'
    _sync_linked_receipt_tx(
        kind='Payment',
        src_id=payment.id,
        to_account_id=getattr(payment, 'payment_account_id', None),
        amount=(amount if amount > 0 and not is_non_cash_credit else 0),
        date_posted=getattr(payment, 'date_posted', None),
        description=desc,
        note=note,
        is_void=bool(getattr(payment, 'is_void', False))
    )
    refund_note = " ".join([x for x in [(payment.note or '').strip(), marker] if x]).strip()
    _sync_linked_client_refund_tx(
        src_id=payment.id,
        from_account_id=getattr(payment, 'payment_account_id', None),
        amount=(amount if amount < 0 else 0),
        date_posted=getattr(payment, 'date_posted', None),
        description=f"Client refund to {payment.client_name or 'Client'} ({bill})",
        note=refund_note,
        is_void=bool(getattr(payment, 'is_void', False)),
    )
    _sync_linked_loss_tx(
        kind='Payment',
        src_id=payment.id,
        amount=float(getattr(payment, 'discount', 0) or 0),
        date_posted=getattr(payment, 'date_posted', None),
        description=f"Waive-off loss for {payment.client_name or 'Client'} ({bill})",
        note=" ".join([x for x in [(payment.discount_reason or '').strip(), _src_marker('Payment', payment.id, suffix='LOSS')] if x]).strip(),
        is_void=bool(getattr(payment, 'is_void', False))
    )


def _sync_direct_sale_accounting(sale):
    if not sale:
        return
    marker = _src_marker('DirectSale', sale.id)
    bill = (_direct_sale_default_bill_ref(sale) or sale.auto_bill_no or f"DS-{sale.id}").strip()
    desc = f"Sale receipt from {sale.client_name or 'Client'} ({bill})"
    note_bits = [str(sale.note or '').strip(), f"Method: {sale.payment_method or ''}".strip(), marker]
    note = " ".join([x for x in note_bits if x]).strip()
    _sync_linked_receipt_tx(
        kind='DirectSale',
        src_id=sale.id,
        to_account_id=getattr(sale, 'payment_account_id', None),
        amount=float(getattr(sale, 'paid_amount', 0) or 0),
        date_posted=getattr(sale, 'date_posted', None),
        description=desc,
        note=note,
        is_void=bool(getattr(sale, 'is_void', False))
    )


def _sync_delivery_person_payment_accounting(payment):
    """Project one driver settlement onto the authoritative account ledger.

    ``amount_paid``  -> money OUT of the selected cash/bank account.
    ``waive_off_amount`` -> a non-cash ``Loss`` row (the driver's payable is
    reduced without any cash leaving an account), exactly as client waive-offs
    are handled.  Both are keyed on the source row, so repeated calls converge
    instead of accumulating duplicates.
    """
    if not payment:
        return
    person = None
    try:
        person = db.session.get(DeliveryPerson, getattr(payment, 'delivery_person_id', None))
    except Exception:
        person = None
    driver_name = getattr(person, 'name', None) or 'Driver'
    reference = (getattr(payment, 'reference', None) or f"DPP-{payment.id}").strip()
    marker = _src_marker('DeliveryPersonPayment', payment.id)
    note_bits = [
        str(getattr(payment, 'note', '') or '').strip(),
        f"Method: {getattr(payment, 'method', '') or ''}".strip(),
        f"Driver: {driver_name}",
        marker,
    ]
    note = " ".join([x for x in note_bits if x and not x.endswith(': ')]).strip()
    _sync_linked_driver_payment_tx(
        kind='DeliveryPersonPayment',
        src_id=payment.id,
        from_account_id=getattr(payment, 'payment_account_id', None),
        amount=float(getattr(payment, 'amount_paid', 0) or 0),
        date_posted=getattr(payment, 'date_posted', None),
        description=f"Driver service payment to {driver_name} ({reference})",
        note=note,
        is_void=bool(getattr(payment, 'is_void', False)),
    )
    _sync_linked_loss_tx(
        kind='DeliveryPersonPayment',
        src_id=payment.id,
        amount=float(getattr(payment, 'waive_off_amount', 0) or 0),
        date_posted=getattr(payment, 'date_posted', None),
        description=f"Driver settlement waive-off for {driver_name} ({reference})",
        note=" ".join([x for x in [
            str(getattr(payment, 'note', '') or '').strip(),
            _src_marker('DeliveryPersonPayment', payment.id, suffix='LOSS'),
        ] if x]).strip(),
        is_void=bool(getattr(payment, 'is_void', False)),
    )


def _sync_supplier_payment_accounting(payment):
    if not payment:
        return
    bill = (payment.manual_bill_no or payment.auto_bill_no or f"SP-{payment.id}").strip()
    desc = f"Supplier payment ({bill})"
    try:
        supplier_obj = Supplier.query.get(getattr(payment, 'supplier_id', None))
        if supplier_obj and supplier_obj.name:
            desc = f"Supplier payment to {supplier_obj.name} ({bill})"
    except Exception:
        pass

    note_bits = [str(getattr(payment, 'note', '') or '').strip(), f"Method: {getattr(payment, 'method', '') or ''}".strip(), _src_marker('SupplierPayment', payment.id)]
    note = " ".join([x for x in note_bits if x]).strip()
    _sync_linked_supplier_payment_tx(
        kind='SupplierPayment',
        src_id=payment.id,
        from_account_id=getattr(payment, 'payment_account_id', None),
        amount=float(getattr(payment, 'amount', 0) or 0),
        date_posted=getattr(payment, 'date_posted', None),
        description=desc,
        note=note,
        is_void=bool(getattr(payment, 'is_void', False))
    )


