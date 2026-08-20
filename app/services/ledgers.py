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
from app.services.billing import (
    _resolve_transaction_type,
)
from app.services.drafts import (
    _infer_driver_name_from_refs,
)
from app.services.finance_clients import (
    _booking_ledger_gross_due,
    _client_balance_as_of,
    _parse_ledger_entry_dt,
    _resolve_cancel_display_amount,
)
from app.services.grn_svc import (
    calculate_grn_total,
)
from app.services.lookups import (
    get_client_by_input,
)
from app.services.sales_core import (
    _direct_sale_bill_refs,
    _rent_reconciliation_from_items,
)
from app.services.time_money import (
    _money_round,
    _parse_dt_safe,
    _to_float_or_zero,
    pk_now,
)
from app.services.void_rebuild import (
    _payment_receipt_refs,
)



# --- from ledgers_svc_rows.py ---
def _build_client_ledger_rows(client):
    client_name_norm = (client.name or '').strip().lower()

    def _fmt_dt(dt_val):
        if not dt_val:
            return ''
        if isinstance(dt_val, str):
            return dt_val
        try:
            return dt_val.strftime('%Y-%m-%d %H:%M')
        except Exception:
            return str(dt_val)

    def _parse_dt(dt_val):
        if isinstance(dt_val, datetime):
            return dt_val
        if isinstance(dt_val, date):
            return datetime.combine(dt_val, datetime.min.time())
        if isinstance(dt_val, str):
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    return datetime.strptime(dt_val, fmt)
                except ValueError:
                    continue
        return datetime.min

    def _fmt_qty(qty_val):
        try:
            q = float(qty_val or 0)
            return f"{q:.2f}".rstrip('0').rstrip('.')
        except Exception:
            return str(qty_val or '0')

    def _fmt_money(val):
        try:
            return f"{float(val or 0):,.2f}".rstrip('0').rstrip('.')
        except Exception:
            return str(val or '0')

    def _line_items_text(items, name_attr, qty_attr='qty', rate_attr=None, max_items=3):
        parts = []
        for it in (items or []):
            name = str(getattr(it, name_attr, '') or '').strip()
            if not name:
                continue
            qty = _fmt_qty(getattr(it, qty_attr, 0))
            if rate_attr:
                rate = _fmt_money(getattr(it, rate_attr, 0))
                parts.append(f"{name} ({qty} x {rate})")
            else:
                parts.append(f"{name} ({qty})")
        if not parts:
            return ''
        if len(parts) > max_items:
            shown = ' | '.join(parts[:max_items])
            return f"{shown} | +{len(parts) - max_items} more"
        return ' | '.join(parts)

    pending_bills = PendingBill.query.filter_by(
        client_code=client.code,
        is_void=False
    ).order_by(PendingBill.id.desc()).all()
    for pb in pending_bills:
        if pb.reason is None:
            pb.reason = ''

    bookings = Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm
    ).all()
    payments = Payment.query.filter(or_(
        Payment.client_id == client.id,
        and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm),
    )).all()
    direct_sales = DirectSale.query.filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm
    ).all()

    financial_history = []
    cancel_bill_refs = set()
    cancel_amount_by_bill = {}
    cancel_bill_rows = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).all()
    for ce in cancel_bill_rows:
        bno = (ce.bill_no or '').strip()
        ano = (ce.auto_bill_no or '').strip()
        if bno:
            cancel_bill_refs.add(bno)
        if ano:
            cancel_bill_refs.add(ano)
        bill_ref = bno or ano
        if bill_ref:
            qty = float(ce.qty or 0)
            mat_ref = (ce.material or ce.booked_material or '').strip()
            amount = _resolve_cancel_display_amount(
                client_name_norm=client_name_norm,
                bill_ref=bill_ref,
                mat_ref=mat_ref,
                qty=qty,
                note=getattr(ce, 'note', None)
            )
            if amount is not None and amount > 0:
                cancel_amount_by_bill[bill_ref] = float(cancel_amount_by_bill.get(bill_ref, 0) or 0) + float(amount)

    for b in bookings:
        if b.is_void:
            continue
        booking_bill_ref = b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}"
        debit = _booking_ledger_gross_due(
            b,
            cancel_value=cancel_amount_by_bill.get(booking_bill_ref, 0),
            allow_legacy_lift=(booking_bill_ref not in cancel_bill_refs)
        )
        credit = b.paid_amount or 0
        discount = getattr(b, 'discount', 0) or 0
        booking_items_text = _line_items_text(
            getattr(b, 'items', []),
            'material_name',
            qty_attr='qty',
            rate_attr='price_at_time'
        )
        booking_desc = 'Booking'
        if booking_items_text:
            booking_desc = f"Booking: {booking_items_text}"
        financial_history.append({
            'date': b.date_posted,
            'date_display': _fmt_dt(b.date_posted),
            'description': booking_desc,
            'bill_no': booking_bill_ref,
            'debit': debit,
            'credit': credit,
            'type': 'Booking',
            'id': b.id
        })
        if float(discount or 0) > 0:
            discount_reason = (getattr(b, 'discount_reason', None) or '').strip()
            discount_desc = 'DISCOUNT WAIVE OFF'
            if discount_reason:
                discount_desc = f'DISCOUNT WAIVE OFF ({discount_reason})'
            financial_history.append({
                'date': b.date_posted,
                'date_display': _fmt_dt(b.date_posted),
                'description': discount_desc,
                'bill_no': booking_bill_ref,
                'debit': 0,
                'credit': float(discount or 0),
                'type': None,
                'id': None
            })

    waive_rows = WaiveOff.query.filter(
        func.lower(func.trim(WaiveOff.client_name)) == client_name_norm,
        WaiveOff.is_void == False
    ).filter(
        ~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%')
    ).order_by(WaiveOff.date_posted.asc(), WaiveOff.id.asc()).all()
    waive_by_payment = {}
    standalone_waive_rows = []
    for w in waive_rows:
        if w.payment_id:
            waive_by_payment.setdefault(w.payment_id, []).append(w)
        else:
            standalone_waive_rows.append(w)

    for p in payments:
        if p.is_void:
            continue
        amt = p.amount or 0
        method_label = p.method or "Cash"
        pay_details = []
        if getattr(p, 'bank_name', None):
            pay_details.append(f"Bank: {p.bank_name}")
        if getattr(p, 'account_name', None):
            pay_details.append(f"A/C Name: {p.account_name}")
        if getattr(p, 'account_no', None):
            pay_details.append(f"A/C No: {p.account_no}")
        details_suffix = f" - {' | '.join(pay_details)}" if pay_details else ''
        if amt >= 0:
            debit = 0
            credit = amt
            payment_desc = f'Payment ({method_label}){details_suffix}'
        else:
            debit = abs(amt)
            credit = 0
            payment_desc = f'Repayment ({method_label}){details_suffix}'

        payment_bill_ref = p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}"
        financial_history.append({
            'date': p.date_posted,
            'date_display': _fmt_dt(p.date_posted),
            'description': payment_desc,
            'bill_no': payment_bill_ref,
            'debit': debit,
            'credit': credit,
            'type': 'Payment',
            'id': p.id
        })

        linked_waive_rows = waive_by_payment.get(p.id, [])
        if linked_waive_rows:
            for w in linked_waive_rows:
                w_desc = 'Waive-Off (Loss)'
                if (w.reason or '').strip():
                    w_desc = f'Waive-Off (Loss) ({w.reason.strip()})'
                financial_history.append({
                    'date': w.date_posted or p.date_posted,
                    'date_display': _fmt_dt(w.date_posted or p.date_posted),
                    'description': w_desc,
                    'bill_no': w.bill_no or payment_bill_ref,
                    'debit': 0,
                    'credit': float(w.amount or 0),
                    'type': None,
                    'id': None
                })
        else:
            p_discount = float(getattr(p, 'discount', 0) or 0)
            if p_discount > 0:
                discount_reason = (getattr(p, 'discount_reason', None) or '').strip()
                discount_desc = 'Waive-Off (Loss)'
                if discount_reason:
                    discount_desc = f'Waive-Off (Loss) ({discount_reason})'
                financial_history.append({
                    'date': p.date_posted,
                    'date_display': _fmt_dt(p.date_posted),
                    'description': discount_desc,
                    'bill_no': payment_bill_ref,
                    'debit': 0,
                    'credit': p_discount,
                    'type': None,
                    'id': None
                })

    def _waive_bill_ref(row):
        ref = (getattr(row, 'bill_no', None) or '').strip()
        if ref:
            return ref
        marker = (getattr(row, 'note', None) or '').strip()
        m = re.match(r'^\[direct_sale_discount:(\d+)\]$', marker, re.IGNORECASE)
        if m:
            sale = db.session.get(DirectSale, int(m.group(1)))
            if sale:
                return (sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}")
        return ''

    for w in standalone_waive_rows:
        w_desc = 'Waive-Off (Loss)'
        if (w.reason or '').strip():
            w_desc = f'Waive-Off (Loss) ({w.reason.strip()})'
        financial_history.append({
            'date': w.date_posted,
            'date_display': _fmt_dt(w.date_posted),
            'description': w_desc,
            'bill_no': _waive_bill_ref(w),
            'debit': 0,
            'credit': float(w.amount or 0),
            'type': None,
            'id': None
        })

    for s in direct_sales:
        if s.is_void:
            continue
        debit = s.amount or 0
        credit = s.paid_amount or 0
        discount = getattr(s, 'discount', 0) or 0
        sale_items_text = _line_items_text(
            getattr(s, 'items', []),
            'product_name',
            qty_attr='qty',
            rate_attr='price_at_time'
        )
        sale_desc = 'Direct Sale'
        if sale_items_text:
            sale_desc = f"Direct Sale: {sale_items_text}"
        sale_bill_ref = (
            (s.invoice.invoice_no if getattr(s, 'invoice', None) else None)
            or s.manual_bill_no
            or s.auto_bill_no
            or f"DS-{s.id}"
        )
        if debit > 0 or credit > 0:
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': sale_desc,
                'bill_no': sale_bill_ref,
                'debit': debit,
                'credit': credit,
                'type': 'DirectSale',
                'id': s.id
            })
        if float(discount or 0) > 0:
            discount_reason = (getattr(s, 'discount_reason', None) or '').strip()
            discount_desc = 'DISCOUNT WAIVE OFF (Direct Sale)'
            if discount_reason:
                discount_desc = f'DISCOUNT WAIVE OFF (Direct Sale) ({discount_reason})'
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': discount_desc,
                'bill_no': sale_bill_ref,
                'debit': 0,
                'credit': float(discount or 0),
                'type': None,
                'id': None
            })
        # Informational row only: keep client balance unchanged while showing why P/L changed.
        rent_loss = float(getattr(s, 'rent_variance_loss', 0) or 0)
        if rent_loss > 0:
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': f'Delivery Rent Variance (Company Loss) Rs.{rent_loss:.2f}',
                'bill_no': sale_bill_ref,
                'debit': 0,
                'credit': 0,
                'type': None,
                'id': None
            })

    # Explicit booking-cancellation rows for readability in financial ledger.
    cancel_entries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).order_by(Entry.date.asc(), Entry.time.asc(), Entry.id.asc()).all()
    for ce in cancel_entries:
        qty = float(ce.qty or 0)
        bill_ref = (ce.bill_no or ce.auto_bill_no or '').strip()
        mat_ref = (ce.material or ce.booked_material or '').strip()
        amount = _resolve_cancel_display_amount(
            client_name_norm=client_name_norm,
            bill_ref=bill_ref,
            mat_ref=mat_ref,
            qty=qty,
            note=getattr(ce, 'note', None)
        )
        desc = f"Booking Cancel ({(ce.material or ce.booked_material or '-')} x {qty:.3f})"
        cancel_dt = _parse_ledger_entry_dt(ce.date, ce.time)
        financial_history.append({
            'date': cancel_dt,
            'date_display': _fmt_dt(cancel_dt),
            'description': desc,
            'bill_no': ce.bill_no or '',
            'debit': 0,
            'credit': float(amount or 0),
            'type': 'Entry',
            'id': ce.id,
            'is_cancel_entry': True,
            'cancel_amount': amount
        })

    financial_history.sort(key=lambda x: _parse_dt(x.get('date')))

    opening_balance = _to_float_or_zero(getattr(client, 'opening_balance', 0))
    if opening_balance != 0:
        opening_dt = (
            getattr(client, 'opening_balance_date', None)
            or getattr(client, 'created_at', None)
            or datetime.min
        )
        financial_history.insert(0, {
            'date': opening_dt,
            'date_display': _fmt_dt(opening_dt),
            'description': 'Opening Balance',
            'bill_no': 'OPENING',
            'debit': opening_balance if opening_balance > 0 else 0,
            'credit': abs(opening_balance) if opening_balance < 0 else 0,
            'type': None,
            'id': None
        })

    running_balance = Decimal('0.00')
    for row in financial_history:
        row['debit'] = _money_round(row.get('debit', 0))
        row['credit'] = _money_round(row.get('credit', 0))
        running_balance += Decimal(str(row['debit'])) - Decimal(str(row['credit']))
        bal = running_balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if bal == Decimal('-0.00'):
            bal = Decimal('0.00')
        row['balance'] = float(bal)

    total_debit = sum(Decimal(str(x.get('debit') or 0)) for x in financial_history)
    total_credit = sum(Decimal(str(x.get('credit') or 0)) for x in financial_history)
    total_balance = total_debit - total_credit
    total_debit = _money_round(total_debit)
    total_credit = _money_round(total_credit)
    total_balance = _money_round(total_balance)

    deliveries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type.in_(['OUT', 'CANCEL']),
        Entry.is_void == False,
        not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
    ).order_by(Entry.date.asc(), Entry.time.asc()).all()

    material_history = []

    bookings_for_material = Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).order_by(Booking.date_posted.asc()).all()
    for b in bookings_for_material:
        for item in b.items:
            created_at = getattr(b, 'created_at', None)
            date_sort = b.date_posted if b.date_posted else None
            if not date_sort and created_at:
                try:
                    date_sort = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
                except Exception:
                    try:
                        date_sort = datetime.strptime(created_at[:10], '%Y-%m-%d')
                    except Exception:
                        date_sort = None
            material_history.append({
                'date': b.date_posted.strftime('%Y-%m-%d') if b.date_posted else (created_at[:10] if created_at else ''),
                'date_sort': date_sort,
                'material': item.material_name,
                'material_group': item.material_name,
                'material_display': item.material_name,
                'qty_added': item.qty,
                'qty_dispatched': 0,
                'bill_no': b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}",
                'nimbus_no': 'Booking',
                'type': 'Booking',
                'source_type': 'Booking',
                'source_id': b.id
            })

    for d in deliveries:
        bill_ref = d.bill_no or d.auto_bill_no
        mat_name = d.booked_material or d.material
        if not mat_name:
            continue
        material_display = mat_name
        if d.booked_material and d.material and d.booked_material != d.material:
            material_display = f"{d.booked_material}>ALT>{d.material}"
        date_sort = None
        try:
            if d.date and d.time:
                date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
            elif d.date:
                date_sort = datetime.strptime(d.date, '%Y-%m-%d')
        except Exception:
            date_sort = None

        row_type = 'Cancel' if d.type == 'CANCEL' else 'Dispatch'
        material_history.append({
            'date': d.date,
            'date_sort': date_sort,
            'material': mat_name,
            'material_group': mat_name,
            'material_display': material_display,
            'qty_added': 0,
            'qty_dispatched': d.qty,
            'bill_no': bill_ref,
            'nimbus_no': d.nimbus_no or 'Booking Cancel',
            'type': row_type,
            'source_type': 'Entry',
            'source_id': d.id
        })

    def _mat_sort_key(x):
        d = x.get('date_sort') or datetime.min
        t = x.get('type')
        if d != datetime.min:
            p = 0
        else:
            if t == 'Booking':
                p = 0
            elif t == 'Cancel':
                p = 1
            else:
                p = 2
        return (d, p)

    material_history.sort(key=_mat_sort_key)

    mat_balances = {}
    for item in material_history:
        mat = item.get('material_group') or item.get('material') or 'Unknown'
        if mat not in mat_balances:
            mat_balances[mat] = 0
        if item.get('type') != 'Cancel':
            mat_balances[mat] += (item.get('qty_added', 0) - item.get('qty_dispatched', 0))
        item['balance'] = mat_balances[mat]

    material_history_grouped = {}
    for item in material_history:
        mat_name = item.get('material_group') or item.get('material') or 'Unknown'
        material_history_grouped.setdefault(mat_name, []).append(item)

    return (
        financial_history,
        pending_bills,
        total_debit,
        total_credit,
        total_balance,
        material_history_grouped
    )


