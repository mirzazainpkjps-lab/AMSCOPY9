"""Domain service."""
from __future__ import annotations

import os, io, json, re, logging, calendar, threading, time, smtplib, shutil, sqlite3, zipfile
import urllib.request, urllib.error, secrets, importlib
from itertools import zip_longest
from urllib.parse import unquote
from contextlib import redirect_stderr
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlalchemy import func, case, text, or_, and_, exists, not_
from sqlalchemy.orm import selectinload
from types import SimpleNamespace
from flask import current_app, render_template, request, redirect, url_for, flash, jsonify
from flask import send_file, Response, make_response, send_from_directory, abort, session, g
from flask_login import login_user, login_required, logout_user, current_user

from models import *
from utils.audit import audit_log
from utils.reconciliation import run_auto_reconcile
from cash_flow_reconciliation_helpers import (
    create_reconciliation, update_reconciliation, delete_reconciliation,
    get_reconciliation_history, migrate_legacy_record,
)
from app.services import constants as C

AUTO_BILL_NS_DEFAULT = C.AUTO_BILL_NS_DEFAULT
AUTO_BILL_NAMESPACES = C.AUTO_BILL_NAMESPACES
OPEN_KHATA_CODE = C.OPEN_KHATA_CODE
OPEN_KHATA_NAME = C.OPEN_KHATA_NAME
PK_TZ = C.PK_TZ
SALE_CATEGORY_CHOICES = C.SALE_CATEGORY_CHOICES
_SALE_CATEGORY_ALIASES = C._SALE_CATEGORY_ALIASES
DOMAIN_WIPE_REGISTRY = C.DOMAIN_WIPE_REGISTRY
USER_PERMISSION_DEFAULTS = C.USER_PERMISSION_DEFAULTS
PERMISSION_LEGACY_FALLBACKS = C.PERMISSION_LEGACY_FALLBACKS
ENDPOINT_PERMISSION_MAP = C.ENDPOINT_PERMISSION_MAP
EDITABLE_USER_PERMISSION_FIELDS = C.EDITABLE_USER_PERMISSION_FIELDS
basedir = C.basedir
legacy_instance_dir = C.legacy_instance_dir
legacy_db_path = C.legacy_db_path
db_path = C.db_path
_DB_HEALTH_SNAPSHOT_PATH = C._DB_HEALTH_SNAPSHOT_PATH
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

from app.services import state

# === explicit service imports ===
from app.services.sales_core import (
    _not_void,
    normalize_sale_category,
)



# --- from bills_parse.py ---
def _normalize_namespace(namespace):
    ns = (namespace or AUTO_BILL_NS_DEFAULT).strip().upper()
    if not ns:
        ns = AUTO_BILL_NS_DEFAULT
    if not re.fullmatch(r'[A-Z][A-Z0-9]{1,7}', ns):
        ns = AUTO_BILL_NS_DEFAULT
    return ns


def _extract_sb_parts(value):
    raw = (value or '').strip()
    if not raw:
        return (None, None)
    txt = raw.upper()
    if txt.startswith('MB NO.'):
        return (None, None)

    m = re.match(r'^SB\s*-\s*([A-Z][A-Z0-9]{1,7})\s*-\s*(\d+)$', txt)
    if m:
        return (_normalize_namespace(m.group(1)), int(m.group(2)))

    body = raw
    if txt.startswith('SB NO.'):
        body = raw.split('.', 1)[1].strip() if '.' in raw else ''
    elif txt.startswith('SB '):
        body = raw[2:].strip()
    elif txt.startswith('AUTO '):
        body = raw[5:].strip()
        body_up = body.upper()
        if body_up.startswith('SB NO.'):
            body = body.split('.', 1)[1].strip() if '.' in body else ''
        elif body_up.startswith('SB '):
            body = body[2:].strip()

    if body.startswith('#'):
        body = body[1:].strip()
    if re.fullmatch(r'\d+\.0+', body or ''):
        body = body.split('.', 1)[0]
    if re.fullmatch(r'\d+', body or ''):
        return (None, int(body))
    return (None, None)


