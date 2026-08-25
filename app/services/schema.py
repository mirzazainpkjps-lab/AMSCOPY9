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


def _migrate_legacy_plaintext_passwords():
    """Convert any stored plaintext password into a Werkzeug hash, then erase it.

    The login path used to accept a plaintext match (from `password_plain`, or
    from a plaintext value sitting in `password_hash`) and upgrade it lazily on
    next successful login. That left usable cleartext credentials in the
    database indefinitely for any account that never logged back in. Doing the
    upgrade here, at boot, means the login fallback can be removed outright
    without locking anyone out.

    No password value is logged.
    """
    from werkzeug.security import generate_password_hash

    def _looks_hashed(value):
        # Werkzeug hashes are "method$salt$digest" (e.g. scrypt/pbkdf2).
        return bool(value) and value.count('$') >= 2

    try:
        rows = db.session.execute(text("PRAGMA table_info('user')")).fetchall()
        cols = [r[1] for r in rows]
        if 'password_hash' not in cols:
            return
        has_plain = 'password_plain' in cols

        select_cols = "id, password_hash" + (", password_plain" if has_plain else "")
        records = db.session.execute(text(f"SELECT {select_cols} FROM user")).fetchall()

        upgraded = 0
        cleared = 0
        for rec in records:
            uid = rec[0]
            stored_hash = (rec[1] or '').strip()
            stored_plain = (rec[2] or '').strip() if has_plain else ''

            new_hash = None
            if stored_hash and not _looks_hashed(stored_hash):
                # Plaintext was stored in the hash column.
                new_hash = generate_password_hash(stored_hash)
            elif not stored_hash and stored_plain:
                new_hash = generate_password_hash(stored_plain)

            if new_hash:
                db.session.execute(
                    text("UPDATE user SET password_hash = :h WHERE id = :i"),
                    {"h": new_hash, "i": uid},
                )
                upgraded += 1

            if has_plain and stored_plain:
                db.session.execute(
                    text("UPDATE user SET password_plain = NULL WHERE id = :i"),
                    {"i": uid},
                )
                cleared += 1

        if upgraded or cleared:
            db.session.commit()
            logging.getLogger("app").warning(
                "Legacy password remediation: hashed %d plaintext password(s), "
                "cleared %d plaintext column value(s).", upgraded, cleared
            )
    except Exception:
        db.session.rollback()
        logging.getLogger("app").exception("Legacy plaintext password migration failed")


def _ensure_model_columns():
    """Add any missing columns declared in models but missing in the DB."""
    from sqlalchemy import String, Integer, Float, Date, DateTime, Boolean, Text, BigInteger, Numeric

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
                    elif isinstance(coltype, BigInteger) or str(coltype).upper().startswith('BIGINT'):
                        sqltype = 'BIGINT'
                    elif isinstance(coltype, (Integer, Boolean)) or str(coltype) == 'BOOLEAN':
                        sqltype = 'INTEGER'
                    elif isinstance(coltype, (Float, Numeric)):
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


def _ensure_auto_bill_unique_indexes():
    """DB-level uniqueness for auto-generated bill numbers.

    The atomic counter allocator in ``billing.get_next_bill_no`` prevents two
    requests from ever receiving the same number, and these partial unique
    indexes make a duplicate commit impossible even if a future code path
    regresses.  A populated (legacy/imported) database can already contain
    duplicated auto bills: in that case the index for that table is skipped
    (logged) so bootstrapping a legacy DB never fails, and the counter stays
    ahead of the highest used sequence via ``_sync_bill_counter_with_db``.
    """
    tables = ("booking", "payment", "supplier_payment", "direct_sale",
              "material_return", "grn", "entry")
    for table in tables:
        try:
            dup = db.session.execute(text(
                f"SELECT COUNT(*) FROM (SELECT auto_bill_no FROM {table} "
                "WHERE auto_bill_no IS NOT NULL AND TRIM(auto_bill_no) <> '' "
                "GROUP BY auto_bill_no HAVING COUNT(*) > 1)"
            )).scalar()
            if dup:
                logging.getLogger(__name__).warning(
                    "Skipping unique auto-bill index for %s: %s duplicated bill numbers exist.",
                    table, dup,
                )
                continue
            # Recreate so blank strings are not treated as a real bill number.
            # The previous WHERE (IS NOT NULL) made every empty auto_bill_no
            # collide, which aborted imports and blocked new writes.
            db.session.execute(text(f"DROP INDEX IF EXISTS uq_{table}_auto_bill_no"))
            db.session.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_auto_bill_no "
                f"ON {table}(auto_bill_no) WHERE auto_bill_no IS NOT NULL "
                f"AND TRIM(auto_bill_no) <> ''"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.getLogger(__name__).warning(
                "Could not create unique auto-bill index for %s", table,
            )


