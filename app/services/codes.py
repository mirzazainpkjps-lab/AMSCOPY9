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

def generate_client_code():
    """Generate next client code in format FBMCL-00001."""
    prefix = 'FBMCL-'
    max_num = 0
    rx = re.compile(r'^FBMCL-(\d+)$', re.IGNORECASE)
    for (raw_code,) in Client.query.with_entities(Client.code).all():
        code = (raw_code or '').strip()
        m = rx.match(code)
        if not m:
            continue
        try:
            max_num = max(max_num, int(m.group(1)))
        except Exception:
            continue
    return f"{prefix}{(max_num + 1):05d}"


def generate_material_code():
    """Generate next material code in format tmpm-00001"""
    last_material = Material.query.filter(
        Material.code.like('tmpm-%')).order_by(Material.code.desc()).first()
    if last_material and last_material.code:
        try:
            num = int(last_material.code.split('-')[1]) + 1
        except:
            num = 1
    else:
        num = 1
    return f"tmpm-{num:05d}"


def _material_category_code_segment(category):
    """Return category segment for material code (e.g., CEM, ST)."""
    raw_name = ((category.name if category else 'General') or 'General').strip().upper()
    normalized = re.sub(r'[^A-Z0-9]+', '', raw_name)
    if not normalized:
        return 'GEN'

    static_map = {
        'CEMENT': 'CEM',
        'STEEL': 'ST',
    }
    if normalized in static_map:
        return static_map[normalized]

    words = [w for w in re.split(r'[^A-Z0-9]+', raw_name) if w]
    if len(words) >= 2:
        return ''.join(w[0] for w in words[:3]).upper()
    return normalized[:3].upper()


def _material_code_profile(material_name):
    """Return company prefix and serial width based on material name."""
    nm = (material_name or '').strip().upper()
    if nm.startswith('FT-'):
        return ('FTP', 4)
    return ('FBM', 6)


def _next_material_code_for_category(category, material_name=''):
    """Generate next category-based code like FBMCEM-000000 or FTPCEM-0000."""
    company_prefix, serial_width = _material_code_profile(material_name)
    cat_segment = _material_category_code_segment(category)
    prefix = f"{company_prefix}{cat_segment}"
    max_num = 0
    code_rx = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.IGNORECASE)

    q = Material.query
    if category and getattr(category, 'id', None):
        q = q.filter(Material.category_id == category.id)

    for mat in q.with_entities(Material.code).all():
        code = (mat[0] or '').strip()
        match = code_rx.match(code)
        if not match:
            continue
        try:
            max_num = max(max_num, int(match.group(1)))
        except Exception:
            continue

    return f"{prefix}-{(max_num + 1):0{serial_width}d}"


def _get_default_material_category_id():
    try:
        cat = get_or_create_material_category('General')
        return cat.id if cat else None
    except Exception:
        return None


