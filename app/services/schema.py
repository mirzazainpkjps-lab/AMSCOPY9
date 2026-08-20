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
    _db_debug_counts,
)
from app.services.lookups import (
    get_client_by_input,
    get_or_create_delivery_person,
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

def _ensure_user_password_column():
    """Ensure `password_hash` column exists on `user` table and copy legacy `password` values."""
    try:
        rows = db.session.execute(text("PRAGMA table_info('user')")).fetchall()
        cols = [r[1] for r in rows]
        if 'password_hash' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN password_hash VARCHAR(200);"))
            if 'password' in cols:
                db.session.execute(text("UPDATE user SET password_hash = password WHERE password_hash IS NULL;"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_model_columns():
    """Add any missing columns declared in models but missing in the DB."""
    from sqlalchemy import String, Integer, Float, Date, DateTime, Boolean, Text, Boolean

    try:
        for table in db.metadata.sorted_tables:
            rows = db.session.execute(text(f"PRAGMA table_info('{table.name}')")).fetchall()
            existing_cols = [r[1] for r in rows]
            for col in table.columns:
                if col.name not in existing_cols:
                    coltype = col.type
                    sqltype = 'VARCHAR(200)'
                    if isinstance(coltype, (String, Text)):
                        sqltype = 'VARCHAR(200)'
                    elif isinstance(coltype, (Integer, Boolean)) or str(coltype) == 'BOOLEAN':
                        sqltype = 'INTEGER'
                    elif isinstance(coltype, Float):
                        sqltype = 'REAL'
                    elif isinstance(coltype, Date):
                        sqltype = 'DATE'
                    elif isinstance(coltype, DateTime):
                        sqltype = 'DATETIME'

                    try:
                        db.session.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {sqltype};"))
                    except Exception:
                        db.session.rollback()
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_material_categories():
    try:
        default_cat = get_or_create_material_category('General')
        if not default_cat:
            return
        mats = Material.query.filter(Material.category_id.is_(None)).all()
        for m in mats:
            m.category_id = default_cat.id
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_discount_columns():
    """Ensure discount and discount_reason columns exist on relevant tables."""
    tables = {
        'direct_sale': ['discount', 'discount_reason'],
        'booking': ['discount', 'discount_reason'],
        'payment': ['discount', 'discount_reason']
    }
    try:
        for table, cols in tables.items():
            rows = db.session.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
            existing = [r[1] for r in rows]
            for col in cols:
                if col not in existing:
                    col_type = 'REAL DEFAULT 0' if col == 'discount' else 'VARCHAR(200)'
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_bill_counter_namespace_defaults():
    """Backfill namespace for legacy bill_counter rows after schema upgrades."""
    try:
        rows = db.session.execute(text("PRAGMA table_info('bill_counter')")).fetchall()
        existing = {r[1] for r in rows}
        if 'namespace' not in existing:
            return
        db.session.execute(text(
            "UPDATE bill_counter SET namespace = 'GEN' "
            "WHERE namespace IS NULL OR TRIM(namespace) = ''"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_waive_off_table():
    """Ensure dedicated waive_off table exists for loss/write-off events."""
    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS waive_off (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER,
                client_code VARCHAR(50),
                client_name VARCHAR(100),
                bill_no VARCHAR(50),
                amount REAL DEFAULT 0,
                reason VARCHAR(300),
                date_posted DATETIME,
                created_by VARCHAR(80),
                note VARCHAR(500),
                is_void INTEGER DEFAULT 0
            )
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_delivery_person_payments_table():
    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS delivery_person_payment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_person_id INTEGER NOT NULL,
                sale_id INTEGER,
                allocation_id INTEGER,
                amount_paid REAL DEFAULT 0,
                waive_off_amount REAL DEFAULT 0,
                note VARCHAR(500),
                date_posted DATETIME,
                created_by VARCHAR(80),
                is_void INTEGER DEFAULT 0
            )
        """))
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_dpp_person ON delivery_person_payment (delivery_person_id)
        """))
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_dpp_alloc ON delivery_person_payment (allocation_id)
        """))
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_dpp_date ON delivery_person_payment (date_posted)
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()
    _ensure_delivery_person_payment_accounting_columns()


def _ensure_delivery_person_payment_accounting_columns():
    """Additive upgrade linking driver payments to the authoritative ledger.

    Existing rows are preserved untouched: the new columns are nullable, and the
    minor-unit mirrors are backfilled from the legacy REAL values.  A partial
    unique index on ``idempotency_key`` makes retried submissions race-safe at
    the database level while leaving legacy NULL keys exempt.
    """
    additive = {
        'amount_paid_minor': 'BIGINT',
        'waive_off_minor': 'BIGINT',
        'payment_account_id': 'INTEGER',
        'method': 'VARCHAR(50)',
        'reference': 'VARCHAR(50)',
        'idempotency_key': 'VARCHAR(64)',
        'revision': 'INTEGER DEFAULT 1',
        'updated_by': 'VARCHAR(80)',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    }
    try:
        rows = db.session.execute(text("PRAGMA table_info('delivery_person_payment')")).fetchall()
        existing = {r[1] for r in rows}
        if not existing:
            return
        for column, sqltype in additive.items():
            if column in existing:
                continue
            try:
                db.session.execute(text(
                    f"ALTER TABLE delivery_person_payment ADD COLUMN {column} {sqltype}"
                ))
            except Exception:
                db.session.rollback()
        db.session.execute(text(
            "UPDATE delivery_person_payment "
            "SET amount_paid_minor = CAST(ROUND(COALESCE(amount_paid, 0) * 100) AS INTEGER) "
            "WHERE amount_paid_minor IS NULL"
        ))
        db.session.execute(text(
            "UPDATE delivery_person_payment "
            "SET waive_off_minor = CAST(ROUND(COALESCE(waive_off_amount, 0) * 100) AS INTEGER) "
            "WHERE waive_off_minor IS NULL"
        ))
        db.session.execute(text(
            "UPDATE delivery_person_payment SET revision = 1 WHERE revision IS NULL"
        ))
        db.session.execute(text(
            "UPDATE delivery_person_payment SET created_at = date_posted WHERE created_at IS NULL"
        ))
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_person_payment_idempotency_key "
            "ON delivery_person_payment(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_dpp_account "
            "ON delivery_person_payment (payment_account_id)"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_legacy_payment_discounts_to_waive_off():
    """
    Backfill legacy Payment.discount values into waive_off rows.
    Keep Payment.discount for compatibility; downstream logic avoids double counting.
    """
    try:
        existing_payment_ids = {
            r[0] for r in WaiveOff.query.filter(
                WaiveOff.payment_id.isnot(None),
                WaiveOff.is_void == False
            ).with_entities(WaiveOff.payment_id).distinct().all()
            if r and r[0] is not None
        }
        legacy_rows = Payment.query.filter(
            Payment.is_void == False,
            Payment.discount > 0
        ).all()

        for pay in legacy_rows:
            if pay.id in existing_payment_ids:
                continue
            client_obj = get_client_by_input(pay.client_name or '')
            bill_ref = (pay.manual_bill_no or pay.auto_bill_no or f"PAY-{pay.id}")
            db.session.add(WaiveOff(
                payment_id=pay.id,
                client_code=(client_obj.code if client_obj else None),
                client_name=(client_obj.name if client_obj else pay.client_name),
                bill_no=bill_ref,
                amount=float(pay.discount or 0),
                reason=(pay.discount_reason or 'Legacy waive-off migration'),
                date_posted=pay.date_posted or pk_now(),
                created_by=None,
                note=pay.note,
                is_void=bool(pay.is_void)
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_sale_delivery_persons_from_legacy():
    """Backfill legacy delivery rent rows into sale_delivery_persons for compatibility."""
    try:
        existing_sale_ids = {
            r[0] for r in db.session.query(SaleDeliveryPerson.sale_id).distinct().all()
            if r and r[0] is not None
        }
        legacy_rows = DeliveryRent.query.all()
        for dr in legacy_rows:
            if not dr.sale_id or dr.sale_id in existing_sale_ids:
                continue
            dp = get_or_create_delivery_person(dr.delivery_person_name)
            if not dp:
                continue
            db.session.add(SaleDeliveryPerson(
                sale_id=dr.sale_id,
                delivery_person_id=dp.id,
                bags_delivered=0,
                rent_amount=float(dr.amount or 0),
                created_at=dr.date_posted or pk_now(),
                is_void=bool(dr.is_void)
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_user_permission_defaults():
    """Backfill NULL permission values so newly added columns remain usable."""
    try:
        rows = db.session.execute(text("PRAGMA table_info('user')")).fetchall()
        existing = {r[1] for r in rows}
        for col, default_value in USER_PERMISSION_DEFAULTS.items():
            if col in existing:
                db.session.execute(
                    text(f'UPDATE "user" SET {col} = :v WHERE {col} IS NULL'),
                    {'v': 1 if default_value else 0}
                )
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_accounting_integrity_columns():
    """Non-destructively initialise exact-money and source/audit metadata.

    Existing REAL values and historical names are preserved.  The explicit
    account opening baseline is inferred as ``stored current - active ledger
    net`` so enabling reproducible ledgers does not change any live balance.
    """
    from utils.money import from_minor, to_minor

    try:
        # Versioned ORM rows require a non-NULL committed version before they
        # can be safely loaded and updated. Initialise it with raw SQL first so
        # legacy NULL rows do not trigger an autoflush/version predicate error.
        for table_name in ('account', 'payment', 'supplier_payment'):
            db.session.execute(text(
                f"UPDATE {table_name} SET revision = 1 WHERE revision IS NULL"
            ))
        db.session.commit()
        db.session.expire_all()

        # Stable payment party/source identities and exact minor-unit mirrors.
        clients_by_name = {
            (c.name or '').strip().lower(): c.id
            for c in Client.query.all() if (c.name or '').strip()
        }
        for p in Payment.query.all():
            if getattr(p, 'amount_minor', None) is None:
                p.amount_minor = to_minor(p.amount or 0)
            if getattr(p, 'discount_minor', None) is None:
                p.discount_minor = to_minor(p.discount or 0)
            if not getattr(p, 'client_id', None):
                p.client_id = clients_by_name.get((p.client_name or '').strip().lower())
            note = p.note or ''
            material_match = re.search(r'\[MATERIAL_RETURN:(\d+)\]', note, re.IGNORECASE)
            if material_match:
                p.payment_type = 'Material Return'
                p.source_type = p.source_type or 'MaterialReturn'
                p.source_id = p.source_id or int(material_match.group(1))
            elif float(p.amount or 0) < 0 or (p.method or '').strip().lower() == 'refund':
                p.payment_type = 'Refund'
            elif float(p.amount or 0) == 0 and float(p.discount or 0) > 0:
                p.payment_type = 'Waive-Off'
            else:
                p.payment_type = p.payment_type or 'Receipt'
            p.revision = p.revision or 1

        for p in SupplierPayment.query.all():
            if getattr(p, 'amount_minor', None) is None:
                p.amount_minor = to_minor(p.amount or 0)
            marker = re.search(r'\[AUTO_GRN_PAY:(\d+)\]', p.note or '', re.IGNORECASE)
            if marker:
                p.source_type = p.source_type or 'GRN'
                p.source_id = p.source_id or int(marker.group(1))
            p.payment_type = p.payment_type or 'Payment'
            p.revision = p.revision or 1

        # Add structured source identity to linked ledger rows while preserving
        # the human-readable legacy marker in ``note``.
        source_patterns = (
            ('Payment', r'\[SRC:Payment:(\d+)\]'),
            ('Payment', r'\[SRC:ClientRefund:(\d+)\]'),
            ('SupplierPayment', r'\[SRC:SupplierPayment:(\d+)\]'),
        )
        for tx in AccountTransaction.query.all():
            if getattr(tx, 'amount_minor', None) is None:
                tx.amount_minor = to_minor(tx.amount or 0)
            if not getattr(tx, 'source_type', None):
                for source_type, pattern in source_patterns:
                    match = re.search(pattern, tx.note or '', re.IGNORECASE)
                    if match:
                        tx.source_type = source_type
                        tx.source_id = int(match.group(1))
                        break

        # Infer a no-change opening baseline for every legacy account.
        for account in Account.query.all():
            account.balance_minor = to_minor(account.balance or 0)
            account.revision = account.revision or 1
            if account.opening_balance is None:
                incoming = sum(
                    (tx.amount_minor if tx.amount_minor is not None else to_minor(tx.amount or 0))
                    for tx in account.incoming_transactions if not tx.is_void
                )
                outgoing = sum(
                    (tx.amount_minor if tx.amount_minor is not None else to_minor(tx.amount or 0))
                    for tx in account.outgoing_transactions if not tx.is_void
                )
                opening_minor = int(account.balance_minor or 0) - incoming + outgoing
                account.opening_balance_minor = opening_minor
                account.opening_balance = float(from_minor(opening_minor))
                account.opening_balance_date = account.created_at
            elif account.opening_balance_minor is None:
                account.opening_balance_minor = to_minor(account.opening_balance or 0)

        # SQLite ALTER TABLE cannot add a UNIQUE constraint.  Partial unique
        # indexes make retried create requests race-safe while allowing NULLs.
        db.session.flush()
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_idempotency_key "
            "ON payment(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_payment_idempotency_key "
            "ON supplier_payment(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('Accounting integrity metadata backfill failed')


def _ensure_account_type_compat():
    """
    Keep legacy `account.type` and newer `account.account_type` consistent.
    Some existing DBs enforce NOT NULL on `account.type`.
    """
    try:
        rows = db.session.execute(text("PRAGMA table_info('account')")).fetchall()
        existing = {r[1] for r in rows}
        if 'type' not in existing or 'account_type' not in existing:
            return
        db.session.execute(text(
            "UPDATE account SET account_type = type "
            "WHERE account_type IS NULL OR TRIM(account_type) = ''"
        ))
        db.session.execute(text(
            "UPDATE account SET type = account_type "
            "WHERE type IS NULL OR TRIM(type) = ''"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_direct_sale_idempotency_index():
    """DB-level duplicate-submission guard for direct sales.

    SQLite's ALTER TABLE cannot add a UNIQUE constraint, so a partial unique
    index is created instead (NULL keys for legacy rows are exempt). The
    application-level check in ``add_direct_sale`` is the primary guard; this
    index makes duplicate commits impossible even under a double-click race.
    """
    try:
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_direct_sale_idempotency_key "
            "ON direct_sale(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


# Indexes that make the hot transaction paths bounded instead of full-scan:
#   * bill-number lookups (find_bill_conflict, _max_used_auto_bill_seq, GRN
#     save, sale save, duplicate-manual-bill checks)
#   * stock movement aggregation (entry.date/material/type group-bys on the
#     dashboard, stock summary, client ledgers)
#   * pending-bill client/bill lookups
# All are additive CREATE INDEX IF NOT EXISTS — no data change, safe to add to
# a populated database, and reversible with DROP INDEX.
_PERFORMANCE_INDEXES = [
    # bill-number fast lookups (conflict detection + sequence sync)
    ("ix_grn_auto_bill_no",        "CREATE INDEX IF NOT EXISTS ix_grn_auto_bill_no        ON grn(auto_bill_no)"),
    ("ix_grn_manual_bill_no",      "CREATE INDEX IF NOT EXISTS ix_grn_manual_bill_no      ON grn(manual_bill_no)"),
    ("ix_direct_sale_manual_bill_no", "CREATE INDEX IF NOT EXISTS ix_direct_sale_manual_bill_no ON direct_sale(manual_bill_no)"),
    ("ix_direct_sale_auto_bill_no",   "CREATE INDEX IF NOT EXISTS ix_direct_sale_auto_bill_no   ON direct_sale(auto_bill_no)"),
    ("ix_booking_manual_bill_no",  "CREATE INDEX IF NOT EXISTS ix_booking_manual_bill_no  ON booking(manual_bill_no)"),
    ("ix_booking_auto_bill_no",    "CREATE INDEX IF NOT EXISTS ix_booking_auto_bill_no    ON booking(auto_bill_no)"),
    ("ix_payment_manual_bill_no",  "CREATE INDEX IF NOT EXISTS ix_payment_manual_bill_no  ON payment(manual_bill_no)"),
    ("ix_payment_auto_bill_no",    "CREATE INDEX IF NOT EXISTS ix_payment_auto_bill_no    ON payment(auto_bill_no)"),
    ("ix_supplier_payment_manual_bill_no", "CREATE INDEX IF NOT EXISTS ix_supplier_payment_manual_bill_no ON supplier_payment(manual_bill_no)"),
    ("ix_supplier_payment_auto_bill_no",   "CREATE INDEX IF NOT EXISTS ix_supplier_payment_auto_bill_no   ON supplier_payment(auto_bill_no)"),
    ("ix_material_return_manual_bill_no",  "CREATE INDEX IF NOT EXISTS ix_material_return_manual_bill_no  ON material_return(manual_bill_no)"),
    ("ix_material_return_auto_bill_no",    "CREATE INDEX IF NOT EXISTS ix_material_return_auto_bill_no    ON material_return(auto_bill_no)"),
    ("ix_entry_bill_no",           "CREATE INDEX IF NOT EXISTS ix_entry_bill_no           ON entry(bill_no)"),
    ("ix_entry_material",          "CREATE INDEX IF NOT EXISTS ix_entry_material          ON entry(material)"),
    ("ix_entry_type",              "CREATE INDEX IF NOT EXISTS ix_entry_type              ON entry(type)"),
    ("ix_pending_bill_bill_no",    "CREATE INDEX IF NOT EXISTS ix_pending_bill_bill_no    ON pending_bill(bill_no)"),
    ("ix_pending_bill_client_code","CREATE INDEX IF NOT EXISTS ix_pending_bill_client_code ON pending_bill(client_code)"),
    # DB-level duplicate-submission guard for GRNs with a manual bill number
    # (double click / browser retry / refresh).  The app-level
    # find_bill_conflict check is the primary guard; this unique partial index
    # makes a second commit impossible even under a race.
    ("uq_grn_manual_bill_no",      "CREATE UNIQUE INDEX IF NOT EXISTS uq_grn_manual_bill_no ON grn(manual_bill_no) WHERE manual_bill_no IS NOT NULL AND TRIM(manual_bill_no) <> ''"),
]


def _ensure_performance_indexes():
    """Add the hot-path query indexes (idempotent, additive, reversible)."""
    try:
        for _name, ddl in _PERFORMANCE_INDEXES:
            db.session.execute(text(ddl))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_default_admin():
    """Create a first admin if the user table is empty (fresh / empty DB)."""
    if User.query.count() > 0:
        return
    username = (os.environ.get("DEFAULT_ADMIN_USER") or "Admin").strip() or "Admin"
    password = (os.environ.get("DEFAULT_ADMIN_PASSWORD") or "Admin@fbm12345").strip() or "Admin@fbm12345"
    user = User(
        username=username,
        role="admin",
        status="active",
        password_hash=generate_password_hash(password),
        password_plain=None,
        can_import_export=True,
        can_manage_directory=True,
        can_manage_clients=True,
        can_manage_suppliers=True,
        can_manage_materials=True,
        can_manage_delivery_persons=True,
        can_access_settings=True,
    )
    db.session.add(user)
    db.session.commit()
    logging.getLogger("app").info("Created default admin user %r", username)


def _bootstrap_database():
    db.create_all()
    try:
        _ensure_user_password_column()
    except Exception:
        pass
    try:
        _ensure_model_columns()
    except Exception:
        pass
    try:
        _ensure_default_admin()
    except Exception:
        # A populated/legacy database may intentionally manage users elsewhere;
        # schema bootstrap must remain non-destructive in that case.
        db.session.rollback()
    try:
        _ensure_account_type_compat()
    except Exception:
        pass
    try:
        _backfill_accounting_integrity_columns()
    except Exception:
        pass
    try:
        _ensure_material_categories()
    except Exception:
        pass
    try:
        _ensure_discount_columns()
    except Exception:
        pass
    try:
        _ensure_bill_counter_namespace_defaults()
    except Exception:
        pass
    try:
        _ensure_waive_off_table()
    except Exception:
        pass
    try:
        _ensure_delivery_person_payments_table()
    except Exception:
        pass
    try:
        _backfill_legacy_payment_discounts_to_waive_off()
    except Exception:
        pass
    try:
        _backfill_sale_delivery_persons_from_legacy()
    except Exception:
        pass
    try:
        _ensure_user_permission_defaults()
    except Exception:
        pass
    try:
        _ensure_direct_sale_idempotency_index()
    except Exception:
        pass
    try:
        _ensure_performance_indexes()
    except Exception:
        pass
    try:
        for sale in DirectSale.query.filter(
            or_(DirectSale.client_code.is_(None), func.trim(DirectSale.client_code) == '')
        ).all():
            cli = get_client_by_input(sale.client_name or '')
            if cli:
                sale.client_code = cli.code
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        bootstrap_tenancy()
    except Exception:
        db.session.rollback()
    try:
        logging.getLogger('app').info('DB loaded: %s | counts=%s', db_path, _db_debug_counts())
    except Exception:
        pass