def ensure_open_khata_client():
    """Materialise the shared Open-Khata walk-in client master row.

    Open-Khata sales are stored with ``client_code='OPEN-KHATA'`` and a
    free-text customer name, historically without any Client master row.
    That made the receivable invisible to the payables report/API/CSV and
    impossible to settle.  A single stable master row keyed by the reserved
    code anchors all such rows for every projection and payment path.
    """
    from app.services.constants import OPEN_KHATA_CODE, OPEN_KHATA_NAME
    if not OPEN_KHATA_CODE:
        return None
    client = Client.query.filter(
        func.lower(func.trim(Client.code)) == str(OPEN_KHATA_CODE).strip().lower()
    ).order_by(Client.id.asc()).first()
    if client:
        if not client.name or client.name == OPEN_KHATA_CODE:
            client.name = OPEN_KHATA_NAME or "Walk-in Customers (Open Khata)"
        return client
    client = Client(
        code=str(OPEN_KHATA_CODE).strip(),
        name=OPEN_KHATA_NAME or "Walk-in Customers (Open Khata)",
        category="Open Khata",
        is_active=True,
    )
    db.session.add(client)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        return None
    return client


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


def _ensure_account_classification_columns():
    """Additive Account Create/Edit classification upgrade.

    Adds the controlled-hierarchy columns (Category/Subcategory/Account Type +
    Channel + channel-specific details + linked entity + status) and the
    adjustment traceability columns (reason + idempotency_key) on
    AccountTransaction.  All new columns are nullable so existing rows and the
    legacy ``category`` / ``source_category`` / ``account_type`` columns stay
    valid.  Existing accounts are backfilled with a confident legacy→new
    mapping; unmappable rows fall back to a valid generic classification.

    Nothing here changes a balance or deletes/renames a column.
    """
    from blueprints.accounts import classification as cls

    try:
        rows = db.session.execute(text("PRAGMA table_info('account')")).fetchall()
        existing = {r[1] for r in rows}
        if existing:
            additive = {
                "class_category": "VARCHAR(50)",
                "class_subcategory": "VARCHAR(80)",
                "class_account_type": "VARCHAR(100)",
                "channel": "VARCHAR(30)",
                "cash_location": "VARCHAR(120)",
                "cash_responsible": "VARCHAR(120)",
                "wallet_provider": "VARCHAR(100)",
                "wallet_number": "VARCHAR(80)",
                "wallet_holder": "VARCHAR(120)",
                "linked_entity_type": "VARCHAR(30)",
                "linked_client_id": "INTEGER",
                "linked_supplier_id": "INTEGER",
                "linked_party_name": "VARCHAR(160)",
                "account_status": "VARCHAR(20) DEFAULT 'active'",
            }
            for column, sqltype in additive.items():
                if column in existing:
                    continue
                try:
                    db.session.execute(text(
                        f"ALTER TABLE account ADD COLUMN {column} {sqltype}"
                    ))
                except Exception:
                    db.session.rollback()
            db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        rows = db.session.execute(text("PRAGMA table_info('account_transaction')")).fetchall()
        existing = {r[1] for r in rows}
        if existing:
            for column, sqltype in (("reason", "VARCHAR(300)",), ("idempotency_key", "VARCHAR(64)",)):
                if column in existing:
                    continue
                try:
                    db.session.execute(text(
                        f"ALTER TABLE account_transaction ADD COLUMN {column} {sqltype}"
                    ))
                except Exception:
                    db.session.rollback()
            # Partial unique index makes retried/double-clicked adjustments
            # race-safe while leaving legacy NULL keys exempt.
            db.session.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_transaction_idempotency_key "
                "ON account_transaction(idempotency_key) WHERE idempotency_key IS NOT NULL"
            ))
            db.session.commit()
    except Exception:
        db.session.rollback()

    # Backfill new classification for any existing account that has none.  The
    # original columns are preserved untouched; only the empty new columns are
    # filled with a confident mapping (or a valid fallback).
    try:
        unmapped = Account.query.filter(
            or_(Account.class_category.is_(None), func.trim(Account.class_category) == "")
        ).all()
        for account in unmapped:
            cat, sub, atype = cls.legacy_to_classification(
                account.source_category, account.account_type, account.category
            )
            node = cls.resolve_node(cat, sub, atype) or cls.resolve_node(*cls.LEGACY_FALLBACK)
            account.class_category = cat
            account.class_subcategory = sub
            account.class_account_type = atype
            account.channel = node["default_channel"]
            if not account.account_status:
                account.account_status = "active" if account.is_active is not False else "inactive"
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('Account classification backfill failed')


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
        # Must run after the columns exist and before the app serves traffic:
        # the login path no longer accepts plaintext, so any legacy cleartext
        # has to be converted to a hash here.
        _migrate_legacy_plaintext_passwords()
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
        _ensure_account_classification_columns()
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
        _ensure_auto_bill_unique_indexes()
    except Exception:
        pass
    try:
        ensure_open_khata_client()
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