def _extract_sb_seq(value, namespace=None):
    parsed_ns, seq = _extract_sb_parts(value)
    if seq is None:
        return None
    if namespace and parsed_ns and _normalize_namespace(namespace) != parsed_ns:
        return None
    return seq


def parse_bill_namespace(value):
    parsed_ns, seq = _extract_sb_parts(value)
    if seq is None:
        return None
    return parsed_ns


def parse_bill_kind(value):
    txt = (value or '').strip().upper()
    if not txt:
        return 'UNKNOWN'
    if txt.startswith('SB NO.') or txt.startswith('SB-'):
        return 'SB'
    if txt.startswith('MB NO.'):
        return 'MB'
    _, seq = _extract_sb_parts(txt)
    if seq is not None:
        return 'SB'
    return 'UNKNOWN'


def normalize_auto_bill(value, namespace=AUTO_BILL_NS_DEFAULT):
    raw = (value or '').strip()
    if not raw:
        return ''
    ns_default = _normalize_namespace(namespace)
    parsed_ns, seq = _extract_sb_parts(raw)
    if seq is None:
        return ''
    ns = parsed_ns or ns_default
    return f"SB-{ns}-{int(seq)}"


def normalize_manual_bill(value):
    raw = (value or '').strip()
    if not raw:
        return ''
    upper = raw.upper()
    if upper.startswith('MB NO.'):
        body = raw.split('.', 1)[1].strip() if '.' in raw else ''
    elif upper.startswith('SB NO.'):
        body = raw.split('.', 1)[1].strip() if '.' in raw else ''
    else:
        body = raw
    if body.startswith('#'):
        body = body[1:].strip()
    if re.fullmatch(r'\d+\.0+', body or ''):
        body = body.split('.', 1)[0]
    if not body:
        return ''
    if re.fullmatch(r'\d+', body):
        body = str(int(body))
    return f"MB NO.{body}"


def _format_bill_no(count, namespace=AUTO_BILL_NS_DEFAULT):
    return normalize_auto_bill(str(int(count or 0)), namespace=namespace)



# --- from bills_counter.py ---
def _bill_counter_sources():
    return [
        (Booking, 'auto_bill_no', AUTO_BILL_NAMESPACES['BOOKING']),
        (Payment, 'auto_bill_no', AUTO_BILL_NAMESPACES['PAYMENT']),
        (SupplierPayment, 'auto_bill_no', AUTO_BILL_NAMESPACES['SUPPLIER_PAYMENT']),
        (DirectSale, 'auto_bill_no', AUTO_BILL_NAMESPACES['DIRECT_SALE']),
        (MaterialReturn, 'auto_bill_no', AUTO_BILL_NAMESPACES['MATERIAL_RETURN']),
        (GRN, 'auto_bill_no', AUTO_BILL_NAMESPACES['GRN']),
        (Entry, 'auto_bill_no', AUTO_BILL_NAMESPACES['ENTRY']),
    ]


def _get_or_create_bill_counter(namespace=AUTO_BILL_NS_DEFAULT):
    ns = _normalize_namespace(namespace)
    counter = BillCounter.query.filter_by(namespace=ns).first()
    if not counter:
        counter = BillCounter(count=1000, namespace=ns)
        db.session.add(counter)
        db.session.flush()
    return counter


def _sync_bill_counter_with_db(namespace=AUTO_BILL_NS_DEFAULT):
    """
    Keep module-scoped auto-bill counter ahead of all existing SB refs.
    This protects against collisions after imports/manual DB changes.
    """
    ns = _normalize_namespace(namespace)
    counter = _get_or_create_bill_counter(ns)
    current = int(counter.count or 1000)
    max_used = _max_used_auto_bill_seq(ns)
    required_next = max(1000, max_used + 1)
    if current < required_next:
        counter.count = required_next
        db.session.flush()
        return required_next
    return current


def peek_next_bill_no(namespace=AUTO_BILL_NS_DEFAULT):
    ns = _normalize_namespace(namespace)
    current = _sync_bill_counter_with_db(ns)
    return _format_bill_no(current or 1000, namespace=ns)