def _build_supplier_ledger_rows(supplier):
    # Fetch GRNs (Credits)
    grns = GRN.query.filter(
        or_(GRN.supplier_id == supplier.id, GRN.supplier == supplier.name),
        GRN.is_void == False
    ).all()

    # Fetch Payments (Debits)
    payments = SupplierPayment.query.filter_by(supplier_id=supplier.id, is_void=False).all()

    ledger = []
    opening_balance = _to_float_or_zero(getattr(supplier, 'opening_balance', 0))
    if opening_balance != 0:
        opening_dt = (
            getattr(supplier, 'opening_balance_date', None)
            or getattr(supplier, 'created_at', None)
            or datetime.min
        )
        ledger.append({
            'date': opening_dt,
            'type': 'OPENING',
            'ref': 'OPENING',
            'bill_no': '',
            'description': 'Opening Balance',
            'credit': opening_balance if opening_balance > 0 else 0,
            'debit': abs(opening_balance) if opening_balance < 0 else 0,
            'id': 0
        })
    for g in grns:
        total = calculate_grn_total(g)
        item_lines = []
        for gi in [i for i in (g.items or []) if not bool(getattr(i, 'is_void', False))]:
            qty_val = float(gi.qty or 0)
            rate_val = float(gi.price_at_time or 0)
            item_lines.append({
                'name': gi.mat_name or '',
                'qty': qty_val,
                'rate': rate_val,
                'amount': qty_val * rate_val
            })
        ledger.append({
            'date': g.date_posted,
            'type': 'GRN',
            'ref': g.manual_bill_no or g.auto_bill_no,
            'bill_no': g.manual_bill_no or g.auto_bill_no,
            'description': f"Goods Receipt ({len(g.items)} items)",
            'credit': total, # Payable to supplier
            'debit': 0,
            'id': g.id,
            'item_lines': item_lines,
            'note': (g.note or '').strip()
        })

    for p in payments:
        ledger.append({
            'date': p.date_posted,
            'type': 'Payment',
            'ref': f"PAY-{p.id}",
            'bill_no': f"PAY-{p.id}",
            'description': f"Payment ({p.method})",
            'credit': 0,
            'debit': p.amount, # Paid to supplier
            'id': p.id,
            'payment_obj': p,
            'item_lines': [],
            'note': (p.note or '').strip()
        })

    # Sort oldest -> newest by exact timestamp.
    def _supplier_row_sort_key(row):
        dt = row.get('date')
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime.combine(dt, datetime.min.time())
        dt_key = dt if isinstance(dt, datetime) else datetime.min
        row_type = row.get('type')
        if row_type == 'OPENING':
            type_key = 0
        elif row_type == 'GRN':
            type_key = 1
        else:
            type_key = 2
        id_key = int(row.get('id') or 0)
        return (dt_key, type_key, id_key)

    ledger.sort(key=_supplier_row_sort_key)

    # Calculate running balance
    balance = 0
    for row in ledger:
        balance += (row['credit'] - row['debit'])
        row['balance'] = balance

    total_bill = sum(float(x.get('credit') or 0) for x in ledger)
    total_paid = sum(float(x.get('debit') or 0) for x in ledger)
    return ledger, balance, total_bill, total_paid



