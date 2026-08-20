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
    # Two grouped queries replace two queries per visible client while keeping
    # the exact counters shown by the template.
    visible_codes = [c.code for c in all_visible_clients if c.code]
    visible_names = [c.name for c in all_visible_clients if c.name]
    bill_counts = dict(
        db.session.query(PendingBill.client_code, func.count(PendingBill.id))
        .filter(PendingBill.client_code.in_(visible_codes))
        .group_by(PendingBill.client_code)
        .all()
    ) if visible_codes else {}
    delivery_totals = dict(
        db.session.query(Entry.client, func.sum(Entry.qty))
        .filter(Entry.type == 'OUT', Entry.client.in_(visible_names))
        .group_by(Entry.client)
        .all()
    ) if visible_names else {}
    for c in all_visible_clients:
        c.total_bills = bill_counts.get(c.code, 0) or 0
        c.total_deliveries = delivery_totals.get(c.name, 0) or 0

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
                           categories=categories)


@bp.route('/clients/<int:client_id>/modals')
@login_required
def client_modals(client_id):
    """Render one active client's edit/transfer dialogs on demand."""
    client = Client.query.filter(Client.id == client_id, Client.is_active == True).first_or_404()
    active_clients = Client.query.filter(Client.is_active == True).order_by(Client.name.asc()).all()
    return render_template('_client_modals.html', c=client, active_clients=active_clients)

