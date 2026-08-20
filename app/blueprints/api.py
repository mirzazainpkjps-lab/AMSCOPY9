"""HTTP routes: api."""
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, make_response, send_from_directory, abort, session
from flask_login import login_required, login_user, logout_user, current_user
from datetime import datetime, date, timedelta
from sqlalchemy import func, case, text, or_, and_, exists, not_
from types import SimpleNamespace
from decimal import Decimal, ROUND_HALF_UP
import os, io, json, re, logging, calendar

from models import *
from app.services.api import *  # noqa
from app.services.financial_ledgers import _client_snapshot_for
from utils.audit import audit_log

bp = Blueprint('api', __name__)

@bp.route('/api/ui/theme', methods=['GET', 'POST'])
def ui_theme_preference_api():
    if request.method == 'GET':
        stored = (session.get('ui_theme') or '').strip().lower()
        theme = stored if stored in ('light', 'dark') else None
        return jsonify({'theme': theme})

    payload = request.get_json(silent=True) or {}
    theme = str(payload.get('theme', '')).strip().lower()
    if theme not in ('light', 'dark'):
        return jsonify({'ok': False, 'error': 'Invalid theme'}), 400
    session['ui_theme'] = theme
    return jsonify({'ok': True, 'theme': theme})


@bp.route('/api/client_booking_status/<client_code>')
@login_required
def api_client_booking_status(client_code):
    client = get_client_by_input(client_code)
    if not client:
        return jsonify([])

    # Get bookings by client name (Booking model uses name)
    bookings = Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == func.lower(func.trim(client.name)),
        Booking.is_void == False
    ).all()
    booking_ids = [b.id for b in bookings]

    def _material_key(v):
        txt = (v or '').strip().lower()
        return re.sub(r'[^a-z0-9]+', '', txt)

    booked_totals = {}
    material_labels = {}
    latest_price = {}
    latest_price_dt = {}
    if booking_ids:
        items = BookingItem.query.filter(BookingItem.booking_id.in_(booking_ids)).all() # BookingItem doesn't have is_void, parent Booking does
        for item in items:
            raw_mat = (item.material_name or '').strip()
            key = _material_key(raw_mat)
            if not key:
                continue
            booked_totals[key] = booked_totals.get(key, 0) + (item.qty or 0)
            if key not in material_labels:
                material_labels[key] = raw_mat
            bk = item.booking
            bk_dt = bk.date_posted if bk and getattr(bk, 'date_posted', None) else None
            if key not in latest_price_dt or (bk_dt and latest_price_dt[key] and bk_dt > latest_price_dt[key]) or (bk_dt and not latest_price_dt[key]):
                latest_price_dt[key] = bk_dt
                latest_price[key] = float(item.price_at_time or 0)
            elif key not in latest_price:
                latest_price[key] = float(item.price_at_time or 0)

    # Get delivered totals from Entry (OUT)
    client_name_norm = (client.name or '').strip().lower()
    entries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'OUT'
    ).filter(
        Entry.is_void == False,
        # Direct Sale credit/cash rows must not consume booking balance.
        not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
    ).all()

    delivered_totals = {}
    for e in entries:
        key = _material_key(e.booked_material or e.material)
        if not key:
            continue
        delivered_totals[key] = delivered_totals.get(key, 0) + e.qty

    returned_totals = {}
    returns = Entry.query.filter(
        (Entry.client_code == client_code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'IN',
        Entry.is_void == False,
        Entry.nimbus_no == 'Material Return',
        Entry.transaction_category == 'Booked Return'
    ).all()
    for e in returns:
        key = _material_key(e.material)
        if not key:
            continue
        returned_totals[key] = returned_totals.get(key, 0) + e.qty

    status_data = []
    for key, booked_qty in booked_totals.items():
        delivered_qty = delivered_totals.get(key, 0)
        returned_qty = returned_totals.get(key, 0)
        status_data.append({
            'material': material_labels.get(key, key),
            'booked': booked_qty,
            'delivered': delivered_qty,
            'returned': returned_qty,
            'balance': booked_qty - delivered_qty + returned_qty,
            'unit_price': latest_price.get(key, 0)
        })

    return jsonify(status_data)


@bp.route('/api/client_financial_summary/<client_code>')
@login_required
def api_client_financial_summary(client_code):
    client = get_client_by_input(client_code)
    if not client:
        resp = jsonify({'found': False, 'generated_at': pk_now().strftime('%Y-%m-%d %H:%M:%S')})
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp
    unified_ledger = build_client_financial_ledger(client, snapshot=_client_snapshot_for(client))
    summary = {
        'balance': float(unified_ledger.get('closing_balance') or 0),
        'debit_total': float(unified_ledger.get('total_debit') or 0),
        'credit_total': float(unified_ledger.get('total_credit') or 0),
        'cash_received_total': float(unified_ledger.get('total_credit') or 0),
        'waive_off_total': sum(float(row.get('credit') or 0) for row in unified_ledger.get('rows', []) if row.get('type') == 'Waive-Off'),
        'status': (unified_ledger.get('status') or '').lower(),
    }
    resp = jsonify({
        'found': True,
        'client_name': client.name,
        'client_code': client.code,
        'generated_at': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
        **summary
    })
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@bp.route('/api/last_sold_price')
@login_required
def api_last_sold_price():
    client = request.args.get('client', '').strip()
    material = request.args.get('material', '').strip()
    if not client or not material:
        return jsonify({'found': False, 'price': None})
    item = db.session.query(DirectSaleItem).join(
        DirectSale, DirectSaleItem.sale_id == DirectSale.id
    ).filter(
        DirectSale.is_void == False,
        DirectSale.client_name == client,
        DirectSaleItem.product_name == material,
    ).order_by(DirectSale.date_posted.desc()).first()
    if item:
        sale_date = item.direct_sale.date_posted.strftime('%Y-%m-%d') if item.direct_sale and item.direct_sale.date_posted else ''
        return jsonify({'found': True, 'price': item.price_at_time, 'date': sale_date})
    return jsonify({'found': False, 'price': None})


@bp.route('/api/material_next_code')
@login_required
def api_material_next_code():
    category_id = (request.args.get('category_id') or '').strip()
    material_name = (request.args.get('material_name') or '').strip()
    category = None
    if category_id:
        try:
            category = db.session.get(MaterialCategory, int(category_id))
        except Exception:
            category = None
    if not category:
        category = get_or_create_material_category('General')
    code = _next_material_code_for_category(category, material_name=material_name)
    return jsonify({
        'success': True,
        'code': code,
        'category_id': category.id if category else None,
        'category_name': category.name if category else 'General',
        'is_ft_product': bool((material_name or '').strip().upper().startswith('FT-'))
    })


@bp.route('/api/client_next_code')
@login_required
def api_client_next_code():
    return jsonify({
        'success': True,
        'code': generate_client_code()
    })


@bp.route('/api/notifications/contact_history/<int:bill_id>')
@login_required
def api_notifications_contact_history(bill_id):
    pb = db.session.get(PendingBill, bill_id)
    if not pb:
        return jsonify({'error': 'Pending bill not found'}), 404

    logs = FollowUpContact.query.filter_by(pending_bill_id=pb.id).order_by(
        FollowUpContact.contacted_at.desc(),
        FollowUpContact.id.desc()
    ).all()
    return jsonify([{
        'id': x.id,
        'contacted_at': x.contacted_at.strftime('%Y-%m-%d %H:%M') if x.contacted_at else '',
        'channel': x.channel or '',
        'response': x.response or '',
        'note': x.note or '',
        'created_by': x.created_by or ''
    } for x in logs])


@bp.route('/api/notifications/due')
@login_required
def api_notifications_due():
    now = pk_now()
    due = FollowUpReminder.query.filter(
        FollowUpReminder.is_done == False,
        FollowUpReminder.remind_at <= now,
        FollowUpReminder.alerted_at == None
    ).order_by(FollowUpReminder.remind_at.asc()).all()
    payload = []
    for r in due:
        pb = r.pending_bill
        payload.append({
            'id': r.id,
            'client': pb.client_name if pb else '',
            'bill_no': pb.bill_no if pb else '',
            'amount': float(pb.amount or 0) if pb else 0,
            'note': r.note or '',
            'remind_at': r.remind_at.strftime('%Y-%m-%d %H:%M')
        })
        r.alerted_at = now
    if due:
        db.session.commit()
    return jsonify(payload)


@bp.route('/api/clients/search')
@login_required
def api_clients_search():
    q = request.args.get('q', '').strip()
    query = Client.query.filter(Client.is_active == True)
    if q:
        query = query.filter(
            db.or_(Client.name.ilike(f'%{q}%'), Client.code.ilike(f'%{q}%'))
        )
    clients = query.order_by(Client.name.asc(), Client.id.asc()).limit(25).all()
    return jsonify([{'id': c.id, 'name': c.name, 'code': c.code, 'category': c.category} for c in clients])


@bp.route('/api/suppliers/search')
@login_required
def api_suppliers_search():
    q = request.args.get('q', '').strip()
    query = Supplier.query.filter(Supplier.is_active == True)
    if q:
        query = query.filter(Supplier.name.ilike(f'%{q}%'))
    suppliers = query.order_by(Supplier.name.asc(), Supplier.id.asc()).limit(25).all()
    return jsonify([{'id': s.id, 'name': s.name, 'phone': s.phone or ''} for s in suppliers])


@bp.route('/api/check_bill/<path:bill_no>')
@login_required
def check_bill_api(bill_no):
    entry = Entry.query.filter_by(bill_no=bill_no).first()
    if entry:
        return jsonify({
            'exists': True,
            'url': url_for('tracking', search=bill_no),
            'material': entry.material,
            'qty': int(entry.qty)
        })
    return jsonify({'exists': False})


@bp.route('/api/supplier_balance/<int:id>')
@login_required
def api_supplier_balance(id):
    supplier = db.session.get(Supplier, id)
    if not supplier:
        return jsonify({'balance': 0})

    ledger = build_supplier_financial_ledger(supplier)
    return jsonify({
        'balance': float(ledger['closing_balance'] or 0),
        'opening_balance': float(_to_float_or_zero(getattr(supplier, 'opening_balance', 0))),
        'total_bill': float(ledger['total_credit'] or 0),
        'total_paid': float(ledger['total_debit'] or 0),
        'rows': len(ledger['rows'] or [])
    })


@bp.route('/api/audit/financial-integrity')
@login_required
def api_financial_integrity_audit():
    """Read-only ghost/duplicate/orphan audit; no questionable data is deleted."""
    return jsonify(financial_integrity_audit())