# --- from ledgers_svc_receipts.py ---
def _receipt_sort_dt(value):
    parsed = _parse_dt_safe(value)
    return parsed or datetime.min


def _extract_receipt_block(rendered_html):
    styles = '\n'.join(
        m.group(0)
        for m in re.finditer(r'<style\b[^>]*>.*?</style>', rendered_html or '', flags=re.IGNORECASE | re.DOTALL)
        if 'rcpt-' in m.group(0)
    )
    wrapper_match = re.search(
        r'(<div\s+class="rcpt-wrapper\b.*?</div>\s*<!--\s*/rcpt-wrapper\s*-->)',
        rendered_html or '',
        flags=re.IGNORECASE | re.DOTALL
    )
    if wrapper_match:
        return f"{styles}\n{wrapper_match.group(1)}"
    content_match = re.search(
        r'{%\s*block\s+content\s*%}(.*?){%\s*endblock\s*%}',
        rendered_html or '',
        flags=re.IGNORECASE | re.DOTALL
    )
    return f"{styles}\n{content_match.group(1) if content_match else (rendered_html or '')}"


def _client_bill_snapshot_context(client, bill_type, bill_obj):
    cutoff_dt = None
    if bill_type == 'Booking':
        cutoff_dt = getattr(bill_obj, 'date_posted', None)
    elif bill_type == 'Payment':
        cutoff_dt = getattr(bill_obj, 'date_posted', None)
    elif bill_type == 'DirectSale':
        cutoff_dt = getattr(bill_obj, 'date_posted', None)
    elif bill_type == 'MaterialReturn':
        cutoff_dt = getattr(bill_obj, 'date_posted', None)

    client_balance = _client_balance_as_of(client, cutoff_dt=cutoff_dt) if client else 0
    effect = 0
    if bill_type == 'Booking':
        effect = (bill_obj.amount or 0) - (bill_obj.paid_amount or 0)
    elif bill_type == 'Payment':
        effect = -(bill_obj.amount or 0)
    elif bill_type == 'DirectSale':
        effect = (bill_obj.amount or 0) - (getattr(bill_obj, 'discount', 0) or 0) - (bill_obj.paid_amount or 0)
    elif bill_type == 'MaterialReturn':
        effect = -(bill_obj.amount or 0)
    previous_balance = client_balance - effect
    return client_balance, previous_balance


