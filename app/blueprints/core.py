"""HTTP routes: core."""
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
from utils.audit import audit_log

bp = Blueprint('core', __name__)

@bp.route('/')
@login_required
def index():
    today = pk_today().strftime('%B %d, %Y')
    today_date = pk_today()

    client_count = db.session.query(func.count(Client.id)).scalar() or 0

    # Current Stock by Brand — authoritative movement ledger (entry IN/OUT,
    # non-voided) grouped by material, merged onto the Material master so:
    #   * every material from the master appears (0/0/0 rows for materials
    #     that exist but have no movements yet — the reported "empty table")
    #   * entry materials that are no longer in the master are still shown
    #     explicitly instead of being silently dropped
    # The grouped aggregation stays in SQL (one query); no per-row loading.
    stats_query = db.session.query(
        Entry.material,
        func.sum(case((Entry.type == 'IN', Entry.qty), else_=0)).label('total_in'),
        func.sum(case((Entry.type == 'OUT', Entry.qty), else_=0)).label('total_out')
    ).filter(Entry.is_void == False).group_by(Entry.material).all()

    material_units = {
        (m.name or '').strip().lower(): (m.unit or 'Bags')
        for m in Material.query.with_entities(Material.name, Material.unit).all()
    }
    material_names = sorted(
        {(m.name or '').strip() for m in
         Material.query.with_entities(Material.name).all()}
    )

    entry_map = {}
    for row in stats_query:
        entry_map[(row.material or '').strip()] = {
            'in': int(row.total_in or 0),
            'out': int(row.total_out or 0),
        }

    # Every material from the master, plus any entry-only material
    # (explicitly marked rather than silently lost).
    all_names = material_names + sorted(
        n for n in entry_map.keys() if n and n not in material_names
    )
    stats = sorted([{
        'name': name or 'Unknown/Unbranded',
        'in': entry_map.get(name, {}).get('in', 0),
        'out': entry_map.get(name, {}).get('out', 0),
        'stock': (entry_map.get(name, {}).get('in', 0)
                  - entry_map.get(name, {}).get('out', 0)),
        'unit': material_units.get((name or '').lower(), 'Bags'),
        'has_movement': name in entry_map,
    } for name in all_names], key=lambda x: x['name'])

    total_stock = sum(s['stock'] for s in stats)

    # Daily Cash Calculation
    cash_payments = db.session.query(func.sum(Payment.amount)).filter(func.date(Payment.date_posted) == today_date, Payment.is_void == False).scalar() or 0
    cash_bookings = db.session.query(func.sum(Booking.paid_amount)).filter(func.date(Booking.date_posted) == today_date, Booking.is_void == False).scalar() or 0
    cash_sales = db.session.query(func.sum(DirectSale.paid_amount)).filter(func.date(DirectSale.date_posted) == today_date, DirectSale.is_void == False).scalar() or 0
    daily_cash = cash_payments + cash_bookings + cash_sales

    # Daily Credit Calculation
    credit_bookings = db.session.query(func.sum(Booking.amount - Booking.paid_amount)).filter(func.date(Booking.date_posted) == today_date, Booking.is_void == False).scalar() or 0
    credit_sales = db.session.query(func.sum(DirectSale.amount - DirectSale.paid_amount)).filter(func.date(DirectSale.date_posted) == today_date, DirectSale.is_void == False).scalar() or 0
    daily_credit = credit_bookings + credit_sales

    # Total Outstanding is the same grouped client-ledger projection used by
    # Current Payables and Accounts.  It must not sum derived PendingBill rows.
    payable_report = build_current_payables(status='outstanding', page=1, per_page=200)
    total_outstanding = float(payable_report.get('total_outstanding') or 0)

    # Daily Sales Breakdown
    sales_breakdown = {}

    # 1. Bookings
    booking_total = db.session.query(func.sum(Booking.amount)).filter(func.date(Booking.date_posted) == today_date, Booking.is_void == False).scalar() or 0
    if booking_total > 0:
        sales_breakdown['Bookings'] = booking_total

    # 2. Direct Sales
    ds_query = db.session.query(DirectSale.category, func.sum(DirectSale.amount))\
        .filter(func.date(DirectSale.date_posted) == today_date, DirectSale.is_void == False)\
        .group_by(DirectSale.category).all()

    for cat, amt in ds_query:
        if amt > 0:
            cat_name = normalize_sale_category(cat, default='Credit Customer')
            if cat_name == 'Credit Customer':
                cat_name = 'Credit Sales'
            elif cat_name == 'Cash':
                cat_name = 'Cash Sales'
            sales_breakdown[cat_name] = sales_breakdown.get(cat_name, 0) + amt

    sales_breakdown_list = [{'category': k, 'amount': v} for k, v in sales_breakdown.items()]
    sales_breakdown_list.sort(key=lambda x: x['amount'], reverse=True)

    # ``clients`` / ``materials`` dropdowns are injected globally by the
    # inject_dropdown_data context processor (same data, 20s app-level cache);
    # re-loading both full tables here duplicated two scans per dashboard hit.
    return render_template('index.html',
                           today_date=today,
                           total_stock=int(total_stock),
                           client_count=client_count,
                           stats=stats,
                           daily_cash=daily_cash,
                           daily_credit=daily_credit,
                           total_outstanding=total_outstanding,
                           sales_breakdown=sales_breakdown_list)