def get_next_bill_no(namespace=AUTO_BILL_NS_DEFAULT):
    """Generate and increment the next auto bill number."""
    ns = _normalize_namespace(namespace)
    counter = _get_or_create_bill_counter(ns)
    current = _sync_bill_counter_with_db(ns)
    bill_no = _format_bill_no(current, namespace=ns)
    # Auto bills must be globally unique per tenant across main bill-bearing modules.
    while find_bill_conflict(bill_no):
        current += 1
        bill_no = _format_bill_no(current, namespace=ns)
    counter.count = current + 1
    db.session.flush()
    return bill_no



# --- from bills_rest.py ---
def _max_used_auto_bill_seq(namespace=AUTO_BILL_NS_DEFAULT):
    ns = _normalize_namespace(namespace)
    max_seq = 0
    for model, col, source_ns in _bill_counter_sources():
        if source_ns != ns:
            continue
        # Bounded scan: only rows whose bill reference carries this namespace
        # prefix are loaded (legacy formats are all normalised to SB-<NS>-####
        # by the before_flush normaliser; the LIKE keeps the filter in SQL so
        # the ix_*_auto_bill_no index is used instead of a full-table scan).
        col_expr = getattr(model, col)
        rows = model.query.with_entities(col_expr).filter(
            col_expr.like(f'SB-{ns}-%')
        ).all()
        for (ref,) in rows:
            parsed_ns, seq = _extract_sb_parts(ref)
            if seq is None:
                continue
            if parsed_ns and parsed_ns != ns:
                continue
            if seq > max_seq:
                max_seq = seq
    return max_seq


def _resolve_transaction_type(bill_type, bill_obj, entry_hint_id=None):
    default = ('general_transaction', 'General Transaction', '')
    if not bill_obj:
        return default

    t = (bill_type or '').strip()
    client_code = getattr(bill_obj, 'client_code', None)
    client_name = (getattr(bill_obj, 'client_name', None) or '').strip()
    bill_refs = _collect_bill_refs_for_lookup(t, bill_obj)

    hinted_entry = None
    if entry_hint_id:
        try:
            hinted_entry = db.session.get(Entry, int(entry_hint_id))
        except Exception:
            hinted_entry = None
        if hinted_entry and hinted_entry.is_void:
            hinted_entry = None
        if hinted_entry and bill_refs and (hinted_entry.bill_no not in bill_refs):
            hinted_entry = None

    latest_entry = hinted_entry or _latest_entry_for_bill_refs(bill_refs, client_code=client_code, client_name=client_name)

    if t == 'GRN':
        return ('grn_purchase', 'GRN / Purchase', 'Stock receiving purchase bill')

    if t == 'Payment':
        method = (getattr(bill_obj, 'method', '') or '').strip()
        note = f"Method: {method}" if method else ''
        return ('payment_only', 'Payment Only', note)

    if t == 'DirectSale':
        cat = normalize_sale_category(getattr(bill_obj, 'category', None))
        mapping = {
            'Cash': ('direct_sale_cash', 'Direct Sale (Cash)'),
            'Credit Customer': ('direct_sale_credit', 'Direct Sale (Credit)'),
            'Mixed Transaction': ('direct_sale_mixed', 'Direct Sale (Booked + Due)'),
            'Booking Delivery': ('direct_sale_booked', 'Direct Sale (Booked Delivery)'),
            'Open Khata': ('direct_sale_open_khata', 'Direct Sale (Open Khata)'),
        }
        code, label = mapping.get(cat, ('direct_sale', 'Direct Sale'))
        display_cat = 'Booked + Due' if cat == 'Mixed Transaction' else cat
        return (code, label, f"Sale Category: {display_cat}")

    # Invoice linked to direct sale must keep direct-sale labeling, not entry fallback.
    if t == 'Invoice':
        if getattr(bill_obj, 'direct_sales', None):
            ds = bill_obj.direct_sales[0] if bill_obj.direct_sales else None
            if ds:
                return _resolve_transaction_type('DirectSale', ds, entry_hint_id=entry_hint_id)
        return ('invoice', 'Invoice', 'General invoice record')

    if latest_entry:
        e_type = (latest_entry.type or '').strip().upper()
        nimbus = (latest_entry.nimbus_no or '').strip().lower()
        tcat = (latest_entry.transaction_category or '').strip().lower()
        if e_type == 'CANCEL' or 'cancel' in nimbus or 'cancel' in tcat:
            return ('cancellation', 'Cancellation', 'Booking cancellation / reversal')
        if e_type == 'OUT' or 'delivery' in nimbus:
            return ('delivery', 'Delivery', 'Material delivery transaction')

    if t == 'Booking':
        return ('booking', 'Booking', 'Booked / reserved material bill')

    if t == 'PendingBill':
        reason = (getattr(bill_obj, 'reason', '') or '').strip().lower()
        if reason.startswith('booking'):
            return ('booking', 'Booking', getattr(bill_obj, 'reason', ''))
        if 'payment received' in reason:
            return ('payment_only', 'Payment Only', getattr(bill_obj, 'reason', ''))
        if 'direct sale' in reason or reason.startswith('auto sale'):
            return ('direct_sale', 'Direct Sale', getattr(bill_obj, 'reason', ''))
        if 'cancel' in reason:
            return ('cancellation', 'Cancellation', getattr(bill_obj, 'reason', ''))
        return ('pending_adjustment', 'Pending / Adjustment', getattr(bill_obj, 'reason', ''))

    return default


