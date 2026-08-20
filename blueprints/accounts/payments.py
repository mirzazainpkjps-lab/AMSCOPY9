"""payments — split from accounts.py."""
from ._common import *  # noqa

def _payment_page_size():
    return min(max(request.args.get('per_page', 50, type=int) or 50, 10), 100)


def _payment_filter_state():
    return {
        'q': (request.args.get('q') or '').strip(),
        'method': (request.args.get('method') or '').strip(),
        'show': (request.args.get('show') or 'active').strip().lower(),
        'party_id': request.args.get('party_id', type=int),
        'account_id': request.args.get('account_id', type=int),
        'per_page': _payment_page_size(),
    }


@accounts_bp.route('/payments/clients')
@login_required
def client_payments():
    """Paginated, intersecting filters and full CRUD for client payments."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    state = _payment_filter_state()
    show_mode = state['show'] if state['show'] in ('active', 'voided', 'all') else 'active'
    date_from, date_to_excl = _parse_date_range()

    q = Payment.query.filter(Payment.date_posted >= date_from, Payment.date_posted < date_to_excl)
    if show_mode == 'active':
        q = q.filter(Payment.is_void == False)
    elif show_mode == 'voided':
        q = q.filter(Payment.is_void == True)
    if state['q']:
        like = f"%{state['q']}%"
        q = q.filter(or_(
            Payment.client_name.ilike(like), Payment.note.ilike(like),
            Payment.account_name.ilike(like), Payment.bank_name.ilike(like),
            Payment.account_no.ilike(like), Payment.manual_bill_no.ilike(like),
            Payment.auto_bill_no.ilike(like), Payment.method.ilike(like),
        ))
    if state['method']:
        method_norm = state['method'].lower()
        method_values = ['bank', 'bank transfer'] if method_norm == 'bank' else (['check', 'cheque'] if method_norm == 'check' else [method_norm])
        q = q.filter(func.lower(Payment.method).in_(method_values))
    if state['party_id']:
        selected_client = db.session.get(Client, state['party_id'])
        if selected_client:
            q = q.filter(or_(Payment.client_id == selected_client.id,
                             and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == selected_client.name.strip().lower())))
        else:
            q = q.filter(False)
    if state['account_id']:
        q = q.filter(Payment.payment_account_id == state['account_id'])

    total_amount = q.with_entities(func.coalesce(func.sum(Payment.amount), 0)).scalar() or 0
    total_count = q.count()
    ordered_q = q.order_by(Payment.date_posted.desc(), Payment.id.desc())
    payments = ordered_q.paginate(page=page, per_page=state['per_page'], error_out=False)
    if payments.pages and page > payments.pages:
        payments = ordered_q.paginate(page=payments.pages, per_page=state['per_page'], error_out=False)
    accounts = _active_accounts().filter(func.lower(func.trim(Account.category)).in_(['cash', 'bank'])).order_by(Account.name.asc(), Account.id.asc()).all()
    clients = _active_clients()
    all_clients = Client.query.order_by(Client.name.asc(), Client.id.asc()).all()
    all_accounts = Account.query.order_by(Account.name.asc(), Account.id.asc()).all()
    client_options = [{'code': c.code, 'name': c.name} for c in clients]
    account_options = [{'id': a.id, 'label': _account_option_label(a)} for a in accounts]
    payment_meta = {}
    from app.services.payments_crud import _client_payment_kind, _client_payment_source
    for p in payments.items:
        source_type, source_id = _client_payment_source(p)
        payment_meta[p.id] = {'kind': _client_payment_kind(p), 'source_type': source_type, 'source_id': source_id}
    payments_readonly = not _payments_permission_ok()

    return render_template(
        'accounts/client_payments.html', payments=payments,
        date_from=date_from, date_to=date_to_excl - timedelta(days=1),
        search=state['q'], method_f=state['method'], show_mode=show_mode,
        party_id_f=state['party_id'], account_id_f=state['account_id'], per_page=state['per_page'],
        total_amount=total_amount, total_count=total_count,
        payments_readonly=payments_readonly, accounts=accounts, clients=clients,
        all_clients=all_clients, all_accounts=all_accounts,
        client_options=client_options, account_options=account_options,
        filter_party_options=[{'id': c.id, 'label': f"{c.name} ({c.code})" + ('' if c.is_active else ' [Inactive]')} for c in all_clients],
        filter_account_options=[{'id': a.id, 'label': _account_option_label(a) + ('' if a.is_active else ' [Inactive]')} for a in all_accounts],
        filter_party_name=(next((f"{c.name} ({c.code})" for c in all_clients if c.id == state['party_id']), '')),
        filter_account_name=(next((_account_option_label(a) for a in all_accounts if a.id == state['account_id']), '')),
        payment_meta=payment_meta,
    )


@accounts_bp.route('/payments/suppliers')
@login_required
def supplier_payments():
    """Paginated, intersecting filters and full CRUD for supplier payments."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    state = _payment_filter_state()
    show_mode = state['show'] if state['show'] in ('active', 'voided', 'all') else 'active'
    date_from, date_to_excl = _parse_date_range()

    q = SupplierPayment.query.filter(SupplierPayment.date_posted >= date_from, SupplierPayment.date_posted < date_to_excl)
    if show_mode == 'active':
        q = q.filter(SupplierPayment.is_void == False)
    elif show_mode == 'voided':
        q = q.filter(SupplierPayment.is_void == True)
    if state['q']:
        like = f"%{state['q']}%"
        q = q.outerjoin(Supplier).filter(or_(
            Supplier.name.ilike(like), SupplierPayment.note.ilike(like),
            SupplierPayment.account_name.ilike(like), SupplierPayment.bank_name.ilike(like),
            SupplierPayment.account_no.ilike(like), SupplierPayment.manual_bill_no.ilike(like),
            SupplierPayment.auto_bill_no.ilike(like), SupplierPayment.method.ilike(like),
        ))
    if state['method']:
        method_norm = state['method'].lower()
        method_values = ['bank', 'bank transfer'] if method_norm == 'bank' else (['check', 'cheque'] if method_norm == 'check' else [method_norm])
        q = q.filter(func.lower(SupplierPayment.method).in_(method_values))
    if state['party_id']:
        q = q.filter(SupplierPayment.supplier_id == state['party_id'])
    if state['account_id']:
        q = q.filter(SupplierPayment.payment_account_id == state['account_id'])

    total_amount = q.with_entities(func.coalesce(func.sum(SupplierPayment.amount), 0)).scalar() or 0
    total_count = q.count()
    ordered_q = q.order_by(SupplierPayment.date_posted.desc(), SupplierPayment.id.desc())
    payments = ordered_q.paginate(page=page, per_page=state['per_page'], error_out=False)
    if payments.pages and page > payments.pages:
        payments = ordered_q.paginate(page=payments.pages, per_page=state['per_page'], error_out=False)
    suppliers = _active_suppliers()
    accounts = _active_accounts().filter(func.lower(func.trim(Account.category)).in_(['cash', 'bank'])).order_by(Account.name.asc(), Account.id.asc()).all()
    all_suppliers = Supplier.query.order_by(Supplier.name.asc(), Supplier.id.asc()).all()
    all_accounts = Account.query.order_by(Account.name.asc(), Account.id.asc()).all()
    supplier_options = [{'id': s.id, 'name': s.name} for s in suppliers]
    account_options = [{'id': a.id, 'label': _account_option_label(a)} for a in accounts]
    payment_meta = {}
    from app.services.payments_crud import _supplier_payment_source
    for p in payments.items:
        source_type, source_id = _supplier_payment_source(p)
        payment_meta[p.id] = {'source_type': source_type, 'source_id': source_id}
    payments_readonly = not _payments_permission_ok()

    return render_template(
        'accounts/supplier_payments.html', payments=payments,
        date_from=date_from, date_to=date_to_excl - timedelta(days=1),
        search=state['q'], method_f=state['method'], show_mode=show_mode,
        party_id_f=state['party_id'], account_id_f=state['account_id'], per_page=state['per_page'],
        total_amount=total_amount, total_count=total_count,
        suppliers=suppliers, accounts=accounts,
        all_suppliers=all_suppliers, all_accounts=all_accounts,
        supplier_options=supplier_options, account_options=account_options,
        filter_party_options=[{'id': s.id, 'label': s.name + ('' if s.is_active else ' [Inactive]')} for s in all_suppliers],
        filter_account_options=[{'id': a.id, 'label': _account_option_label(a) + ('' if a.is_active else ' [Inactive]')} for a in all_accounts],
        filter_party_name=(next((s.name for s in all_suppliers if s.id == state['party_id']), '')),
        filter_account_name=(next((_account_option_label(a) for a in all_accounts if a.id == state['account_id']), '')),
        payments_readonly=payments_readonly, payment_meta=payment_meta,
    )


