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

def _safe_download_name(name, default='document.pdf'):
    raw = (name or '').strip()
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', raw).strip('._')
    if not safe:
        safe = default
    if '.' not in safe:
        safe = f"{safe}.pdf"
    return safe


def _download_stamp(dt=None):
    dt = dt or pk_now()
    return dt.strftime('%d-%m-%Y_%I-%M%p')


def _download_filename(section, ext='pdf', dt=None):
    sec = re.sub(r'[^A-Za-z0-9]+', '', (section or 'DOWNLOAD')).upper() or 'DOWNLOAD'
    ext = (ext or '').lstrip('.').lower() or 'dat'
    return f"{sec}-{_download_stamp(dt)}.{ext}"


def _ext_from_name(name, fallback='dat'):
    ext = os.path.splitext(str(name or ''))[1].lstrip('.').lower()
    return ext or fallback


def _try_render_weasy_pdf(rendered_html, download_name, disposition='attachment'):
    """
    Render PDF with WeasyPrint only when available.
    On first failure, disable future attempts to avoid repeated noisy warnings.

    When WeasyPrint cannot be used (missing module, or missing system libraries
    such as pango on shared hosts) the request is NOT downgraded to HTML: a
    genuine PDF is produced by the ReportLab fallback instead, so a download
    that asked for a PDF always receives one.
    """

    safe_name = _safe_download_name(download_name, default='document.pdf')
    try:
        if state.WEASYPRINT_MODULE is None:
            # WeasyPrint prints dependency warnings to stderr on import; silence them here.
            with redirect_stderr(io.StringIO()):
                state.WEASYPRINT_MODULE = importlib.import_module('flask_weasyprint')
        # If a previous attempt already confirmed WeasyPrint is unavailable, bail early.
        if state.WEASYPRINT_MODULE is False:
            return _render_reportlab_pdf(rendered_html, safe_name, disposition)
        # Enforce consistent PDF paper format across all exports.
        # Required layout: width 14.8cm, height 21cm with 1cm margins on all sides.
        forced_page_css = (
            "<style>"
            "@page { size: 14.8cm 21cm; margin: 1cm; }"
            "</style>"
        )
        html_for_pdf = f"{forced_page_css}{rendered_html}"
        # Render with the *print* media type so the app's own ``@media print``
        # rules apply.  Those rules hide the sidebar, modals, loading overlay
        # and anything marked ``d-print-none``; without this the exported
        # document carries the whole application chrome.
        try:
            html_doc = state.WEASYPRINT_MODULE.HTML(
                string=html_for_pdf, media_type='print'
            )
        except TypeError:
            # Older flask_weasyprint builds without media_type support.
            html_doc = state.WEASYPRINT_MODULE.HTML(string=html_for_pdf)
        response = state.WEASYPRINT_MODULE.render_pdf(
            html_doc,
            download_name=safe_name
        )
        response.headers['Content-Disposition'] = f'{disposition}; filename={safe_name}'
        _disable_response_cache(response)
        return response
    except ModuleNotFoundError:
        state.WEASYPRINT_MODULE = False
        logging.getLogger(__name__).warning(
            'flask_weasyprint is not installed; using the ReportLab PDF fallback.'
        )
    except Exception as exc:
        state.WEASYPRINT_MODULE = False
        # A missing native library (pango/cairo) is an expected condition on
        # shared hosts, not an application fault - report it once, briefly, and
        # carry on with the ReportLab path instead of dumping a traceback.
        message = str(exc).lower()
        if 'cannot load library' in message or 'shared object' in message:
            logging.getLogger(__name__).warning(
                'WeasyPrint native libraries unavailable (%s); '
                'using the ReportLab PDF fallback.', type(exc).__name__
            )
        else:
            logging.getLogger(__name__).exception(
                'WeasyPrint PDF generation failed; using the ReportLab PDF fallback.'
            )
    return _render_reportlab_pdf(rendered_html, safe_name, disposition)


def _render_reportlab_pdf(rendered_html, safe_name, disposition):
    """Build a real PDF with ReportLab; return a response, or None if impossible."""
    try:
        from app.services.pdf_fallback import render_html_to_pdf
    except Exception:
        logging.getLogger(__name__).exception('PDF fallback module is not importable.')
        return None

    data = render_html_to_pdf(rendered_html, download_name=safe_name, title=safe_name)
    if not data:
        return None

    response = make_response(data)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Length'] = str(len(data))
    response.headers['Content-Disposition'] = f'{disposition}; filename={safe_name}'
    _disable_response_cache(response)
    return response



def _disable_response_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def save_photo(file):
    """Save uploaded photo and return filename"""
    if file and file.filename != '':
        filename = secure_filename(
            f"{pk_now().strftime('%Y%m%d%H%M%S')}_{file.filename}")
        upload_folder = os.path.join(basedir, 'static', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        file_path = os.path.join(upload_folder, filename)
        file.save(file_path)
        return filename
    return None


