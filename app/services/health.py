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

def _db_debug_counts():
    """Lightweight counts for verifying which DB file is loaded."""
    counts = {}
    try:
        counts['client_total'] = int(db.session.execute(text("SELECT COUNT(*) FROM client")).scalar() or 0)
    except Exception:
        pass
    try:
        counts['client_active'] = int(db.session.execute(text("SELECT COUNT(*) FROM client WHERE COALESCE(is_active, 1)=1")).scalar() or 0)
    except Exception:
        pass
    for t in ['direct_sale', 'booking', 'pending_bill', 'entry', 'payment']:
        try:
            counts[t] = int(db.session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
        except Exception:
            continue
    return counts


def _read_health_snapshot():
    try:
        if os.path.exists(_DB_HEALTH_SNAPSHOT_PATH):
            with open(_DB_HEALTH_SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _write_health_snapshot(snapshot):
    try:
        with open(_DB_HEALTH_SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
        return True
    except Exception:
        return False


def _health_snapshot_payload(counts=None, intentional_operation=None, previous_snapshot=None, reset_context=None):
    counts = counts if counts is not None else _collect_health_counts()
    previous_snapshot = previous_snapshot if previous_snapshot is not None else _read_health_snapshot()
    active_reset_context = reset_context or state.RESET_CONTEXT
    snapshot = {
        'schema_version': 2,
        'db_path': db_path,
        'mtime': os.path.getmtime(db_path) if os.path.exists(db_path) else None,
        'size': os.path.getsize(db_path) if os.path.exists(db_path) else None,
        'counts': counts,
        'total': sum(counts.values()),
        'captured_at': pk_now().strftime('%Y-%m-%d %H:%M:%S')
    }
    last_intentional = intentional_operation
    if not last_intentional and previous_snapshot:
        last_intentional = previous_snapshot.get('last_intentional_operation')
    if last_intentional:
        snapshot['last_intentional_operation'] = last_intentional
    if active_reset_context:
        snapshot['intentional_reset'] = True
        snapshot['reset_source'] = active_reset_context
    return snapshot


def _rebuild_health_snapshot(intentional_operation=None, reset_context=None):
    snapshot = _health_snapshot_payload(
        intentional_operation=intentional_operation,
        reset_context=reset_context
    )
    if not _write_health_snapshot(snapshot):
        raise RuntimeError('Unable to write DB health snapshot.')
    return snapshot


def _guard_db_file_before_bootstrap():
    if os.path.exists(db_path):
        if os.path.getsize(db_path) < _DB_HEALTH_MIN_BYTES and not _ALLOW_EMPTY_DB:
            raise RuntimeError(
                f"DB health check failed: '{db_path}' is too small to be a valid data file. "
                "Set ALLOW_EMPTY_DB=1 to initialize a new database explicitly."
            )
        _guard_db_integrity(db_path)
        return
    # A deleted database is an intentional fresh-start state.  Remember this
    # for the post-bootstrap health check so an old snapshot from the deleted
    # file cannot make the new, empty database look like accidental data loss.
    app.config['_DB_FILE_WAS_MISSING'] = True
    logging.getLogger('app').info(
        "DB file not found at %s; initializing a fresh database.",
        db_path,
    )


def _guard_db_integrity(path: str) -> None:
    """Fail fast with an actionable message when the SQLite file is corrupt.

    A malformed database otherwise surfaces later as an opaque
    ``sqlite3.DatabaseError: database disk image is malformed`` from deep
    inside SQLAlchemy during ``db.create_all()`` / the first request, which is
    hard to attribute to the DB file. ``PRAGMA quick_check`` verifies page
    integrity without re-validating every index entry, so it stays fast enough
    for a startup guard on typical single-shop databases.
    """
    recovery_hint = (
        "Recover it with `sqlite3 <db> '.recover' | sqlite3 recovered.db` "
        "(after backing up the original plus its -wal/-shm files), or start "
        "fresh with ALLOW_EMPTY_DB=1 and ALLOW_DB_DROP=1."
    )
    try:
        con = sqlite3.connect(path)
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"DB health check failed: '{path}' cannot be read — {exc}. "
            "The SQLite file is corrupt or is not a valid database. "
            f"{recovery_hint}"
        ) from exc
    if len(rows) != 1 or rows[0][0] != "ok":
        details = " | ".join(" ".join(str(c) for c in r) for r in rows[:5]) or "unknown error"
        raise RuntimeError(
            f"DB health check failed: '{path}' failed SQLite quick_check: "
            f"{details}. {recovery_hint}"
        )


def _collect_health_counts():
    return {
        'user': db.session.query(func.count(User.id)).scalar() or 0,
        'client': db.session.query(func.count(Client.id)).scalar() or 0,
        'booking': db.session.query(func.count(Booking.id)).scalar() or 0,
        'direct_sale': db.session.query(func.count(DirectSale.id)).scalar() or 0,
        'entry': db.session.query(func.count(Entry.id)).scalar() or 0,
        'pending_bill': db.session.query(func.count(PendingBill.id)).scalar() or 0
    }


def _db_health_check_after_bootstrap():
    warnings = []
    errors = []

    def _norm_path(p):
        if not p:
            return None
        try:
            p = os.path.expanduser(str(p))
            p = os.path.abspath(p)
            p = os.path.normpath(p)
            p = os.path.normcase(p)
            return p
        except Exception:
            return str(p)

    if os.path.exists(db_path) and os.path.getsize(db_path) < _DB_HEALTH_MIN_BYTES:
        warnings.append(f"DB file '{db_path}' is unusually small ({os.path.getsize(db_path)} bytes).")

    snapshot = _read_health_snapshot()
    counts = _collect_health_counts()
    total_now = sum(counts.values())

    if app.config.pop('_DB_FILE_WAS_MISSING', False):
        warnings.append(
            "A fresh database was initialized because the previous database file was not present."
        )
        app.config['DB_HEALTH_WARNINGS'] = warnings
        # Replace the stale baseline with the new database baseline.  The
        # default admin account is intentionally included in the user count;
        # business tables remain empty until data is imported or entered.
        _write_health_snapshot(
            _health_snapshot_payload(counts=counts, previous_snapshot=None)
        )
        return

    if snapshot:
        prev_path = snapshot.get('db_path')
        prev_counts = snapshot.get('counts') or {}
        prev_total = int(snapshot.get('total') or 0)
        intentional_reset = (
            snapshot.get('intentional_reset') is True
            and snapshot.get('reset_source') == 'granular_wipe'
        )
        if intentional_reset:
            warnings.append(
                "DB health baseline was created by an intentional granular wipe. "
                "Startup protection remains active against drops below this baseline."
            )

        prev_norm = _norm_path(prev_path)
        now_norm = _norm_path(db_path)
        if prev_path and prev_norm != now_norm and not _ALLOW_DB_DROP:
            # Path changes are expected when moving the same project/DB between machines
            # or operating systems (e.g., Linux -> Windows). Only hard-fail when the
            # previous DB path is still reachable on this machine, because that is a
            # strong signal the app might be starting against the wrong file.
            if os.path.exists(str(prev_path)):
                errors.append(
                    f"DB path changed from '{prev_path}' to '{db_path}'. "
                    "Refusing to start to prevent data loss. Set ALLOW_DB_DROP=1 to override."
                )
            else:
                warnings.append(
                    f"DB path changed from '{prev_path}' to '{db_path}' (previous path not found on this machine). "
                    "Continuing and updating the health snapshot."
                )

        if prev_total > 0:
            if total_now == 0 and not _ALLOW_DB_DROP:
                errors.append("DB appears empty compared to last snapshot. Refusing to start.")
            if total_now < prev_total:
                drop = prev_total - total_now
                ratio = (total_now / prev_total) if prev_total else 1.0
                if (drop >= _DB_HEALTH_DROP_MIN or ratio < _DB_HEALTH_DROP_RATIO) and not _ALLOW_DB_DROP:
                    errors.append(
                        f"DB row count dropped from {prev_total} to {total_now}. "
                        "Refusing to start to prevent partial data loss. "
                        "Set ALLOW_DB_DROP=1 to override."
                    )

        for key, prev_val in prev_counts.items():
            if key not in counts:
                # Health counters can change between app versions (e.g., single-store removes tenant tracking).
                continue
            now_val = counts.get(key, 0)
            if prev_val and now_val < prev_val:
                drop = prev_val - now_val
                ratio = (now_val / prev_val) if prev_val else 1.0
                if (drop >= _DB_HEALTH_DROP_MIN or ratio < _DB_HEALTH_DROP_RATIO) and not _ALLOW_DB_DROP:
                    errors.append(
                        f"Table '{key}' rows dropped from {prev_val} to {now_val}. "
                        "Refusing to start to prevent partial data loss. "
                        "Set ALLOW_DB_DROP=1 to override."
                    )

    snapshot_next = _health_snapshot_payload(counts=counts, previous_snapshot=snapshot)

    if warnings:
        app.config['DB_HEALTH_WARNINGS'] = warnings
    if errors:
        raise RuntimeError("DB health check failed: " + " | ".join(errors))
    _write_health_snapshot(snapshot_next)