@accounts_bp.route('/payments/clients/void/<int:id>', methods=['POST'])
@login_required
def client_payment_void(id):
    if not (current_user.role in ('admin', 'root') or getattr(current_user, 'can_manage_payments', False)):
        flash('Permission denied', 'danger')
        return redirect(request.referrer or url_for('accounts.client_payments'))

    payment = Payment.query.get_or_404(id)
    try:
        from app.services.payments_crud import delete_client_payment
        if delete_client_payment(payment, actor=current_user):
            db.session.commit()
            flash('Payment deleted. Balances reversed and recalculated.', 'success')
        else:
            flash('Payment already deleted.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Client payment delete failed')
        flash(f'Unable to delete payment: {exc}', 'danger')

    return redirect(request.referrer or url_for('accounts.client_payments'))


@accounts_bp.route('/payments/clients/restore/<int:id>', methods=['POST'])
@login_required
def client_payment_restore(id):
    if not (current_user.role in ('admin', 'root') or getattr(current_user, 'can_manage_payments', False)):
        flash('Permission denied', 'danger')
        return redirect(request.referrer or url_for('accounts.client_payments'))

    payment = Payment.query.get_or_404(id)
    try:
        from app.services.payments_crud import restore_client_payment
        if restore_client_payment(payment, actor=current_user):
            db.session.commit()
            flash('Payment restored. Balances re-applied.', 'success')
        else:
            flash('Payment is already active.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Client payment restore failed')
        flash(f'Unable to restore payment: {exc}', 'danger')

    return redirect(request.referrer or url_for('accounts.client_payments'))


