"""Domain service module — extracted from legacy ERP core."""
from __future__ import annotations

import os
import io
import secrets
import json
import calendar
import threading
import time
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
from app.services.billing import (
    _bill_no_variants,
    _entry_best_bill_ref,
    find_bill_conflict,
    normalize_auto_bill,
    normalize_manual_bill,
    parse_bill_kind,
)
from app.services.ledgers import (
    _payment_receipt_pending_bill_rows,
)
from app.services.lookups import (
    get_client_by_input,
)
from app.services.sales_core import (
    _direct_sale_bill_refs,
    _direct_sale_client_identity,
    _entry_client_scope_filter,
    _pending_client_scope_filter,
)
from app.services.time_money import (
    pk_now,
)
from app.services.void_rebuild import (
    _booking_bill_refs,
    _set_payment_receipt_pending_bill_void_state,
    _unvoid_direct_sale_entries_and_apply_stock,
    _void_direct_sale_entries_and_restore_stock,
)
from app.services.waive import (
    _direct_sale_waive_marker,
    _sync_direct_sale_waive_off,
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

def _start_hourly_backup_worker():
    if state.HOURLY_BACKUP_WORKER_STARTED:
        return
    t = threading.Thread(target=_hourly_backup_worker_loop, daemon=True, name='hourly-backup-mailer')
    t.start()
    state.HOURLY_BACKUP_WORKER_STARTED = True


def _start_reconcile_worker():
    if state.RECON_WORKER_STARTED:
        return
    if not _AUTO_RECONCILE_ENABLED:
        return
    t = threading.Thread(
        target=run_auto_reconcile,
        kwargs={
            'app': app,
            'interval_seconds': _AUTO_RECONCILE_INTERVAL_SEC,
            'tolerance': _AUTO_RECONCILE_TOL,
            'fix': _AUTO_RECONCILE_FIX,
        },
        daemon=True,
        name='auto-reconcile'
    )
    t.start()
    state.RECON_WORKER_STARTED = True


def _root_backup_dir():
    root_dir = os.path.join(basedir, 'instance', 'root_hourly_backups')
    os.makedirs(root_dir, exist_ok=True)
    return root_dir


def _normalize_csv_emails(raw_value):
    return [x.strip() for x in str(raw_value or '').split(',') if x.strip()]


def _get_or_create_root_backup_settings():
    row = RootBackupSettings.query.first()
    if row:
        return row
    row = RootBackupSettings(
        enabled=False,
        frequency='hourly',
        include_full_raw_xlsx=True,
        include_sqlite_db=True,
        keep_history_count=200,
        subject_prefix='PWARE Root Backup'
    )
    settings_obj = Settings.query.first()
    if settings_obj and (settings_obj.company_email or '').strip():
        row.recipient_emails = (settings_obj.company_email or '').strip()
    db.session.add(row)
    db.session.commit()
    return row


def _build_root_backup_zip(settings_row):
    """Compatibility adapter over the authoritative backup service.

    The ZIP exists only in memory for the legacy download/email UI. The sole
    persisted copy remains the validated, retention-controlled backup folder.
    """
    from app.services.maintenance import create_backup

    result = create_backup(app._get_current_object(), reason='root-backup-ui')
    backup_dir = result['path']
    zip_io = io.BytesIO()
    zip_name = f"{result['name']}.zip"
    with zipfile.ZipFile(zip_io, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, names in os.walk(backup_dir):
            for name in sorted(names):
                path = os.path.join(root, name)
                zf.write(path, os.path.relpath(path, backup_dir))
    return zip_name, backup_dir, zip_io.getvalue()


def _cleanup_root_backup_history(keep_count):
    keep_count = max(1, int(keep_count or 200))
    rows = RootBackupEmailHistory.query.order_by(RootBackupEmailHistory.created_at.desc()).all()
    if len(rows) <= keep_count:
        return
    for row in rows[keep_count:]:
        fpath = (row.backup_path or '').strip()
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass
        db.session.delete(row)
    db.session.commit()


def _log_root_backup_history(settings_row, trigger_type, status, recipients, subject, attachment_name, attachment_size_bytes, backup_path, message):
    db.session.add(RootBackupEmailHistory(
        trigger_type=(trigger_type or 'auto'),
        status=(status or 'failed'),
        recipient_emails=', '.join(recipients or []),
        subject=subject,
        attachment_name=attachment_name,
        attachment_size_kb=(int(attachment_size_bytes / 1024) if attachment_size_bytes else 0),
        backup_path=(backup_path or ''),
        message=(message or '')[:1000]
    ))
    settings_row.last_sent_at = pk_now()
    settings_row.last_status = status
    settings_row.last_message = (message or '')[:500]
    db.session.commit()
    _cleanup_root_backup_history(settings_row.keep_history_count or 200)


def _send_hourly_all_tenants_backup_email(trigger_type='auto-hourly', force_send=False):
    settings_row = _get_or_create_root_backup_settings()
    if not force_send and not settings_row.enabled:
        return False, 'Root backup automation disabled'

    recipients = _normalize_csv_emails(settings_row.recipient_emails)
    if not recipients:
        return False, 'No recipient emails configured'

    zip_name = ''
    zip_path = ''
    zip_bytes = b''
    try:
        zip_name, zip_path, zip_bytes = _build_root_backup_zip(settings_row=settings_row)
        msg = 'Validated backup saved locally. Email delivery is not enabled in this build.'
        _log_root_backup_history(
            settings_row=settings_row,
            trigger_type=trigger_type,
            status='success',
            recipients=recipients,
            subject=(settings_row.subject_prefix or 'PWARE Root Backup').strip(),
            attachment_name=zip_name,
            attachment_size_bytes=len(zip_bytes or b''),
            backup_path=zip_path,
            message=msg
        )
        return True, msg
    except Exception as e:
        try:
            _log_root_backup_history(
                settings_row=settings_row,
                trigger_type=trigger_type,
                status='failed',
                recipients=recipients,
                subject='Root Backup Failed',
                attachment_name=zip_name,
                attachment_size_bytes=len(zip_bytes or b''),
                backup_path=zip_path,
                message=str(e)
            )
        except Exception:
            db.session.rollback()
        return False, f'Backup send failed: {e}'


def _hourly_backup_worker_loop():
    while True:
        try:
            with app.app_context():
                now = pk_now()
                slot = now.strftime('%Y%m%d%H')
                if now.minute == 0 and state.HOURLY_BACKUP_LAST_SLOT != slot:
                    _send_hourly_all_tenants_backup_email(trigger_type='auto-hourly', force_send=False)
                    state.HOURLY_BACKUP_LAST_SLOT = slot
            time.sleep(30)
        except Exception:
            time.sleep(60)


def _run_reconciliation(apply_fixes=False):
    report = {
        'ran_at': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
        'mode': 'fix' if apply_fixes else 'scan',
        'entries_scanned': 0,
        'broken_refs_count': 0,
        'broken_refs_sample': [],
        'direct_sale_mismatch_count': 0,
        'direct_sale_waive_mismatch_count': 0,
        'booking_mismatch_count': 0,
        'payment_mismatch_count': 0,
        'bill_normalized_count': 0,
        'bill_normalized_sample': [],
        'fixes_applied': 0
    }

    def _track_norm(entity, rid, field, old, new):
        if old == new:
            return
        report['bill_normalized_count'] += 1
        if len(report['bill_normalized_sample']) < 50:
            report['bill_normalized_sample'].append({
                'entity': entity,
                'id': rid,
                'field': field,
                'from': old,
                'to': new
            })
        if apply_fixes:
            report['fixes_applied'] += 1

    # 0) Bill normalization/backfill (legacy #123 / 123 / 123.0 -> SB-<NS>-n / MB NO.<x>)
    for bk in Booking.query.all():
        old_m = (bk.manual_bill_no or '').strip()
        old_a = (bk.auto_bill_no or '').strip()
        new_m = normalize_manual_bill(old_m) if old_m else ''
        new_a = normalize_auto_bill(old_a, namespace=AUTO_BILL_NAMESPACES['BOOKING']) if old_a else ''
        _track_norm('Booking', bk.id, 'manual_bill_no', old_m, new_m)
        _track_norm('Booking', bk.id, 'auto_bill_no', old_a, new_a)
        if apply_fixes:
            bk.manual_bill_no = new_m or None
            bk.auto_bill_no = new_a or None

    for py in Payment.query.all():
        old_m = (py.manual_bill_no or '').strip()
        old_a = (py.auto_bill_no or '').strip()
        new_m = normalize_manual_bill(old_m) if old_m else ''
        new_a = normalize_auto_bill(old_a, namespace=AUTO_BILL_NAMESPACES['PAYMENT']) if old_a else ''
        _track_norm('Payment', py.id, 'manual_bill_no', old_m, new_m)
        _track_norm('Payment', py.id, 'auto_bill_no', old_a, new_a)
        if apply_fixes:
            py.manual_bill_no = new_m or None
            py.auto_bill_no = new_a or None

    for sp in SupplierPayment.query.all():
        old_m = (sp.manual_bill_no or '').strip()
        old_a = (sp.auto_bill_no or '').strip()
        new_m = normalize_manual_bill(old_m) if old_m else ''
        new_a = normalize_auto_bill(old_a, namespace=AUTO_BILL_NAMESPACES['SUPPLIER_PAYMENT']) if old_a else ''
        _track_norm('SupplierPayment', sp.id, 'manual_bill_no', old_m, new_m)
        _track_norm('SupplierPayment', sp.id, 'auto_bill_no', old_a, new_a)
        if apply_fixes:
            sp.manual_bill_no = new_m or None
            sp.auto_bill_no = new_a or None

    for ds in DirectSale.query.all():
        old_m = (ds.manual_bill_no or '').strip()
        old_a = (ds.auto_bill_no or '').strip()
        new_m = normalize_manual_bill(old_m) if old_m else ''
        new_a = normalize_auto_bill(old_a, namespace=AUTO_BILL_NAMESPACES['DIRECT_SALE']) if old_a else ''
        _track_norm('DirectSale', ds.id, 'manual_bill_no', old_m, new_m)
        _track_norm('DirectSale', ds.id, 'auto_bill_no', old_a, new_a)
        if apply_fixes:
            ds.manual_bill_no = new_m or None
            ds.auto_bill_no = new_a or None

    for grn in GRN.query.all():
        old_m = (grn.manual_bill_no or '').strip()
        old_a = (grn.auto_bill_no or '').strip()
        new_m = normalize_manual_bill(old_m) if old_m else ''
        new_a = normalize_auto_bill(old_a, namespace=AUTO_BILL_NAMESPACES['GRN']) if old_a else ''
        _track_norm('GRN', grn.id, 'manual_bill_no', old_m, new_m)
        _track_norm('GRN', grn.id, 'auto_bill_no', old_a, new_a)
        if apply_fixes:
            grn.manual_bill_no = new_m or None
            grn.auto_bill_no = new_a or None

    for ent in Entry.query.all():
        old_a = (ent.auto_bill_no or '').strip()
        new_a = normalize_auto_bill(old_a, namespace=AUTO_BILL_NAMESPACES['ENTRY']) if old_a else ''
        _track_norm('Entry', ent.id, 'auto_bill_no', old_a, new_a)
        if apply_fixes:
            ent.auto_bill_no = new_a or None

    for inv in Invoice.query.all():
        old_no = (inv.invoice_no or '').strip()
        # Preserve INV-* style invoice numbers; normalize numeric/custom refs as MB.
        new_no = old_no
        if old_no and not old_no.upper().startswith('INV-'):
            new_no = normalize_manual_bill(old_no)
        _track_norm('Invoice', inv.id, 'invoice_no', old_no, new_no)
        if apply_fixes:
            inv.invoice_no = new_no or None

    for pb in PendingBill.query.all():
        old_no = (pb.bill_no or '').strip()
        old_kind = (pb.bill_kind or '').strip().upper()
        is_manual = bool(pb.is_manual)
        if old_no:
            if is_manual:
                new_no = normalize_manual_bill(old_no)
            else:
                new_no = normalize_auto_bill(old_no, namespace=AUTO_BILL_NS_DEFAULT)
                if not new_no:
                    new_no = normalize_manual_bill(old_no)
            new_kind = parse_bill_kind(new_no)
        else:
            new_no = ''
            new_kind = 'UNKNOWN'
        _track_norm('PendingBill', pb.id, 'bill_no', old_no, new_no)
        _track_norm('PendingBill', pb.id, 'bill_kind', old_kind, new_kind)
        if apply_fixes:
            pb.bill_no = new_no or None
            pb.bill_kind = new_kind

    # 1) Broken/ambiguous bill refs in transaction entries.
    entries = Entry.query.filter(Entry.is_void == False).all()
    for e in entries:
        ref = _entry_best_bill_ref(e)
        if not ref or ref.upper().startswith('UNBILLED'):
            continue
        report['entries_scanned'] += 1

        owner = find_bill_conflict(ref)
        if owner:
            continue

        resolved_variant = None
        for candidate in _bill_no_variants(ref):
            if candidate == ref:
                continue
            if find_bill_conflict(candidate):
                resolved_variant = candidate
                break

        if resolved_variant and apply_fixes:
            if (e.bill_no or '').strip() == ref:
                e.bill_no = resolved_variant
                report['fixes_applied'] += 1
                continue
            if (e.auto_bill_no or '').strip() == ref:
                e.auto_bill_no = resolved_variant
                report['fixes_applied'] += 1
                continue

        report['broken_refs_count'] += 1
        if len(report['broken_refs_sample']) < 25:
            report['broken_refs_sample'].append({
                'entry_id': e.id,
                'ref': ref,
                'client': e.client or '',
                'client_code': e.client_code or ''
            })

    # 2) Direct sale consistency (sale <-> entries/pending/rent void flags).
    for sale in DirectSale.query.all():
        refs = _direct_sale_bill_refs(sale)
        client_code, client_name = _direct_sale_client_identity(sale)
        ds_q = Entry.query.filter(Entry.bill_no.in_(refs), Entry.nimbus_no == 'Direct Sale')
        entry_client_filter = _entry_client_scope_filter(client_code, client_name)
        if entry_client_filter is not None:
            ds_q = ds_q.filter(entry_client_filter)
        ds_entries = ds_q.all()
        pb_q = PendingBill.query.filter(PendingBill.bill_no.in_(refs))
        pending_client_filter = _pending_client_scope_filter(client_code, client_name)
        if pending_client_filter is not None:
            pb_q = pb_q.filter(pending_client_filter)
        pb_rows = pb_q.all()
        rent_rows = DeliveryRent.query.filter_by(sale_id=sale.id).all()

        mismatch = False
        if sale.is_void and any(not x.is_void for x in ds_entries + pb_rows + rent_rows):
            mismatch = True
        if (not sale.is_void) and any(x.is_void for x in ds_entries + pb_rows + rent_rows):
            mismatch = True

        if mismatch:
            report['direct_sale_mismatch_count'] += 1
            if apply_fixes:
                if sale.is_void:
                    _void_direct_sale_entries_and_restore_stock(sale, refs=refs, client_code=client_code, client_name=client_name)
                    scoped_pb_q = PendingBill.query.filter(PendingBill.bill_no.in_(refs))
                    if pending_client_filter is not None:
                        scoped_pb_q = scoped_pb_q.filter(pending_client_filter)
                    scoped_pb_q.update({'is_void': True}, synchronize_session=False)
                    DeliveryRent.query.filter_by(sale_id=sale.id).update({'is_void': True}, synchronize_session=False)
                else:
                    _unvoid_direct_sale_entries_and_apply_stock(sale, refs=refs, client_code=client_code, client_name=client_name)
                    scoped_pb_q = PendingBill.query.filter(PendingBill.bill_no.in_(refs))
                    if pending_client_filter is not None:
                        scoped_pb_q = scoped_pb_q.filter(pending_client_filter)
                    scoped_pb_q.update({'is_void': False}, synchronize_session=False)
                    DeliveryRent.query.filter_by(sale_id=sale.id).update({'is_void': False}, synchronize_session=False)
                report['fixes_applied'] += 1

        sale_waive_rows = WaiveOff.query.filter(
            WaiveOff.payment_id.is_(None),
            WaiveOff.note == _direct_sale_waive_marker(sale.id)
        ).all()
        expected_waive = max(0.0, float(sale.discount or 0))
        waive_mismatch = False
        if expected_waive <= 0 and sale_waive_rows:
            waive_mismatch = True
        elif expected_waive > 0:
            if not sale_waive_rows:
                waive_mismatch = True
            elif any(abs(float((w.amount or 0) - expected_waive)) > 0.01 for w in sale_waive_rows):
                waive_mismatch = True
            elif any(bool(w.is_void) != bool(sale.is_void) for w in sale_waive_rows):
                waive_mismatch = True

        if waive_mismatch:
            report['direct_sale_waive_mismatch_count'] += 1
            if apply_fixes:
                _sync_direct_sale_waive_off(sale)
                report['fixes_applied'] += 1

    # 3) Booking consistency (booking <-> pending bill void flags).
    for bk in Booking.query.all():
        refs = _booking_bill_refs(bk)
        client_obj = get_client_by_input(bk.client_name or '')
        pending_client_filter = _pending_client_scope_filter(
            client_obj.code if client_obj else None,
            client_obj.name if client_obj else bk.client_name
        )
        rows_q = PendingBill.query.filter(PendingBill.bill_no.in_(refs))
        if pending_client_filter is not None:
            rows_q = rows_q.filter(pending_client_filter)
        rows = rows_q.all()
        mismatch = any(pb.is_void != bool(bk.is_void) for pb in rows)
        if mismatch:
            report['booking_mismatch_count'] += 1
            if apply_fixes:
                rows_q.update({'is_void': bool(bk.is_void)}, synchronize_session=False)
                report['fixes_applied'] += 1

    # 4) Payment consistency (payment <-> payment-receipt pending bill void flags).
    for pay in Payment.query.all():
        rows = _payment_receipt_pending_bill_rows(pay)
        mismatch = any(pb.is_void != bool(pay.is_void) for pb in rows)
        if mismatch:
            report['payment_mismatch_count'] += 1
            if apply_fixes:
                _set_payment_receipt_pending_bill_void_state(pay, is_void=bool(pay.is_void))
                report['fixes_applied'] += 1

    return report


