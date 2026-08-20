"""dispatch — split from ops.py."""
from ._common import *  # noqa

@bp.route('/add_record', methods=['POST'])
@login_required
def add_record():
    entry_date = request.form.get('date') or pk_now().strftime('%Y-%m-%d')

    if current_user.role == 'user' and entry_date != pk_now().strftime('%Y-%m-%d'):
        flash('Permission Denied: Standard users cannot add back-dated records.', 'danger')
        return redirect(url_for('index'))

    now = pk_now()
    client_name = request.form.get('client', '').strip()
    client_code = None
    client_obj = None
    note = request.form.get('note', '').strip()
    driver_name = (request.form.get('driver_name') or '').strip()

    client_obj = get_client_by_input(client_name)
    if client_obj:
        client_code = client_obj.code
        client_name = client_obj.name

    entry_type = request.form.get('type', 'IN')
    if entry_type == 'OUT' and not driver_name:
        flash('Driver name is required for delivery dispatch.', 'danger')
        return redirect(url_for('dispatching'))
    if entry_type == 'OUT':
        get_or_create_delivery_person(driver_name)

    # For OUT dispatches to unknown clients, redirect to Direct Sale
    if entry_type == 'OUT' and not client_obj:
        flash('Unknown client: For cash customers, please use the Direct Sale form.', 'warning')
        return redirect(url_for('direct_sales_page', client_name=client_name or ''))

    if entry_type == 'OUT' and client_obj:
        mat_input = request.form.get('material', '')
        try:
            mat_obj = resolve_transaction_material(
                material_id=request.form.get('material_id'),
                typed_text=mat_input,
                require_active=True,
            )
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(url_for('dispatching'))
        if not mat_obj:
            flash(MATERIAL_NOT_SELECTED_MSG, 'danger')
            return redirect(url_for('dispatching'))
        mat_name = mat_obj.name
        mat_key = _material_norm_key(mat_name)

        try:
            req_qty = float(request.form.get('qty', 0) or 0)
        except ValueError:
            req_qty = 0

        booked = 0.0
        booking_items = BookingItem.query.join(Booking).filter(
            func.lower(func.trim(Booking.client_name)) == func.lower(func.trim(client_obj.name)),
            Booking.is_void == False
        ).all()
        for item in booking_items:
            if _material_norm_key(item.material_name) == mat_key:
                booked += float(item.qty or 0)

        dispatched = 0.0
        dispatch_entries = Entry.query.filter(
            or_(Entry.client_code == client_obj.code,
                func.lower(func.trim(Entry.client)) == func.lower(func.trim(client_obj.name))),
            Entry.is_void == False,
            Entry.type == 'OUT',
            not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
        ).all()
        for entry in dispatch_entries:
            if _material_norm_key(entry.booked_material or entry.material) == mat_key:
                dispatched += float(entry.qty or 0)

        remaining = booked - dispatched

        if req_qty > remaining:
            flash(f"Cannot dispatch {req_qty} bags. Only {remaining} bags available from booking. (Booked: {booked}, Dispatched: {dispatched})", 'danger')
            return redirect(url_for('dispatching'))

    # Auto-mark as Booking Delivery if fulfilling a booking
    nimbus_no_val = request.form.get('nimbus_no', '').strip()
    if entry_type == 'OUT' and client_obj and not nimbus_no_val:
        nimbus_no_val = "Booking Delivery"

    # Resolve material for entry
    mat_input = request.form.get('material', '')
    mat_obj = get_material_by_input(mat_input)
    entry_material_name = mat_obj.name if mat_obj else mat_input
    entry = Entry(date=entry_date,
                  time=now.strftime('%H:%M:%S'),
                  type=entry_type,
                  material=entry_material_name,
                  client=client_name,
                  client_code=client_code,
                  qty=float(request.form.get('qty', 0) or 0),
                  bill_no=request.form.get('bill_no', '').strip(),
                  nimbus_no=nimbus_no_val,
                  created_by=current_user.username,
                  client_category=client_obj.category if client_obj else None,
                  driver_name=(driver_name if entry_type == 'OUT' else None),
                  note=note)
    db.session.add(entry)
    db.session.flush()

    # Update material stock with correct direction:
    # IN increases stock, OUT decreases stock.
    if mat_obj:
        if entry_type == 'IN':
            mat_obj.total = (mat_obj.total or 0) + entry.qty
        elif entry_type == 'OUT':
            mat_obj.total = (mat_obj.total or 0) - entry.qty

    hv = request.form.get('has_bill')
    has_bill = True if hv is None else hv in ['on', '1', 'true', 'True']

    unit_price = (mat_obj.unit_price if mat_obj else 0) or 0
    amount = float(entry.qty) * float(unit_price)

    create_invoice = bool(request.form.get('create_invoice'))

    if client_obj and getattr(client_obj, 'require_manual_invoice', False) and entry_type == 'OUT' and not entry.bill_no and not create_invoice:
        db.session.rollback()
        flash('Manual invoice required for this client.', 'danger')
        return redirect(url_for('dispatching'))

    invoice_no = None
    inv = None

    # Only create Invoice for non-OUT entries (e.g. IN/Receiving)
    # For OUT entries (Dispatching), we are fulfilling bookings which already have financial records.
    if entry_type != 'OUT' and has_bill and (create_invoice or entry.bill_no):
        if entry.bill_no:
            invoice_no = entry.bill_no
            is_manual = True
        else:
            invoice_no = get_next_bill_no(AUTO_BILL_NAMESPACES['ENTRY'])
            entry.auto_bill_no = invoice_no
            is_manual = False

        existing_global = Invoice.query.filter_by(invoice_no=invoice_no).first()
        if existing_global and not is_manual:
            while Invoice.query.filter_by(invoice_no=invoice_no).first():
                invoice_no = get_next_bill_no(AUTO_BILL_NAMESPACES['ENTRY'])
            entry.auto_bill_no = invoice_no
        elif existing_global and is_manual:
            if existing_global.client_code != entry.client_code:
                db.session.rollback()
                flash(f'Invoice number "{invoice_no}" is already used by another client.', 'danger')
                return redirect(url_for('dispatching'))

        inv = Invoice.query.filter_by(invoice_no=invoice_no, client_code=entry.client_code).first()
        if inv:
            inv.client_name = entry.client
            inv.total_amount = amount
            inv.balance = amount
            inv.is_cash = bool(request.form.get('track_as_cash'))
            inv.date = datetime.strptime(entry.date, '%Y-%m-%d').date() if entry.date else pk_now().date()
            inv.note = note
        else:
            inv = Invoice(client_code=entry.client_code,
                          client_name=entry.client,
                          invoice_no=invoice_no,
                          is_manual=is_manual,
                          date=datetime.strptime(entry.date, '%Y-%m-%d').date() if entry.date else pk_now().date(),
                          total_amount=amount,
                          balance=amount,
                          is_cash=bool(request.form.get('track_as_cash')),
                          note=note,
                          created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                          created_by=current_user.username)
            db.session.add(inv)
            db.session.flush()
        entry.invoice_id = inv.id

    # Pending Bill logic for OUT entries REMOVED
    # Dispatching (OUT) is now strictly for booking fulfillment, so no new financial bills are created.
    # The financial obligation is tracked via the original Booking and its PendingBill.

    db.session.commit()
    flash("Record Saved", "success")
    return redirect(url_for('index'))