@accounts_bp.route('/expenditures')
@login_required
def expenditures():
    """View and manage personal expenditures."""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    search = (request.args.get('q') or '').strip()
    category_f = (request.args.get('category') or '').strip() # Filter for category/description
    method_f = (request.args.get('method') or '').strip() # Filter for payment method/account category
    date_from, date_to_excl = _parse_date_range()

    # Fetch FbmCashDrawerEntry expenditures
    fbm_q = FbmCashDrawerEntry.query.filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.date_posted >= date_from,
        FbmCashDrawerEntry.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        fbm_q = fbm_q.filter(or_(FbmCashDrawerEntry.note.ilike(like), FbmCashDrawerEntry.category.ilike(like)))
    if category_f:
        fbm_q = fbm_q.filter(FbmCashDrawerEntry.category == category_f)
    if method_f:
        fbm_q = fbm_q.filter(func.lower(FbmCashDrawerEntry.method) == method_f.lower())
    fbm_expenditures = fbm_q.all()

    # Fetch AccountTransaction expenditures (Expenses and Payments)
    tx_q = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment']),
        AccountTransaction.from_account_id.isnot(None)
    )
    if search:
        like = f'%{search}%'
        tx_q = tx_q.filter(or_(AccountTransaction.description.ilike(like), AccountTransaction.note.ilike(like)))
    if category_f:
        tx_q = tx_q.filter(AccountTransaction.description == category_f)
    if method_f:
        # Filter AccountTransactions by the category of their 'from' account
        tx_q = tx_q.join(Account, AccountTransaction.from_account_id == Account.id).filter(func.lower(Account.category) == method_f.lower())
    tx_expenditures = tx_q.all()

    all_expenditures = []
    for e in fbm_expenditures:
        all_expenditures.append({
            'date_posted': e.date_posted,
            'category': e.category or '',
            'amount': float(e.amount or 0),
            'method': e.method or '',
            'note': e.note or '',
            'type': 'FBM Cash Drawer'
        })
    for tx in tx_expenditures:
        acc = Account.query.get(tx.from_account_id)
        all_expenditures.append({
            'date_posted': tx.date_posted,
            'category': ('Driver / Delivery Services' if tx.transaction_type == 'Driver Payment'
                         else (tx.description or 'Expense')),
            'amount': float(tx.amount or 0),
            'method': (acc.category.capitalize() if acc else 'Other'), # Use account category as method
            'note': tx.note or '',
            'type': 'Account Transaction'
        })

    all_expenditures.sort(key=lambda x: x['date_posted'], reverse=True)

    total_amount = sum(item['amount'] for item in all_expenditures)
    total_count = len(all_expenditures)

    # Manual pagination
    start = (page - 1) * per_page
    end = start + per_page
    paginated_expenditures = all_expenditures[start:end]
    
    # Wrap in a SimpleNamespace to mimic a Pagination object for the template
    pagination_wrapper = SimpleNamespace(
        items=paginated_expenditures,
        page=page,
        per_page=per_page,
        total=total_count,
        pages=(total_count + per_page - 1) // per_page,
        has_prev=(page > 1),
        has_next=(end < total_count),
        prev_num=page - 1,
        next_num=page + 1
    )

    # Collect categories for filter dropdown from both sources
    all_categories = set()
    for r in db.session.query(FbmCashDrawerEntry.category).filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.category.isnot(None),
        FbmCashDrawerEntry.category != ''
    ).distinct(FbmCashDrawerEntry.category).all():
        all_categories.add(r[0])
    for r in db.session.query(AccountTransaction.description).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment']),
        AccountTransaction.description.isnot(None),
        AccountTransaction.description != ''
    ).distinct(AccountTransaction.description).all():
        all_categories.add(r[0])
    categories = sorted(list(all_categories))

    # Collect methods for filter dropdown from both sources
    all_methods = set()
    for r in db.session.query(FbmCashDrawerEntry.method).filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.method.isnot(None),
        FbmCashDrawerEntry.method != ''
    ).distinct(FbmCashDrawerEntry.method).all():
        all_methods.add(r[0])
    for r in db.session.query(Account.category).filter(
        Account.is_active == True,
        Account.category.isnot(None),
        Account.category != ''
    ).distinct(Account.category).all():
        all_methods.add(r[0].capitalize()) # Capitalize to match 'Cash', 'Bank' etc.
    methods = sorted(list(all_methods))

    return render_template('accounts/expenditures.html', expenditures=pagination_wrapper,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1),
                           search=search, category_f=category_f, method_f=method_f,
                           categories=categories, methods=methods,
                           total_amount=total_amount, total_count=total_count,
                           page=page, per_page=per_page,
                           has_prev=(page > 1), has_next=(end < total_count))