def _effective_collision_candidates(candidates_map):
    """
    PendingBill is a derivative tracker and should not trigger collision prompt
    when a primary bill source exists.
    """
    if not candidates_map:
        return {}
    primary_keys = [k for k in candidates_map.keys() if k != 'pending_bill']
    if primary_keys:
        return {k: candidates_map[k] for k in primary_keys}
    return candidates_map



# --- from bills_lookup.py ---
def find_bill_conflict(bill_no, exclude_sale_id=None):
    """Enhanced duplicate validation with consistency checking and orphan repair.
    
    Returns: tuple (source, id) if bill_no conflicts, else None
    
    CRITICAL: Before checking for conflicts, this function:
    1. Detects and logs orphaned Entry records (voided entries with non-voided parents)
    2. Auto-repairs orphaned entries to restore consistency
    3. Checks bill state consistency
    4. Logs all findings for audit trail
    """
    from app.services.void_rebuild import (
        _get_bill_consistency_status,
        _auto_repair_orphaned_entries,
        _log_bill_repair,
    )
    if not bill_no:
        return None
    
    base = (bill_no or '').strip()
    if not base:
        return None
    
    candidates = _bill_no_variants(base)
    if not candidates:
        return None
    
    # PRE-CHECK: Auto-repair orphaned entries
    try:
        orphan_count = _auto_repair_orphaned_entries(base)
        if orphan_count > 0:
            db.session.flush()
            _log_bill_repair(
                'ORPHAN_CLEANUP',
                base,
                f"auto-repaired {orphan_count} orphaned Entry records"
            )
    except Exception as e:
        _log_bill_repair(
            'ORPHAN_CLEANUP_FAILED',
            base,
            f"orphan repair attempt failed: {str(e)}",
            severity='ERROR'
        )
        db.session.rollback()
    
    # PRE-CHECK: Consistency verification
    try:
        consistency = _get_bill_consistency_status(base)
        if consistency and not consistency['is_consistent']:
            _log_bill_repair(
                'INCONSISTENCY_DETECTED',
                base,
                f"issues={consistency['issues']}"
            )
    except Exception:
        pass

    # CONFLICT DETECTION: Direct Sales
    q = DirectSale.query.filter(
        _not_void(DirectSale),
        or_(
            DirectSale.manual_bill_no.in_(candidates),
            DirectSale.auto_bill_no.in_(candidates)
        )
    )
    if exclude_sale_id:
        q = q.filter(DirectSale.id != exclude_sale_id)
    ds = q.first()
    if ds:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"DirectSale id={ds.id} client={ds.client_name}"
        )
        return ("DirectSale", ds.id)

    # CONFLICT DETECTION: Bookings
    bk = Booking.query.filter(
        _not_void(Booking),
        or_(
            Booking.manual_bill_no.in_(candidates),
            Booking.auto_bill_no.in_(candidates)
        )
    ).first()
    if bk:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"Booking id={bk.id} client={bk.client_name}"
        )
        return ("Booking", bk.id)

    # CONFLICT DETECTION: Payments
    pay = Payment.query.filter(
        _not_void(Payment),
        or_(
            Payment.manual_bill_no.in_(candidates),
            Payment.auto_bill_no.in_(candidates)
        )
    ).first()
    if pay:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"Payment id={pay.id} client={pay.client_name}"
        )
        return ("Payment", pay.id)

    # CONFLICT DETECTION: Material Returns
    mret = MaterialReturn.query.filter(
        _not_void(MaterialReturn),
        or_(
            MaterialReturn.manual_bill_no.in_(candidates),
            MaterialReturn.auto_bill_no.in_(candidates)
        )
    ).first()
    if mret:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"MaterialReturn id={mret.id} client={mret.client_name}"
        )
        return ("MaterialReturn", mret.id)

    # CONFLICT DETECTION: GRN
    grn = GRN.query.filter(
        _not_void(GRN),
        or_(
            GRN.manual_bill_no.in_(candidates),
            GRN.auto_bill_no.in_(candidates)
        )
    ).first()
    if grn:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"GRN id={grn.id} supplier={grn.supplier}"
        )
        return ("GRN", grn.id)

    # CONFLICT DETECTION: Invoices
    inv = Invoice.query.filter(_not_void(Invoice), Invoice.invoice_no.in_(candidates)).first()
    if inv:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"Invoice id={inv.id} client={inv.client_name}"
        )
        return ("Invoice", inv.id)

    # CONFLICT DETECTION: Pending Bills (fallback)
    pb = PendingBill.query.filter(_not_void(PendingBill), PendingBill.bill_no.in_(candidates)).first()
    if pb:
        _log_bill_repair(
            'CONFLICT_DETECTED',
            base,
            f"PendingBill id={pb.id} client={pb.client_name}"
        )
        return ("PendingBill", pb.id)

    return None


