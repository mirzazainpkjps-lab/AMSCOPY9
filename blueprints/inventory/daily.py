"""daily — split from inventory.py."""
from ._common import *  # noqa

@inventory_bp.route('/daily_transactions')
@login_required
def daily_transactions():
    # Support date range and category filtering
    date_from = request.args.get('date_from') or request.args.get('date') or date.today().strftime('%Y-%m-%d')
    date_to = request.args.get('date_to') or date_from
    category = request.args.get('category', '').strip()
    trans_category = request.args.get('transaction_category', '').strip()
    material = request.args.get('material', '').strip()
    material_category = request.args.get('material_category', '').strip()
    bill_no = request.args.get('bill_no', '').strip()
    client_filter = request.args.get('client', '').strip()
    show_mode = (request.args.get('show') or 'active').strip().lower()

    page = request.args.get('page', 1, type=int)
    per_page = 50  # Increased for better visibility

    # Fix: Ensure query uses models correctly
    q = Entry.query.filter(Entry.date >= date_from, Entry.date <= date_to)
    if show_mode == 'voided':
        q = q.filter(Entry.is_void == True)
    elif show_mode == 'all':
        q = q
    else:
        show_mode = 'active'
        q = q.filter(Entry.is_void == False)
    if category:
        q = q.filter(or_(
            Entry.client_category == category,
            Entry.client_code.in_(db.session.query(Client.code).filter(Client.category == category))
        ))
    if trans_category:
        tc_norm = trans_category.strip().lower()
        if tc_norm == 'billed':
            q = q.filter(func.lower(func.coalesce(Entry.transaction_category, '')) == 'billed')
        elif tc_norm == 'unbilled':
            q = q.filter(func.lower(func.coalesce(Entry.transaction_category, '')).in_(['unbilled', 'unbilled cash']))
        elif tc_norm == 'open khata':
            q = q.filter(func.lower(func.coalesce(Entry.transaction_category, '')) == 'open khata')
        else:
            q = q.filter(func.lower(func.coalesce(Entry.transaction_category, '')) == tc_norm)
    if material:
        q = q.filter(Entry.material == material)
    if material_category:
        try:
            cat_id_int = int(material_category)
            cat_materials = [m.name for m in Material.query.filter(Material.category_id == cat_id_int).all()]
            if cat_materials:
                q = q.filter(Entry.material.in_(cat_materials))
            else:
                q = q.filter(Entry.id == -1)
        except ValueError:
            pass
    if bill_no:
        q = q.filter(or_(Entry.bill_no.ilike(f'%{bill_no}%'), Entry.auto_bill_no.ilike(f'%{bill_no}%')))
    if client_filter:
        # If filter looks like a code, do an exact match on code.
        if client_filter.lower().startswith(('tmpc-', 'fbm-')):
            q = q.filter(Entry.client_code == client_filter)
        else: # Otherwise, do a 'contains' search on the name.
            q = q.filter(Entry.client.ilike(f'%{client_filter}%'))
        
    entries_list = q.order_by(Entry.date.desc(), Entry.time.desc()).all()
    for e in entries_list:
        e.bill_ref = _entry_best_bill_ref(e)
        e.source_type = 'Entry'
        e.void_reason_label = ''

    # KPI totals (full filtered range, not just page slice)
    entry_in_qty = float(q.with_entities(func.sum(case((Entry.type == 'IN', Entry.qty), else_=0))).scalar() or 0)
    entry_out_qty = float(q.with_entities(func.sum(case((Entry.type == 'OUT', Entry.qty), else_=0))).scalar() or 0)

    payment_rows = []
    include_payments = not material and not material_category
    payments_total = 0.0
    if include_payments:
        pay_q = Payment.query.filter(
            func.date(Payment.date_posted) >= date_from,
            func.date(Payment.date_posted) <= date_to
        )
        if show_mode == 'voided':
            pay_q = pay_q.filter(Payment.is_void == True)
        elif show_mode == 'all':
            pay_q = pay_q
        else:
            pay_q = pay_q.filter(Payment.is_void == False)
        if bill_no:
            pay_q = pay_q.filter(or_(Payment.manual_bill_no.ilike(f'%{bill_no}%'), Payment.auto_bill_no.ilike(f'%{bill_no}%')))
        if client_filter:
            if client_filter.lower().startswith(('tmpc-', 'fbm-')):
                client_obj = Client.query.filter_by(code=client_filter).first()
                if client_obj:
                    pay_q = pay_q.filter(Payment.client_name == client_obj.name)
                else:
                    pay_q = pay_q.filter(Payment.id == -1)
            else:
                pay_q = pay_q.filter(Payment.client_name.ilike(f'%{client_filter}%'))
        if category:
            category_clients = [row[0] for row in db.session.query(Client.name).filter(Client.category == category).all() if row[0]]
            if category_clients:
                pay_q = pay_q.filter(Payment.client_name.in_(category_clients))
            else:
                pay_q = pay_q.filter(Payment.id == -1)

        payments_total = float(pay_q.with_entities(func.sum(Payment.amount)).scalar() or 0)
        client_code_map = {c.name: c.code for c in Client.query.with_entities(Client.name, Client.code).all()}
        for p in pay_q.order_by(Payment.date_posted.desc(), Payment.id.desc()).all():
            dt = p.date_posted or datetime.now()
            payment_rows.append(SimpleNamespace(
                id=p.id,
                date=dt.strftime('%Y-%m-%d'),
                time=dt.strftime('%H:%M:%S'),
                type='PAYMENT',
                client=(p.client_name or ''),
                client_code=(client_code_map.get(p.client_name, '') or ''),
                material='-',
                qty=float(p.amount or 0),
                bill_no=(p.manual_bill_no or ''),
                auto_bill_no=(p.auto_bill_no or ''),
                nimbus_no='Payment',
                created_by='System',
                is_void=bool(p.is_void),
                bill_ref=(p.manual_bill_no or p.auto_bill_no or f'PAY-{p.id}'),
                source_type='Payment'
            ))

    all_rows = entries_list + payment_rows
    all_rows.sort(
        key=lambda r: datetime.strptime(f"{(getattr(r, 'date', '') or '').strip()} {(getattr(r, 'time', '') or '').strip()}".strip(), '%Y-%m-%d %H:%M:%S')
        if (getattr(r, 'date', None) and getattr(r, 'time', None)) else datetime.min,
        reverse=True
    )
    total = len(all_rows)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    end_idx = start + per_page
    paged_rows = all_rows[start:end_idx]

    # Classify "voided by edit" rows for clearer audit readability.
    for row in paged_rows:
        if getattr(row, 'source_type', '') != 'Entry':
            continue
        if not bool(getattr(row, 'is_void', False)):
            continue
        if (getattr(row, 'nimbus_no', '') or '').strip().lower() != 'direct sale':
            continue
        bill_ref = (getattr(row, 'bill_no', '') or '').strip() or (getattr(row, 'auto_bill_no', '') or '').strip()
        if not bill_ref:
            continue
        replacement = Entry.query.filter(
            Entry.id != row.id,
            Entry.is_void == False,
            Entry.nimbus_no == row.nimbus_no,
            Entry.type == row.type,
            Entry.material == row.material,
            Entry.client == row.client,
            Entry.qty == row.qty,
            or_(Entry.bill_no == bill_ref, Entry.auto_bill_no == bill_ref)
        ).order_by(Entry.id.desc()).first()
        if replacement and replacement.id > row.id:
            row.void_reason_label = 'Deleted by Edit'

    entries_pagination = SimpleNamespace(
        page=page,
        pages=pages,
        total=total,
        has_prev=(page > 1),
        has_next=(page < pages),
        prev_num=(page - 1),
        next_num=(page + 1),
        items=paged_rows
    )

    materials = Material.query.all()
    material_category_map = {m.name: (m.category.name if m.category else '') for m in materials}
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    material_categories = MaterialCategory.query.order_by(MaterialCategory.name.asc()).all()
    
    # Build categories list for filter efficiently
    categories_query = db.session.query(Client.category).distinct().filter(Client.category != None, Client.category != '').all()
    categories = sorted([c[0] for c in categories_query])
    if 'Open Khata' not in categories:
        categories.append('Open Khata')
        categories = sorted(categories, key=lambda x: str(x).lower())
    
    # Add Transaction Categories
    transaction_categories = ['Billed', 'Unbilled', 'Open Khata']
    
    # Get bill metadata (photos/urls) for this date range's entries
    bill_numbers = set()
    for e in paged_rows:
        if e.bill_no: bill_numbers.add(e.bill_no)
        if e.auto_bill_no: bill_numbers.add(e.auto_bill_no)
    
    bill_meta = {}
    if bill_numbers:
        def populate_meta(model):
            records = model.query.filter(or_(model.manual_bill_no.in_(list(bill_numbers)), model.auto_bill_no.in_(list(bill_numbers)))).all()
            for r in records:
                meta = {'photo_path': r.photo_path, 'photo_url': r.photo_url}
                if r.manual_bill_no: bill_meta[r.manual_bill_no] = meta
                if r.auto_bill_no: bill_meta[r.auto_bill_no] = meta
        
        populate_meta(DirectSale)
        populate_meta(Booking)
        populate_meta(Payment)
        populate_meta(GRN)

    return render_template('daily_transactions.html', 
                           entries=paged_rows, 
                           pagination=entries_pagination, 
                           sel_date=date_from,
                           date_from=date_from,
                           date_to=date_to,
                           category_filter=category,
                           transaction_category_filter=trans_category,
                           material_filter=material,
                           material_category_filter=material_category,
                           bill_no_filter=bill_no,
                           client_filter=client_filter,
                           entry_in_qty=entry_in_qty,
                           entry_out_qty=entry_out_qty,
                           payments_total=payments_total,
                           include_payments=include_payments,
                           clients=clients,
                           materials=materials,
                           material_category_map=material_category_map,
                           categories=categories,
                           material_categories=material_categories,
                           transaction_categories=transaction_categories,
                           show_mode=show_mode,
                           bill_meta=bill_meta)


@inventory_bp.route('/inventory_log')
@login_required
def inventory_log():
    # Keep it for compatibility or redirect
    return redirect(url_for('inventory.stock_summary'))


