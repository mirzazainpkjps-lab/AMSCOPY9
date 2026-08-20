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
from app.services.health import (
    _rebuild_health_snapshot,
)
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

def reset_accounts_domain(session=None):
    """Centralized reset for all account-related state.

    Executes within a single transaction. Deletes transaction/snapshot tables,
    nullifies payment-account links, and resets account balances to zero.

    The function is the single source of truth for account-domain wipes and
    should be updated when new accounting tables are added.
    """
    # Keep backward-compat shim: use the domain wipe engine to perform the reset.
    return execute_domain_wipe('accounts_domain', session)


def _resolve_model_class(model_name):
    """Resolve a model name string to its actual SQLAlchemy class.
    
    Uses globals() to look up the class, allowing dynamic resolution
    without import order dependencies.
    
    Returns the model class or None if not found.
    """
    try:
        return globals().get(model_name)
    except Exception:
        return None


def execute_domain_wipe(domain_name, session=None):
    """Generic wipe engine.

    Responsibilities:
    - Resolve models from `DOMAIN_WIPE_REGISTRY` (string names → classes)
    - Delete rows from those models
    - Log operations

    NOTE: This function is transaction-neutral and must not open/commit/rollback
    transactions itself. The caller is responsible for transaction boundaries.
    This ensures the engine can be composed with domain post-processors
    within a single atomic transaction.
    """
    logger = logging.getLogger('wipe')
    if domain_name not in DOMAIN_WIPE_REGISTRY:
        raise ValueError(f"Unknown domain: {domain_name}")
    model_names = list(DOMAIN_WIPE_REGISTRY[domain_name])
    s = session or db.session
    operations = []
    try:
        for model_name in model_names:
            # Resolve model name string to actual class
            model = _resolve_model_class(model_name)
            if not model:
                logger.warning('Wipe: could not resolve model %s; skipping', model_name)
                continue
            table_name = getattr(model, '__tablename__', '') or ''
            if table_name in ('user', 'user_login_session') or model_name in ('User', 'UserLoginSession'):
                logger.warning('Wipe: refusing to delete identity table %s', table_name or model_name)
                continue
            try:
                deleted = s.query(model).delete()
                table_name = getattr(model, '__tablename__', model_name)
                operations.append((table_name, 'deleted', int(deleted or 0)))
                logger.info('Wipe: %s deleted %s rows', table_name, deleted)
            except Exception as exc:
                # If the underlying table does not exist, log and continue.
                msg = str(exc).lower()
                if 'no such table' in msg or 'no such column' in msg:
                    logger.warning('Wipe: skipping missing table for model %s: %s', model_name, exc)
                    continue
                raise
    except Exception:
        logger.exception('Domain wipe failed for %s', domain_name)
        raise
    logger.info('Domain wipe %s completed. Ops: %s', domain_name, operations)
    return operations


def accounts_domain_post_reset(session=None):
    """Domain-specific post-processor for `accounts_domain`.

    This function contains business logic and must be separate from the
    generic wipe engine. It resets `Account.balance`, clears payment
    -> account links, and performs any ledger/derived-state fixes.
    """
    logger = logging.getLogger('wipe.post')
    s = session or db.session
    operations = []
    try:
        # Nullify payment->account links
        try:
            updated = s.query(Payment).update({'payment_account_id': None}, synchronize_session=False)
            operations.append(('payment', 'updated_payment_account_id', int(updated or 0)))
            logger.info('Post-reset: nullified payment.payment_account_id for %s rows', updated)
        except Exception:
            logger.exception('Post-reset: failed to nullify Payment.payment_account_id')
            raise

        try:
            updated = s.query(SupplierPayment).update({'payment_account_id': None}, synchronize_session=False)
            operations.append(('supplier_payment', 'updated_payment_account_id', int(updated or 0)))
            logger.info('Post-reset: nullified supplier_payment.payment_account_id for %s rows', updated)
        except Exception:
            logger.exception('Post-reset: failed to nullify SupplierPayment.payment_account_id')
            raise

        # Reset account balances to 0
        try:
            updated = s.query(Account).update({'balance': 0}, synchronize_session=False)
            operations.append(('account', 'reset_balance', int(updated or 0)))
            logger.info('Post-reset: reset balance for %s accounts', updated)
        except Exception:
            logger.exception('Post-reset: failed to reset Account.balance')
            raise

        # TODO: add further ledger consistency fixes here (journal snapshots, recalcs)
    except Exception:
        logger.exception('Accounts post-reset failed')
        raise
    logger.info('Accounts post-reset completed. Ops: %s', operations)
    return operations


