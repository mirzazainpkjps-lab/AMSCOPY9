"""bookings — split from sales.py."""
from sqlalchemy.exc import IntegrityError

from ._common import *  # noqa


def _sync_booking_paid_into_account(booking, *, payment_account_id, method='Cash', require_account=True):
    """Post Paid Now into the chosen cash/bank account without a second client Payment row."""
    paid = float(booking.paid_amount or 0)
    method = (method or 'Cash').strip() or 'Cash'
    expected = _payment_expected_account_category(method) or 'cash'
    try:
        acc_id = int(payment_account_id) if str(payment_account_id or '').strip() else None
    except (TypeError, ValueError):
        acc_id = None
    acc = Account.query.get(acc_id) if acc_id else None
    if paid > 0:
        if not acc or not getattr(acc, 'is_active', True):
            if require_account:
                raise ValueError('Select the cash/bank account that should receive Paid Now.')
            # Legacy bookings often have Paid Now with no receive-into account.
            # Leave any existing receipt / account link untouched.
            return None
        acc_cat = (acc.category or '').strip().lower()
        if acc_cat and acc_cat != expected:
            raise ValueError(f'Selected account must be a {expected} account for method "{method}".')
        booking.receive_in_account_id = acc.id
    else:
        booking.receive_in_account_id = None
    bill = booking.manual_bill_no or booking.auto_bill_no or f'BK-{booking.id}'
    marker = f'[SRC:Booking:{booking.id}]'
    note = ' '.join(x for x in [(booking.note or '').strip(), f'Method: {method}', marker] if x).strip()
    _sync_linked_receipt_tx(
        kind='Booking',
        src_id=booking.id,
        to_account_id=(acc.id if acc and paid > 0 else None),
        amount=paid,
        date_posted=booking.date_posted,
        description=f'Booking paid now from {booking.client_name or "Client"} ({bill})',
        note=note,
        is_void=bool(getattr(booking, 'is_void', False)) or paid <= 0,
    )
    return acc


def _collect_booking_item_updates(materials_list, qtys, rates, item_ids=None, material_ids=None):
    """Normalize submitted booking lines against existing Material Master records."""
    rows = []
    for mat, qty, rate, item_id, mid in zip_longest(
        materials_list or [], qtys or [], rates or [], item_ids or [], material_ids or [], fillvalue=''
    ):
        mat_name = str(mat or '').strip()
        qty_val = _to_float_or_zero(qty)
        rate_val = _to_float_or_zero(rate)
        if qty_val <= 0 and not mat_name and not str(mid or '').strip():
            continue
        if qty_val > 0 and rate_val <= 0:
            raise ValueError(f'Unit rate is required and must be greater than 0 for "{mat_name}".')
        if not mat_name and not str(mid or '').strip():
            continue
        mat_obj = resolve_transaction_material(
            material_id=mid,
            typed_text=mat_name,
            require_active=True,
        )
        if not mat_obj:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        try:
            keep_id = int(item_id) if str(item_id or '').strip() else None
        except (TypeError, ValueError):
            keep_id = None
        rows.append((keep_id, mat_obj.name, qty_val, rate_val))
    return rows


def _apply_booking_item_updates(booking, desired_rows):
    """Update booking lines in place so delivered allocations keep their FKs.

    The previous delete-all/recreate path crashed with IntegrityError (HTTP 500)
    as soon as any BookingAllocation still pointed at a BookingItem.
    """
    existing = list(
        BookingItem.query.filter_by(booking_id=booking.id).order_by(BookingItem.id.asc()).all()
    )
    allocated = _booking_allocated_qty_map([it.id for it in existing])
    linked_ids = set()
    if existing:
        linked_ids = {
            row[0]
            for row in db.session.query(BookingAllocation.booking_item_id)
            .filter(BookingAllocation.booking_item_id.in_([it.id for it in existing]))
            .distinct()
            .all()
            if row[0]
        }

    unused = {it.id: it for it in existing}
    for keep_id, mat_name, qty_val, rate_val in desired_rows:
        item = None
        if keep_id and keep_id in unused:
            item = unused.pop(keep_id)
        else:
            match_id = next(
                (eid for eid, it in unused.items() if (it.material_name or '').strip() == mat_name),
                None,
            )
            if match_id is not None:
                item = unused.pop(match_id)

        if item is None:
            db.session.add(BookingItem(
                booking_id=booking.id,
                material_name=mat_name,
                qty=qty_val,
                price_at_time=rate_val,
            ))
            continue

        used_qty = float(allocated.get(item.id, 0) or 0)
        if qty_val + 1e-6 < used_qty:
            raise ValueError(
                f'Cannot reduce "{mat_name}" below the already delivered quantity {used_qty:g}.'
            )
        if item.id in linked_ids and (item.material_name or '').strip() != mat_name:
            raise ValueError(
                f'Cannot change "{item.material_name}" to "{mat_name}" because that line has already been delivered.'
            )
        item.material_name = mat_name
        item.qty = qty_val
        item.price_at_time = rate_val

    for item in unused.values():
        if item.id in linked_ids or float(allocated.get(item.id, 0) or 0) > 0:
            raise ValueError(
                f'Cannot remove "{item.material_name}" because it has already been delivered. '
                'Void or correct that sale first.'
            )
        db.session.delete(item)
    db.session.flush()


