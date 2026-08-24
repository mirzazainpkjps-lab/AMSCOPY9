from ._common import *  # noqa

@bp.route('/clients')
@login_required
def clients():
    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()
    category_normalized = category.lower()
    page_active = request.args.get('page_active', 1, type=int)
    page_inactive = request.args.get('page_inactive', 1, type=int)

    active_query = Client.query.filter(Client.is_active == True)
    if search:
        active_query = active_query.filter(
            db.or_(Client.name.ilike(f'%{search}%'), Client.code.ilike(f'%{search}%')))
    if category:
        active_query = active_query.filter(func.lower(func.trim(Client.category)) == category_normalized)
    active_pagination = active_query.order_by(Client.name.asc()).paginate(page=page_active, per_page=10)

    inactive_query = Client.query.filter(Client.is_active == False)
    if search:
        inactive_query = inactive_query.filter(
            db.or_(Client.name.ilike(f'%{search}%'), Client.code.ilike(f'%{search}%')))
    if category:
        inactive_query = inactive_query.filter(func.lower(func.trim(Client.category)) == category_normalized)
    inactive_pagination = inactive_query.order_by(Client.name.asc()).paginate(page=page_inactive, per_page=10)

    all_visible_clients = active_pagination.items + inactive_pagination.items

    # Build authoritative financial snapshot once, then project useful
    # payable/balance data for each visible client. This replaces the old
    # bill-count / delivery-qty columns with real accounting data.
    snapshot = None
    try:
        from app.services.financial_ledgers import _client_snapshot, build_client_financial_ledger
        snapshot = _client_snapshot()
    except Exception:
        snapshot = None
        from app.services.financial_ledgers import build_client_financial_ledger  # noqa: F811

    # aggregates for summary cards (only visible page, but useful)
    total_payable_visible = 0.0
    total_advance_visible = 0.0
    total_billed_visible = 0.0
    total_received_visible = 0.0

    for c in all_visible_clients:
        try:
            if snapshot is not None:
                ledger = build_client_financial_ledger(c, snapshot=snapshot)
            else:
                ledger = build_client_financial_ledger(c)
            current_balance = float(ledger.get('closing_balance') or 0)
            total_debit = float(ledger.get('total_debit') or 0)
            total_credit = float(ledger.get('total_credit') or 0)
        except Exception:
            # Fallback to opening balance only if ledger projection fails
            current_balance = float(getattr(c, 'opening_balance', 0) or 0)
            total_debit = float(current_balance) if current_balance > 0 else 0.0
            total_credit = float(-current_balance) if current_balance < 0 else 0.0
            ledger = {'status': 'Unknown', 'last_transaction_date': None, 'last_payment_date': None, 'obligations': []}

        c.current_balance = current_balance
        c.total_debit = total_debit
        c.total_credit = total_credit
        c.payable_amount = current_balance
        c.outstanding = max(0.0, current_balance)
        c.advance_amount = max(0.0, -current_balance)
        c.status_label = ledger.get('status') or ('Outstanding' if current_balance > 0.5 else ('Credit' if current_balance < -0.5 else 'Settled'))
        c.last_txn_date = ledger.get('last_transaction_date')
        c.last_payment_date = ledger.get('last_payment_date')
        c.obligations_count = len(ledger.get('obligations', []) or [])

        # keep old attrs for backward compat if template still references them
        c.total_bills = c.obligations_count
        c.total_deliveries = 0

        total_payable_visible += c.outstanding
        total_advance_visible += c.advance_amount
        total_billed_visible += total_debit
        total_received_visible += total_credit

    all_clients_list = Client.query.order_by(Client.name.asc()).all()
    categories = [
        row[0] for row in db.session.query(Client.category).distinct().filter(
            Client.category != None,
            func.trim(Client.category) != ''
        ).order_by(Client.category.asc()).all()
    ]
    for default_cat in ['General', 'Open Khata', 'Walking-Customer', 'Misc']:
        if default_cat not in categories:
            categories.append(default_cat)
    categories = sorted(categories, key=lambda x: str(x).lower())

    return render_template('clients.html',
                           active_pagination=active_pagination,
                           inactive_pagination=inactive_pagination,
                           search=search,
                           category=category,
                           all_clients=all_clients_list,
                           categories=categories,
                           total_payable_visible=total_payable_visible,
                           total_advance_visible=total_advance_visible,
                           total_billed_visible=total_billed_visible,
                           total_received_visible=total_received_visible)


@bp.route('/clients/<int:client_id>/modals')
@login_required
def client_modals(client_id):
    """Render one active client's edit/transfer dialogs on demand."""
    client = Client.query.filter(Client.id == client_id, Client.is_active == True).first_or_404()
    active_clients = Client.query.filter(Client.is_active == True).order_by(Client.name.asc()).all()
    return render_template('_client_modals.html', c=client, active_clients=active_clients)

