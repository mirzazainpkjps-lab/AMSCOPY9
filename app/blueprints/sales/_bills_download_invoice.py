"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/download_invoice/<path:bill_no>')
@login_required
def download_invoice(bill_no):
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

    candidates_map = _bill_lookup_candidates_map(booking, payment, invoice, sale, grn, pending)
    effective_candidates = _effective_collision_candidates(candidates_map)
    if not hint_type and not hint_id and len(effective_candidates) > 1:
        flash('Multiple records match this bill reference. Please choose the exact document type first.', 'warning')
        return redirect(url_for('view_bill', bill_no=bill_no))

    bill_obj = None
    bill_type = ''
    items = []

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
        bill_obj = booking
        bill_type = 'Booking'
        items = booking.items
        if not (getattr(booking, 'driver_name', None) or '').strip():
            inferred_driver = _infer_driver_name_from_refs(
                [booking.manual_bill_no, booking.auto_bill_no, f"BK-{booking.id}"],
                allow_booking=True
            )
            if inferred_driver:
                setattr(booking, 'driver_name', inferred_driver)
    elif payment:
        bill_obj = payment
        bill_type = 'Payment'
    elif sale:
        bill_obj = sale
        bill_type = 'DirectSale'
        sale_entries = Entry.query.filter(
            Entry.source_module == 'sales',
            Entry.source_table == 'direct_sale',
            Entry.source_id == sale.id,
            Entry.is_void == False
        ).order_by(Entry.id.asc()).all()
        entry_map = {}
        for e in sale_entries:
            key = ((e.material or '').strip(), float(e.qty or 0))
            entry_map.setdefault(key, []).append(e)

        items = []
        for it in (sale.items or []):
            key = ((it.product_name or '').strip(), float(it.qty or 0))
            entry = entry_map.get(key, []).pop(0) if entry_map.get(key) else None
            name = it.product_name
            if entry and entry.booked_material and entry.material and entry.booked_material.strip() != entry.material.strip():
                name = f"{entry.booked_material.strip()} > ALT > {entry.material.strip()}"
            items.append({'name': name, 'qty': it.qty, 'price_at_time': it.price_at_time})

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
    elif invoice:
        bill_obj = invoice
        bill_type = 'Invoice'
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
    elif grn:
        bill_obj = grn
        bill_type = 'GRN'
        grn.amount = calculate_grn_total(grn) + (grn.discount or 0)
        grn.paid_amount = grn.paid_amount or 0
        items = grn.items
    elif pending:
        bill_obj = SimpleNamespace(
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
        bill_type = 'PendingBill'
        items = []

    if not bill_obj:
        flash('Bill not found for download', 'danger')
        return redirect(url_for('index'))

    action = (request.args.get('action') or 'download').lower()
    disposition = 'inline' if action in ['print', 'view'] else 'attachment'
    tx_code, tx_label, tx_note = _resolve_transaction_type(bill_type, bill_obj, entry_hint_id=entry_hint_id)
    section_map = {
        'Booking': 'BOOKING',
        'Payment': 'PAYMENT',
        'DirectSale': 'DIRECTSALE',
        'Invoice': 'INVOICE',
        'GRN': 'GRN',
        'PendingBill': 'PENDINGBILL'
    }
    section = section_map.get(bill_type, 'BILL')
    template_name = 'view_bill.html'
    if bill_type == 'Payment':
        template_name = 'payment_receipt.html'

    rendered = render_template(
        template_name,
        bill=bill_obj,
        type=bill_type,
        items=items,
        client=client,
        client_balance=client_balance,
        previous_balance=previous_balance,
        recent_deliveries=recent_deliveries,
        material_ledger_recent=material_ledger_recent,
        material_stock_summary=material_stock_summary,
        transaction_type_code=tx_code,
        transaction_type_label=tx_label,
        transaction_type_note=tx_note,
        direct_sale_rent_reconciliation=direct_sale_rent_reconciliation,
        delivery_people=delivery_people,
        pk_now=pk_now,
        auto_print=(action == 'print')
    )

    if action != 'print':
        pdf_response = _try_render_weasy_pdf(
            rendered,
            _download_filename(section, 'pdf'),
            disposition=disposition
        )
        if pdf_response:
            return pdf_response

    response = make_response(rendered)
    fallback_name = _download_filename(section, 'html')
    response.headers['Content-Disposition'] = f'{disposition}; filename={fallback_name}'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

