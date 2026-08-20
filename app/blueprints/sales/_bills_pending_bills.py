"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/pending_bills')
@login_required
def pending_bills():
    page = request.args.get('page', 1, type=int)
    category = request.args.get('category', '').strip()
    filters = {
        'client_code': request.args.get('client_code', '').strip(),
        'bill_no': request.args.get('bill_no', '').strip(),
        'bill_from': request.args.get('bill_from', '').strip(),
        'bill_to': request.args.get('bill_to', '').strip(),
        'category': category,
        'bill_kind': request.args.get('bill_kind', '').strip().upper(),
        'is_cash': request.args.get('is_cash', '').strip(),
        'is_manual': request.args.get('is_manual', '').strip()
    }

    query = PendingBill.query

    if filters['client_code']: # Add is_void filter
        query = query.filter(PendingBill.client_code == filters['client_code'])
    if filters['bill_no']:
        bill_q = filters['bill_no']
        variants = _bill_no_variants(bill_q)
        ors = [PendingBill.bill_no.ilike(f"%{bill_q}%")]
        ors.extend([PendingBill.bill_no.ilike(v) for v in variants if v])
        query = query.filter(or_(*ors))
    if filters['bill_kind'] in ['SB', 'MB']:
        query = query.filter(PendingBill.bill_kind == filters['bill_kind'])
    if filters['is_cash'] != '':
        query = query.filter(PendingBill.is_cash == (filters['is_cash'] == '1'))
    if filters['is_manual'] != '':
        query = query.filter(PendingBill.is_manual == (filters['is_manual'] == '1'))

    query = query.filter(PendingBill.is_void == False)

    normalized_category = normalize_sale_category(category, default=category) if category else ''

    if category == 'Unbilled Cash' or normalized_category == 'Cash':
        query = query.filter(PendingBill.is_cash == True)
    elif category == 'Cash Paid':
        query = query.filter(
            PendingBill.is_paid == True,
            or_(
                PendingBill.client_code == OPEN_KHATA_CODE,
                func.upper(PendingBill.client_name) == OPEN_KHATA_NAME
            )
        )
    elif normalized_category == 'Open Khata':
        query = query.filter(or_(
            PendingBill.client_code == OPEN_KHATA_CODE,
            func.upper(PendingBill.client_name) == OPEN_KHATA_NAME
        ))
    elif normalized_category in ['Booking Delivery', 'Mixed Transaction', 'Credit Customer']:
        query = query.filter(
            func.lower(func.coalesce(PendingBill.reason, '')).like(
                f"direct sale ({normalized_category.lower()}):%"
            )
        )
    elif category:
        query = query.join(Client, PendingBill.client_code == Client.code).filter(Client.category == category)

    pagination = query.order_by(PendingBill.id.desc()).paginate(page=page, per_page=15)

    active_clients = Client.query.filter(Client.is_active == True).order_by(Client.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()

    return render_template('pending_bills.html',
                           bills=pagination.items,
                           pagination=pagination,
                           filters=filters,
                           clients=active_clients,
                           materials=materials)


@bp.route('/pending_bills/<int:bill_id>/modals')
@login_required
def pending_bill_modals(bill_id):
    """Render one pending bill's view/edit dialogs on demand."""
    bill = PendingBill.query.filter(PendingBill.id == bill_id).first_or_404()
    clients = Client.query.filter(Client.is_active == True).order_by(Client.name.asc()).all()
    return render_template('_pending_bill_modals.html', bill=bill, clients=clients)

