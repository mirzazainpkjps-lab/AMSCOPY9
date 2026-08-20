"""other — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/decision_ledger')
@login_required
def decision_ledger():
    # --- Part 1: Per-Client Financial Summary ---
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    client_financial_summary = []

    for client in clients:
        # Financial totals (void-safe)
        b_debit = db.session.query(func.sum(Booking.amount)).filter_by(client_name=client.name, is_void=False).scalar() or 0
        b_credit = db.session.query(func.sum(Booking.paid_amount)).filter_by(client_name=client.name, is_void=False).scalar() or 0
        p_credit = db.session.query(func.sum(Payment.amount)).filter_by(client_name=client.name, is_void=False).scalar() or 0
        ds_debit = db.session.query(func.sum(DirectSale.amount)).filter(func.lower(DirectSale.client_name) == client.name.lower(), DirectSale.is_void==False).scalar() or 0
        ds_credit = db.session.query(func.sum(DirectSale.paid_amount)).filter(func.lower(DirectSale.client_name) == client.name.lower(), DirectSale.is_void==False).scalar() or 0
        
        b_discount = 0
        try:
            b_discount = db.session.query(func.sum(Booking.discount)).filter(
                func.lower(func.trim(Booking.client_name)) == client.name.lower(),
                Booking.is_void == False
            ).scalar() or 0
        except Exception:
            pass

        p_discount = 0
        try:
            p_discount = _client_waive_off_total((client.name or '').strip().lower())
        except Exception:
            pass

        ds_discount = 0
        try:
            ds_discount = db.session.query(func.sum(DirectSale.discount)).filter(
                func.lower(func.trim(DirectSale.client_name)) == client.name.lower(),
                DirectSale.is_void == False
            ).scalar() or 0
        except Exception:
            pass

        opening_balance = _to_float_or_zero(getattr(client, 'opening_balance', 0))
        opening_debit = opening_balance if opening_balance > 0 else 0
        opening_credit = abs(opening_balance) if opening_balance < 0 else 0

        total_debit = b_debit + ds_debit + opening_debit
        total_credit = b_credit + p_credit + ds_credit + ds_discount + b_discount + p_discount + opening_credit
        balance = total_debit - total_credit

        # --- Per-Client Material Summary ---
        booked_res = db.session.query(BookingItem.material_name, func.sum(BookingItem.qty))\
            .join(Booking).filter(Booking.client_name == client.name, Booking.is_void == False)\
            .group_by(BookingItem.material_name).all()
        booked_map = {}
        material_labels = {}
        for mat_name, qty in booked_res:
            mat_key = _material_norm_key(mat_name)
            if not mat_key:
                continue
            booked_map[mat_key] = float(booked_map.get(mat_key, 0) or 0) + float(qty or 0)
            material_labels.setdefault(mat_key, (mat_name or '').strip())

        # Latest unit price per material (from booking items)
        latest_price = {}
        latest_price_dt = {}
        booking_items = BookingItem.query.join(Booking).filter(
            Booking.client_name == client.name,
            Booking.is_void == False
        ).all()
        for item in booking_items:
            mat_name = item.material_name
            if not mat_name:
                continue
            mat_key = _material_norm_key(mat_name)
            if not mat_key:
                continue
            material_labels.setdefault(mat_key, (mat_name or '').strip())
            bk = item.booking
            bk_dt = bk.date_posted if bk and getattr(bk, 'date_posted', None) else None
            if mat_key not in latest_price_dt or (bk_dt and latest_price_dt[mat_key] and bk_dt > latest_price_dt[mat_key]) or (bk_dt and not latest_price_dt[mat_key]):
                latest_price_dt[mat_key] = bk_dt
                latest_price[mat_key] = float(item.price_at_time or 0)
            elif mat_key not in latest_price:
                latest_price[mat_key] = float(item.price_at_time or 0)

        entries = Entry.query.filter(
            (Entry.client_code == client.code) | (Entry.client == client.name),
            Entry.type == 'OUT',
            Entry.is_void == False
        ).filter(
            or_(
                func.coalesce(Entry.nimbus_no, '') != 'Direct Sale',
                Entry.client_category == 'Booking Delivery'
            )
        ).all()

        dispatched_map = {}
        for e in entries:
            key = _material_norm_key(e.booked_material or e.material)
            if key:
                dispatched_map[key] = dispatched_map.get(key, 0) + float(e.qty or 0)
                material_labels.setdefault(key, (e.booked_material or e.material or '').strip())

        materials_summary = []
        total_remaining_qty = 0
        total_reserved_cost = 0
        total_booked_cost = 0
        total_dispatched_cost = 0
        all_mats = set(booked_map.keys()) | set(dispatched_map.keys())

        for m in sorted(all_mats, key=lambda x: str(material_labels.get(x, x)).lower()):
            b = booked_map.get(m, 0)
            d = dispatched_map.get(m, 0)
            rem = b - d
            unit_price = latest_price.get(m, 0)
            booked_cost = b * unit_price
            dispatched_cost = d * unit_price
            remaining_cost = rem * unit_price
            if b > 0 or d > 0 or rem != 0:
                materials_summary.append({
                    'name': material_labels.get(m, m),
                    'booked': b,
                    'dispatched': d,
                    'remaining': rem,
                    'unit_price': unit_price,
                    'booked_cost': booked_cost,
                    'dispatched_cost': dispatched_cost,
                    'remaining_cost': remaining_cost
                })
                total_remaining_qty += rem
                total_reserved_cost += remaining_cost
                total_booked_cost += booked_cost
                total_dispatched_cost += dispatched_cost

        client_financial_summary.append({
            'client': client,
            'financial': {
                'debit': total_debit,
                'credit': total_credit,
                'balance': balance
            },
            'materials': materials_summary,
            'material_totals': {
                'total_remaining_qty': total_remaining_qty,
                'total_reserved_cost': total_reserved_cost,
                'total_booked_cost': total_booked_cost,
                'total_dispatched_cost': total_dispatched_cost
            }
        })

    # --- Filters & Pagination (Client Summary) ---
    q = request.args.get('q', '').strip()
    category_filter = request.args.get('category', '').strip()
    balance_filter = request.args.get('balance', 'all').strip().lower()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(max(per_page, 5), 100)

    def _match(row):
        if q:
            ql = q.lower()
            if ql not in (row['client'].name or '').lower() and ql not in (row['client'].code or '').lower():
                return False
        if category_filter and (row['client'].category or '') != category_filter:
            return False
        bal = row['financial']['balance']
        if balance_filter == 'debit' and bal <= 0:
            return False
        if balance_filter == 'credit' and bal >= 0:
            return False
        if balance_filter == 'zero' and bal != 0:
            return False
        return True

    filtered = [r for r in client_financial_summary if _match(r)]
    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = start + per_page
    paged = filtered[start:end]

    # --- Part 2: Overall Material Summary ---
    total_booked_q = db.session.query(
        BookingItem.material_name,
        func.sum(BookingItem.qty).label('total_booked')
    ).join(Booking).filter(Booking.is_void == False).group_by(BookingItem.material_name).all()
    total_booked_map = {}
    total_material_labels = {}
    for r in total_booked_q:
        mat_key = _material_norm_key(r.material_name)
        if not mat_key:
            continue
        total_booked_map[mat_key] = float(total_booked_map.get(mat_key, 0) or 0) + float(r.total_booked or 0)
        total_material_labels.setdefault(mat_key, (r.material_name or '').strip())

    all_dispatches = db.session.query(
        func.coalesce(Entry.booked_material, Entry.material).label('booked_mat'),
        func.sum(Entry.qty).label('total_dispatched')
    ).filter(
        Entry.type == 'OUT', Entry.is_void == False
    ).filter(
        or_(
            func.coalesce(Entry.nimbus_no, '') != 'Direct Sale',
            Entry.client_category == 'Booking Delivery'
        )
    ).group_by(func.coalesce(Entry.booked_material, Entry.material)).all()
    total_dispatched_map = {}
    for r in all_dispatches:
        mat_key = _material_norm_key(r.booked_mat)
        if not mat_key:
            continue
        total_dispatched_map[mat_key] = float(total_dispatched_map.get(mat_key, 0) or 0) + float(r.total_dispatched or 0)
        total_material_labels.setdefault(mat_key, (r.booked_mat or '').strip())

    all_materials = {m for m in (set(total_booked_map.keys()) | set(total_dispatched_map.keys())) if m}
    overall_material_summary = []
    overall_remaining_total = 0
    for m in sorted(list(all_materials), key=lambda x: str(total_material_labels.get(x, x)).lower()):
        booked = total_booked_map.get(m, 0)
        dispatched = total_dispatched_map.get(m, 0)
        remaining = booked - dispatched
        overall_material_summary.append({
            'name': total_material_labels.get(m, m),
            'booked': booked,
            'dispatched': dispatched,
            'remaining': remaining
        })
        overall_remaining_total += remaining

    category_options = sorted({c.category for c in clients if c.category})
    for default_cat in ['General', 'Open Khata', 'Walking-Customer', 'Misc']:
        if default_cat not in category_options:
            category_options.append(default_cat)
    category_options = sorted(category_options, key=lambda x: str(x).lower())

    return render_template('decision_ledger.html',
                           overall_material_summary=overall_material_summary,
                           data=paged,
                           q=q,
                           category_filter=category_filter,
                           balance_filter=balance_filter,
                           page=page,
                           per_page=per_page,
                           total=total,
                           total_pages=total_pages,
                           categories=category_options,
                           overall_remaining_total=overall_remaining_total)


@bp.route('/material_ledger/<int:mat_id>')
@login_required
def material_ledger_page(mat_id):
    material = Material.query.get_or_404(mat_id)

    # Fetch all entries
    entries = Entry.query.filter_by(material=material.name, is_void=False).all()

    # Helper to parse date for sorting
    def parse_entry_datetime(e):
        d_str = e.date or ""
        t_str = e.time or "00:00:00"
        try:
            return datetime.strptime(f"{d_str} {t_str}", '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
        try:
            return datetime.strptime(f"{d_str} {t_str}", '%d-%m-%Y %H:%M:%S')
        except ValueError:
            pass
        return datetime.min

    # Sort by Date/Time, then ID to ensure stable sort
    entries.sort(key=lambda x: (parse_entry_datetime(x), x.id))

    history = []
    running_balance = 0

    for e in entries:
        qty_add = e.qty if e.type == 'IN' else 0
        qty_delivered = e.qty if e.type == 'OUT' else 0
        running_balance += (qty_add - qty_delivered)

        date_display = e.date
        try:
            dt = datetime.strptime(e.date, '%Y-%m-%d')
            date_display = dt.strftime('%d-%m-%Y')
        except (ValueError, TypeError):
            pass

        history.append({
            'date': date_display,
            'item': e.material,
            'bill_no': e.bill_no or e.auto_bill_no or '',
            'add': qty_add,
            'delivered': qty_delivered,
            'balance': running_balance
        })

    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    all_materials = Material.query.order_by(Material.name.asc()).all()

    return render_template('material_ledger.html',
                           material=material,
                           history=history,
                           clients=clients,
                           materials=all_materials)


