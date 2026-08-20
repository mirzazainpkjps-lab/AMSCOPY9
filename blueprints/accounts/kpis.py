"""kpis — split from accounts.py."""
from ._common import *  # noqa

@accounts_bp.route('/api/kpi/client_payments')
@login_required
def api_client_payments_today():
    """API endpoint for client payments KPI drill-down."""
    today = pk_today()
    
    payments = Payment.query.filter(
        Payment.date_posted >= today,
        Payment.date_posted < today + timedelta(days=1),
        Payment.is_void == False
    ).order_by(Payment.date_posted.desc()).all()
    
    data = [{
        'id': p.id,
        'client_name': p.client_name,
        'amount': p.amount,
        'method': p.method,
        'date_posted': p.date_posted.strftime('%Y-%m-%d %H:%M'),
        'note': p.note
    } for p in payments]
    
    return jsonify(data)


@accounts_bp.route('/api/kpi/supplier_payments')
@login_required
def api_supplier_payments_today():
    """API endpoint for supplier payments KPI drill-down."""
    today = pk_today()
    
    payments = SupplierPayment.query.filter(
        SupplierPayment.date_posted >= today,
        SupplierPayment.date_posted < today + timedelta(days=1),
        SupplierPayment.is_void == False
    ).join(Supplier).order_by(SupplierPayment.date_posted.desc()).all()
    
    data = [{
        'type': 'Supplier Payment',
        'id': p.id,
        'supplier_name': p.supplier.name if p.supplier else '',
        'amount': p.amount,
        'method': p.method,
        'date_posted': p.date_posted.strftime('%Y-%m-%d %H:%M'),
        'note': p.note,
        'account_name': p.account_name
    } for p in payments]

    grns = GRN.query.filter(
        GRN.date_posted >= today,
        GRN.date_posted < today + timedelta(days=1),
        GRN.is_void == False,
        GRN.paid_amount > 0
    ).order_by(GRN.date_posted.desc()).all()

    for g in grns:
        # New GRNs are already represented by their marked SupplierPayment.
        # Keep only legacy GRN-only rows in this fallback list.
        if _grn_has_active_auto_payment(g):
            continue
        data.append({
            'type': 'GRN Purchase Payment',
            'id': g.id,
            'supplier_name': (g.supplier_rel.name if getattr(g, 'supplier_rel', None) else (g.supplier or '')),
            'amount': float(g.paid_amount or 0),
            'method': g.payment_type or '',
            'date_posted': g.date_posted.strftime('%Y-%m-%d %H:%M'),
            'note': g.note,
            'account_name': g.account_name
        })
    
    return jsonify(data)


@accounts_bp.route('/api/kpi/expenditures')
@login_required
def api_expenditures_today():
    """API endpoint for expenditures KPI drill-down."""
    today = pk_today()
    
    expenditures = FbmCashDrawerEntry.query.filter(
        FbmCashDrawerEntry.date_posted >= today,
        FbmCashDrawerEntry.date_posted < today + timedelta(days=1),
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False
    ).order_by(FbmCashDrawerEntry.date_posted.desc()).all()
    
    data = [{
        'id': e.id,
        'amount': e.amount,
        'category': e.category or 'Other',
        'method': e.method,
        'date_posted': e.date_posted.strftime('%Y-%m-%d %H:%M'),
        'note': e.note
    } for e in expenditures]

    tx_receipts = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= today,
        AccountTransaction.date_posted < today + timedelta(days=1),
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment']),
        AccountTransaction.transaction_type != 'Supplier Payment',
        AccountTransaction.to_account_id.isnot(None)
    ).all()
    
    for tx in tx_receipts:
        acc = Account.query.get(tx.to_account_id)
        data.append({
            'id': f"tx_{tx.id}",
            'client_name': tx.description or 'Other Receipt',
            'amount': float(tx.amount or 0),
            'method': (acc.category.capitalize() if acc else 'Other'),
            'date_posted': tx.date_posted.strftime('%Y-%m-%d %H:%M'),
            'note': tx.note or ''
        })
    
    return jsonify(data)


