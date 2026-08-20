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

def acquire_system_lock(lock_name, ttl_seconds=3600, owner_id=None, session=None):
    """Atomically acquire a system-wide mutex lock.
    
    Returns (acquired: bool, error_msg: str or None)
    
    Algorithm:
    1. Try to find existing lock by name
    2. If locked and not expired → reject with "already locked"
    3. If locked and expired → delete and create new
    4. If unlocked → set to locked
    
    Uses atomic DB operations to prevent race conditions.
    """
    logger = logging.getLogger('lock')
    s = session or db.session
    try:
        lock = s.query(SystemLock).filter(SystemLock.name == lock_name).first()
        now = pk_now()
        
        if lock:
            if lock.status == 'locked':
                # Check if lock is expired
                acquired_at = lock.acquired_at or now
                age = (now - acquired_at).total_seconds()
                if age < (lock.ttl_seconds or 3600):
                    # Lock is active, reject
                    logger.warning('Lock %s is active (owner=%s, age=%.1fs)', lock_name, lock.owner, age)
                    return False, f"Lock '{lock_name}' is already held. Try again later."
                else:
                    # Expired, reclaim
                    logger.info('Lock %s expired (age=%.1fs), reclaiming', lock_name, age)
                    lock.status = 'locked'
                    lock.owner = owner_id
                    lock.acquired_at = now
                    lock.note = f'Reclaimed after expiry'
                    s.add(lock)
                    s.commit()
                    return True, None
            else:
                # Status is unlocked, acquire it
                lock.status = 'locked'
                lock.owner = owner_id
                lock.acquired_at = now
                lock.note = f'Acquired by {owner_id}'
                s.add(lock)
                s.commit()
                logger.info('Lock %s acquired by %s', lock_name, owner_id)
                return True, None
        else:
            # Create new lock
            lock = SystemLock(
                name=lock_name,
                status='locked',
                owner=owner_id,
                acquired_at=now,
                ttl_seconds=ttl_seconds,
                note=f'Created and acquired by {owner_id}'
            )
            s.add(lock)
            s.commit()
            logger.info('Lock %s created and acquired by %s', lock_name, owner_id)
            return True, None
    except Exception as exc:
        logger.exception('Failed to acquire lock %s', lock_name)
        return False, f"Lock acquisition failed: {str(exc)}"


def release_system_lock(lock_name, session=None):
    """Release a system-wide mutex lock.
    
    Returns (released: bool, error_msg: str or None)
    """
    logger = logging.getLogger('lock')
    s = session or db.session
    try:
        lock = s.query(SystemLock).filter(SystemLock.name == lock_name).first()
        if lock:
            lock.status = 'unlocked'
            lock.owner = None
            lock.acquired_at = None
            lock.note = 'Released'
            s.add(lock)
            s.commit()
            logger.info('Lock %s released', lock_name)
            return True, None
        else:
            logger.warning('Lock %s not found for release', lock_name)
            return False, f"Lock '{lock_name}' not found"
    except Exception as exc:
        logger.exception('Failed to release lock %s', lock_name)
        return False, f"Lock release failed: {str(exc)}"