def _render_client_history_receipt(client, bill_type, bill_obj):
    all_clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    all_materials = Material.query.order_by(Material.name.asc()).all()
    items = []
    template_name = 'view_bill.html'
    direct_sale_rent_reconciliation = None
    delivery_people = None

    if bill_type == 'Booking':
        items = bill_obj.items
        if not (getattr(bill_obj, 'driver_name', None) or '').strip():
            inferred_driver = _infer_driver_name_from_refs(
                [bill_obj.manual_bill_no, bill_obj.auto_bill_no, f"BK-{bill_obj.id}"],
                allow_booking=True
            )
            if inferred_driver:
                setattr(bill_obj, 'driver_name', inferred_driver)
    elif bill_type == 'Payment':
        template_name = 'payment_receipt.html'
    elif bill_type == 'DirectSale':
        items = bill_obj.items
        if not (bill_obj.driver_name or '').strip():
            inferred_driver = _infer_driver_name_from_refs(_direct_sale_bill_refs(bill_obj))
            if inferred_driver:
                bill_obj.driver_name = inferred_driver
        delivery_people = []
        for alloc in (getattr(bill_obj, 'delivery_person_allocations', None) or []):
            if getattr(alloc, 'is_void', False):
                continue
            dp = getattr(alloc, 'delivery_person', None)
            name = (getattr(dp, 'name', None) or '').strip()
            if name:
                delivery_people.append(name)
        if not delivery_people and (bill_obj.driver_name or '').strip():
            delivery_people = [bill_obj.driver_name.strip()]
        rent_row = DeliveryRent.query.filter_by(sale_id=bill_obj.id, is_void=False).order_by(DeliveryRent.id.desc()).first()
        sale_items_payload = [
            {'product_name': it.product_name, 'qty': it.qty, 'price_at_time': it.price_at_time}
            for it in (bill_obj.items or [])
        ]
        delivery_cost = float(getattr(bill_obj, 'delivery_rent_cost', 0) or 0)
        if delivery_cost <= 0 and rent_row:
            delivery_cost = float(rent_row.amount or 0)
        calc_rec = _rent_reconciliation_from_items(
            sale_items_payload,
            delivery_rent_cost=delivery_cost,
            client_name=bill_obj.client_name
        )
        direct_sale_rent_reconciliation = {
            'rent_item_revenue': float(getattr(bill_obj, 'rent_item_revenue', 0) or calc_rec['rent_item_revenue']),
            'delivery_rent_cost': float(getattr(bill_obj, 'delivery_rent_cost', 0) or calc_rec['delivery_rent_cost']),
            'rent_variance_loss': float(getattr(bill_obj, 'rent_variance_loss', 0) or calc_rec['rent_variance_loss'])
        }
    elif bill_type == 'MaterialReturn':
        items = bill_obj.items
        if not hasattr(bill_obj, 'paid_amount'):
            setattr(bill_obj, 'paid_amount', 0)

    client_balance, previous_balance = _client_bill_snapshot_context(client, bill_type, bill_obj)
    tx_code, tx_label, tx_note = _resolve_transaction_type(bill_type, bill_obj)
    rendered = render_template(
        template_name,
        bill=bill_obj,
        type=bill_type,
        items=items,
        client=client,
        client_balance=client_balance,
        previous_balance=previous_balance,
        recent_deliveries=[],
        material_ledger_recent=[],
        material_stock_summary=[],
        clients=all_clients,
        materials=all_materials,
        transaction_type_code=tx_code,
        transaction_type_label=tx_label,
        transaction_type_note=tx_note,
        direct_sale_rent_reconciliation=direct_sale_rent_reconciliation,
        delivery_people=delivery_people,
        pk_now=pk_now,
        auto_print=False
    )
    return _extract_receipt_block(rendered)


