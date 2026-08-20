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

def _material_ledger_recent(client_obj, only_booking=True, limit_per_material=5, cutoff_dt=None):
    if not client_obj:
        return []
    events_by_material = {}
    client_name_norm = (client_obj.name or '').strip().lower()

    bookings = Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).order_by(Booking.date_posted.asc()).all()

    for b in bookings:
        bill_ref = b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}"
        for item in b.items:
            mat = (item.material_name or '').strip()
            if not mat:
                continue
            events_by_material.setdefault(mat, []).append({
                'date_dt': b.date_posted,
                'date_str': b.date_posted.strftime('%Y-%m-%d') if b.date_posted else '',
                'bill_no': bill_ref,
                'material': mat,
                'material_display': mat,
                'qty_added': float(item.qty or 0),
                'qty_dispatched': 0,
                'source': 'Booking'
            })

    delivery_q = Entry.query.filter(
        Entry.type == 'OUT',
        Entry.is_void == False,
        or_(
            Entry.client_code == client_obj.code,
            func.lower(func.trim(Entry.client)) == client_name_norm
        )
    )
    if only_booking:
        delivery_q = delivery_q.filter(Entry.client_category == 'Booking Delivery')
    deliveries = delivery_q.all()

    for d in deliveries:
        mat = (d.booked_material or d.material or '').strip()
        if not mat:
            continue
        dt_val = _parse_dt_safe(f"{d.date} {d.time}") or _parse_dt_safe(d.date) or datetime.min
        bill_ref = d.bill_no or d.auto_bill_no or ''
        material_display = mat
        if d.booked_material and d.material and d.booked_material != d.material:
            material_display = f"{d.booked_material}>ALT>{d.material}"
        events_by_material.setdefault(mat, []).append({
            'date_dt': dt_val,
            'date_str': d.date or '',
            'bill_no': bill_ref,
            'material': mat,
            'material_display': material_display,
            'qty_added': 0,
            'qty_dispatched': float(d.qty or 0),
            'source': d.nimbus_no or 'Dispatch'
        })

    grouped = []
    for mat, events in sorted(events_by_material.items(), key=lambda x: x[0].lower()):
        events_sorted = sorted(events, key=lambda e: e['date_dt'] or datetime.min)
        if cutoff_dt:
            events_sorted = [e for e in events_sorted if (e['date_dt'] or datetime.min) <= cutoff_dt]
        running = 0
        for e in events_sorted:
            running += (e['qty_added'] - e['qty_dispatched'])
            e['remaining'] = running
        tail = events_sorted[-limit_per_material:] if limit_per_material else events_sorted
        tail_display = tail
        grouped.append({
            'material': mat,
            'rows': tail_display
        })
    return grouped


def _material_ledger_current_summary(material_ledger_recent, bill_refs):
    if not material_ledger_recent or not bill_refs:
        return []
    refs = {str(r).strip() for r in bill_refs if r}
    if not refs:
        return []
    summary = []
    for group in material_ledger_recent:
        rows = group.get('rows') or []
        if not rows:
            continue
        matched = [r for r in rows if str(r.get('bill_no') or '').strip() in refs]
        if not matched:
            continue
        matched_sorted = sorted(matched, key=lambda r: r.get('date_dt') or datetime.min)
        last_row = matched_sorted[-1]
        dispatched = sum(float(r.get('qty_dispatched') or 0) for r in matched_sorted)
        added = sum(float(r.get('qty_added') or 0) for r in matched_sorted)
        last_stock = float(last_row.get('remaining') or 0) - float(last_row.get('qty_added') or 0) + float(last_row.get('qty_dispatched') or 0)
        remaining = float(rows[-1].get('remaining') or 0)
        summary.append({
            'material': last_row.get('material_display') or group.get('material') or '',
            'dispatched': dispatched,
            'added': added,
            'last_stock': last_stock,
            'remaining': remaining
        })
    return summary


def _pending_bill_category(pb):
    is_open_khata = pb.client_code == OPEN_KHATA_CODE or (pb.client_name or '').strip().upper() == OPEN_KHATA_NAME
    if is_open_khata and pb.is_paid and pb.is_cash:
        return 'Cash Paid'
    if is_open_khata:
        return 'Open Khata'
    if pb.is_cash:
        return 'Unbilled Cash'
    if pb.bill_no:
        return 'Billed'
    return 'Unbilled'


def _pending_bill_age_days(pb):
    created_dt = _parse_dt_safe(pb.created_at)
    if not created_dt:
        return 0
    return max(0, (pk_now() - created_dt).days)


def _normalize_risk_label(value):
    txt = (value or '').strip().lower().replace(' ', '_')
    if txt == 'veryhigh':
        txt = 'very_high'
    return txt


def _risk_label_pretty(value):
    mapping = {
        'low': 'Low',
        'medium': 'Medium',
        'high': 'High',
        'very_high': 'Very High',
    }
    return mapping.get(_normalize_risk_label(value), 'Low')


def _pending_bill_risk(pb, contact_count=0):
    amt = float(pb.amount or 0)
    valid_overrides = {'low', 'medium', 'high', 'very_high'}
    override_key = _normalize_risk_label(getattr(pb, 'risk_override', None))

    if override_key in valid_overrides:
        level_key = override_key
    else:
        if pb.is_paid:
            level_key = 'low'
        elif amt > 10000:
            level_key = 'very_high'
        elif amt > 5000:
            level_key = 'high'
        elif amt > 0:
            level_key = 'medium'
        else:
            level_key = 'low'

        if (not pb.is_paid) and contact_count >= 2:
            level_key = 'very_high'

    severity_rank = {'low': 1, 'medium': 2, 'high': 3, 'very_high': 4}
    score = (severity_rank.get(level_key, 1) * 1000000.0) + amt + _pending_bill_age_days(pb)
    return score, _risk_label_pretty(level_key)