@bp.route('/bookings')
@login_required
def bookings_page():
    show_mode = (request.args.get('show', 'active') or 'active').strip().lower()
    client_filter = (request.args.get('client') or '').strip()
    resolved_filter_client = get_client_by_input(client_filter) if client_filter else None
    bill_filter = (request.args.get('bill_no') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    per_page = min(max(per_page, 10), 50)

    if date_from and date_to and date_to < date_from:
        date_to = date_from

    bookings_q = Booking.query.options(selectinload(Booking.items))
    if show_mode == 'voided':
        bookings_q = bookings_q.filter(Booking.is_void == True)
    elif show_mode == 'all':
        bookings_q = bookings_q
    else:
        show_mode = 'active'
        bookings_q = bookings_q.filter(Booking.is_void == False)

    if client_filter:
        if resolved_filter_client:
            bookings_q = bookings_q.filter(
                func.lower(func.trim(Booking.client_name)) == resolved_filter_client.name.strip().lower()
            )
        else:
            bookings_q = bookings_q.filter(Booking.client_name.ilike(f"%{client_filter}%"))
    if bill_filter:
        bookings_q = bookings_q.filter(or_(
            Booking.manual_bill_no.ilike(f"%{bill_filter}%"),
            Booking.auto_bill_no.ilike(f"%{bill_filter}%")
        ))
    if date_from:
        bookings_q = bookings_q.filter(func.date(Booking.date_posted) >= date_from)
    if date_to:
        bookings_q = bookings_q.filter(func.date(Booking.date_posted) <= date_to)

    bookings_pagination = bookings_q.order_by(Booking.date_posted.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    bookings = bookings_pagination.items
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    accounts = Account.query.filter(func.coalesce(Account.is_active, True) == True).order_by(Account.name.asc()).all()
    next_auto = peek_next_bill_no(AUTO_BILL_NAMESPACES['BOOKING'])
    return render_template('bookings.html',
                           bookings=bookings,
                           clients=clients,
                           materials=materials,
                           accounts=accounts,
                           next_auto=next_auto,
                           show_mode=show_mode,
                           client_filter=client_filter,
                           client_filter_display=(resolved_filter_client.name if resolved_filter_client else ''),
                           bill_filter=bill_filter,
                           date_from=date_from,
                           date_to=date_to,
                           pagination=bookings_pagination,
                           per_page=per_page)


@bp.route('/bookings/<int:booking_id>/edit-modal')
@login_required
def booking_edit_modal(booking_id):
    """Render one booking edit form on demand instead of duplicating it in the list."""
    booking = (
        Booking.query
        .options(selectinload(Booking.items))
        .filter(Booking.id == booking_id)
        .first_or_404()
    )
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    accounts = Account.query.filter(func.coalesce(Account.is_active, True) == True).order_by(Account.name.asc()).all()
    materials = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    return render_template('_booking_edit_modal.html', booking=booking, clients=clients, accounts=accounts, materials=materials)


@bp.route('/add_booking', methods=['POST'])
@login_required
def add_booking():
    client_input = request.form.get('client_code', '').strip() or request.form.get('client_name', '').strip()
    materials_list = request.form.getlist('material_name[]')
    material_ids = request.form.getlist('material_id[]')
    qtys = request.form.getlist('qty[]')
    rates = request.form.getlist('unit_rate[]')
    amount = _to_float_or_zero(request.form.get('amount', 0))
    paid_amount = _to_float_or_zero(request.form.get('paid_amount', 0))
    try:
        discount, discount_reason = _parse_discount_fields(
            request.form.get('discount', 0),
            request.form.get('discount_reason', ''),
            label='Booking discount',
            require_reason=False
        )
    except ValueError as ve:
        flash(str(ve), 'danger')
        return redirect(url_for('bookings_page'))
    manual_bill_raw = request.form.get('manual_bill_no', '').strip()
    manual_bill_no = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
    note = request.form.get('note', '').strip()
    date_str = (request.form.get('date') or '').strip()

    photo_path = save_photo(request.files.get('photo'))
    photo_url = request.form.get('photo_url', '').strip()

    # Find client by name or code
    client = get_client_by_input(client_input)

    if not client:
        flash(f'Client "{client_input}" not found. Please add client first.', 'danger')
        return redirect(url_for('bookings_page'))

    if manual_bill_no:
        conflict = find_bill_conflict(manual_bill_no)
        if conflict:
            flash(f"Manual bill '{manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
            return redirect(url_for('bookings_page'))

    # Calculate pending amount (what's still owed)
    pending_amount = max(0.0, amount - discount - paid_amount)

    auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES['BOOKING'])

    booking_posted_at = resolve_posted_datetime(date_str)

    # Create the booking
    booking = Booking(client_name=client.name,
                      amount=amount,
                      paid_amount=paid_amount,
                      discount=discount,
                      discount_reason=discount_reason,
                      manual_bill_no=manual_bill_no,
                      auto_bill_no=auto_bill_no,
                      photo_path=photo_path,
                      photo_url=photo_url,
                      date_posted=booking_posted_at,
                      note=note)
    db.session.add(booking)
    db.session.flush()

    # Add booking items from existing Material Master records only.
    for mat, qty, rate, mid in zip_longest(materials_list, qtys, rates, material_ids, fillvalue=''):
        mat_name = str(mat or '').strip()
        qty_val = _to_float_or_zero(qty)
        rate_val = _to_float_or_zero(rate)
        if qty_val <= 0 and not mat_name and not str(mid or '').strip():
            continue
        if qty_val > 0 and rate_val <= 0:
            flash(f'Unit rate is required and must be greater than 0 for "{mat_name}".', 'danger')
            return redirect(url_for('bookings_page'))
        try:
            mat_obj = resolve_transaction_material(
                material_id=mid,
                typed_text=mat_name,
                require_active=True,
            )
        except ValueError as ve:
            db.session.rollback()
            flash(str(ve), 'danger')
            return redirect(url_for('bookings_page'))
        if not mat_obj or qty_val <= 0:
            continue
        db.session.add(
            BookingItem(booking_id=booking.id,
                        material_name=mat_obj.name,
                        qty=qty_val,
                        price_at_time=rate_val))

    # Pending bills are derived from the booking source. Rebuild this source's row
    # instead of incrementing an existing snapshot.
    bill_ref = manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
    _sync_booking_pending_bill(booking, materials_list[0] if materials_list else '')

    try:
        _sync_booking_paid_into_account(
            booking,
            payment_account_id=(request.form.get('payment_account_id') or request.form.get('receive_in_account_id')),
            method=request.form.get('payment_method') or request.form.get('method') or 'Cash',
        )
    except ValueError as ve:
        db.session.rollback()
        flash(str(ve), 'danger')
        return redirect(url_for('bookings_page'))

    db.session.commit()

    msg = f'Booking added successfully'
    if manual_bill_no:
        msg += f' (Bill: {manual_bill_no})'
    if pending_amount > 0:
        msg += f' â€” Pending amount: {pending_amount}'
    flash(msg, 'success')

    bill_ref = manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
    return redirect(url_for(
        'bookings_page',
        download_bill=bill_ref,
        download_src='booking',
        download_src_id=booking.id,
        download_client_code=(client.code if client else None),
        download_client_name=(client.name if client else booking.client_name)
    ))


@bp.route('/edit_bill/Booking/<int:id>', methods=['POST'])
@login_required
def edit_booking(id):
    booking = Booking.query.get_or_404(id)

    old_bill_no = booking.manual_bill_no
    old_paid = float(booking.paid_amount or 0)
    old_account_id = booking.receive_in_account_id
    old_client = Client.query.filter_by(name=booking.client_name).first()
    old_client_code = old_client.code if old_client else None

    client_code = request.form.get('client_code', '').strip()
    client_name_input = request.form.get('client_name', '').strip()

    # Find client by name or code
    client = get_client_by_input(client_code) or get_client_by_input(client_name_input)
    if client:
        booking.client_name = client.name

    materials_list = request.form.getlist('material_name[]')
    material_ids = request.form.getlist('material_id[]')
    qtys = request.form.getlist('qty[]')
    rates = request.form.getlist('unit_rate[]')
    item_ids = request.form.getlist('booking_item_id[]')
    booking.amount = _to_float_or_zero(request.form.get('amount', 0))
    booking.paid_amount = _to_float_or_zero(request.form.get('paid_amount', 0))
    try:
        booking.discount, booking.discount_reason = _parse_discount_fields(
            request.form.get('discount', 0),
            request.form.get('discount_reason', ''),
            label='Booking discount',
            require_reason=False
        )
        desired_rows = _collect_booking_item_updates(materials_list, qtys, rates, item_ids)
        _apply_booking_item_updates(booking, desired_rows)
    except ValueError as ve:
        db.session.rollback()
        flash(str(ve), 'danger')
        return redirect(url_for('bookings_page'))
    new_manual_raw = request.form.get('manual_bill_no', '').strip()
    booking.manual_bill_no = normalize_manual_bill(new_manual_raw) if new_manual_raw else ''
    booking.note = request.form.get('note', '').strip()
    date_str = (request.form.get('date') or '').strip()
    if date_str:
        parsed_posted_at = resolve_posted_datetime(date_str, fallback_dt=booking.date_posted or pk_now())
        # Keep original timestamp if the submitted datetime is unchanged at minute precision.
        if booking.date_posted:
            old_minute = booking.date_posted.replace(second=0, microsecond=0)
            new_minute = parsed_posted_at.replace(second=0, microsecond=0)
            if new_minute != old_minute:
                booking.date_posted = parsed_posted_at
        else:
            booking.date_posted = parsed_posted_at

    booking.photo_url = request.form.get('photo_url', '').strip()
    new_photo = save_photo(request.files.get('photo'))
    if new_photo:
        booking.photo_path = new_photo

    if booking.manual_bill_no:
        conflict = find_bill_conflict(booking.manual_bill_no)
        if conflict and not (conflict[0] == 'Booking' and conflict[1] == booking.id):
            db.session.rollback()
            flash(f"Manual bill '{booking.manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
            return redirect(url_for('bookings_page'))

    new_client = Client.query.filter_by(name=booking.client_name).first()
    new_client_code = new_client.code if new_client else None

    old_bill_ref = old_bill_no or booking.auto_bill_no or f"BK-{id}"
    _sync_booking_pending_bill(
        booking,
        materials_list[0] if materials_list else '',
        extra_void_refs=[old_bill_ref]
    )

    form_account = (
        request.form.get('payment_account_id')
        or request.form.get('receive_in_account_id')
        or old_account_id
    )
    paid_increased = float(booking.paid_amount or 0) > old_paid + 0.0001
    try:
        _sync_booking_paid_into_account(
            booking,
            payment_account_id=form_account,
            method=request.form.get('payment_method') or request.form.get('method') or 'Cash',
            require_account=bool(form_account) or paid_increased,
        )
        db.session.commit()
    except ValueError as ve:
        db.session.rollback()
        flash(str(ve), 'danger')
        return redirect(url_for('bookings_page'))
    except IntegrityError:
        db.session.rollback()
        flash(
            'Booking could not be saved because a delivered line is still linked to this bill. '
            'Change the discount or unused items only, or void the related sale first.',
            'danger',
        )
        return redirect(url_for('bookings_page'))

    flash('Booking updated', 'success')

    bill_ref = booking.manual_bill_no or booking.auto_bill_no or f"BK-{id}"
    return redirect(url_for(
        'bookings_page',
        download_bill=bill_ref,
        download_src='booking',
        download_src_id=booking.id,
        download_client_code=(new_client_code if new_client_code else None),
        download_client_name=booking.client_name
    ))