def _client_history_receipts(client):
    client_name_norm = (client.name or '').strip().lower()
    records = []

    for row in Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).all():
        records.append({'dt': _receipt_sort_dt(row.date_posted), 'id': row.id or 0, 'type': 'Booking', 'obj': row})

    for row in Payment.query.filter(
        or_(Payment.client_id == client.id,
            and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm)),
        Payment.is_void == False
    ).all():
        records.append({'dt': _receipt_sort_dt(row.date_posted), 'id': row.id or 0, 'type': 'Payment', 'obj': row})

    for row in DirectSale.query.filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
        DirectSale.is_void == False
    ).all():
        records.append({'dt': _receipt_sort_dt(row.date_posted), 'id': row.id or 0, 'type': 'DirectSale', 'obj': row})

    for row in MaterialReturn.query.filter(
        func.lower(func.trim(MaterialReturn.client_name)) == client_name_norm,
        MaterialReturn.is_void == False
    ).all():
        records.append({'dt': _receipt_sort_dt(row.date_posted), 'id': row.id or 0, 'type': 'MaterialReturn', 'obj': row})

    records.sort(key=lambda x: (x['dt'], x['id']))
    receipt_blocks = []
    for rec in records:
        try:
            receipt_blocks.append({
                'date': rec['dt'],
                'type': rec['type'],
                'id': rec['id'],
                'html': _render_client_history_receipt(client, rec['type'], rec['obj'])
            })
        except Exception:
            logging.getLogger(__name__).exception(
                'Failed rendering full-history receipt %s #%s for client #%s',
                rec['type'], rec['id'], client.id
            )
    return receipt_blocks