def _bill_no_variants(ref):
    out = []
    val = (ref or '').strip()
    if not val:
        return out

    # Some routes/templates can double-encode '#' as %2523, so decode progressively.
    seed_values = [val]
    if '%' in val:
        decoded = val
        for _ in range(3):
            next_decoded = unquote(decoded).strip()
            if not next_decoded or next_decoded == decoded:
                break
            seed_values.append(next_decoded)
            decoded = next_decoded

    for seed in [x for x in dict.fromkeys(seed_values) if x]:
        out.append(seed)
        kind = parse_bill_kind(seed)
        if kind == 'SB':
            parsed_ns, parsed_seq = _extract_sb_parts(seed)
            if parsed_seq is not None:
                ns = parsed_ns or AUTO_BILL_NS_DEFAULT
                out.append(normalize_auto_bill(str(parsed_seq), namespace=ns))
                # Keep numeric aliases only for legacy no-namespace values.
                if parsed_ns is None:
                    out.append(str(parsed_seq))
                    out.append(f'#{parsed_seq}')
        elif kind == 'MB':
            body = seed.split('.', 1)[1].strip() if '.' in seed else ''
            if body:
                out.append(normalize_manual_bill(body))
                out.append(body)
                out.append(f'#{body}')
        else:
            maybe_auto = normalize_auto_bill(seed, namespace=AUTO_BILL_NS_DEFAULT)
            maybe_manual = normalize_manual_bill(seed)
            if maybe_auto:
                out.append(maybe_auto)
            if maybe_manual:
                out.append(maybe_manual)
        # Legacy/imported rows can carry integer bill numbers as float-like text (e.g. "6230.0").
        # Normalize those to integer-style variants so lookup remains stable across sources.
        if re.fullmatch(r'\d+\.0+', seed):
            int_like = seed.split('.', 1)[0]
            if int_like:
                out.append(int_like)
                out.append(f'#{int_like}')
                out.append(normalize_auto_bill(int_like, namespace=AUTO_BILL_NS_DEFAULT))
                out.append(normalize_manual_bill(int_like))
        if seed.startswith('#') and re.fullmatch(r'\d+\.0+', seed[1:]):
            int_like = seed[1:].split('.', 1)[0]
            if int_like:
                out.append(int_like)
                out.append(f'#{int_like}')
                out.append(normalize_auto_bill(int_like, namespace=AUTO_BILL_NS_DEFAULT))
                out.append(normalize_manual_bill(int_like))
        if seed.startswith('#') and len(seed) > 1:
            out.append(seed[1:])
            out.append(normalize_auto_bill(seed[1:], namespace=AUTO_BILL_NS_DEFAULT))
            out.append(normalize_manual_bill(seed[1:]))
        elif seed.isdigit():
            out.append(f'#{seed}')
            out.append(normalize_auto_bill(seed, namespace=AUTO_BILL_NS_DEFAULT))
            out.append(normalize_manual_bill(seed))

    return [x for x in dict.fromkeys(out) if x]