def verify_accounts_domain_wipe_integrity(session=None):
    """Read-only integrity checks after an accounts-domain wipe.

    Returns (ok: bool, report: dict)
    """
    logger = logging.getLogger('wipe.verify')
    s = session or db.session
    report = {}
    try:
        # 1) Domain tables listed in the registry must be empty
        domain_model_names = list(DOMAIN_WIPE_REGISTRY.get('accounts_domain', []))
        model_emptiness = {}
        for model_name in domain_model_names:
            model = _resolve_model_class(model_name)
            if not model:
                logger.warning('Verify: could not resolve model %s', model_name)
                model_emptiness[model_name] = 'unresolved'
                continue
            try:
                cnt = int(s.query(func.count(getattr(model, 'id'))).scalar() or 0)
            except Exception:
                # If table missing, treat as zero rows for verification but note it.
                cnt = 0
            table_name = getattr(model, '__tablename__', model_name)
            model_emptiness[table_name] = cnt
        report['domain_model_row_counts'] = model_emptiness

        # 2) Account balances must all be zero
        nonzero_balances = int(s.query(func.count(Account.id)).filter(Account.balance != 0).scalar() or 0)
        report['nonzero_account_balances'] = nonzero_balances

        # 3) No payment references remain
        payments_with_account = int(s.query(func.count(Payment.id)).filter(Payment.payment_account_id.isnot(None)).scalar() or 0)
        supplier_payments_with_account = int(s.query(func.count(SupplierPayment.id)).filter(SupplierPayment.payment_account_id.isnot(None)).scalar() or 0)
        direct_sales_with_account = int(s.query(func.count(DirectSale.id)).filter(DirectSale.payment_account_id.isnot(None)).scalar() or 0)
        grns_with_account = int(s.query(func.count(GRN.id)).filter(GRN.payment_account_id.isnot(None)).scalar() or 0)
        fbm_rentals_with_account = int(s.query(func.count(FBMRental.id)).filter(FBMRental.payment_account_id.isnot(None)).scalar() or 0)
        report['payments_with_account_refs'] = payments_with_account
        report['supplier_payments_with_account_refs'] = supplier_payments_with_account
        report['direct_sales_with_account_refs'] = direct_sales_with_account
        report['grns_with_account_refs'] = grns_with_account
        report['fbm_rentals_with_account_refs'] = fbm_rentals_with_account

        # 4) No orphan account references in AccountTransaction (from/to)
        at_count = int(s.query(func.count(AccountTransaction.id)).scalar() or 0)
        report['account_transaction_rows'] = at_count

        # Build overall verdict
        ok = True
        # Only count integer row counts that are non-zero; skip 'unresolved' strings
        # which indicate the model couldn't be loaded (not a wipe failure).
        if any(isinstance(v, int) and v > 0 for v in model_emptiness.values()):
            ok = False
        if nonzero_balances:
            ok = False
        if payments_with_account or supplier_payments_with_account or direct_sales_with_account or grns_with_account or fbm_rentals_with_account:
            ok = False
        if at_count:
            ok = False

        report['ok'] = ok
        return ok, report
    except Exception:
        logger.exception('Integrity verification failed unexpectedly')
        return False, {'error': 'verification_exception'}


def _create_pre_wipe_tenant_backup(tenant):
    """Automatic pre-wipe file backups are disabled.

    Operators keep their own backup process. The application must not silently
    write another copy of the database or a tenant export under instance/.
    """
    logging.getLogger('wipe').info(
        'Skipping automatic pre-wipe tenant backup for tenant_id=%s',
        getattr(tenant, 'id', None),
    )
    return None, None


def _enforce_tenant_wipe_backup_retention(tenant_id, keep=3):
    rows = TenantWipeBackupHistory.query.filter_by(tenant_id=tenant_id).order_by(TenantWipeBackupHistory.created_at.desc()).all()
    if len(rows) <= keep:
        return
    old_rows = rows[keep:]
    for r in old_rows:
        fpath = (r.backup_path or '').strip()
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass
        db.session.delete(r)
    db.session.commit()


