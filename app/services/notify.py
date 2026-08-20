"""Domain service module — extracted from legacy ERP core."""
from __future__ import annotations

import os
import io
import json
import calendar
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
from app.services.risk import (
    _normalize_risk_label,
    _pending_bill_age_days,
    _pending_bill_category,
    _pending_bill_risk,
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


def _build_notification_rows(category_filter='all', status_filter='all', risk_filter='all', q=''):
    q = (q or '').strip().lower()
    bills = PendingBill.query.filter(PendingBill.is_void == False).all()
    contact_count_rows = db.session.query(
        FollowUpContact.pending_bill_id,
        func.count(FollowUpContact.id)
    ).group_by(FollowUpContact.pending_bill_id).all()
    contact_count_by_bill = {bill_id: int(cnt or 0) for bill_id, cnt in contact_count_rows}
    rows = []
    for pb in bills:
        # Credit follow-up queue: only open credit balances.
        # Exclude paid, zero/negative, and cash-tagged rows.
        if pb.is_paid:
            continue
        if float(pb.amount or 0) <= 0:
            continue
        if pb.is_cash:
            continue

        category = _pending_bill_category(pb)
        age_days = _pending_bill_age_days(pb)
        contact_count = contact_count_by_bill.get(pb.id, 0)
        score, risk_level = _pending_bill_risk(pb, contact_count=contact_count)
        status = 'Paid' if pb.is_paid else 'Pending'
        row = {
            'bill': pb,
            'category': category,
            'status': status,
            'age_days': age_days,
            'risk_score': score,
            'risk_level': risk_level,
            'risk_level_key': _normalize_risk_label(risk_level),
            'amount': float(pb.amount or 0),
            'client_text': f"{pb.client_name or ''} {pb.client_code or ''}".strip(),
            'contact_count': contact_count
        }
        if category_filter != 'all':
            if category_filter == 'billed' and category != 'Billed':
                continue
            if category_filter == 'unbilled' and category != 'Unbilled':
                continue
            if category_filter == 'open_khata' and category != 'Open Khata':
                continue
            if category_filter == 'cash_unbilled' and category != 'Unbilled Cash':
                continue
            if category_filter == 'cash_paid' and category != 'Cash Paid':
                continue
        if status_filter != 'all' and status.lower() != status_filter.lower():
            continue
        if risk_filter != 'all' and _normalize_risk_label(risk_level) != _normalize_risk_label(risk_filter):
            continue
        if q:
            combined = f"{pb.client_name or ''} {pb.client_code or ''} {pb.bill_no or ''} {pb.reason or ''}".lower()
            if q not in combined:
                continue
        rows.append(row)

    rows.sort(key=lambda r: (r['risk_score'], r['age_days'], r['amount']), reverse=True)
    return rows


def _resolve_reminder_with_contact(rem, response_text, channel='Call', note='', contacted_at=None, created_by=''):
    if not rem:
        return False, 'Reminder not found'
    if not response_text:
        return False, 'Customer response is required'

    if channel not in ['Call', 'WhatsApp', 'SMS', 'Email', 'Visit', 'Other']:
        channel = 'Other'
    contact_time = contacted_at or pk_now()

    db.session.add(FollowUpContact(
        pending_bill_id=rem.pending_bill_id,
        reminder_id=rem.id,
        contacted_at=contact_time,
        channel=channel,
        response=response_text[:200],
        note=(note or 'Reminder marked done')[:500],
        created_by=created_by or ''
    ))
    rem.is_done = True
    rem.acknowledged_at = pk_now()
    db.session.commit()
    return True, 'Reminder closed and history saved'