def _collect_bill_refs_for_lookup(bill_type, bill_obj):
    if not bill_obj:
        return []
    refs = set()
    t = (bill_type or '').strip()
    if t == 'Booking':
        refs.update([getattr(bill_obj, 'manual_bill_no', None), getattr(bill_obj, 'auto_bill_no', None), f"BK-{bill_obj.id}"])
    elif t == 'Payment':
        refs.update([getattr(bill_obj, 'manual_bill_no', None), getattr(bill_obj, 'auto_bill_no', None), f"PAY-{bill_obj.id}"])
    elif t == 'DirectSale':
        refs.update([getattr(bill_obj, 'manual_bill_no', None), getattr(bill_obj, 'auto_bill_no', None), f"DS-{bill_obj.id}", f"CSH-{bill_obj.id}", f"UNBILLED-{bill_obj.id}"])
        if getattr(bill_obj, 'invoice', None) and bill_obj.invoice and bill_obj.invoice.invoice_no:
            refs.add(bill_obj.invoice.invoice_no)
    elif t == 'MaterialReturn':
        refs.update([getattr(bill_obj, 'manual_bill_no', None), getattr(bill_obj, 'auto_bill_no', None), f"RTN-{bill_obj.id}"])
    elif t == 'Invoice':
        refs.update([getattr(bill_obj, 'invoice_no', None)])
    elif t == 'GRN':
        refs.update([getattr(bill_obj, 'manual_bill_no', None), getattr(bill_obj, 'auto_bill_no', None)])
    elif t == 'PendingBill':
        refs.update([getattr(bill_obj, 'bill_no', None), getattr(bill_obj, 'manual_bill_no', None), getattr(bill_obj, 'auto_bill_no', None)])
    refs = {r for r in refs if r}
    all_refs = set()
    for r in refs:
        all_refs.update(_bill_no_variants(r))
    return [r for r in all_refs if r]


def _latest_entry_for_bill_refs(bill_refs, client_code=None, client_name=None):
    refs = [r for r in (bill_refs or []) if r]
    if not refs:
        return None
    q = Entry.query.filter(
        Entry.is_void == False,
        Entry.bill_no.in_(refs)
    )
    if client_code:
        q = q.filter(Entry.client_code == client_code)
    elif client_name:
        q = q.filter(func.lower(func.trim(func.coalesce(Entry.client, ''))) == client_name.strip().lower())
    return q.order_by(Entry.date.desc(), Entry.time.desc(), Entry.id.desc()).first()


def _entry_best_bill_ref(entry_obj):
    if not entry_obj:
        return ''
    primary = (getattr(entry_obj, 'bill_no', None) or '').strip()
    auto = (getattr(entry_obj, 'auto_bill_no', None) or '').strip()
    if primary and not primary.upper().startswith('UNBILLED'):
        return primary
    if auto and not auto.upper().startswith('UNBILLED'):
        return auto
    inv_id = getattr(entry_obj, 'invoice_id', None)
    if inv_id:
        inv = db.session.get(Invoice, inv_id)
        if inv and not inv.is_void and inv.invoice_no:
            return (inv.invoice_no or '').strip()
    return ''


