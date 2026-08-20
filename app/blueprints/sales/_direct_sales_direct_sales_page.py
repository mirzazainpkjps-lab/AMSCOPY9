"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/direct_sales')
@login_required
def direct_sales_page():
    show_mode = (request.args.get('show', 'active') or 'active').strip().lower()
    filter_client = (request.args.get('client') or '').strip()
    filter_bill_no = (request.args.get('bill_no') or '').strip()
    filter_bill_state = (request.args.get('bill_state') or '').strip().lower()  # all|billed|unbilled
    filter_category = (request.args.get('category') or '').strip()
    filter_material = (request.args.get('material') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    per_page = min(max(per_page, 10), 50)

    sales_q = DirectSale.query.options(
        selectinload(DirectSale.items),
        selectinload(DirectSale.invoice)
    )
    if show_mode == 'voided':
        sales_q = sales_q.filter(DirectSale.is_void == True)
    elif show_mode == 'all':
        sales_q = sales_q
    else:
        show_mode = 'active'
        sales_q = sales_q.filter(DirectSale.is_void == False)

    if filter_client:
        # The client combobox submits the master client code, while historical
        # sales are displayed/stored by name (and older rows may have no code).
        # Resolve a selected client and match both identities so choosing a
        # client returns the same transactions shown in its ledger.
        resolved_filter_client = get_client_by_input(filter_client)
        if resolved_filter_client:
            client_code = (resolved_filter_client.code or '').strip().lower()
            client_name = (resolved_filter_client.name or '').strip().lower()
            sales_q = sales_q.filter(or_(
                func.lower(func.trim(func.coalesce(DirectSale.client_code, ''))) == client_code,
                func.lower(func.trim(func.coalesce(DirectSale.client_name, ''))) == client_name,
            ))
        else:
            # Preserve free-text searching, including partial client codes.
            sales_q = sales_q.filter(or_(
                DirectSale.client_name.ilike(f'%{filter_client}%'),
                DirectSale.client_code.ilike(f'%{filter_client}%'),
            ))

    if filter_bill_no:
        sales_q = sales_q.filter(or_(
            DirectSale.manual_bill_no.ilike(f'%{filter_bill_no}%'),
            DirectSale.auto_bill_no.ilike(f'%{filter_bill_no}%'),
            DirectSale.invoice.has(Invoice.invoice_no.ilike(f'%{filter_bill_no}%'))
        ))

    if filter_bill_state == 'billed':
        sales_q = sales_q.filter(or_(
            DirectSale.category != 'Cash',
            func.length(func.trim(func.coalesce(DirectSale.manual_bill_no, ''))) > 0,
            DirectSale.invoice_id.isnot(None)
        ))
    elif filter_bill_state == 'unbilled':
        sales_q = sales_q.filter(
            DirectSale.category == 'Cash',
            func.length(func.trim(func.coalesce(DirectSale.manual_bill_no, ''))) == 0,
            DirectSale.invoice_id.is_(None)
        )

    if filter_category and filter_category in SALE_CATEGORY_CHOICES:
        sales_q = sales_q.filter(DirectSale.category == filter_category)

    if filter_material:
        sales_q = sales_q.filter(
            DirectSale.items.any(DirectSaleItem.product_name.ilike(f'%{filter_material}%'))
        )

    sales_pagination = sales_q.order_by(DirectSale.date_posted.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    sales = sales_pagination.items
    materials = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    delivery_persons = DeliveryPerson.query.order_by(DeliveryPerson.name.asc()).all()
    delivery_person_by_name = {
        (person.name or '').strip().lower(): person
        for person in delivery_persons if (person.name or '').strip()
    }
    # Get GRNs for selection
    grns = GRN.query.filter_by(is_void=False).options(selectinload(GRN.items)).order_by(GRN.date_posted.desc()).limit(100).all()
    # Keep sales categories concise and business-focused.
    categories = SALE_CATEGORY_CHOICES
    client_name_prefill = request.args.get('client_name', '').strip()
    next_auto = peek_next_bill_no(AUTO_BILL_NAMESPACES['DIRECT_SALE'])

    # Keep counters consistent with row status logic in templates/direct_sales.html
    # BILLED: has manual bill no or linked invoice (except Open Khata)
    # UNBILLED: no manual bill no and no linked invoice (except Open Khata)
    active_sales_q = DirectSale.query.filter_by(is_void=False).filter(DirectSale.category != 'Open Khata')
    billed_count = active_sales_q.filter(or_(
        DirectSale.category != 'Cash',
        func.length(func.trim(func.coalesce(DirectSale.manual_bill_no, ''))) > 0,
        DirectSale.invoice_id.isnot(None)
    )).count()
    unbilled_count = active_sales_q.filter(
        DirectSale.category == 'Cash',
        func.length(func.trim(func.coalesce(DirectSale.manual_bill_no, ''))) == 0,
        DirectSale.invoice_id.is_(None)
    ).count()

    stats = {
        'billed': billed_count,
        'unbilled': unbilled_count
    }

    settings = Settings.query.first()
    sale_ids = [s.id for s in sales]
    rent_rows = DeliveryRent.query.filter(
        DeliveryRent.is_void == False,
        DeliveryRent.sale_id.in_(sale_ids)
    ).all() if sale_ids else []
    rents_by_sale = {}
    for rr in rent_rows:
        if rr.sale_id:
            rents_by_sale[rr.sale_id] = rr
    delivery_allocations_by_sale = {}
    delivery_rent_totals_by_sale = {}
    if sale_ids:
        alloc_rows = SaleDeliveryPerson.query.filter(
            SaleDeliveryPerson.sale_id.in_(sale_ids),
            SaleDeliveryPerson.is_void == False
        ).all()
        for ar in alloc_rows:
            delivery_allocations_by_sale.setdefault(ar.sale_id, []).append(ar)
            delivery_rent_totals_by_sale[ar.sale_id] = delivery_rent_totals_by_sale.get(ar.sale_id, 0.0) + float(ar.rent_amount or 0)

    for s in sales:
        if s.id in delivery_allocations_by_sale:
            continue
        fallback_rent = float(getattr(s, 'delivery_rent_cost', 0) or 0)
        rent_row = rents_by_sale.get(s.id)
        if rent_row and rent_row.amount is not None:
            fallback_rent = float(rent_row.amount or 0)
        if (s.driver_name or '').strip() or fallback_rent > 0:
            dp_match = delivery_person_by_name.get((s.driver_name or '').strip().lower())
            delivery_allocations_by_sale[s.id] = [{
                'delivery_person': dp_match,
                'delivery_person_id': (dp_match.id if dp_match else None),
                'delivery_person_name': (s.driver_name or '').strip(),
                'bags_delivered': 0,
                'rent_amount': fallback_rent
            }]
            delivery_rent_totals_by_sale[s.id] = fallback_rent
    booked_client_codes = []
    booked_by_client = {}
    booked_rows_all = db.session.query(
        func.lower(func.trim(Booking.client_name)).label('client_norm'),
        BookingItem.material_name,
        func.sum(BookingItem.qty)
    ).join(Booking, BookingItem.booking_id == Booking.id).filter(
        Booking.is_void == False
    ).group_by(
        func.lower(func.trim(Booking.client_name)),
        BookingItem.material_name
    ).all()
    for client_norm, mat_name, qty in booked_rows_all:
        if not client_norm or not mat_name:
            continue
        mat_map = booked_by_client.setdefault(client_norm, {})
        mat_key = _material_norm_key(mat_name)
        if mat_key:
            mat_map[mat_key] = float(mat_map.get(mat_key, 0) or 0) + float(qty or 0)

    delivered_by_client = {}
    delivered_by_code = {}
    delivered_rows_all = db.session.query(
        Entry.client_code,
        func.lower(func.trim(Entry.client)).label('client_norm'),
        func.coalesce(Entry.booked_material, Entry.material).label('mat_name'),
        func.sum(Entry.qty)
    ).filter(
        Entry.type == 'OUT',
        Entry.is_void == False,
        not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
    ).group_by(
        Entry.client_code,
        func.lower(func.trim(Entry.client)),
        func.coalesce(Entry.booked_material, Entry.material)
    ).all()
    for client_code, client_norm, mat_name, qty in delivered_rows_all:
        if not mat_name:
            continue
        mat_key = _material_norm_key(mat_name)
        if not mat_key:
            continue
        if client_norm:
            mat_map = delivered_by_client.setdefault(client_norm, {})
            mat_map[mat_key] = float(mat_map.get(mat_key, 0) or 0) + float(qty or 0)
        if client_code:
            mat_map_code = delivered_by_code.setdefault(client_code, {})
            mat_map_code[mat_key] = float(mat_map_code.get(mat_key, 0) or 0) + float(qty or 0)

    for c in clients:
        norm_name = (c.name or '').strip().lower()
        booked_map = booked_by_client.get(norm_name) or {}
        if not booked_map:
            continue
        delivered_map = _merge_delivery_maps(
            delivered_by_client.get(norm_name) or {},
            delivered_by_code.get(c.code) or {},
        )
        if any((float(bq or 0) - float(delivered_map.get(mat, 0) or 0)) > 0 for mat, bq in booked_map.items()):
            booked_client_codes.append(c.code)

    client_code_by_name = {
        (c.name or '').strip().lower(): c.code for c in clients if (c.name or '').strip()
    }
    sale_client_code_by_id = {}
    for s in sales:
        norm = (s.client_name or '').strip().lower()
        if norm in client_code_by_name:
            sale_client_code_by_id[s.id] = client_code_by_name[norm]

    sale_form_draft = session.pop('direct_sale_form_draft', None)
    resume_mode = (request.args.get('resume') or '').strip().lower()
    hold_drafts = DirectSaleDraft.query.order_by(DirectSaleDraft.updated_at.desc()).limit(200).all()

    return render_template('direct_sales.html',
                           sales=sales,
                           materials=materials,
                           clients=clients,
                           grns=grns,
                           accounts=Account.query.filter(func.coalesce(Account.is_active, True) == True).order_by(Account.name.asc()).all(),
                           sale_client_code_by_id=sale_client_code_by_id,
                           booked_client_codes=booked_client_codes,
                           delivery_persons=delivery_persons,
                           categories=categories,
                           next_auto=next_auto,
                           client_name_prefill=client_name_prefill,
                           stats=stats,
                           settings=settings,
                           delivery_allocations_by_sale=delivery_allocations_by_sale,
                           delivery_rent_totals_by_sale=delivery_rent_totals_by_sale,
                           sale_form_draft=sale_form_draft,
                           resume_mode=resume_mode,
                           hold_drafts=hold_drafts,
                           show_mode=show_mode,
                           pagination=sales_pagination,
                           per_page=per_page,
                           filters={
                               'client': filter_client,
                               'bill_no': filter_bill_no,
                               'bill_state': filter_bill_state or 'all',
                               'category': filter_category,
                               'material': filter_material
                           })