@accounts_bp.route('/api/kpi/receipts')
@login_required
def api_receipts_today():
    """API endpoint for receipts KPI drill-down."""
    today = pk_today()
    
    # Direct sales receipts (paid amounts only)
    sales = DirectSale.query.filter(
        DirectSale.date_posted >= today,
        DirectSale.date_posted < today + timedelta(days=1),
        DirectSale.is_void == False,
        DirectSale.paid_amount > 0
    ).order_by(DirectSale.date_posted.desc()).all()
    
    sales_data = [{
        'type': 'Direct Sale Receipt',
        'client_name': s.client_name,
        'amount': float(s.paid_amount or 0),
        'date_posted': s.date_posted.strftime('%Y-%m-%d %H:%M'),
        'note': s.note
    } for s in sales]
    
    # Booking receipts (paid amounts only)
    bookings = Booking.query.filter(
        Booking.date_posted >= today,
        Booking.date_posted < today + timedelta(days=1),
        Booking.is_void == False,
        Booking.paid_amount > 0
    ).order_by(Booking.date_posted.desc()).all()

    booking_data = [{
        'type': 'Booking Receipt',
        'client_name': b.client_name,
        'amount': float(b.paid_amount or 0),
        'date_posted': b.date_posted.strftime('%Y-%m-%d %H:%M'),
        'note': getattr(b, 'note', None)
    } for b in bookings]

    # Ledger receipts
    tx_receipts = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= today,
        AccountTransaction.date_posted < today + timedelta(days=1),
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type == 'Receipt',
        AccountTransaction.to_account_id.isnot(None),
        or_(
            AccountTransaction.note.is_(None),
            ~AccountTransaction.note.ilike('%[SRC:%')
        )
    ).all()
    
    tx_data = [{
        'type': 'Ledger Receipt',
        'client_name': tx.description or 'Other Receipt',
        'amount': float(tx.amount or 0),
        'date_posted': tx.date_posted.strftime('%Y-%m-%d %H:%M'),
        'note': tx.note or ''
    } for tx in tx_receipts]
    
    # Note: client_payments_today (Payment table) drilldown is handled by /api/kpi/client_payments
    return jsonify(booking_data + sales_data + tx_data)


@accounts_bp.route('/api/kpi/company_money')
@login_required
def api_company_money():
    accounts = _company_accounts()
    data = [{
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'account_type': a.account_type,
        'balance': float(a.balance or 0)
    } for a in accounts]
    return jsonify(data)


@accounts_bp.route('/kpi/client_payments')
@login_required
def kpi_client_payments():
    """KPI drill-down page: payments received from clients."""
    date_from, date_to_excl = _parse_date_range(default_days=0)
    search = (request.args.get('q') or '').strip()
    method_f = (request.args.get('method') or '').strip()

    q = Payment.query.filter(
        Payment.is_void == False,
        Payment.date_posted >= date_from,
        Payment.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        q = q.filter(or_(Payment.client_name.ilike(like), Payment.note.ilike(like), Payment.account_name.ilike(like)))
    if method_f:
        q = q.filter(func.lower(Payment.method) == method_f.lower())

    payments = q.order_by(Payment.date_posted.desc(), Payment.id.desc()).all()

    tx_receipts = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.transaction_type == 'Receipt',
        or_(
            AccountTransaction.note.is_(None),
            ~AccountTransaction.note.ilike('%[SRC:%')
        )
    ).all()

    items = []
    for p in payments:
        client_obj = _resolve_client(p.client_name or '')
        items.append({
            'date_posted': p.date_posted,
            'party': p.client_name or '',
            'amount': float(p.amount or 0),
            'method': p.method or '',
            'account_name': p.account_name or '',
            'bill_no': p.manual_bill_no or p.auto_bill_no or '',
            'note': p.note or '',
            'link': (url_for('client_ledger', id=client_obj.id) if client_obj else None),
        })

    for tx in tx_receipts:
        items.append({
            'date_posted': tx.date_posted,
            'party': tx.description or 'Ledger Receipt',
            'amount': float(tx.amount or 0),
            'method': 'Other',
            'account_name': '',
            'bill_no': '',
            'note': tx.note or '',
        })

    total_amount = float(sum(float(i['amount'] or 0) for i in items))

    return render_template(
        'accounts/kpi_client_payments.html',
        items=items,
        total_amount=total_amount,
        total_count=len(items),
        date_from=date_from,
        date_to=date_to_excl - timedelta(days=1),
        search=search,
        method_f=method_f
    )


