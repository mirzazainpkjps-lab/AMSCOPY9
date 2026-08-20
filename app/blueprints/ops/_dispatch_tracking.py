"""dispatch — split from ops.py."""
from ._common import *  # noqa

@bp.route('/tracking')
@login_required
def tracking():
    s = request.args.get('start_date')
    end = request.args.get('end_date')
    cl = request.args.get('client')
    m = request.args.get('material')
    bill_no = request.args.get('bill_no', '').strip()
    category = request.args.get('category', '').strip()
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    type_filter = request.args.get('type', '').strip()
    has_bill_filter = request.args.get('has_bill', '').strip()

    has_filter = bool(s or end or cl or m or search or bill_no or category or type_filter or has_bill_filter in ['0', '1'])

    entries = []
    pagination = None
    summary = {}
    total_qty = 0

    if has_filter:
        query = Entry.query
        if s:
            query = query.filter(Entry.date >= s) # Show voided in tracking? Yes, but maybe filterable. For now show all.
        if end:
            query = query.filter(Entry.date <= end)
        if cl:
            query = query.filter(Entry.client == cl)
        if m:
            query = query.filter(Entry.material == m)
        if bill_no:
            query = query.filter(db.or_(Entry.bill_no.ilike(f'%{bill_no}%'), Entry.auto_bill_no.ilike(f'%{bill_no}%')))
        if category:
            query = query.outerjoin(Client, Entry.client_code == Client.code).filter(
                or_(Entry.client_category == category, Client.category == category)
            )
        if type_filter and type_filter != 'PAYMENT':
            query = query.filter(Entry.type == type_filter)
        if has_bill_filter == '1':
            query = query.filter(db.or_(Entry.bill_no != None, Entry.auto_bill_no != None))\
                         .filter(db.or_(Entry.bill_no != '', Entry.auto_bill_no != ''))\
                         .filter(db.or_(Entry.bill_no == None, db.not_(Entry.bill_no.like('UNBILLED%'))))\
                         .filter(db.or_(Entry.bill_no == None, db.not_(Entry.bill_no.like('#%'))))
        if has_bill_filter == '0':
            query = query.filter(db.or_(
                db.and_(
                    db.or_(Entry.bill_no == None, Entry.bill_no == ''),
                    db.or_(Entry.auto_bill_no == None, Entry.auto_bill_no == '')
                ),
                Entry.bill_no.like('UNBILLED%'),
                Entry.bill_no.like('#%')
            ))
        if search:
            query = query.filter(
                db.or_(Entry.material.ilike(f'%{search}%'),
                       Entry.client.ilike(f'%{search}%'),
                       Entry.client_code.ilike(f'%{search}%'),
                       Entry.bill_no.ilike(f'%{search}%'),
                       Entry.nimbus_no.ilike(f'%{search}%'),
                       Entry.note.ilike(f'%{search}%')))

        entry_rows = query.order_by(Entry.date.desc(), Entry.time.desc()).all()
        for e in entry_rows:
            e.bill_ref = _entry_best_bill_ref(e)
            e.source_type = 'Entry'

        # Bulk-lookup unit rates from DirectSaleItem (one query, no N+1)
        ds_ids = {e.source_id for e in entry_rows
                  if getattr(e, 'source_table', '') == 'direct_sale' and e.source_id}
        unit_rate_map = {}
        if ds_ids:
            for dsi in DirectSaleItem.query.filter(DirectSaleItem.sale_id.in_(ds_ids)).all():
                unit_rate_map[(dsi.sale_id, dsi.product_name)] = dsi.price_at_time
        for e in entry_rows:
            if getattr(e, 'source_table', '') == 'direct_sale' and e.source_id:
                e.unit_rate = unit_rate_map.get((e.source_id, e.material), 0)
            else:
                e.unit_rate = 0

        payment_rows = []
        if not type_filter or type_filter == 'PAYMENT':
            pay_query = Payment.query
            if s:
                pay_query = pay_query.filter(func.date(Payment.date_posted) >= s)
            if end:
                pay_query = pay_query.filter(func.date(Payment.date_posted) <= end)
            if cl:
                pay_query = pay_query.filter(Payment.client_name == cl)
            if bill_no:
                pay_query = pay_query.filter(or_(
                    Payment.manual_bill_no.ilike(f'%{bill_no}%'),
                    Payment.auto_bill_no.ilike(f'%{bill_no}%')
                ))
            if category:
                category_names = [x[0] for x in db.session.query(Client.name).filter(Client.category == category).all() if x[0]]
                if category_names:
                    pay_query = pay_query.filter(Payment.client_name.in_(category_names))
                else:
                    pay_query = pay_query.filter(Payment.id == -1)
            if has_bill_filter == '1':
                pay_query = pay_query.filter(
                    or_(Payment.manual_bill_no != None, Payment.auto_bill_no != None)
                ).filter(
                    or_(Payment.manual_bill_no != '', Payment.auto_bill_no != '')
                )
            if has_bill_filter == '0':
                pay_query = pay_query.filter(
                    and_(
                        or_(Payment.manual_bill_no == None, Payment.manual_bill_no == ''),
                        or_(Payment.auto_bill_no == None, Payment.auto_bill_no == '')
                    )
                )
            if search:
                pay_query = pay_query.filter(or_(
                    Payment.client_name.ilike(f'%{search}%'),
                    Payment.manual_bill_no.ilike(f'%{search}%'),
                    Payment.auto_bill_no.ilike(f'%{search}%'),
                    Payment.method.ilike(f'%{search}%'),
                    Payment.note.ilike(f'%{search}%')
                ))

            code_by_client = {c.name: c.code for c in Client.query.with_entities(Client.name, Client.code).all()}
            for p in pay_query.order_by(Payment.date_posted.desc(), Payment.id.desc()).all():
                dt = p.date_posted or pk_now()
                payment_rows.append(SimpleNamespace(
                    id=p.id,
                    date=dt.strftime('%Y-%m-%d'),
                    time=dt.strftime('%H:%M:%S'),
                    type='PAYMENT',
                    client=(p.client_name or ''),
                    client_code=(code_by_client.get(p.client_name, '') or ''),
                    material='-',
                    qty=float(p.amount or 0),
                    auto_bill_no=(p.auto_bill_no or ''),
                    bill_no=(p.manual_bill_no or ''),
                    bill_ref=(p.manual_bill_no or p.auto_bill_no or f'PAY-{p.id}'),
                    nimbus_no='Payment',
                    created_by='System',
                    note=(p.note or ''),
                    is_void=bool(p.is_void),
                    source_type='Payment',
                    unit_rate=0
                ))

        combined_rows = payment_rows if type_filter == 'PAYMENT' else (entry_rows + payment_rows)
        combined_rows.sort(
            key=lambda r: _parse_dt_safe(f"{(getattr(r, 'date', '') or '').strip()} {(getattr(r, 'time', '') or '').strip()}".strip()) or datetime.min,
            reverse=True
        )

        per_page = 15
        total = len(combined_rows)
        pages = max(1, (total + per_page - 1) // per_page)
        if page > pages:
            page = pages
        start_idx = (page - 1) * per_page
        entries = combined_rows[start_idx:start_idx + per_page]
        pagination = SimpleNamespace(
            page=page,
            pages=pages,
            total=total,
            has_prev=(page > 1),
            has_next=(page < pages),
            prev_num=(page - 1),
            next_num=(page + 1)
        )

        # Summary calculation
        base_query = db.session.query(
            Entry.material,
            func.sum(case(
                (Entry.type == 'IN', Entry.qty),
                (Entry.type == 'OUT', -Entry.qty),
                else_=0
            )).label('net'))

        # Ensure summary excludes voided transactions
        base_query = base_query.filter(Entry.is_void == False)

        if category:
            base_query = base_query.outerjoin(Client, Entry.client_code == Client.code).filter(
                or_(Entry.client_category == category, Client.category == category)
            )
        if s:
            base_query = base_query.filter(Entry.date >= s)
        if end:
            base_query = base_query.filter(Entry.date <= end)
        if cl:
            base_query = base_query.filter(Entry.client == cl)
        if m:
            base_query = base_query.filter(Entry.material == m)
        if bill_no:
            base_query = base_query.filter(db.or_(Entry.bill_no.ilike(f'%{bill_no}%'), Entry.auto_bill_no.ilike(f'%{bill_no}%')))
        if type_filter and type_filter != 'PAYMENT':
            base_query = base_query.filter(Entry.type == type_filter)
        if has_bill_filter == '1':
            base_query = base_query.filter(db.or_(Entry.bill_no != None, Entry.auto_bill_no != None))\
                         .filter(db.or_(Entry.bill_no != '', Entry.auto_bill_no != ''))\
                         .filter(db.or_(Entry.bill_no == None, db.not_(Entry.bill_no.like('UNBILLED%'))))
        if has_bill_filter == '0':
            base_query = base_query.filter(db.or_(
                db.and_(
                    db.or_(Entry.bill_no == None, Entry.bill_no == ''),
                    db.or_(Entry.auto_bill_no == None, Entry.auto_bill_no == '')
                ),
                Entry.bill_no.like('UNBILLED%')
            ))
        if search:
            base_query = base_query.filter(
                db.or_(Entry.material.ilike(f'%{search}%'),
                       Entry.client.ilike(f'%{search}%'),
                       Entry.client_code.ilike(f'%{search}%'),
                       Entry.bill_no.ilike(f'%{search}%'),
                       Entry.nimbus_no.ilike(f'%{search}%'),
                       Entry.note.ilike(f'%{search}%')))

        summary_query = base_query.group_by(Entry.material).all()
        summary = {row.material: row.net for row in summary_query}
        total_qty = sum(summary.values()) if summary else 0

    today_str = pk_today().strftime('%Y-%m-%d')
    pending_photos = {
        b.bill_no: b.photo_url
        for b in PendingBill.query.filter(PendingBill.photo_url != '').all()
        if b.bill_no
    }

    return render_template(
        'tracking.html',
        entries=entries,
        pagination=pagination,
        clients=Client.query.filter(Client.is_active == True).order_by(Client.name.asc()).all(),
        materials=Material.query.order_by(Material.name.asc()).all(),
        start_date=s,
        end_date=end,
        client_filter=cl,
        material_filter=m,
        bill_no_filter=bill_no,
        category_filter=category,
        search_query=search,
        now_date=today_str,
        total_qty=total_qty,
        summary=summary,
        has_filter=has_filter,
        pending_photos=pending_photos,
        type_filter=type_filter,
        has_bill_filter=has_bill_filter)