def _client_all_active_bills(client):
    client_name_norm = (client.name or '').strip().lower()
    bills = PendingBill.query.filter(
        PendingBill.is_void == False,
        or_(
            PendingBill.client_code == client.code,
            func.lower(func.trim(func.coalesce(PendingBill.client_name, ''))) == client_name_norm
        )
    ).order_by(PendingBill.created_at.asc(), PendingBill.id.asc()).all()
    for bill in bills:
        if bill.reason is None:
            bill.reason = ''
    return bills


def _client_history_summary(client, financial_history, material_history_grouped, active_bills):
    client_name_norm = (client.name or '').strip().lower()
    total_bookings = db.session.query(func.sum(Booking.amount)).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).scalar() or 0
    total_direct_sales = db.session.query(func.sum(DirectSale.amount)).filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
        DirectSale.is_void == False
    ).scalar() or 0
    total_payments = db.session.query(func.sum(Payment.amount)).filter(
        or_(Payment.client_id == client.id,
            and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm)),
        Payment.is_void == False
    ).scalar() or 0
    total_booking_paid = db.session.query(func.sum(Booking.paid_amount)).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).scalar() or 0
    total_sale_paid = db.session.query(func.sum(DirectSale.paid_amount)).filter(
        func.lower(func.trim(DirectSale.client_name)) == client_name_norm,
        DirectSale.is_void == False
    ).scalar() or 0
    total_waive = db.session.query(func.sum(WaiveOff.amount)).filter(
        func.lower(func.trim(WaiveOff.client_name)) == client_name_norm,
        WaiveOff.is_void == False
    ).scalar() or 0
    total_pending = sum(float(b.amount or 0) for b in active_bills if not b.is_paid)
    current_balance = financial_history[-1]['balance'] if financial_history else 0
    total_materials_booked = db.session.query(func.sum(BookingItem.qty)).join(Booking).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False
    ).scalar() or 0
    total_materials_delivered = 0
    for rows in (material_history_grouped or {}).values():
        for row in rows:
            total_materials_delivered += float(row.get('qty_dispatched') or 0)
    return {
        'total_bookings': float(total_bookings or 0),
        'total_direct_sales': float(total_direct_sales or 0),
        'total_paid': float(total_payments or 0) + float(total_booking_paid or 0) + float(total_sale_paid or 0),
        'total_pending': float(total_pending or 0),
        'total_waive_off': float(total_waive or 0),
        'current_balance': float(current_balance or 0),
        'total_materials_booked': float(total_materials_booked or 0),
        'total_materials_delivered': float(total_materials_delivered or 0),
    }


def _payment_receipt_pending_bill_rows(payment):
    refs = _payment_receipt_refs(payment)
    if not refs:
        return []
    reason_filter = func.lower(func.coalesce(PendingBill.reason, '')).like('payment received%')
    bill_filter = or_(*[PendingBill.bill_no.ilike(r) for r in refs])
    client_obj = get_client_by_input(payment.client_name or '')
    if client_obj:
        client_filter = or_(
            PendingBill.client_code == client_obj.code,
            func.lower(func.coalesce(PendingBill.client_name, '')) == client_obj.name.lower(),
            func.coalesce(PendingBill.client_code, '') == ''
        )
    else:
        client_filter = func.lower(func.coalesce(PendingBill.client_name, '')) == (payment.client_name or '').strip().lower()
    return PendingBill.query.filter(reason_filter, bill_filter, client_filter).all()