@accounts_bp.route('/receipts')
@login_required
def receipts():
    """View receipts from sales and GRN paid amounts within a date range."""
    date_from, date_to_excl = _parse_date_range(default_days=0)

    sales = DirectSale.query.filter(
        DirectSale.date_posted >= date_from,
        DirectSale.date_posted < date_to_excl,
        DirectSale.is_void == False
    ).order_by(DirectSale.date_posted.desc()).all()

    grns = GRN.query.filter(
        GRN.date_posted >= date_from,
        GRN.date_posted < date_to_excl,
        GRN.is_void == False,
        GRN.paid_amount > 0
    ).order_by(GRN.date_posted.desc()).all()

    return render_template('accounts/receipts.html', sales=sales, grns=grns,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1))




def _payments_permission_ok():
    return current_user.role in ('admin', 'root') or getattr(current_user, 'can_manage_payments', False)


@accounts_bp.route('/payments/clients/save', methods=['POST'])
@login_required
def client_payment_save():
    """Create or update a client payment (single shared form for Create + Edit)."""
    if not _payments_permission_ok():
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.client_payments'))
    show_mode = (request.form.get('show') or 'active').strip().lower()
    payment_id = request.form.get('payment_id', type=int)
    try:
        from app.services.files_pdf import save_photo
        from app.services.payments_crud import save_client_payment
        uploaded_photo = save_photo(request.files.get('photo'))
        payment, created = save_client_payment(
            payment_id=payment_id,
            client_name=request.form.get('client_name', ''),
            client_code=request.form.get('client_code', ''),
            amount=request.form.get('amount', 0),
            discount=request.form.get('discount', 0),
            discount_reason=request.form.get('discount_reason', ''),
            payment_type=request.form.get('payment_type', 'Receipt'),
            method=request.form.get('method', 'Cash'),
            payment_account_id=request.form.get('payment_account_id'),
            manual_bill_no=request.form.get('manual_bill_no', ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', ''),
            photo_path=uploaded_photo,
            photo_url=request.form.get('photo_url', ''),
            idempotency_key=request.form.get('idempotency_key'),
            expected_revision=request.form.get('revision'),
            actor=current_user,
        )
        replay = bool(getattr(payment, '_idempotent_replay', False))
        db.session.commit()
        if replay:
            flash('This payment submission was already processed; no duplicate was created.', 'info')
        else:
            flash('Payment saved successfully.' if created else 'Payment updated. All balances recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Client payment save failed')
        flash(f'Unable to save payment: {exc}', 'danger')
    return redirect(url_for('accounts.client_payments', show=show_mode))


@accounts_bp.route('/payments/clients/<int:id>/data')
@login_required
def client_payment_data(id):
    """JSON payload that populates the shared create/edit form for an existing payment."""
    p = Payment.query.get_or_404(id)
    c = db.session.get(Client, p.client_id) if getattr(p, 'client_id', None) else None
    if c is None:
        try:
            from app.services.lookups import get_client_by_input
            c = get_client_by_input(p.client_name or '')
        except Exception:
            c = None
    from app.services.payments_crud import _client_payment_kind, _client_payment_source
    source_type, source_id = _client_payment_source(p)
    payment_kind = _client_payment_kind(p)
    account_label = _account_option_label(p.payment_account) if p.payment_account else (p.account_name or '')
    return jsonify({
        'id': p.id,
        'revision': getattr(p, 'revision', None) or 1,
        'client_name': p.client_name or '',
        'client_code': (c.code if c else ''),
        'amount': round(abs(float(p.amount or 0)), 2),
        'discount': round(float(p.discount or 0), 2),
        'discount_reason': p.discount_reason or '',
        'payment_type': payment_kind,
        'method': ('Cash' if (p.method or '').strip().lower() == 'refund' else (p.method or 'Cash')),
        'payment_account_id': p.payment_account_id,
        'bank_name': p.bank_name or '',
        'account_name': p.account_name or '',
        'account_label': account_label,
        'account_no': p.account_no or '',
        'manual_bill_no': p.manual_bill_no or '',
        'auto_bill_no': p.auto_bill_no or '',
        'date': p.date_posted.strftime('%Y-%m-%d') if p.date_posted else '',
        'note': p.note or '',
        'photo_url': p.photo_url or '',
        'photo_path': p.photo_path or '',
        'is_void': bool(p.is_void),
        'source_type': source_type,
        'source_id': source_id,
    })


@accounts_bp.route('/payments/suppliers/save', methods=['POST'])
@login_required
def supplier_payment_save():
    """Create or update a supplier payment (single shared form for Create + Edit)."""
    if not _payments_permission_ok():
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    show_mode = (request.form.get('show') or 'active').strip().lower()
    payment_id = request.form.get('payment_id', type=int)
    try:
        from app.services.payments_crud import save_supplier_payment
        payment, created = save_supplier_payment(
            payment_id=payment_id,
            supplier_id=request.form.get('supplier_id'),
            amount=request.form.get('amount', 0),
            method=request.form.get('method', 'Cash'),
            payment_account_id=request.form.get('payment_account_id'),
            bank_name=request.form.get('bank_name', ''),
            account_name=request.form.get('account_name', ''),
            account_no=request.form.get('account_no', ''),
            manual_bill_no=request.form.get('manual_bill_no', ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', ''),
            idempotency_key=request.form.get('idempotency_key'),
            expected_revision=request.form.get('revision'),
            actor=current_user,
        )
        replay = bool(getattr(payment, '_idempotent_replay', False))
        db.session.commit()
        if replay:
            flash('This supplier-payment submission was already processed; no duplicate was created.', 'info')
        else:
            flash('Supplier payment saved successfully.' if created else 'Supplier payment updated. All balances recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Supplier payment save failed')
        flash(f'Unable to save supplier payment: {exc}', 'danger')
    return redirect(url_for('accounts.supplier_payments', show=show_mode))


@accounts_bp.route('/payments/suppliers/<int:id>/data')
@login_required
def supplier_payment_data(id):
    """JSON payload that populates the shared create/edit form for a supplier payment."""
    p = SupplierPayment.query.get_or_404(id)
    from app.services.payments_crud import _supplier_payment_source
    source_type, source_id = _supplier_payment_source(p)
    account_label = _account_option_label(p.payment_account) if p.payment_account else (p.account_name or '')
    return jsonify({
        'id': p.id,
        'revision': getattr(p, 'revision', None) or 1,
        'supplier_id': p.supplier_id,
        'supplier_name': (p.supplier.name if p.supplier else ''),
        'amount': round(float(p.amount or 0), 2),
        'method': p.method or 'Cash',
        'payment_account_id': p.payment_account_id,
        'bank_name': p.bank_name or '',
        'account_name': p.account_name or '',
        'account_label': account_label,
        'account_no': p.account_no or '',
        'manual_bill_no': p.manual_bill_no or '',
        'auto_bill_no': p.auto_bill_no or '',
        'date': p.date_posted.strftime('%Y-%m-%d') if p.date_posted else '',
        'note': p.note or '',
        'is_void': bool(p.is_void),
        'source_type': source_type,
        'source_id': source_id,
    })


@accounts_bp.route('/payments/suppliers/<int:id>/delete', methods=['POST'])
@login_required
def supplier_payment_delete(id):
    """Soft-delete a supplier payment and reverse its accounting effects."""
    if not _payments_permission_ok():
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import delete_supplier_payment
        if delete_supplier_payment(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment deleted. Balances reversed.', 'success')
        else:
            flash('Supplier payment already deleted.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Supplier payment delete failed')
        flash(f'Unable to delete supplier payment: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.supplier_payments'))


@accounts_bp.route('/payments/suppliers/<int:id>/restore', methods=['POST'])
@login_required
def supplier_payment_restore(id):
    """Restore a deleted supplier payment and re-apply its accounting effects."""
    if not _payments_permission_ok():
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import restore_supplier_payment
        if restore_supplier_payment(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment restored. Balances re-applied.', 'success')
        else:
            flash('Supplier payment is already active.', 'warning')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Supplier payment restore failed')
        flash(f'Unable to restore supplier payment: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.supplier_payments'))
