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

def pk_now():
    """Current Pakistan local datetime (naive) for app-wide timestamps."""
    return datetime.now(PK_TZ).replace(tzinfo=None)


def pk_today():
    """Current Pakistan local date for app-wide date defaults."""
    return pk_now().date()


def resolve_posted_datetime(date_str=None, fallback_dt=None):
    """
    Normalize transaction timestamps to PKT:
    - No date: current PK time
    - datetime-local: keep selected PK date+time
    - Selected today (date-only): current PK time
    - Selected past date (date-only): selected date at 00:00:00
    """
    if not date_str:
        return fallback_dt or pk_now()
    try:
        raw = str(date_str).strip()
        for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(raw, fmt)
            except Exception:
                pass
        parsed = datetime.strptime(raw, '%Y-%m-%d')
        if parsed.date() == pk_today():
            return pk_now()
        return parsed
    except Exception:
        return fallback_dt or pk_now()


def _to_float_or_zero(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _norm_text(value):
    """Normalize text for comparison (lowercase, trim, collapse whitespace)."""
    value = (value or '').strip().lower()
    return ' '.join(value.split())


def _money_round(value):
    try:
        d = Decimal(str(value or 0))
    except Exception:
        d = Decimal('0')
    d = d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if d == Decimal('-0.00'):
        d = Decimal('0.00')
    return float(d)


def _parse_discount_fields(raw_discount, raw_reason='', *, label='Discount', require_reason=True):
    """
    Normalize discount + reason from forms.
    - Prevent negative values.
    - Round consistently to 2 decimals.
    - Require explicit reason when discount > 0 (intent guard).
    """
    discount = _money_round(_to_float_or_zero(raw_discount))
    reason = (raw_reason or '').strip()
    if discount < 0:
        raise ValueError(f'{label} cannot be negative.')
    if discount <= 0:
        return 0.0, ''
    if require_reason and not reason:
        raise ValueError(f'{label} reason is required when discount is greater than zero.')
    return discount, reason


def _parse_dt_safe(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    txt = str(value).strip()
    if not txt:
        return None
    # Support both browser datetime-local format (with "T") and standard space-separated values.
    for fmt in (
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d'
    ):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