@accounts_bp.route('/kpi/supplier_payments')
@login_required
def kpi_supplier_payments():
    """KPI drill-down page: payments made to suppliers (SupplierPayment + GRN paid_amount)."""
    date_from, date_to_excl = _parse_date_range(default_days=0)
    search = (request.args.get('q') or '').strip()
    method_f = (request.args.get('method') or '').strip()

    sp_q = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        SupplierPayment.date_posted >= date_from,
        SupplierPayment.date_posted < date_to_excl
    ).join(Supplier)

    if search:
        like = f'%{search}%'
        sp_q = sp_q.filter(or_(Supplier.name.ilike(like), SupplierPayment.note.ilike(like), SupplierPayment.account_name.ilike(like)))
    if method_f:
        sp_q = sp_q.filter(func.lower(SupplierPayment.method) == method_f.lower())

    supplier_payments = sp_q.order_by(SupplierPayment.date_posted.desc(), SupplierPayment.id.desc()).all()

    grn_q = GRN.query.filter(
        GRN.date_posted >= date_from,
        GRN.date_posted < date_to_excl,
        GRN.is_void == False,
        GRN.paid_amount > 0
    )
    if search:
        like = f'%{search}%'
        grn_q = grn_q.filter(or_(func.coalesce(GRN.supplier, '').ilike(like), func.coalesce(GRN.note, '').ilike(like), func.coalesce(GRN.account_name, '').ilike(like)))
    if method_f:
        grn_q = grn_q.filter(func.lower(func.coalesce(GRN.payment_type, '')) == method_f.lower())

    grns = grn_q.order_by(GRN.date_posted.desc(), GRN.id.desc()).all()

    items = []
    total_amount = 0.0

    for p in supplier_payments:
        supplier_obj = getattr(p, 'supplier', None)
        supplier_name = supplier_obj.name if supplier_obj else ''
        items.append({
            'date_posted': p.date_posted,
            'source': 'Supplier Payment',
            'party': supplier_name,
            'amount': float(p.amount or 0),
            'method': p.method or '',
            'account_name': p.account_name or '',
            'bill_no': p.manual_bill_no or p.auto_bill_no or '',
            'note': p.note or '',
            'link': (url_for('supplier_ledger', id=supplier_obj.id) if supplier_obj else None),
        })
        total_amount += float(p.amount or 0)

    for g in grns:
        # New GRNs already have an active SupplierPayment row.  Showing both
        # source representations makes this KPI disagree with the ledger.
        if _grn_has_active_auto_payment(g):
            continue
        supplier_obj = getattr(g, 'supplier_rel', None)
        supplier_name = (supplier_obj.name if supplier_obj else (g.supplier or ''))
        items.append({
            'date_posted': g.date_posted,
            'source': 'GRN Purchase Payment',
            'party': supplier_name,
            'amount': float(g.paid_amount or 0),
            'method': g.payment_type or '',
            'account_name': g.account_name or '',
            'bill_no': g.manual_bill_no or g.auto_bill_no or '',
            'note': g.note or '',
            'link': (url_for('supplier_ledger', id=supplier_obj.id) if supplier_obj else None),
        })
        total_amount += float(g.paid_amount or 0)

    items.sort(key=lambda x: (x.get('date_posted') or pk_now()), reverse=True)

    return render_template(
        'accounts/kpi_supplier_payments.html',
        items=items,
        total_amount=total_amount,
        total_count=len(items),
        date_from=date_from,
        date_to=date_to_excl - timedelta(days=1),
        search=search,
        method_f=method_f
    )