def _wipe_dataset_preview_map():
    def q(model):
        return lambda: model.query

    return {
        'clients': {'label': 'Clients', 'tables': ['client', 'recon_basket'], 'queries': [('client', q(Client)), ('recon_basket', q(ReconBasket))]},
        'suppliers': {'label': 'Suppliers', 'tables': ['supplier', 'supplier_payment'], 'queries': [('supplier', q(Supplier)), ('supplier_payment', q(SupplierPayment))]},
        'supplier_payments': {'label': 'Supplier Payments', 'tables': ['supplier_payment'], 'queries': [('supplier_payment', q(SupplierPayment))]},
        'pending_bills': {'label': 'Pending Bills', 'tables': ['pending_bill', 'follow_up_contact', 'follow_up_reminder'], 'queries': [('pending_bill', q(PendingBill)), ('follow_up_contact', q(FollowUpContact)), ('follow_up_reminder', q(FollowUpReminder))]},
        'notifications': {'label': 'Notifications Data', 'tables': ['follow_up_contact', 'follow_up_reminder', 'staff_email'], 'queries': [('follow_up_contact', q(FollowUpContact)), ('follow_up_reminder', q(FollowUpReminder)), ('staff_email', q(StaffEmail))]},
        'dispatching': {'label': 'Dispatch (OUT)', 'tables': ['entry', 'delivery_item', 'delivery'], 'queries': [('entry OUT', lambda: Entry.query.filter_by(type='OUT')), ('delivery_item', q(DeliveryItem)), ('delivery', q(Delivery))]},
        'receiving': {'label': 'Receive (IN)', 'tables': ['entry'], 'queries': [('entry IN', lambda: Entry.query.filter_by(type='IN'))]},
        'grn': {'label': 'GRN', 'tables': ['grn_item', 'grn'], 'queries': [('grn_item', q(GRNItem)), ('grn', q(GRN))]},
        'materials': {'label': 'Materials', 'tables': ['material'], 'queries': [('material', q(Material))]},
        'material_categories': {'label': 'Material Categories', 'tables': ['material_category'], 'queries': [('material_category', q(MaterialCategory))]},
        'direct_sales': {'label': 'Direct Sales', 'tables': ['direct_sale_item', 'direct_sale', 'delivery_rent', 'sale_delivery_person', 'delivery_person_payment', 'pending_bill', 'entry', 'invoice'], 'queries': [('direct_sale_item', q(DirectSaleItem)), ('direct_sale', q(DirectSale)), ('delivery_rent', q(DeliveryRent)), ('sale_delivery_person', q(SaleDeliveryPerson))]},
        'material_returns': {'label': 'Material Returns', 'tables': ['material_return_item', 'material_return', 'entry', 'payment'], 'queries': [('material_return_item', q(MaterialReturnItem)), ('material_return', q(MaterialReturn))]},
        'delivery_rents': {'label': 'Delivery Rents', 'tables': ['delivery_rent', 'sale_delivery_person', 'delivery_person_payment'], 'queries': [('delivery_rent', q(DeliveryRent)), ('sale_delivery_person', q(SaleDeliveryPerson)), ('delivery_person_payment', q(DeliveryPersonPayment))]},
        'delivery_persons': {'label': 'Delivery Persons', 'tables': ['delivery_person', 'sale_delivery_person', 'delivery_person_payment'], 'queries': [('delivery_person', q(DeliveryPerson)), ('sale_delivery_person', q(SaleDeliveryPerson)), ('delivery_person_payment', q(DeliveryPersonPayment))]},
        'invoices': {'label': 'Invoices', 'tables': ['invoice'], 'queries': [('invoice', q(Invoice))]},
        'payments': {'label': 'Payments', 'tables': ['payment', 'waive_off', 'pending_bill'], 'queries': [('payment', q(Payment)), ('waive_off', q(WaiveOff))]},
        'bookings': {'label': 'Bookings', 'tables': ['booking_item', 'booking', 'pending_bill'], 'queries': [('booking_item', q(BookingItem)), ('booking', q(Booking))]},
        'accounts': {'label': 'Financial Accounts', 'tables': ['account'], 'queries': [('account', q(Account))]},
        'account_categories': {'label': 'Account Categories', 'tables': ['account_category'], 'queries': [('account_category', q(AccountCategory))]},
        'cash_drawer_entries': {'label': 'Cash Drawer Entries', 'tables': ['fbm_cash_drawer_entry'], 'queries': [('fbm_cash_drawer_entry', q(FbmCashDrawerEntry))]},
        'cash_drawer_categories': {'label': 'Cash Drawer Categories', 'tables': ['fbm_cash_drawer_category'], 'queries': [('fbm_cash_drawer_category', q(FbmCashDrawerCategory))]},
        'account_transactions': {'label': 'Account Transactions', 'tables': ['account_transaction'], 'queries': [('account_transaction', q(AccountTransaction))]},
        'cash_reconciliation_data': {'label': 'Cash Reconciliation Data', 'tables': ['cash_flow_difference_adjustment'], 'queries': [('cash_flow_difference_adjustment', q(CashFlowDifferenceAdjustment))]},
        'cash_reconciliation_audit': {'label': 'Cash Audit Trail', 'tables': ['cash_flow_reconciliation_audit'], 'queries': [('cash_flow_reconciliation_audit', q(CashFlowReconciliationAudit))]},
        'delivery_person_payments': {'label': 'Driver Payments', 'tables': ['delivery_person_payment'], 'queries': [('delivery_person_payment', q(DeliveryPersonPayment))]},
        'direct_sale_drafts': {'label': 'Unsaved Sales Drafts', 'tables': ['direct_sale_draft'], 'queries': [('direct_sale_draft', q(DirectSaleDraft))]},
        'fbm_rental_items': {'label': 'Rental Inventory', 'tables': ['fbm_rental_item', 'fbm_rental'], 'queries': [('fbm_rental_item', q(FBMRentalItem)), ('linked fbm_rental', lambda: FBMRental.query.filter(FBMRental.item_id.isnot(None)))]},
        'fbm_rental_clients': {'label': 'Rental Customers', 'tables': ['fbm_client', 'fbm_rental'], 'queries': [('fbm_client', q(FBMClient)), ('linked fbm_rental', lambda: FBMRental.query.filter(FBMRental.client_id.isnot(None)))]},
        'fbm_rentals': {'label': 'Rental Agreements', 'tables': ['fbm_rental'], 'queries': [('fbm_rental', q(FBMRental))]},
    }


