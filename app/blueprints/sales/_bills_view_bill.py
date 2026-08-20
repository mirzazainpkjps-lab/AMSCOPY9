"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/view_bill/<path:bill_no>')
@login_required
def view_bill(bill_no):
    all_clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    all_materials = Material.query.order_by(Material.name.asc()).all()
    material_ledger_recent = []

    booking, payment, invoice, sale, grn, pending = _lookup_bill(
        bill_no,
        hint_type=request.args.get('src'),
        hint_id=request.args.get('src_id'),
        hint_client_code=request.args.get('client_code'),
        hint_client_name=request.args.get('client_name'),
        hint_entry_id=request.args.get('entry_id')
    )
    entry_hint_id = request.args.get('entry_id')
    hint_type = (request.args.get('src') or '').strip().lower()
    hint_id = (request.args.get('src_id') or '').strip()

    # Entry-driven fallback: if original ref is weak/legacy, try best bill ref from that entry.
    if not (booking or payment or invoice or sale or grn or pending) and entry_hint_id:
        hinted_entry = None
        try:
            hinted_entry = db.session.get(Entry, int(entry_hint_id))
        except Exception:
            hinted_entry = None
        fallback_ref = _entry_best_bill_ref(hinted_entry)
        if fallback_ref and fallback_ref != (bill_no or '').strip():
            booking, payment, invoice, sale, grn, pending = _lookup_bill(
                fallback_ref,
                hint_type='entry',
                hint_id=None,
                hint_client_code=(getattr(hinted_entry, 'client_code', None) if hinted_entry else None),
                hint_client_name=(getattr(hinted_entry, 'client', None) if hinted_entry else None),
                hint_entry_id=entry_hint_id
            )

    # If no explicit source hint was provided and the same ref matches multiple bill types,
    # force user to choose instead of opening a potentially wrong document.
    candidates_map = _bill_lookup_candidates_map(booking, payment, invoice, sale, grn, pending)
    effective_candidates = _effective_collision_candidates(candidates_map)
    if not hint_type and not hint_id and len(effective_candidates) > 1:
        return render_template(
            'bill_collision_resolution.html',
            bill_no=bill_no,
            candidates=effective_candidates
        )

    client = None
    client_balance = 0
    previous_balance = 0
    recent_deliveries = []
    material_ledger_recent = []
    material_stock_summary = []
    direct_sale_rent_reconciliation = None
    delivery_people = None

    if booking or payment or invoice or sale or pending:
        bill_obj_temp = booking or payment or invoice or sale or pending
        c_name = getattr(bill_obj_temp, 'client_name', None)
        c_code = getattr(bill_obj_temp, 'client_code', None)
        if c_code: client = Client.query.filter_by(code=c_code).first()
        if not client and c_name: client = Client.query.filter_by(name=c_name).first()

        if client:
            cutoff_dt = _bill_cutoff_dt_for_snapshot(
                booking=booking,
                payment=payment,
                invoice=invoice,
                sale=sale,
                pending=pending
            )
            client_balance = _client_balance_as_of(client, cutoff_dt=cutoff_dt)

            effect = 0
            if booking: effect = (booking.amount or 0) - (booking.paid_amount or 0)
            elif sale: effect = (sale.amount or 0) - (getattr(sale, 'discount', 0) or 0) - (sale.paid_amount or 0)
            elif payment: effect = -(payment.amount or 0)
            elif invoice: effect = invoice.balance or 0
            elif pending: effect = pending.amount or 0

            previous_balance = client_balance - effect

            is_booking_flow = False
            if booking:
                is_booking_flow = True
            elif sale and normalize_sale_category(getattr(sale, 'category', None)) in ['Booking Delivery', 'Mixed Transaction']:
                is_booking_flow = True
            elif invoice and getattr(invoice, 'direct_sales', None):
                for ds in invoice.direct_sales:
                    if normalize_sale_category(ds.category) in ['Booking Delivery', 'Mixed Transaction']:
                        is_booking_flow = True
                        break

            delivery_query = Entry.query.filter(
                (Entry.client_code == client.code) | (Entry.client == client.name),
                Entry.type == 'OUT',
                Entry.is_void == False
            )
            if is_booking_flow:
                delivery_query = delivery_query.filter(Entry.client_category == 'Booking Delivery')
            delivery_rows = delivery_query.all()
            if cutoff_dt:
                filtered_rows = []
                for d in delivery_rows:
                    d_dt = _parse_dt_safe(f"{d.date} {d.time}") or _parse_dt_safe(d.date) or datetime.min
                    if d_dt <= cutoff_dt:
                        filtered_rows.append(d)
                delivery_rows = filtered_rows
            recent_deliveries = sorted(
                delivery_rows,
                key=lambda d: (
                    _parse_dt_safe(f"{d.date} {d.time}") or _parse_dt_safe(d.date) or datetime.min,
                    d.id or 0
                ),
                reverse=True
            )[:5]
            if is_booking_flow:
                material_ledger_recent = _material_ledger_recent(
                    client,
                    only_booking=True,
                    limit_per_material=5,
                    cutoff_dt=cutoff_dt
                )

            bill_refs = []
            if booking:
                bill_refs = [booking.manual_bill_no, booking.auto_bill_no, f"BK-{booking.id}"]
            elif payment:
                bill_refs = [payment.manual_bill_no, payment.auto_bill_no, f"PAY-{payment.id}"]
            elif sale:
                bill_refs = [sale.manual_bill_no, sale.auto_bill_no, f"DS-{sale.id}", f"CSH-{sale.id}"]
                if getattr(sale, 'invoice', None) and sale.invoice and getattr(sale.invoice, 'invoice_no', None):
                    bill_refs.append(sale.invoice.invoice_no)
            elif invoice:
                bill_refs = [invoice.invoice_no]
                if getattr(invoice, 'direct_sales', None):
                    for ds in invoice.direct_sales:
                        bill_refs.extend([ds.manual_bill_no, ds.auto_bill_no, f"DS-{ds.id}", f"CSH-{ds.id}"])
            elif pending:
                bill_refs = [pending.bill_no]

            if is_booking_flow:
                material_stock_summary = _material_ledger_current_summary(material_ledger_recent, bill_refs)
            else:
                material_stock_summary = []

    if booking:
        if not (getattr(booking, 'driver_name', None) or '').strip():
            inferred_driver = _infer_driver_name_from_refs(
                [booking.manual_bill_no, booking.auto_bill_no, f"BK-{booking.id}"],
                allow_booking=True
            )
            if inferred_driver:
                setattr(booking, 'driver_name', inferred_driver)
        tx_code, tx_label, tx_note = _resolve_transaction_type('Booking', booking, entry_hint_id=entry_hint_id)
        return render_template('view_bill.html', bill=booking, type='Booking', items=booking.items, client=client, client_balance=client_balance, previous_balance=previous_balance, recent_deliveries=recent_deliveries, material_ledger_recent=material_ledger_recent, material_stock_summary=material_stock_summary, clients=all_clients, materials=all_materials, transaction_type_code=tx_code, transaction_type_label=tx_label, transaction_type_note=tx_note, pk_now=pk_now)
    if payment:
        tx_code, tx_label, tx_note = _resolve_transaction_type('Payment', payment, entry_hint_id=entry_hint_id)
        return render_template('payment_receipt.html', bill=payment, type='Payment', items=[], client=client, client_balance=client_balance, previous_balance=previous_balance, recent_deliveries=recent_deliveries, material_ledger_recent=material_ledger_recent, material_stock_summary=material_stock_summary, clients=all_clients, materials=all_materials, transaction_type_code=tx_code, transaction_type_label=tx_label, transaction_type_note=tx_note, pk_now=pk_now)
    if sale:
        if not (sale.driver_name or '').strip():
            inferred_driver = _infer_driver_name_from_refs(_direct_sale_bill_refs(sale))
            if inferred_driver:
                sale.driver_name = inferred_driver
        delivery_people = []
        for alloc in (getattr(sale, 'delivery_person_allocations', None) or []):
            if getattr(alloc, 'is_void', False):
                continue
            dp = getattr(alloc, 'delivery_person', None)
            name = (getattr(dp, 'name', None) or '').strip()
            if name:
                delivery_people.append(name)
        if not delivery_people and (sale.driver_name or '').strip():
            delivery_people = [sale.driver_name.strip()]
        rent_row = DeliveryRent.query.filter_by(sale_id=sale.id, is_void=False).order_by(DeliveryRent.id.desc()).first()
        sale_items_payload = [
            {
                'product_name': it.product_name,
                'qty': it.qty,
                'price_at_time': it.price_at_time
            }
            for it in (sale.items or [])
        ]
        fallback_delivery_cost = float(rent_row.amount or 0) if rent_row else 0.0
        effective_delivery_cost = float(getattr(sale, 'delivery_rent_cost', 0) or 0)
        if effective_delivery_cost <= 0:
            effective_delivery_cost = fallback_delivery_cost
        calc_rec = _rent_reconciliation_from_items(
            sale_items_payload,
            delivery_rent_cost=effective_delivery_cost,
            client_name=sale.client_name
        )
        direct_sale_rent_reconciliation = {
            'rent_item_revenue': float(getattr(sale, 'rent_item_revenue', 0) or calc_rec['rent_item_revenue']),
            'delivery_rent_cost': float(getattr(sale, 'delivery_rent_cost', 0) or calc_rec['delivery_rent_cost']),
            'rent_variance_loss': float(getattr(sale, 'rent_variance_loss', 0) or calc_rec['rent_variance_loss'])
        }
        # Preserve alternate booked material display on the bill by using ledger entry mapping
        sale_entries = Entry.query.filter(
            Entry.source_module == 'sales',
            Entry.source_table == 'direct_sale',
            Entry.source_id == sale.id,
            Entry.is_void == False
        ).order_by(Entry.id.asc()).all()
        sale_entry_map = {}
        for e in sale_entries:
            key = ((e.material or '').strip(), float(e.qty or 0))
            sale_entry_map.setdefault(key, []).append(e)

        bill_items = []
        for it in (sale.items or []):
            key = ((it.product_name or '').strip(), float(it.qty or 0))
            entry = sale_entry_map.get(key, []).pop(0) if sale_entry_map.get(key) else None
            name = it.product_name
            if entry and entry.booked_material and entry.material and entry.booked_material.strip() != entry.material.strip():
                name = f"{entry.booked_material.strip()} > ALT > {entry.material.strip()}"
            bill_items.append({
                'name': name,
                'qty': it.qty,
                'price_at_time': it.price_at_time
            })

        tx_code, tx_label, tx_note = _resolve_transaction_type('DirectSale', sale, entry_hint_id=entry_hint_id)
        return render_template('view_bill.html', bill=sale, type='DirectSale', items=bill_items, client=client, client_balance=client_balance, previous_balance=previous_balance, recent_deliveries=recent_deliveries, material_ledger_recent=material_ledger_recent, material_stock_summary=material_stock_summary, clients=all_clients, materials=all_materials, transaction_type_code=tx_code, transaction_type_label=tx_label, transaction_type_note=tx_note, direct_sale_rent_reconciliation=direct_sale_rent_reconciliation, delivery_people=delivery_people, pk_now=pk_now)
    if invoice:
        invoice.amount = invoice.total_amount
        driver_name = ''
        if getattr(invoice, 'direct_sales', None):
            for ds in invoice.direct_sales:
                if (ds.driver_name or '').strip():
                    driver_name = ds.driver_name.strip()
                    break
            if not driver_name:
                refs = []
                for ds in invoice.direct_sales:
                    refs.extend(_direct_sale_bill_refs(ds))
                driver_name = _infer_driver_name_from_refs(list(set(refs)), allow_booking=True)
        if driver_name:
            setattr(invoice, 'driver_name', driver_name)
        delivery_people = []
        if getattr(invoice, 'direct_sales', None):
            for ds in invoice.direct_sales:
                for alloc in (getattr(ds, 'delivery_person_allocations', None) or []):
                    if getattr(alloc, 'is_void', False):
                        continue
                    dp = getattr(alloc, 'delivery_person', None)
                    name = (getattr(dp, 'name', None) or '').strip()
                    if name:
                        delivery_people.append(name)
        if not delivery_people and (getattr(invoice, 'driver_name', None) or '').strip():
            delivery_people = [invoice.driver_name.strip()]
        # Calculate discount from linked sales
        invoice_discount = 0
        if getattr(invoice, 'direct_sales', None):
            invoice_discount = sum((getattr(ds, 'discount', 0) or 0) for ds in invoice.direct_sales)
        invoice.discount = invoice_discount
        invoice.paid_amount = max(0, (invoice.total_amount or 0) - invoice_discount - (invoice.balance or 0))
        invoice.date_posted = _parse_dt_safe(getattr(invoice, 'created_at', None)) or (datetime.combine(invoice.date, datetime.min.time()) if invoice.date else None)

        items = []
        if getattr(invoice, 'entries', None) and invoice.entries:
            entry_total_qty = sum(float(e.qty or 0) for e in invoice.entries)
            inferred_rate = (float(invoice.total_amount or 0) / entry_total_qty) if entry_total_qty > 0 else 0
            items = []
            for e in invoice.entries:
                name = (e.material or '').strip()
                if e.booked_material and e.material and e.booked_material.strip() != e.material.strip():
                    name = f"{e.booked_material.strip()} > ALT > {e.material.strip()}"
                items.append({
                    'name': name,
                    'qty': e.qty,
                    'price_at_time': inferred_rate
                })
        elif getattr(invoice, 'direct_sales', None) and invoice.direct_sales:
            ds = invoice.direct_sales[0]
            # Preserve item rates so line amount matches invoice totals on first-open.
            items = [
                {
                    'name': it.product_name,
                    'qty': it.qty,
                    'price_at_time': (it.price_at_time or 0)
                }
                for it in ds.items
            ]
        tx_code, tx_label, tx_note = _resolve_transaction_type('Invoice', invoice, entry_hint_id=entry_hint_id)
        return render_template('view_bill.html', bill=invoice, type='Invoice', items=items, client=client, client_balance=client_balance, previous_balance=previous_balance, recent_deliveries=recent_deliveries, material_ledger_recent=material_ledger_recent, material_stock_summary=material_stock_summary, clients=all_clients, materials=all_materials, transaction_type_code=tx_code, transaction_type_label=tx_label, transaction_type_note=tx_note, delivery_people=delivery_people, pk_now=pk_now)
    if grn:
        grn.amount = calculate_grn_total(grn) + (grn.discount or 0)
        grn.paid_amount = grn.paid_amount or 0
        tx_code, tx_label, tx_note = _resolve_transaction_type('GRN', grn, entry_hint_id=entry_hint_id)
        return render_template('view_bill.html', bill=grn, type='GRN', items=grn.items, client=None, client_balance=0, previous_balance=0, recent_deliveries=[], material_ledger_recent=[], material_stock_summary=[], clients=all_clients, materials=all_materials, transaction_type_code=tx_code, transaction_type_label=tx_label, transaction_type_note=tx_note, pk_now=pk_now)
    if pending:
        pending_bill_view = SimpleNamespace(
            manual_bill_no=pending.bill_no,
            auto_bill_no='',
            invoice_no='',
            date_posted=_parse_dt_safe(pending.created_at),
            client_name=pending.client_name,
            client_code=pending.client_code,
            amount=pending.amount or 0,
            paid_amount=0,
            reason=pending.reason or '',
            nimbus_no=pending.nimbus_no or '',
            method='',
            photo_path=''
        )
        tx_code, tx_label, tx_note = _resolve_transaction_type('PendingBill', pending_bill_view, entry_hint_id=entry_hint_id)
        return render_template('view_bill.html', bill=pending_bill_view, type='PendingBill', items=[], client=client, client_balance=client_balance, previous_balance=previous_balance, recent_deliveries=recent_deliveries, material_ledger_recent=material_ledger_recent, material_stock_summary=material_stock_summary, clients=all_clients, materials=all_materials, transaction_type_code=tx_code, transaction_type_label=tx_label, transaction_type_note=tx_note, pk_now=pk_now)

    flash('Bill not found', 'danger')
    return redirect(url_for('index'))

