"""profit — split from reports.py."""
from ._common import *  # noqa

@bp.route('/profit_reports')
@login_required
def profit_reports():
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    material_query = request.args.get('material', '').strip()
    client_query = request.args.get('client', '').strip()
    grn_id_raw = (request.args.get('grn_id') or '').strip()
    entry_metric = (request.args.get('metric') or '').strip().lower()
    view_mode = (request.args.get('view') or '').strip().lower()
    template_name = 'profit_entries.html' if view_mode == 'entries' else 'profit_reports.html'

    today_str = pk_today().strftime('%Y-%m-%d')
    month_start_str = pk_today().replace(day=1).strftime('%Y-%m-%d')
    if not start_date:
        start_date = month_start_str
    if not end_date:
        end_date = today_str

    def _safe_parse_date(value):

        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except Exception:
            return None

    start_dt = _safe_parse_date(start_date)
    end_dt = _safe_parse_date(end_date)

    if not start_dt or not end_dt:
        flash('Invalid date format. Please use YYYY-MM-DD.', 'danger')
        return redirect(url_for('profit_reports'))

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date = end_dt.strftime('%Y-%m-%d')

    def _safe_pct(num, den):
        if not den:
            return 0.0
        return (float(num or 0) / float(den or 0)) * 100.0

    resolved_client = client_query
    resolved_client_obj = None
    if client_query:
        code_match = Client.query.filter(Client.code.ilike(f'%{client_query}%')).first()
        if code_match:
            resolved_client_obj = code_match
            resolved_client = code_match.name
        else:
            resolved_client_obj = Client.query.filter(func.lower(func.trim(Client.name)) == client_query.lower()).first()

    purchase_query = db.session.query(GRNItem, GRN).join(GRN, GRNItem.grn_id == GRN.id).filter(
        GRN.is_void == False,
        GRNItem.is_void == False,
    )
    if material_query:
        purchase_query = purchase_query.filter(GRNItem.mat_name.ilike(f'%{material_query}%'))

    purchase_index = {}
    purchase_cost_rows = []
    material_unit_cost_index = {}
    # The legacy cost helper orders purchase rows newest-first.  Retain that
    # order so the request-local resolver below has identical date/future
    # fallback semantics without issuing a query for every report line.
    purchase_rows_for_cost = purchase_query.order_by(GRN.date_posted.desc()).all()
    for item, grn in purchase_rows_for_cost:
        mat_name = (item.mat_name or '').strip()
        if not mat_name:
            continue
        mat_key = _norm_text(mat_name)
        posted_dt = grn.date_posted or datetime.min
        rate = float(item.price_at_time or 0)
        purchase_index.setdefault(mat_key, []).append((posted_dt, rate))
        purchase_cost_rows.append((mat_name.casefold(), posted_dt, rate))

    for key in purchase_index:
        purchase_index[key].sort(key=lambda x: x[0])

    material_cost_rows = Material.query.with_entities(Material.name, Material.unit_price).all()
    for name, unit_price in material_cost_rows:
        mk = _norm_text(name)
        if mk and float(unit_price or 0) > 0:
            material_unit_cost_index[mk] = float(unit_price or 0)

    report_cost_cache = {}

    def _report_cost_for_material(material_name, tx_date=None):
        """Query-free equivalent of sales_core._cost_rate_for_material."""
        needle = (material_name or '').strip().casefold()
        date_key = tx_date.isoformat() if hasattr(tx_date, 'isoformat') else str(tx_date or '')
        cache_key = (needle, date_key)
        if cache_key in report_cost_cache:
            return report_cost_cache[cache_key]
        if not needle:
            report_cost_cache[cache_key] = (0.0, False)
            return report_cost_cache[cache_key]

        matching = [row for row in purchase_cost_rows if needle in row[0]]
        if tx_date:
            for _name, posted, rate in matching:
                if posted.date() <= tx_date and rate > 0:
                    report_cost_cache[cache_key] = (rate, True)
                    return report_cost_cache[cache_key]
        if matching and matching[0][2] > 0:
            report_cost_cache[cache_key] = (matching[0][2], True)
            return report_cost_cache[cache_key]
        for name, unit_price in material_cost_rows:
            if needle in (name or '').casefold() and float(unit_price or 0) > 0:
                report_cost_cache[cache_key] = (float(unit_price), True)
                return report_cost_cache[cache_key]
        report_cost_cache[cache_key] = (0.0, False)
        return report_cost_cache[cache_key]

    grn_id = None
    if grn_id_raw:
        try:
            grn_id = int(grn_id_raw)
        except (TypeError, ValueError):
            grn_id = None

    # Build GRN dropdown options (filtered by material + date range when possible).
    grn_filter_options = []
    try:
        grn_options_q = db.session.query(GRN).filter(
            GRN.is_void == False,
            func.date(GRN.date_posted) >= start_date,
            func.date(GRN.date_posted) <= end_date,
        )
        if material_query:
            grn_options_q = grn_options_q.filter(GRN.items.any(GRNItem.is_void == False, GRNItem.mat_name.ilike(f'%{material_query}%')))
        grn_filter_options = grn_options_q.order_by(GRN.date_posted.desc(), GRN.id.desc()).limit(300).all()
    except Exception:
        grn_filter_options = []

    def _bill_ref_variants(ref_value):
        ref = (ref_value or '').strip()
        if not ref:
            return set()
        variants = {ref}
        if ref.startswith('#') and len(ref) > 1:
            variants.add(ref[1:])
        elif ref.isdigit():
            variants.add(f"#{ref}")
        return {v.strip().lower() for v in variants if v}

    def _add_waive_to_map(target_map, bill_ref, amount):
        amt = float(amount or 0)
        if amt <= 0:
            return
        variants = _bill_ref_variants(bill_ref)
        if not variants:
            return
        for k in variants:
            target_map[k] = target_map.get(k, 0.0) + amt

    # Build waive-off(loss) index by bill reference.
    waive_off_by_bill = {}
    waive_query = WaiveOff.query.filter(
        WaiveOff.is_void == False,
        func.date(WaiveOff.date_posted) >= start_date,
        func.date(WaiveOff.date_posted) <= end_date
    )
    # DirectSale discounts are handled from DirectSale rows; avoid double counting.
    waive_query = waive_query.filter(
        ~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%')
    )
    # Ignore orphan rows that reference a deleted payment.
    waive_query = waive_query.filter(
        or_(
            WaiveOff.payment_id.is_(None),
            exists().where(Payment.id == WaiveOff.payment_id)
        )
    )
    if resolved_client:
        waive_query = waive_query.filter(WaiveOff.client_name.ilike(f'%{resolved_client}%'))
    waive_rows = waive_query.all()
    represented_payment_ids = set()
    waive_events = []
    for w in waive_rows:
        if w.payment_id:
            represented_payment_ids.add(w.payment_id)
        amount = float(w.amount or 0)
        _add_waive_to_map(waive_off_by_bill, w.bill_no, amount)
        waive_events.append({
            'client_norm': _norm_text(w.client_name),
            'ref_variants': _bill_ref_variants(w.bill_no),
            'amount': amount
        })

    # Legacy fallback: Payment.discount rows not represented in waive_off table.
    legacy_discount_q = Payment.query.filter(Payment.is_void == False, Payment.discount > 0)
    legacy_discount_q = legacy_discount_q.filter(
        func.date(Payment.date_posted) >= start_date,
        func.date(Payment.date_posted) <= end_date
    )
    if resolved_client:
        if resolved_client_obj:
            legacy_discount_q = legacy_discount_q.filter(or_(
                Payment.client_id == resolved_client_obj.id,
                and_(Payment.client_id.is_(None), Payment.client_name.ilike(f'%{resolved_client}%')),
            ))
        else:
            legacy_discount_q = legacy_discount_q.filter(Payment.client_name.ilike(f'%{resolved_client}%'))
    for p in legacy_discount_q.all():
        if p.id in represented_payment_ids:
            continue
        bill_ref = p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}"
        amount = float(p.discount or 0)
        _add_waive_to_map(waive_off_by_bill, bill_ref, amount)
        waive_events.append({
            'client_norm': _norm_text(p.client_name),
            'ref_variants': _bill_ref_variants(bill_ref),
            'amount': amount
        })

    def _waive_for_bill(ref_value):
        total = 0.0
        for k in _bill_ref_variants(ref_value):
            total += float(waive_off_by_bill.get(k, 0.0) or 0.0)
        return total

    transactions = []

    # Bulk index: GRNItem.id -> GRN info (for GRN-basis sales profit reporting).
    direct_sale_grn_map = {}

    booking_query = db.session.query(BookingItem, Booking).join(
        Booking, BookingItem.booking_id == Booking.id
    ).filter(
        Booking.is_void == False,
        func.date(Booking.date_posted) >= start_date,
        func.date(Booking.date_posted) <= end_date
    )
    if resolved_client:
        booking_query = booking_query.filter(Booking.client_name.ilike(f'%{resolved_client}%'))
    if material_query:
        booking_query = booking_query.filter(BookingItem.material_name.ilike(f'%{material_query}%'))

    booking_rows = booking_query.all()
    booking_gross_map = {}
    booking_waive_map = {}
    for item, booking in booking_rows:
        item_value = float(item.qty or 0) * float(item.price_at_time or 0)
        booking_gross_map[booking.id] = booking_gross_map.get(booking.id, 0) + item_value
        if booking.id not in booking_waive_map:
            booking_ref = booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
            booking_waive_map[booking.id] = _waive_for_bill(booking_ref)

    booking_gross_total = 0.0
    for item, booking in booking_rows:
        qty = float(item.qty or 0)
        sale_rate = float(item.price_at_time or 0)
        gross_revenue = qty * sale_rate
        booking_gross_total += gross_revenue
        booking_gross = float(booking_gross_map.get(booking.id, 0) or 0)
        booking_discount = float(booking.discount or 0)
        booking_waive = float(booking_waive_map.get(booking.id, 0) or 0)
        total_adjustment = booking_discount + booking_waive
        discount_share = (gross_revenue / booking_gross) * total_adjustment if booking_gross > 0 else 0
        net_revenue = max(0.0, gross_revenue - discount_share)

        tx_date = booking.date_posted.date() if booking.date_posted else None
        cost_rate, cogs_known = _report_cost_for_material(item.material_name, tx_date)
        cogs = qty * cost_rate
        profit = net_revenue - cogs

        transactions.append({
            'date': booking.date_posted,
            'source': 'Booking',
            'reference': booking.manual_bill_no or booking.auto_bill_no or f'BK-{booking.id}',
            'client': booking.client_name,
            '_client_norm': _norm_text(booking.client_name),
            '_ref_variants': _bill_ref_variants(booking.manual_bill_no or booking.auto_bill_no or f'BK-{booking.id}'),
            'material': item.material_name,
            'qty': qty,
            'sale_rate': sale_rate,
            'cost_rate': cost_rate,
            'discount_loss': discount_share,
            'revenue': net_revenue,
            'cogs': cogs,
            'profit': profit,
            'is_loss': profit < 0,
            'cogs_known': cogs_known
        })

    direct_query = db.session.query(DirectSaleItem, DirectSale).join(
        DirectSale, DirectSaleItem.sale_id == DirectSale.id
    ).filter(
        DirectSale.is_void == False,
        func.date(DirectSale.date_posted) >= start_date,
        func.date(DirectSale.date_posted) <= end_date
    )
    if resolved_client:
        direct_query = direct_query.filter(DirectSale.client_name.ilike(f'%{resolved_client}%'))
    if material_query:
        direct_query = direct_query.filter(DirectSaleItem.product_name.ilike(f'%{material_query}%'))
    if grn_id:
        # Restrict to direct-sale items linked to the selected GRN.
        direct_query = direct_query.join(
            GRNItem, DirectSaleItem.grn_item_id == GRNItem.id
        ).filter(
            GRNItem.grn_id == grn_id
        )

    direct_rows = direct_query.all()
    # Prime invoice relationships on the identity-mapped sale objects in two
    # bounded queries; bill-reference helpers below can then remain unchanged.
    direct_sale_ids = sorted({int(s.id) for _item, s in direct_rows})
    if direct_sale_ids:
        DirectSale.query.options(selectinload(DirectSale.invoice)).filter(
            DirectSale.id.in_(direct_sale_ids)
        ).all()
    # Build a bulk map for GRN linkage to avoid per-row DB queries.
    try:
        direct_grn_ids = sorted({int(it.grn_item_id) for it, _s in direct_rows if getattr(it, 'grn_item_id', None)})
    except Exception:
        direct_grn_ids = []
    if direct_grn_ids:
        grn_link_rows = db.session.query(GRNItem, GRN).join(
            GRN, GRNItem.grn_id == GRN.id
        ).filter(
            GRNItem.id.in_(direct_grn_ids)
        ).all()
        for gi, grn in grn_link_rows:
            if not gi or not grn:
                continue
            # Keep linkage even if later voided; profit should show "unknown" when cost is invalid.
            direct_sale_grn_map[int(gi.id)] = {
                'grn_id': int(grn.id),
                'grn_bill': (grn.manual_bill_no or grn.auto_bill_no or f"GRN-{grn.id}"),
                'grn_date': grn.date_posted,
                'supplier': (grn.supplier or '').strip(),
                'grn_is_void': bool(getattr(grn, 'is_void', False)),
                'grn_item_is_void': bool(getattr(gi, 'is_void', False)),
                'cost_rate': float(getattr(gi, 'price_at_time', 0) or 0),
            }

    sale_gross_map = {}
    sale_waive_map = {}
    for item, sale in direct_rows:
        sale_category = normalize_sale_category(getattr(sale, 'category', None))
        # Booking-delivery direct sale rows are fulfillment entries for existing bookings.
        # Profit is already recognized on booking lines, so exclude these from direct-sale P/L.
        if sale_category == 'Booking Delivery':
            continue
        if float(item.price_at_time or 0) <= 0:
            continue
        item_value = float(item.qty or 0) * float(item.price_at_time or 0)
        sale_gross_map[sale.id] = sale_gross_map.get(sale.id, 0) + item_value
        if sale.id not in sale_waive_map:
            sale_ref = (_direct_sale_default_bill_ref(sale) or sale.auto_bill_no or f"DS-{sale.id}").strip()
            sale_waive_map[sale.id] = _waive_for_bill(sale_ref)

    direct_gross_total = 0.0
    for item, sale in direct_rows:
        sale_category = normalize_sale_category(getattr(sale, 'category', None))
        if sale_category == 'Booking Delivery':
            continue
        qty = float(item.qty or 0)
        sale_rate = float(item.price_at_time or 0)
        if sale_rate <= 0:
            continue
        gross_revenue = qty * sale_rate
        direct_gross_total += gross_revenue
        sale_gross = float(sale_gross_map.get(sale.id, 0) or 0)
        sale_discount = float(sale.discount or 0)
        sale_waive = float(sale_waive_map.get(sale.id, 0) or 0)
        total_adjustment = sale_discount + sale_waive
        discount_share = (gross_revenue / sale_gross) * total_adjustment if sale_gross > 0 else 0
        net_revenue = max(0.0, gross_revenue - discount_share)

        tx_date = sale.date_posted.date() if sale.date_posted else None
        frozen_cost = float(getattr(item, 'cost_rate_at_sale', 0) or 0)
        cost_rate, cogs_known = (frozen_cost, True) if frozen_cost > 0 else (0.0, False)
        grn_info = direct_sale_grn_map.get(int(item.grn_item_id)) if getattr(item, 'grn_item_id', None) else None
        if not cogs_known and grn_info and not grn_info['grn_is_void'] and not grn_info['grn_item_is_void']:
            grn_cost = float(grn_info.get('cost_rate') or 0)
            if grn_cost > 0:
                cost_rate, cogs_known = grn_cost, True
        if not cogs_known:
            cost_rate, cogs_known = _report_cost_for_material(item.product_name, tx_date)
        cogs = qty * cost_rate
        profit = net_revenue - cogs

        sale_ref = (_direct_sale_default_bill_ref(sale) or sale.auto_bill_no or f"DS-{sale.id}").strip()
        sale_ref_variants = set()
        try:
            for r in _direct_sale_bill_refs(sale):
                sale_ref_variants |= _bill_ref_variants(r)
        except Exception:
            sale_ref_variants = _bill_ref_variants(sale_ref)

        grn_info = direct_sale_grn_map.get(int(item.grn_item_id)) if getattr(item, 'grn_item_id', None) else None
        transactions.append({
            'date': sale.date_posted,
            'source': 'Direct Sale',
            'reference': sale_ref,
            'client': sale.client_name,
            '_client_norm': _norm_text(sale.client_name),
            '_ref_variants': sale_ref_variants,
            'material': item.product_name,
            'qty': qty,
            'sale_rate': sale_rate,
            'cost_rate': cost_rate,
            'discount_loss': discount_share,
            'revenue': net_revenue,
            'cogs': cogs,
            'profit': profit,
            'is_loss': profit < 0,
            'cogs_known': cogs_known,
            'grn_item_id': (int(item.grn_item_id) if getattr(item, 'grn_item_id', None) else None),
            'grn_id': (int(grn_info.get('grn_id')) if grn_info else None),
            'grn_bill': (grn_info.get('grn_bill') if grn_info else None),
            'grn_supplier': (grn_info.get('supplier') if grn_info else None),
        })

    # Delivery-rent variance:
    # - Positive difference (client rent charged > delivery rent cost) => company profit
    # - Negative difference (delivery rent cost > client rent charged) => company loss
    # This adjustment is operational and must not affect client ledger due.
    include_rent_variance = (not material_query) or _is_rent_material_name(material_query)
    if include_rent_variance:
        rent_loss_query = DirectSale.query.filter(
            DirectSale.is_void == False,
            func.date(DirectSale.date_posted) >= start_date,
            func.date(DirectSale.date_posted) <= end_date
        )
        if resolved_client:
            rent_loss_query = rent_loss_query.filter(DirectSale.client_name.ilike(f'%{resolved_client}%'))
        rent_sales = rent_loss_query.options(
            selectinload(DirectSale.items),
            selectinload(DirectSale.invoice),
        ).all()
        rent_sale_ids = [sale.id for sale in rent_sales]
        fallback_rent_by_sale = {}
        if rent_sale_ids:
            for rent_row in DeliveryRent.query.filter(
                DeliveryRent.sale_id.in_(rent_sale_ids),
                DeliveryRent.is_void == False,
            ).order_by(DeliveryRent.id.desc()).all():
                fallback_rent_by_sale.setdefault(rent_row.sale_id, rent_row)

        needs_any_booking_rate = any(
            float(item.qty or 0) > 0
            and float(item.price_at_time or 0) <= 0
            and _is_rent_material_name(item.product_name)
            for sale in rent_sales for item in (sale.items or [])
        )
        rent_client_name_map = {}
        booking_rate_by_client = {}
        if needs_any_booking_rate:
            # Reproduce get_client_by_input's common exact/case-insensitive
            # resolution in memory, then derive all latest booking rates in a
            # single joined query rather than three queries per sale.
            client_rows_for_rent = Client.query.all()
            by_code = {}
            by_name = {}
            by_folded = {}
            for client_row in client_rows_for_rent:
                by_code.setdefault((client_row.code or '').strip(), client_row)
                by_name.setdefault((client_row.name or '').strip(), client_row)
                by_folded.setdefault((client_row.code or '').strip().casefold(), client_row)
                by_folded.setdefault((client_row.name or '').strip().casefold(), client_row)
            for sale in rent_sales:
                value = (sale.client_code or '').strip() or (sale.client_name or '').strip()
                resolved = by_code.get(value) or by_name.get(value) or by_folded.get(value.casefold())
                if not resolved and sale.client_name:
                    resolved = by_folded.get((sale.client_name or '').strip().casefold())
                if resolved:
                    rent_client_name_map[sale.id] = resolved.name

            wanted_client_names = set(rent_client_name_map.values())
            latest_booking_dates = {}
            if wanted_client_names:
                booking_rate_rows = db.session.query(BookingItem, Booking).join(
                    Booking, BookingItem.booking_id == Booking.id
                ).filter(
                    Booking.is_void == False,
                    Booking.client_name.in_(wanted_client_names),
                ).all()
                for booked_item, booked_sale in booking_rate_rows:
                    material_key = _material_norm_key(booked_item.material_name)
                    if not material_key:
                        continue
                    key = (booked_sale.client_name, material_key)
                    posted = booked_sale.date_posted
                    previous = latest_booking_dates.get(key)
                    if key not in booking_rate_by_client or (posted and (not previous or posted > previous)):
                        latest_booking_dates[key] = posted
                        booking_rate_by_client[key] = float(booked_item.price_at_time or 0)
        for sale in rent_sales:
            sale_items_payload = [
                {
                    'product_name': it.product_name,
                    'qty': it.qty,
                    'price_at_time': it.price_at_time
                }
                for it in (sale.items or [])
            ]
            fallback_rent_row = fallback_rent_by_sale.get(sale.id)
            fallback_delivery_cost = float(fallback_rent_row.amount or 0) if fallback_rent_row else 0.0
            effective_delivery_cost = float(getattr(sale, 'delivery_rent_cost', 0) or 0)
            if effective_delivery_cost <= 0:
                effective_delivery_cost = fallback_delivery_cost

            # Most posted rent lines already carry their effective rate.  In
            # that normal case reconciliation is pure arithmetic; preserve the
            # legacy booking-rate lookup only for zero-rate rent lines.
            rent_item_revenue = 0.0
            needs_booking_rate = False
            for rent_item in sale_items_payload:
                if not _is_rent_material_name(rent_item.get('product_name')):
                    continue
                qty = float(rent_item.get('qty') or 0)
                rate = float(rent_item.get('price_at_time') or 0)
                if qty > 0 and rate > 0:
                    rent_item_revenue += qty * rate
                elif qty > 0:
                    needs_booking_rate = True
            if needs_booking_rate:
                rent_item_revenue = 0.0
                canonical_client_name = rent_client_name_map.get(sale.id)
                for rent_item in sale_items_payload:
                    material_name = (rent_item.get('product_name') or '').strip()
                    if not _is_rent_material_name(material_name):
                        continue
                    qty = float(rent_item.get('qty') or 0)
                    rate = float(rent_item.get('price_at_time') or 0)
                    if rate <= 0 and canonical_client_name:
                        rate = float(booking_rate_by_client.get(
                            (canonical_client_name, _material_norm_key(material_name)), 0
                        ) or 0)
                    if qty > 0 and rate > 0:
                        rent_item_revenue += qty * rate
            rent_rec = {
                'rent_item_revenue': rent_item_revenue,
                'delivery_rent_cost': max(0.0, effective_delivery_cost),
            }
            rent_revenue = float(getattr(sale, 'rent_item_revenue', 0) or rent_rec['rent_item_revenue'])
            rent_cost = float(getattr(sale, 'delivery_rent_cost', 0) or rent_rec['delivery_rent_cost'])
            variance = rent_revenue - rent_cost
            if abs(variance) <= 0.0001:
                continue

            sale_ref = sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"
            if getattr(sale, 'invoice', None) and sale.invoice and sale.invoice.invoice_no:
                sale_ref = sale.invoice.invoice_no

            transactions.append({
                'date': sale.date_posted,
                'source': ('Delivery Rent Variance (Company Profit)' if variance > 0 else 'Delivery Rent Variance (Company Loss)'),
                'reference': sale_ref,
                'client': sale.client_name,
                '_client_norm': _norm_text(sale.client_name),
                '_ref_variants': _bill_ref_variants(sale_ref),
                'material': 'Delivery Rent Difference',
                'qty': 0.0,
                'sale_rate': 0.0,
                'cost_rate': 0.0,
                'discount_loss': (abs(variance) if variance < 0 else 0.0),
                'revenue': 0.0,
                'cogs': 0.0,
                'profit': variance,
                'is_loss': variance < 0,
                'cogs_known': True
            })

        waive_q = db.session.query(
            DeliveryPersonPayment,
            SaleDeliveryPerson,
            DirectSale
        ).join(
            SaleDeliveryPerson, DeliveryPersonPayment.allocation_id == SaleDeliveryPerson.id
        ).join(
            DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id
        ).filter(
            DeliveryPersonPayment.is_void == False,
            DeliveryPersonPayment.waive_off_amount > 0,
            func.date(DeliveryPersonPayment.date_posted) >= start_date,
            func.date(DeliveryPersonPayment.date_posted) <= end_date
        )
        if resolved_client:
            waive_q = waive_q.filter(DirectSale.client_name.ilike(f'%{resolved_client}%'))

        for pay, alloc, sale in waive_q.all():
            sale_ref = sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}"
            if getattr(sale, 'invoice', None) and sale.invoice and sale.invoice.invoice_no:
                sale_ref = sale.invoice.invoice_no
            amt = float(pay.waive_off_amount or 0)
            if amt <= 0:
                continue
            transactions.append({
                'date': pay.date_posted,
                'source': 'Delivery Person Waive-Off (Profit)',
                'reference': sale_ref,
                'client': sale.client_name,
                '_client_norm': _norm_text(sale.client_name),
                '_ref_variants': _bill_ref_variants(sale_ref),
                'material': 'Delivery Rent Waive-Off',
                'qty': 0.0,
                'sale_rate': 0.0,
                'cost_rate': 0.0,
                'discount_loss': 0.0,
                'revenue': 0.0,
                'cogs': 0.0,
                'profit': amt,
                'is_loss': False,
                'cogs_known': True
            })

    # Allocate waive-off events that are not directly bill-linked to any transaction row.
    matched_event_idx = set()
    for idx, ev in enumerate(waive_events):
        ev_refs = ev.get('ref_variants') or set()
        ev_client = ev.get('client_norm') or ''
        if not ev_refs:
            continue
        for tx in transactions:
            tx_refs = tx.get('_ref_variants') or set()
            tx_client = tx.get('_client_norm') or ''
            if ev_client and tx_client and ev_client != tx_client:
                continue
            if ev_refs & tx_refs:
                matched_event_idx.add(idx)
                break

    unallocated_by_client = {}
    for idx, ev in enumerate(waive_events):
        if idx in matched_event_idx:
            continue
        ckey = ev.get('client_norm') or ''
        if not ckey:
            continue
        unallocated_by_client[ckey] = unallocated_by_client.get(ckey, 0.0) + float(ev.get('amount') or 0.0)

    for ckey, amount in unallocated_by_client.items():
        if amount <= 0:
            continue
        candidates = [t for t in transactions if (t.get('_client_norm') == ckey and float(t.get('revenue') or 0) > 0)]
        if not candidates:
            # No sale row to allocate against: record standalone loss so it is visible in P/L.
            transactions.append({
                'date': datetime.combine(end_dt, datetime.min.time()),
                'source': 'Waive-Off',
                'reference': f'LOSS-{ckey[:10].upper() or "UNLINKED"}',
                'client': ckey or 'Unlinked',
                'material': '-',
                'qty': 0.0,
                'sale_rate': 0.0,
                'cost_rate': 0.0,
                'discount_loss': amount,
                'revenue': 0.0,
                'cogs': 0.0,
                'profit': -float(amount or 0),
                'is_loss': True,
                'cogs_known': True
            })
            continue
        total_rev = sum(float(t.get('revenue') or 0) for t in candidates)
        if total_rev <= 0:
            transactions.append({
                'date': datetime.combine(end_dt, datetime.min.time()),
                'source': 'Waive-Off',
                'reference': f'LOSS-{ckey[:10].upper() or "UNLINKED"}',
                'client': ckey or 'Unlinked',
                'material': '-',
                'qty': 0.0,
                'sale_rate': 0.0,
                'cost_rate': 0.0,
                'discount_loss': amount,
                'revenue': 0.0,
                'cogs': 0.0,
                'profit': -float(amount or 0),
                'is_loss': True,
                'cogs_known': True
            })
            continue
        allocated = 0.0
        for i, t in enumerate(candidates):
            if i == len(candidates) - 1:
                share = amount - allocated
            else:
                share = (float(t.get('revenue') or 0) / total_rev) * amount
                allocated += share
            t['revenue'] = max(0.0, float(t.get('revenue') or 0) - share)
            t['discount_loss'] = float(t.get('discount_loss') or 0) + float(share or 0)
            t['profit'] = float(t.get('revenue') or 0) - float(t.get('cogs') or 0)
            t['is_loss'] = bool(t.get('cogs_known')) and (float(t.get('profit') or 0) < 0)

    # Finalised account reconciliation differences are explicit P/L events.
    # They are never folded into sale rows because they have their own immutable
    # reconciliation reference and account-ledger adjustment.
    if not resolved_client and not material_query:
        reconciliation_q = AccountTransaction.query.filter(
            AccountTransaction.is_void == False,
            AccountTransaction.transaction_type.in_(['Reconciliation Loss', 'Reconciliation Excess']),
            func.date(AccountTransaction.date_posted) >= start_date,
            func.date(AccountTransaction.date_posted) <= end_date,
        )
        for adjustment in reconciliation_q.order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all():
            amount = float(adjustment.amount or 0)
            is_loss = adjustment.transaction_type == 'Reconciliation Loss'
            account = adjustment.from_account if is_loss else adjustment.to_account
            transactions.append({
                'date': adjustment.date_posted,
                'source': ('Account Reconciliation Loss' if is_loss else 'Account Reconciliation Profit / Excess'),
                'reference': f'RECON-{adjustment.reconciliation_id or adjustment.source_id or adjustment.id}',
                'client': account.name if account else 'Account',
                'material': 'Account Reconciliation',
                'qty': 0.0, 'sale_rate': 0.0, 'cost_rate': 0.0,
                'discount_loss': amount if is_loss else 0.0,
                'revenue': 0.0, 'cogs': 0.0,
                'profit': -amount if is_loss else amount,
                'is_loss': is_loss, 'cogs_known': True,
            })

    # Remove internal helper keys before rendering.
    for t in transactions:
        t.pop('_client_norm', None)
        t.pop('_ref_variants', None)

    transactions.sort(key=lambda x: x['date'] or datetime.min, reverse=True)

    metric_label_map = {
        'revenue': 'Revenue Rows',
        'discount_loss': 'Discount/Loss Rows',
        'cogs': 'Known COGS Rows',
        'net_profit': 'Net Profit/Loss Rows',
        'unknown_cost': 'Unknown Cost Rows',
    }
    metric_help_map = {
        'revenue': 'Shows rows where revenue > 0 from Booking and Direct Sale items.',
        'discount_loss': 'Shows all rows where discount/loss > 0 including bill discounts, waive-off losses, and delivery-rent variance loss.',
        'cogs': 'Shows rows with known material cost used in Estimated COGS.',
        'net_profit': 'Shows rows that contribute to net profit/loss where cost is known (profit = revenue - cogs).',
        'unknown_cost': 'Shows rows with unknown cost (N/A cost), excluded from net profit known-cost calculation.',
    }
    metric_filter_map = {
        'revenue': lambda t: float(t.get('revenue') or 0) > 0,
        'discount_loss': lambda t: float(t.get('discount_loss') or 0) > 0,
        'cogs': lambda t: bool(t.get('cogs_known')) and float(t.get('cogs') or 0) > 0,
        'net_profit': lambda t: bool(t.get('cogs_known')),
        'unknown_cost': lambda t: not bool(t.get('cogs_known')),
    }

    entries_transactions = transactions
    entry_metric_label = ''
    entry_metric_help = ''
    if entry_metric in metric_filter_map:
        entries_transactions = [t for t in transactions if metric_filter_map[entry_metric](t)]
        entry_metric_label = metric_label_map.get(entry_metric, '')
        entry_metric_help = metric_help_map.get(entry_metric, '')

    total_revenue = sum(float(t.get('revenue') or 0) for t in transactions)
    total_discount_loss = sum(float(t.get('discount_loss') or 0) for t in transactions)
    total_cogs = sum(float(t.get('cogs') or 0) for t in transactions if t.get('cogs_known'))
    known_cost_revenue = sum(float(t.get('revenue') or 0) for t in transactions if t.get('cogs_known'))
    unknown_cost_revenue = max(0.0, total_revenue - known_cost_revenue)
    total_profit = sum(float(t.get('profit') or 0) for t in transactions if t.get('cogs_known'))
    unknown_cost_rows = sum(1 for t in transactions if not t.get('cogs_known'))
    profit_rows = sum(1 for t in transactions if t.get('cogs_known') and float(t.get('profit') or 0) >= 0)
    loss_rows = sum(1 for t in transactions if t.get('cogs_known') and float(t.get('profit') or 0) < 0)
    margin_pct = (total_profit / known_cost_revenue * 100.0) if known_cost_revenue > 0 else 0.0
    markup_pct = (total_profit / total_cogs * 100.0) if total_cogs > 0 else 0.0

    # -------------------- GRN-basis sales profit summary (Direct Sale linked items only) --------------------
    grn_sales_summary_map = {}
    for t in transactions:
        if t.get('source') != 'Direct Sale':
            continue
        grn_id = t.get('grn_id')
        if not grn_id:
            continue
        key = int(grn_id)
        row = grn_sales_summary_map.setdefault(key, {
            'grn_id': key,
            'grn_bill': t.get('grn_bill') or f"GRN-{key}",
            'supplier': t.get('grn_supplier') or '',
            'sold_qty': 0.0,
            'revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_revenue': 0.0,
            'rows': 0,
            'unknown_rows': 0,
        })
        row['rows'] += 1
        row['sold_qty'] += float(t.get('qty') or 0)
        row['revenue'] += float(t.get('revenue') or 0)
        row['discount_loss'] += float(t.get('discount_loss') or 0)
        if bool(t.get('cogs_known')):
            row['cogs_known'] += float(t.get('cogs') or 0)
            row['profit_known'] += float(t.get('profit') or 0)
        else:
            row['unknown_rows'] += 1
            row['unknown_revenue'] += float(t.get('revenue') or 0)

    grn_sales_summary = sorted(
        grn_sales_summary_map.values(),
        key=lambda r: (float(r.get('profit_known') or 0), float(r.get('revenue') or 0)),
        reverse=True
    )
    for r in grn_sales_summary:
        known_rev = max(0.0, float(r.get('revenue', 0) - r.get('unknown_revenue', 0)))
        r['margin_pct_known'] = _safe_pct(r.get('profit_known', 0), known_rev)
        r['markup_pct_known'] = _safe_pct(r.get('profit_known', 0), r.get('cogs_known', 0))

    # -------------------- Operational position (date-range level) --------------------
    # 1) Purchase side: GRN qty/value within selected date window.
    purchase_period_query = db.session.query(
        func.sum(GRNItem.qty),
        func.sum(GRNItem.qty * GRNItem.price_at_time)
    ).join(GRN, GRNItem.grn_id == GRN.id).filter(
        GRN.is_void == False,
        GRNItem.is_void == False,
        func.date(GRN.date_posted) >= start_date,
        func.date(GRN.date_posted) <= end_date
    )
    if material_query:
        purchase_period_query = purchase_period_query.filter(GRNItem.mat_name.ilike(f'%{material_query}%'))
    purchase_row = purchase_period_query.first() or (0, 0)
    purchase_qty = float(purchase_row[0] or 0)
    purchase_value = float(purchase_row[1] or 0)
    supplier_purchase_qty = purchase_qty
    supplier_purchase_amount = purchase_value

    purchase_material_breakdown = []
    purchase_material_query = db.session.query(
        GRNItem.mat_name,
        func.sum(GRNItem.qty),
        func.sum(GRNItem.qty * GRNItem.price_at_time)
    ).join(GRN, GRNItem.grn_id == GRN.id).filter(
        GRN.is_void == False,
        GRNItem.is_void == False,
        func.date(GRN.date_posted) >= start_date,
        func.date(GRN.date_posted) <= end_date
    )
    if material_query:
        purchase_material_query = purchase_material_query.filter(GRNItem.mat_name.ilike(f'%{material_query}%'))
    purchase_material_rows = purchase_material_query.group_by(GRNItem.mat_name).all()
    for mat_name, qty_sum, amt_sum in purchase_material_rows:
        purchase_material_breakdown.append({
            'material': (mat_name or '').strip() or '-',
            'qty': float(qty_sum or 0),
            'amount': float(amt_sum or 0),
        })
    purchase_material_breakdown.sort(key=lambda x: x.get('amount', 0), reverse=True)

    # Supplier credit/payable position in selected date window.
    supplier_credit_query = GRN.query.filter(
        GRN.is_void == False,
        func.date(GRN.date_posted) >= start_date,
        func.date(GRN.date_posted) <= end_date
    )
    if material_query:
        supplier_credit_query = supplier_credit_query.filter(
            GRN.items.any(GRNItem.mat_name.ilike(f'%{material_query}%'))
        )
    supplier_credit_total = 0.0
    for g in supplier_credit_query.all():
        supplier_credit_total += float(calculate_grn_total(g) or 0)

    supplier_paid_query = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) >= start_date,
        func.date(SupplierPayment.date_posted) <= end_date
    )
    supplier_paid_total = float(supplier_paid_query.with_entities(func.sum(SupplierPayment.amount)).scalar() or 0)
    supplier_net_payable = float(supplier_credit_total - supplier_paid_total)

    # 2) Delivery side (physical movement): OUT entries qty in the date window.
    delivery_qty_query = db.session.query(func.sum(Entry.qty)).filter(
        Entry.is_void == False,
        Entry.type == 'OUT',
        func.date(Entry.date) >= start_date,
        func.date(Entry.date) <= end_date
    )
    if resolved_client:
        delivery_qty_query = delivery_qty_query.filter(Entry.client.ilike(f'%{resolved_client}%'))
    if material_query:
        delivery_qty_query = delivery_qty_query.filter(Entry.material.ilike(f'%{material_query}%'))
    delivered_qty = float(delivery_qty_query.scalar() or 0)
    delivered_value = float(booking_gross_total + direct_gross_total)
    delivered_material_map = {}
    for t in transactions:
        mat = (t.get('material') or '').strip()
        if not mat or mat == '-':
            continue
        row = delivered_material_map.setdefault(mat, {'material': mat, 'qty': 0.0, 'amount': 0.0})
        row['qty'] += float(t.get('qty') or 0)
        row['amount'] += float(t.get('revenue') or 0)
    delivered_material_breakdown = sorted(delivered_material_map.values(), key=lambda x: x.get('amount', 0), reverse=True)

    # 3) Credit generated in selected period (booking + direct sale), prorated by filtered materials.
    credit_generated_booking = 0.0
    for item, booking in booking_rows:
        row_gross = float(item.qty or 0) * float(item.price_at_time or 0)
        bill_gross = float(booking_gross_map.get(booking.id, 0) or 0)
        if row_gross <= 0 or bill_gross <= 0:
            continue
        bill_credit = max(0.0, float(booking.amount or 0) - float(booking.discount or 0) - float(booking.paid_amount or 0))
        credit_generated_booking += (row_gross / bill_gross) * bill_credit

    credit_generated_sale = 0.0
    sale_paid_from_sales = 0.0
    for item, sale in direct_rows:
        sale_category = normalize_sale_category(getattr(sale, 'category', None))
        if sale_category == 'Booking Delivery':
            continue
        row_gross = float(item.qty or 0) * float(item.price_at_time or 0)
        if row_gross <= 0:
            continue
        sale_gross = float(sale_gross_map.get(sale.id, 0) or 0)
        if sale_gross <= 0:
            continue
        bill_credit = max(0.0, float(sale.amount or 0) - float(sale.discount or 0) - float(sale.paid_amount or 0))
        credit_generated_sale += (row_gross / sale_gross) * bill_credit
        sale_paid_from_sales += (row_gross / sale_gross) * float(sale.paid_amount or 0)

    credit_generated = float(credit_generated_booking + credit_generated_sale)

    # 4) Payments received in selected period.
    payment_received_only_q = Payment.query.filter(
        Payment.is_void == False,
        func.date(Payment.date_posted) >= start_date,
        func.date(Payment.date_posted) <= end_date
    )
    if resolved_client:
        if resolved_client_obj:
            payment_received_only_q = payment_received_only_q.filter(or_(
                Payment.client_id == resolved_client_obj.id,
                and_(Payment.client_id.is_(None), Payment.client_name.ilike(f'%{resolved_client}%')),
            ))
        else:
            payment_received_only_q = payment_received_only_q.filter(Payment.client_name.ilike(f'%{resolved_client}%'))
    payment_received_only = float(payment_received_only_q.with_entities(func.sum(Payment.amount)).scalar() or 0)

    booking_paid_collected = 0.0
    for item, booking in booking_rows:
        row_gross = float(item.qty or 0) * float(item.price_at_time or 0)
        bill_gross = float(booking_gross_map.get(booking.id, 0) or 0)
        if row_gross <= 0 or bill_gross <= 0:
            continue
        booking_paid_collected += (row_gross / bill_gross) * float(booking.paid_amount or 0)

    payment_received_total = float(payment_received_only + booking_paid_collected + sale_paid_from_sales)
    client_cash_received = payment_received_total
    client_credit_generated = credit_generated
    net_credit_movement = float(credit_generated - payment_received_total)
    # Client bill should represent generated bill value (paid-at-bill-time + credit),
    # not plus later payment receipts (which would double count).
    client_total_bill = float(client_credit_generated + booking_paid_collected + sale_paid_from_sales)
    client_total_paid = float(booking_paid_collected + sale_paid_from_sales + payment_received_only)
    client_total_pending = float(max(0.0, client_credit_generated - payment_received_only))
    entries_count = len(entries_transactions if view_mode == 'entries' else transactions)

    # -------------------- Grouped summaries for report analysis --------------------
    material_summary_map = {}
    for row in purchase_material_breakdown:
        mat = (row.get('material') or '').strip() or '-'
        rec = material_summary_map.setdefault(mat, {
            'material': mat,
            'received_qty': 0.0,
            'received_amount': 0.0,
            'sold_qty': 0.0,
            'sold_revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_cost_qty': 0.0,
            'unknown_cost_revenue': 0.0,
            'known_rows': 0,
            'unknown_rows': 0,
        })
        rec['received_qty'] += float(row.get('qty') or 0)
        rec['received_amount'] += float(row.get('amount') or 0)

    date_summary_map = {}
    received_date_rows = db.session.query(
        func.date(GRN.date_posted),
        func.sum(GRNItem.qty),
        func.sum(GRNItem.qty * GRNItem.price_at_time)
    ).join(GRN, GRNItem.grn_id == GRN.id).filter(
        GRN.is_void == False,
        GRNItem.is_void == False,
        func.date(GRN.date_posted) >= start_date,
        func.date(GRN.date_posted) <= end_date
    )
    if material_query:
        received_date_rows = received_date_rows.filter(GRNItem.mat_name.ilike(f'%{material_query}%'))
    received_date_rows = received_date_rows.group_by(func.date(GRN.date_posted)).all()
    for day_key, qty_sum, amt_sum in received_date_rows:
        dkey = str(day_key or '')
        row = date_summary_map.setdefault(dkey, {
            'date': dkey,
            'received_qty': 0.0,
            'received_amount': 0.0,
            'sold_qty': 0.0,
            'sold_revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_cost_revenue': 0.0,
            'known_rows': 0,
            'unknown_rows': 0,
        })
        row['received_qty'] += float(qty_sum or 0)
        row['received_amount'] += float(amt_sum or 0)

    client_summary_map = {}
    client_material_map = {}
    for t in transactions:
        mat = (t.get('material') or '').strip() or '-'
        client_name = (t.get('client') or '').strip() or '-'
        qty = float(t.get('qty') or 0)
        rev = float(t.get('revenue') or 0)
        disc = float(t.get('discount_loss') or 0)
        cogs = float(t.get('cogs') or 0)
        prof = float(t.get('profit') or 0)
        cogs_known_flag = bool(t.get('cogs_known'))
        day_val = t.get('date')
        day_key = day_val.strftime('%Y-%m-%d') if day_val else ''

        mrow = material_summary_map.setdefault(mat, {
            'material': mat,
            'received_qty': 0.0,
            'received_amount': 0.0,
            'sold_qty': 0.0,
            'sold_revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_cost_qty': 0.0,
            'unknown_cost_revenue': 0.0,
            'known_rows': 0,
            'unknown_rows': 0,
        })
        mrow['sold_qty'] += qty
        mrow['sold_revenue'] += rev
        mrow['discount_loss'] += disc
        if cogs_known_flag:
            mrow['cogs_known'] += cogs
            mrow['profit_known'] += prof
            mrow['known_rows'] += 1
        else:
            mrow['unknown_cost_qty'] += qty
            mrow['unknown_cost_revenue'] += rev
            mrow['unknown_rows'] += 1

        drow = date_summary_map.setdefault(day_key, {
            'date': day_key,
            'received_qty': 0.0,
            'received_amount': 0.0,
            'sold_qty': 0.0,
            'sold_revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_cost_revenue': 0.0,
            'known_rows': 0,
            'unknown_rows': 0,
        })
        drow['sold_qty'] += qty
        drow['sold_revenue'] += rev
        drow['discount_loss'] += disc
        if cogs_known_flag:
            drow['cogs_known'] += cogs
            drow['profit_known'] += prof
            drow['known_rows'] += 1
        else:
            drow['unknown_cost_revenue'] += rev
            drow['unknown_rows'] += 1

        crow = client_summary_map.setdefault(client_name, {
            'client': client_name,
            'sold_qty': 0.0,
            'sold_revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_cost_revenue': 0.0,
            'known_rows': 0,
            'unknown_rows': 0,
        })
        crow['sold_qty'] += qty
        crow['sold_revenue'] += rev
        crow['discount_loss'] += disc
        if cogs_known_flag:
            crow['cogs_known'] += cogs
            crow['profit_known'] += prof
            crow['known_rows'] += 1
        else:
            crow['unknown_cost_revenue'] += rev
            crow['unknown_rows'] += 1

        cm_key = (client_name, mat)
        cmrow = client_material_map.setdefault(cm_key, {
            'client': client_name,
            'material': mat,
            'sold_qty': 0.0,
            'sold_revenue': 0.0,
            'discount_loss': 0.0,
            'cogs_known': 0.0,
            'profit_known': 0.0,
            'unknown_cost_revenue': 0.0,
            'known_rows': 0,
            'unknown_rows': 0,
        })
        cmrow['sold_qty'] += qty
        cmrow['sold_revenue'] += rev
        cmrow['discount_loss'] += disc
        if cogs_known_flag:
            cmrow['cogs_known'] += cogs
            cmrow['profit_known'] += prof
            cmrow['known_rows'] += 1
        else:
            cmrow['unknown_cost_revenue'] += rev
            cmrow['unknown_rows'] += 1

    # Note: GRN-basis sales profit summary is built earlier as `grn_sales_summary`.

    material_summary = sorted(material_summary_map.values(), key=lambda x: (x.get('sold_revenue', 0), x.get('received_amount', 0)), reverse=True)
    date_summary = sorted([v for v in date_summary_map.values() if v.get('date')], key=lambda x: x.get('date', ''), reverse=True)
    client_summary = sorted(client_summary_map.values(), key=lambda x: x.get('sold_revenue', 0), reverse=True)
    client_material_summary = sorted(client_material_map.values(), key=lambda x: (x.get('client', ''), -x.get('sold_revenue', 0)))

    for row in material_summary:
        known_rev = max(0.0, float(row.get('sold_revenue', 0) - row.get('unknown_cost_revenue', 0)))
        row['margin_pct_known'] = _safe_pct(row.get('profit_known', 0), known_rev)
        row['markup_pct_known'] = _safe_pct(row.get('profit_known', 0), row.get('cogs_known', 0))
    for row in date_summary:
        known_rev = max(0.0, float(row.get('sold_revenue', 0) - row.get('unknown_cost_revenue', 0)))
        row['margin_pct_known'] = _safe_pct(row.get('profit_known', 0), known_rev)
    for row in client_summary:
        known_rev = max(0.0, float(row.get('sold_revenue', 0) - row.get('unknown_cost_revenue', 0)))
        row['margin_pct_known'] = _safe_pct(row.get('profit_known', 0), known_rev)
    for row in client_material_summary:
        known_rev = max(0.0, float(row.get('sold_revenue', 0) - row.get('unknown_cost_revenue', 0)))
        row['margin_pct_known'] = _safe_pct(row.get('profit_known', 0), known_rev)

    missing_cost_materials = [
        {
            'material': r.get('material'),
            'unknown_rows': int(r.get('unknown_rows', 0) or 0),
            'unknown_qty': float(r.get('unknown_cost_qty', 0) or 0),
            'unknown_revenue': float(r.get('unknown_cost_revenue', 0) or 0),
        }
        for r in material_summary if float(r.get('unknown_cost_revenue', 0) or 0) > 0
    ]

    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()

    return render_template(
        template_name,
        transactions=(entries_transactions if view_mode == 'entries' else transactions),
        start_date=start_date,
        end_date=end_date,
        material=material_query,
        client=client_query,
        grn_id=(str(grn_id) if grn_id else ''),
        grn_filter_options=grn_filter_options,
        clients=clients,
        materials=materials,
        total_revenue=total_revenue,
        total_discount_loss=total_discount_loss,
        total_cogs=total_cogs,
        total_profit=total_profit,
        margin_pct=margin_pct,
        markup_pct=markup_pct,
        profit_rows=profit_rows,
        loss_rows=loss_rows,
        known_cost_revenue=known_cost_revenue,
        unknown_cost_revenue=unknown_cost_revenue,
        unknown_cost_rows=unknown_cost_rows,
        purchase_qty=purchase_qty,
        purchase_value=purchase_value,
        delivered_qty=delivered_qty,
        delivered_value=delivered_value,
        credit_generated=credit_generated,
        payment_received_total=payment_received_total,
        payment_received_only=payment_received_only,
        net_credit_movement=net_credit_movement,
        supplier_purchase_qty=supplier_purchase_qty,
        supplier_purchase_amount=supplier_purchase_amount,
        purchase_material_breakdown=purchase_material_breakdown,
        delivered_material_breakdown=delivered_material_breakdown,
        supplier_credit_total=supplier_credit_total,
        supplier_paid_total=supplier_paid_total,
        supplier_net_payable=supplier_net_payable,
        client_cash_received=client_cash_received,
        client_credit_generated=client_credit_generated,
        client_total_bill=client_total_bill,
        client_total_paid=client_total_paid,
        client_total_pending=client_total_pending,
        entries_count=entries_count,
        view_mode=view_mode,
        entry_metric=entry_metric,
        entry_metric_label=entry_metric_label,
        entry_metric_help=entry_metric_help,
        grn_sales_summary=grn_sales_summary
    )