def _wipe_preview_for_targets(targets):
    preview_map = _wipe_dataset_preview_map()
    selected = []
    affected_tables = []
    total_estimated_rows = 0
    for target in sorted(set(targets or [])):
        cfg = preview_map.get(target)
        if not cfg:
            selected.append({'key': target, 'label': target, 'estimated_rows': 0, 'tables': []})
            continue
        rows = 0
        table_counts = []
        for table_label, query_factory in cfg.get('queries', []):
            try:
                count = int(query_factory().count() or 0)
            except Exception:
                count = 0
            rows += count
            table_counts.append({'table': table_label, 'rows': count})
        total_estimated_rows += rows
        affected_tables.extend(cfg.get('tables', []))
        selected.append({
            'key': target,
            'label': cfg.get('label', target),
            'estimated_rows': rows,
            'tables': sorted(set(cfg.get('tables', []))),
            'table_counts': table_counts
        })
    return {
        'selected_datasets': selected,
        'affected_tables': sorted(set(affected_tables)),
        'estimated_rows': total_estimated_rows
    }


def _create_pre_wipe_safety_backups(targets):
    """No-op: do not copy the live database into instance/pre_wipe_backups/.

    External/operator backups are the source of truth. Returning an empty
    backup_info keeps wipe callers working without writing files.
    """
    logging.getLogger('wipe').info(
        'Skipping automatic pre-wipe safety backup for datasets=%s',
        sorted(set(targets or [])),
    )
    return {
        'timestamp': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
        'datasets': sorted(set(targets or [])),
        'backup_dir': None,
        'db_backup_path': None,
        'health_snapshot_backup_path': None,
        'skipped': True,
        'reason': 'automatic_pre_wipe_backups_disabled',
    }


def _complete_intentional_wipe_workflow(targets, deleted_info, backup_info, mode):
    operation = {
        'operation': 'granular_wipe',
        'intentional': True,
        'reset_context': 'granular_wipe',
        'reset_source': 'granular_wipe',
        'timestamp': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
        'datasets': sorted(set(targets or [])),
        'deleted': list(deleted_info or []),
        'mode': mode,
        'performed_by': getattr(current_user, 'username', None),
        'backup': backup_info or {}
    }
    snapshot = _rebuild_health_snapshot(
        intentional_operation=operation,
        reset_context='granular_wipe'
    )
    audit_details = json.dumps({
        'intentional': True,
        'operation': 'granular_wipe',
        'mode': mode,
        'datasets': operation['datasets'],
        'deleted': operation['deleted'],
        'new_total': snapshot.get('total'),
        'backup_dir': (backup_info or {}).get('backup_dir')
    }, sort_keys=True)[:1000]
    audit_log(current_user, 'data.wipe.intentional_baseline', audit_details)
    return snapshot