@accounts_bp.route('/kpi/expenditures')
@login_required
def kpi_expenditures():
    """KPI drill-down page: expenditures (cash drawer out entries)."""
    date_from, date_to_excl = _parse_date_range(default_days=0)
    search = (request.args.get('q') or '').strip()
    method_f = (request.args.get('method') or '').strip()

    q = FbmCashDrawerEntry.query.filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.date_posted >= date_from,
        FbmCashDrawerEntry.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        q = q.filter(or_(func.coalesce(FbmCashDrawerEntry.category, '').ilike(like), func.coalesce(FbmCashDrawerEntry.note, '').ilike(like)))
    if method_f:
        q = q.filter(func.lower(FbmCashDrawerEntry.method) == method_f.lower())

    rows = q.order_by(FbmCashDrawerEntry.date_posted.desc(), FbmCashDrawerEntry.id.desc()).all()
    items = [{
        'date_posted': r.date_posted,
        'category': r.category or '',
        'amount': float(r.amount or 0),
        'method': r.method or '',
        'note': r.note or '',
    } for r in rows]

    tx_rows = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment']),
        AccountTransaction.transaction_type != 'Supplier Payment',
        AccountTransaction.from_account_id.isnot(None)
    ).all()

    if search:
        like = f'%{search}%'
        tx_rows = [r for r in tx_rows if like.lower() in (r.description or '').lower() or like.lower() in (r.note or '').lower()]

    for tx in tx_rows:
        acc = Account.query.get(tx.from_account_id)
        method = (acc.category.capitalize() if acc else 'Other')
        if method_f and method.lower() != method_f.lower():
            continue
        items.append({
            'date_posted': tx.date_posted,
            'category': ('Driver / Delivery Services' if tx.transaction_type == 'Driver Payment'
                         else (tx.description or 'Expense')),
            'amount': float(tx.amount or 0),
            'method': method,
            'note': tx.note or '',
        })

    items.sort(key=lambda x: (x.get('date_posted') or pk_now()), reverse=True)
    total_amount = float(sum(float(i['amount'] or 0) for i in items))

    return render_template(
        'accounts/kpi_expenditures.html',
        items=items,
        total_amount=total_amount,
        total_count=len(items),
        date_from=date_from,
        date_to=date_to_excl - timedelta(days=1),
        search=search,
        method_f=method_f
    )


@accounts_bp.route('/kpi/receipts')
@login_required
def kpi_receipts():
    """KPI drill-down page: receipts (Booking + DirectSale paid amounts)."""
    date_from, date_to_excl = _parse_date_range(default_days=0)
    search = (request.args.get('q') or '').strip()

    bookings_q = Booking.query.filter(
        Booking.date_posted >= date_from,
        Booking.date_posted < date_to_excl,
        Booking.is_void == False,
        Booking.paid_amount > 0
    )
    sales_q = DirectSale.query.filter(
        DirectSale.date_posted >= date_from,
        DirectSale.date_posted < date_to_excl,
        DirectSale.is_void == False,
        DirectSale.paid_amount > 0
    )
    if search:
        like = f'%{search}%'
        bookings_q = bookings_q.filter(or_(func.coalesce(Booking.client_name, '').ilike(like), func.coalesce(Booking.manual_bill_no, '').ilike(like), func.coalesce(Booking.auto_bill_no, '').ilike(like)))
        sales_q = sales_q.filter(or_(func.coalesce(DirectSale.client_name, '').ilike(like), func.coalesce(DirectSale.manual_bill_no, '').ilike(like), func.coalesce(DirectSale.auto_bill_no, '').ilike(like)))

    tx_receipts = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type == 'Receipt',
        AccountTransaction.to_account_id.isnot(None),
        or_(
            AccountTransaction.note.is_(None),
            ~AccountTransaction.note.ilike('%[SRC:%')
        )
    ).all()

    bookings = bookings_q.order_by(Booking.date_posted.desc(), Booking.id.desc()).all()
    sales = sales_q.order_by(DirectSale.date_posted.desc(), DirectSale.id.desc()).all()

    items = []
    total_amount = 0.0

    for b in bookings:
        items.append({
            'date_posted': b.date_posted,
            'source': 'Booking Receipt',
            'party': b.client_name or '',
            'amount': float(b.paid_amount or 0),
            'bill_no': getattr(b, 'manual_bill_no', None) or getattr(b, 'auto_bill_no', None) or '',
            'note': getattr(b, 'note', None) or '',
        })
        total_amount += float(b.paid_amount or 0)

    for s in sales:
        items.append({
            'date_posted': s.date_posted,
            'source': 'Direct Sale Receipt',
            'party': s.client_name or '',
            'amount': float(s.paid_amount or 0),
            'bill_no': s.manual_bill_no or s.auto_bill_no or '',
            'note': s.note or '',
        })
        total_amount += float(s.paid_amount or 0)

    for tx in tx_receipts:
        items.append({
            'date_posted': tx.date_posted,
            'source': 'Ledger Receipt',
            'party': tx.description or 'Other Receipt',
            'amount': float(tx.amount or 0),
            'bill_no': '',
            'note': tx.note or '',
        })
        total_amount += float(tx.amount or 0)

    items.sort(key=lambda x: (x.get('date_posted') or pk_now()), reverse=True)

    return render_template(
        'accounts/kpi_receipts.html',
        items=items,
        total_amount=total_amount,
        total_count=len(items),
        date_from=date_from,
        date_to=date_to_excl - timedelta(days=1),
        search=search
    )