def _lookup_bill(bill_no, hint_type=None, hint_id=None, hint_client_code=None, hint_client_name=None, hint_entry_id=None):
    """Resolve a bill number to an object; use optional hints to avoid collisions on imported legacy data."""
    hint_type = (hint_type or '').strip().lower()
    hint_client_name_norm = (hint_client_name or '').strip().lower()
    bill_variants = _bill_no_variants(bill_no)

    if hint_entry_id:
        try:
            hinted_entry = db.session.get(Entry, int(hint_entry_id))
        except Exception:
            hinted_entry = None
        if hinted_entry and not hinted_entry.is_void and hinted_entry.bill_no in bill_variants:
            if not hint_client_code:
                hint_client_code = hinted_entry.client_code
            if not hint_client_name:
                hint_client_name = hinted_entry.client

    def _bill_or_expr(model_manual, model_auto=None):
        clauses = []
        for b in bill_variants:
            clauses.append(model_manual == b)
            if model_auto is not None:
                clauses.append(model_auto == b)
        return or_(*clauses) if clauses else (model_manual == bill_no)

    booking = None
    payment = None
    invoice = None
    sale = None
    grn = None
    pending = None

    # If caller gives exact source + id, trust that first to avoid bill_no collisions.
    if hint_id:
        try:
            hid = int(hint_id)
        except Exception:
            hid = None
        if hid:
            if hint_type in ['booking', 'booked', 'bk']:
                row = db.session.get(Booking, hid)
                if row and not row.is_void:
                    refs = {row.manual_bill_no, row.auto_bill_no, f"BK-{row.id}"}
                    if bill_no in refs or any(v in refs for v in bill_variants):
                        booking = row
            elif hint_type in ['payment', 'pay']:
                row = db.session.get(Payment, hid)
                if row and not row.is_void:
                    refs = {row.manual_bill_no, row.auto_bill_no, f"PAY-{row.id}"}
                    if bill_no in refs or any(v in refs for v in bill_variants):
                        payment = row
            elif hint_type in ['direct_sale', 'sale', 'directsale', 'ds', 'cash']:
                row = db.session.get(DirectSale, hid)
                if row and not row.is_void:
                    refs = {row.manual_bill_no, row.auto_bill_no, f"DS-{row.id}", f"CSH-{row.id}", f"UNBILLED-{row.id}"}
                    if row.invoice and row.invoice.invoice_no:
                        refs.add(row.invoice.invoice_no)
                    if bill_no in refs or any(v in refs for v in bill_variants):
                        sale = row
            elif hint_type in ['invoice', 'inv']:
                row = db.session.get(Invoice, hid)
                if row and not row.is_void:
                    refs = {row.invoice_no}
                    if bill_no in refs or any(v in refs for v in bill_variants):
                        invoice = row
            elif hint_type in ['grn', 'purchase']:
                row = db.session.get(GRN, hid)
                if row and not row.is_void:
                    refs = {row.manual_bill_no, row.auto_bill_no}
                    if bill_no in refs or any(v in refs for v in bill_variants):
                        grn = row
            elif hint_type in ['pending', 'pending_bill', 'pb']:
                row = db.session.get(PendingBill, hid)
                if row and not row.is_void:
                    refs = {row.bill_no}
                    if bill_no in refs or any(v in refs for v in bill_variants):
                        pending = row

    # If caller provided an explicit source hint that resolved successfully, trust it
    # and avoid filling other object types that may share the same bill reference.
    if hint_id and (
        booking is not None or
        payment is not None or
        invoice is not None or
        sale is not None or
        grn is not None or
        pending is not None
    ):
        return booking, payment, invoice, sale, grn, pending

    if not booking:
        booking_q = Booking.query.filter(Booking.is_void == False, _bill_or_expr(Booking.manual_bill_no, Booking.auto_bill_no))
        if hint_client_name_norm:
            booking_q = booking_q.filter(func.lower(func.trim(Booking.client_name)) == hint_client_name_norm)
        booking = booking_q.order_by(Booking.id.desc()).first()
    if not payment:
        payment_q = Payment.query.filter(Payment.is_void == False, _bill_or_expr(Payment.manual_bill_no, Payment.auto_bill_no))
        if hint_client_name_norm:
            payment_q = payment_q.filter(func.lower(func.trim(Payment.client_name)) == hint_client_name_norm)
        payment = payment_q.order_by(Payment.id.desc()).first()
    if not invoice:
        invoice_q = Invoice.query.filter(Invoice.is_void == False, _bill_or_expr(Invoice.invoice_no))
        if hint_client_code:
            invoice_q = invoice_q.filter(Invoice.client_code == hint_client_code)
        if hint_client_name_norm:
            invoice_q = invoice_q.filter(func.lower(func.trim(Invoice.client_name)) == hint_client_name_norm)
        invoice = invoice_q.order_by(Invoice.id.desc()).first()
    if not sale:
        sale_q = DirectSale.query.filter(DirectSale.is_void == False, _bill_or_expr(DirectSale.manual_bill_no, DirectSale.auto_bill_no))
        if hint_client_name_norm:
            sale_q = sale_q.filter(func.lower(func.trim(DirectSale.client_name)) == hint_client_name_norm)
        sale = sale_q.order_by(DirectSale.id.desc()).first()
    if not grn:
        grn = GRN.query.filter(
            GRN.is_void == False,
            _bill_or_expr(GRN.manual_bill_no, GRN.auto_bill_no)
        ).order_by(GRN.id.desc()).first()
    if not pending:
        pending_q = PendingBill.query.filter(
            PendingBill.is_void == False,
            _bill_or_expr(PendingBill.bill_no)
        )
        if hint_client_code:
            pending_q = pending_q.filter(PendingBill.client_code == hint_client_code)
        if hint_client_name_norm:
            pending_q = pending_q.filter(func.lower(func.trim(PendingBill.client_name)) == hint_client_name_norm)
        pending = pending_q.order_by(PendingBill.id.desc()).first()

    # Handle generated IDs (BK-ID, DS-ID, CSH-ID) if not found by direct match
    if not (booking or payment or invoice or sale or grn):
        if bill_no.startswith('BK-'):
            try:
                booking = db.session.get(Booking, int(bill_no.split('-')[1]))
            except: pass
        elif bill_no.startswith('DS-') or bill_no.startswith('CSH-') or bill_no.startswith('UNBILLED-'):
            try:
                sale = db.session.get(DirectSale, int(bill_no.split('-')[1]))
            except: pass
        elif bill_no.startswith('PAY-'):
            try:
                payment = db.session.get(Payment, int(bill_no.split('-')[1]))
            except: pass
    
    return booking, payment, invoice, sale, grn, pending


def _bill_lookup_candidates_map(booking=None, payment=None, invoice=None, sale=None, grn=None, pending=None):
    candidates = {}
    if booking:
        candidates['booking'] = {'id': booking.id, 'label': f"Booking #{booking.id}", 'bill_no': booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"}
    if payment:
        candidates['payment'] = {'id': payment.id, 'label': f"Payment #{payment.id}", 'bill_no': payment.manual_bill_no or payment.auto_bill_no or f"PAY-{payment.id}"}
    if invoice:
        candidates['invoice'] = {'id': invoice.id, 'label': f"Invoice #{invoice.id}", 'bill_no': invoice.invoice_no}
    if sale:
        candidates['direct_sale'] = {'id': sale.id, 'label': f"Direct Sale #{sale.id}", 'bill_no': sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"}
    if grn:
        candidates['grn'] = {'id': grn.id, 'label': f"GRN #{grn.id}", 'bill_no': grn.manual_bill_no or grn.auto_bill_no}
    if pending:
        candidates['pending_bill'] = {'id': pending.id, 'label': f"Pending Bill #{pending.id}", 'bill_no': pending.bill_no}
    return candidates


