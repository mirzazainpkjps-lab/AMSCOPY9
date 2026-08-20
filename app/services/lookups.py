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

def get_client_by_input(input_str):
    """Helper to find client by name, code, or mixed string."""
    if not input_str:
        return None
    input_str = input_str.strip()

    # 1. Exact Code Match
    client = Client.query.filter_by(code=input_str).first()
    if client: return client

    # 2. Exact Name Match
    client = Client.query.filter_by(name=input_str).first()
    if client: return client

    # 3. Try to extract code from format "Name (Code)" or "Code - Name"
    match = re.search(r'\((tmpc-\d+|FBM-\d+|FBMCL-\d+)\)$', input_str, re.IGNORECASE)
    if match:
        code = match.group(1)
        client = Client.query.filter_by(code=code).first()
        if client: return client

    # 4. Case-insensitive Code/Name
    client = Client.query.filter(or_(Client.code.ilike(input_str), Client.name.ilike(input_str))).first()
    if client: return client

    return None


MATERIAL_NOT_SELECTED_MSG = (
    'Material not selected. Please select an existing material from the Material Master.'
)


def get_material_by_input(input_str):
    """Helper to find material by name or code.

    This is an exact identity lookup (case-insensitive). It never creates a
    Material and never treats a partial search string as a match.
    """
    if not input_str:
        return None
    input_str = input_str.strip()

    mat = Material.query.filter(or_(Material.name == input_str, Material.code == input_str)).first()
    if mat: return mat

    mat = Material.query.filter(or_(Material.name.ilike(input_str), Material.code.ilike(input_str))).first()
    return mat


def _material_lookup_key(value):
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def _material_text_matches_record(mat, typed_text):
    """True when typed text is empty or is exactly this material's name/code."""
    typed = _material_lookup_key(typed_text)
    if not typed:
        return True
    if not mat:
        return False
    return typed in {
        _material_lookup_key(mat.name),
        _material_lookup_key(mat.code),
    }


def resolve_transaction_material(material_id=None, typed_text=None, *, require_active=True, allow_inactive_names=None):
    """Resolve an existing Material Master record for an inventory transaction.

    ``material_id`` is authoritative when provided. Typed text is only a
    search/display value and is never used to create a Material.

    Returns the Material, or None when both id and text are empty (blank row).
    Raises ValueError when the user supplied text/id that is not a selected
    existing Material Master record.
    """
    allow_inactive_names = {
        (n or '').strip().lower()
        for n in (allow_inactive_names or [])
        if (n or '').strip()
    }

    raw_id = '' if material_id is None else str(material_id).strip()
    typed = (typed_text or '').strip()
    mat = None

    if raw_id:
        try:
            mid = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        mat = db.session.get(Material, mid)
        if not mat:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        # A stale hidden id after the user edited the search box is invalid.
        if typed and not _material_text_matches_record(mat, typed):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    elif typed:
        mat = get_material_by_input(typed)
        if not mat:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    else:
        return None

    if require_active and not bool(getattr(mat, 'is_active', True)):
        hist = (mat.name or '').strip().lower()
        if hist not in allow_inactive_names:
            raise ValueError(f'Material "{mat.name}" was not found or is suspended.')
    return mat


def get_supplier_by_input(input_str):
    """Helper to find supplier by name."""
    if not input_str:
        return None
    input_str = input_str.strip()
    return Supplier.query.filter(func.lower(Supplier.name) == input_str.lower()).first()


def get_or_create_delivery_person(name_input, phone=None):
    name = (name_input or '').strip()
    if not name:
        return None
    phone_value = (phone or '').strip()
    existing = DeliveryPerson.query.filter(func.lower(func.trim(DeliveryPerson.name)) == name.lower()).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        if phone_value:
            existing.phone = phone_value
        return existing
    # DeliveryPerson.name is globally unique in current schema.
    # Under tenant-scoped ORM filters, a name that exists in another tenant can be invisible here.
    # Use raw SQL fallback to avoid duplicate insert integrity errors.
    global_row = db.session.execute(
        text("SELECT id FROM delivery_person WHERE lower(trim(name)) = :n LIMIT 1"),
        {'n': name.lower()}
    ).fetchone()
    if global_row:
        return None
    dp = DeliveryPerson(name=name, phone=phone_value or None, is_active=True)
    db.session.add(dp)
    db.session.flush()
    return dp