@accounts_bp.route('/kpi/company_money')
@login_required
def kpi_company_money():
    """KPI drill-down page: company money available (active company accounts)."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    accounts = _company_accounts()
    total_money = float(sum(float(a.balance or 0) for a in accounts))
    items = [{
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'account_type': a.account_type,
        'balance': float(a.balance or 0),
        'link': url_for('accounts.account_ledger', account_id=a.id)
    } for a in accounts]

    items.sort(key=lambda x: x.get('name') or '')

    return render_template('accounts/kpi_company_money.html', items=items, total_money=total_money, total_count=len(items))


@accounts_bp.route('/kpi/cash_money')
@login_required
def kpi_cash_money():
    """KPI drill-down page: total cash + total bank (2-step drill-down)."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    cash_accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.category)) == 'cash'
    ).order_by(Account.name.asc()).all()
    
    bank_accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.category)) == 'bank'
    ).order_by(Account.name.asc()).all()
    
    total_cash = float(sum(float(a.balance or 0) for a in cash_accounts))
    total_bank = float(sum(float(a.balance or 0) for a in bank_accounts))

    return render_template(
        'accounts/kpi_cash_money.html',
        total_cash=total_cash,
        total_bank=total_bank,
        cash_count=len(cash_accounts),
        bank_count=len(bank_accounts),
    )


@accounts_bp.route('/kpi/cash_accounts')
@login_required
def kpi_cash_accounts():
    """KPI drill-down page: list all cash accounts and their balances."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    cash_accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.category)) == 'cash'
    ).order_by(Account.name.asc()).all()
    total_cash = float(sum(float(a.balance or 0) for a in cash_accounts))
    items = [{
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'account_type': a.account_type,
        'balance': float(a.balance or 0),
        'link': url_for('accounts.account_ledger', account_id=a.id)
    } for a in cash_accounts]
    return render_template('accounts/kpi_cash_accounts.html', items=items, total_amount=total_cash, total_count=len(items))


@accounts_bp.route('/kpi/bank_accounts')
@login_required
def kpi_bank_accounts():
    """KPI drill-down page: list all bank accounts and their balances."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    bank_accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.category)) == 'bank'
    ).order_by(Account.name.asc()).all()
    total_bank = float(sum(float(a.balance or 0) for a in bank_accounts))
    items = [{
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'account_type': a.account_type,
        'balance': float(a.balance or 0),
        'link': url_for('accounts.account_ledger', account_id=a.id)
    } for a in bank_accounts]
    return render_template('accounts/kpi_bank_accounts.html', items=items, total_amount=total_bank, total_count=len(items))


