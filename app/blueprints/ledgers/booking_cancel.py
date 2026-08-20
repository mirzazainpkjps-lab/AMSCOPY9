"""booking_cancel — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/client_booking_cancel/<int:client_id>', methods=['POST'])
@login_required
def client_booking_cancel(client_id):
    client = db.session.get(Client, client_id)
    if not client:
        flash('Client not found', 'danger')
        return redirect(url_for('clients'))

    from app.services.booking_cancel_plan import build_client_booking_cancel_plan

    # Shared plan: remaining = booked - delivered + returned_booked, so qty
    # credited back by a booked material return is cancellable again.
    cancel_plan, cancel_total, cancel_total_qty = build_client_booking_cancel_plan(client)

    if not cancel_plan:
        flash('No remaining booking items to cancel.', 'info')
        return redirect(url_for('client_ledger', id=client.id))

    selected_item_ids_raw = request.form.getlist('selected_item_ids')
    selected_item_ids_csv = (request.form.get('selected_item_ids_csv') or '').strip()
    if selected_item_ids_csv:
        selected_item_ids_raw = [x.strip() for x in selected_item_ids_csv.split(',') if x.strip()]
    has_selection_ui = request.form.get('has_selection_ui') == '1'
    selected_item_ids = set()
    for raw in selected_item_ids_raw:
        try:
            selected_item_ids.add(int(raw))
        except Exception:
            continue

    if has_selection_ui:
        if not selected_item_ids:
            flash('Select at least one material row to cancel.', 'warning')
            return redirect(url_for('client_ledger', id=client.id))
        cancel_plan = [r for r in cancel_plan if r.get('item') and r['item'].id in selected_item_ids]
        if not cancel_plan:
            flash('Selected material rows are no longer available. Please review and retry.', 'warning')
            return redirect(url_for('client_ledger', id=client.id))

    # Preflight every hard-delete before writing cancellation entries or
    # balances. A booking line with an active allocation represents a posted
    # sale and must not be deleted merely because the legacy Entry-based FIFO
    # calculation disagrees with it.
    deleting_item_ids = [
        row['item'].id
        for row in cancel_plan
        if row.get('item')
        and float(row['item'].qty or 0) - float(row.get('remaining_qty') or 0) <= 0
    ]
    if deleting_item_ids:
        active_allocated_ids = {
            row[0]
            for row in db.session.query(BookingAllocation.booking_item_id)
            .filter(
                BookingAllocation.booking_item_id.in_(deleting_item_ids),
                or_(
                    BookingAllocation.is_void.is_(False),
                    BookingAllocation.is_void.is_(None),
                ),
            )
            .distinct()
            .all()
        }
        if active_allocated_ids:
            flash(
                'Cancellation blocked: a booking line still has an active sale allocation. '
                'Void or correct that sale first; no booking changes were saved.',
                'danger'
            )
            return redirect(url_for('client_ledger', id=client.id))

    from app.services.allocation_integrity import archive_and_delete_booking_allocations

    touched_bookings = set()
    now = pk_now()
    for row in cancel_plan:
        item = row.get('item')
        remaining_qty = float(row.get('remaining_qty') or 0)
        if not item or remaining_qty <= 0:
            continue
        booking = item.booking
        rate = float(item.price_at_time or 0)
        amount = remaining_qty * rate
        bill_ref = booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}" if booking else ''
        db.session.add(Entry(
            date=now.strftime('%Y-%m-%d'),
            time=now.strftime('%H:%M:%S'),
            type='CANCEL',
            material=item.material_name,
            client=client.name,
            client_code=client.code,
            qty=remaining_qty,
            bill_no=bill_ref,
            nimbus_no='Booking Cancel',
            created_by=current_user.username,
            client_category='Booking Delivery',
            transaction_category='Cancel',
            note=f"Booking cancellation|rate={rate:.6f}|amount={amount:.6f}"
        ))
        new_qty = float(item.qty or 0) - remaining_qty
        if new_qty <= 0:
            # Only void historical links can remain after the active-allocation
            # preflight. Preserve them before deleting their parent line.
            old_allocations = BookingAllocation.query.filter_by(booking_item_id=item.id).all()
            archive_and_delete_booking_allocations(
                old_allocations,
                reason='booking cancellation deleted an unconsumed source booking line',
            )
            db.session.delete(item)
        else:
            item.qty = new_qty
        if booking:
            touched_bookings.add(booking)

    for booking in touched_bookings:
        items = BookingItem.query.filter_by(booking_id=booking.id).all()
        new_amount = sum((i.qty or 0) * (i.price_at_time or 0) for i in items)
        booking.amount = new_amount

        bill_ref = booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
        new_pending = max(0.0, (booking.amount or 0) - (booking.discount or 0) - (booking.paid_amount or 0))
        pb = PendingBill.query.filter_by(bill_no=bill_ref, client_code=client.code).first()
        if new_pending <= 0:
            if pb:
                db.session.delete(pb)
        else:
            if pb:
                pb.amount = new_pending
                pb.client_name = booking.client_name
            else:
                db.session.add(PendingBill(
                    client_code=client.code,
                    client_name=booking.client_name,
                    bill_no=bill_ref,
                    bill_kind=parse_bill_kind(bill_ref),
                    amount=new_pending,
                    reason='Booking (Adjusted)',
                    is_manual=bool(booking.manual_bill_no),
                    created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                    created_by=current_user.username
                ))

    db.session.commit()
    flash(f'Booking cancellation applied. Total cancelled: {cancel_total_qty:.2f} items, value {cancel_total:.2f}', 'success')
    return redirect(url_for('client_ledger', id=client.id))


@bp.route('/client_booking_cancel_revert/<int:client_id>/<int:entry_id>', methods=['POST'])
@login_required
def client_booking_cancel_revert(client_id, entry_id):
    client = db.session.get(Client, client_id)
    if not client:
        flash('Client not found', 'danger')
        return redirect(url_for('clients'))

    entry = db.session.get(Entry, entry_id)
    if not entry:
        flash('Cancellation entry not found', 'danger')
        return redirect(url_for('client_ledger', id=client.id))
    if entry.is_void:
        flash('This cancellation is already reverted.', 'info')
        return redirect(url_for('client_ledger', id=client.id))
    if (entry.type or '').upper() != 'CANCEL':
        flash('Selected row is not a booking cancellation entry.', 'warning')
        return redirect(url_for('client_ledger', id=client.id))

    client_name_norm = (client.name or '').strip().lower()
    entry_client_norm = (entry.client or '').strip().lower()
    if (entry.client_code and entry.client_code != client.code) and (entry_client_norm != client_name_norm):
        flash('Cancellation row does not belong to this client.', 'danger')
        return redirect(url_for('client_ledger', id=client.id))

    bill_ref = (entry.bill_no or entry.auto_bill_no or '').strip()
    if not bill_ref:
        flash('Cannot revert: cancellation row has no bill reference.', 'warning')
        return redirect(url_for('client_ledger', id=client.id))

    booking = Booking.query.filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False,
        or_(Booking.manual_bill_no == bill_ref, Booking.auto_bill_no == bill_ref)
    ).order_by(Booking.id.desc()).first()
    if not booking:
        flash('Cannot revert: original booking for this bill was not found.', 'warning')
        return redirect(url_for('client_ledger', id=client.id))

    material_name = (entry.material or entry.booked_material or '').strip()
    qty = float(entry.qty or 0)
    if not material_name or qty <= 0:
        flash('Cannot revert: cancellation row has invalid material/qty.', 'warning')
        return redirect(url_for('client_ledger', id=client.id))

    rate = _parse_cancel_rate_from_note(entry.note)
    if rate is None:
        amount = _parse_cancel_amount_from_note(entry.note)
        if amount is not None and qty > 0:
            rate = float(amount) / float(qty)
    if rate is None:
        existing_item = BookingItem.query.filter_by(booking_id=booking.id, material_name=material_name).first()
        rate = float(existing_item.price_at_time or 0) if existing_item else 0.0

    item = BookingItem.query.filter_by(booking_id=booking.id, material_name=material_name).first()
    if item:
        item.qty = float(item.qty or 0) + qty
        if float(item.price_at_time or 0) <= 0 and rate > 0:
            item.price_at_time = rate
    else:
        db.session.add(BookingItem(
            booking_id=booking.id,
            material_name=material_name,
            qty=qty,
            price_at_time=float(rate or 0)
        ))

    # Recompute booking amount and pending due.
    items = BookingItem.query.filter_by(booking_id=booking.id).all()
    booking.amount = sum((float(i.qty or 0) * float(i.price_at_time or 0)) for i in items)
    bill_ref_booking = booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}"
    new_pending = max(0.0, (booking.amount or 0) - (booking.discount or 0) - (booking.paid_amount or 0))
    pb = PendingBill.query.filter_by(bill_no=bill_ref_booking, client_code=client.code).first()
    if new_pending <= 0:
        if pb:
            db.session.delete(pb)
    else:
        if pb:
            pb.amount = new_pending
            pb.client_name = booking.client_name
            pb.is_void = False
        else:
            db.session.add(PendingBill(
                client_code=client.code,
                client_name=booking.client_name,
                bill_no=bill_ref_booking,
                bill_kind=parse_bill_kind(bill_ref_booking),
                amount=new_pending,
                reason='Booking (Adjusted)',
                is_manual=bool(booking.manual_bill_no),
                created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                created_by=current_user.username
            ))

    # Mark cancellation row voided (audit-preserving revert).
    entry.is_void = True
    db.session.commit()
    flash(f'Cancellation reverted for {material_name} ({qty:.2f}).', 'success')
    return redirect(url_for('client_ledger', id=client.id))


