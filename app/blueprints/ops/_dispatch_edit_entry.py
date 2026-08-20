"""dispatch — split from ops.py."""
from ._common import *  # noqa

@bp.route('/edit_entry/<int:id>', methods=['POST'])
@login_required
def edit_entry(id):
    e = db.session.get(Entry, id)
    if not e:
        return redirect(url_for('index'))

    today_str = pk_today().strftime('%Y-%m-%d')
    if current_user.role != 'admin' and e.date != today_str:
        flash('Permission Denied: Only Admins can edit back-dated records.', 'danger')
        return redirect(url_for('index'))

    old_bill_no = e.bill_no
    old_client_code = e.client_code
    old_qty = e.qty
    old_material = e.material

    e.date = request.form.get('date') or e.date
    e.time = request.form.get('time') or e.time
    e.type = request.form.get('type') or e.type

    mat_obj = get_material_by_input(request.form.get('material'))
    e.material = mat_obj.name if mat_obj else (request.form.get('material') or e.material)

    client_input = request.form.get('client', '').strip()
    if client_input:
        client_obj = get_client_by_input(client_input)
        if client_obj:
            e.client = client_obj.name
            e.client_code = client_obj.code
        else:
            e.client = client_input
            e.client_code = None
    else:
        e.client = None
        e.client_code = None

    e.qty = float(request.form.get('qty', e.qty) or e.qty)
    e.bill_no = request.form.get('bill_no', '').strip() or None
    e.nimbus_no = request.form.get('nimbus_no', '').strip() or None
    driver_name = (request.form.get('driver_name') or '').strip()
    if e.type == 'OUT' and not driver_name:
        flash('Driver name is required for delivery dispatch.', 'danger')
        return redirect(request.referrer or url_for('tracking'))
    if e.type == 'OUT' and driver_name:
        get_or_create_delivery_person(driver_name)
    e.driver_name = driver_name or None
    e.note = request.form.get('note', '').strip()

    # Update Material Totals if qty or material changed
    if e.type == 'OUT' or e.type == 'IN':
        # Revert old qty from old material
        old_mat_obj = Material.query.filter_by(name=old_material).first()
        if old_mat_obj:
            if e.type == 'IN': old_mat_obj.total -= old_qty
            else: old_mat_obj.total += old_qty

        # Apply new qty to new material
        new_mat_obj = mat_obj if mat_obj else Material.query.filter_by(name=e.material).first()
        if new_mat_obj:
            if e.type == 'IN': new_mat_obj.total += e.qty
            else: new_mat_obj.total -= e.qty

    # Synchronize PendingBill - REMOVED DANGEROUS LOGIC
    # We do NOT want to auto-update PendingBills for OUT entries here because
    # it risks overwriting Booking bills with partial dispatch amounts.
    # Only update bill reference if changed.
    if e.type == 'OUT':
        pass

    db.session.commit()
    flash('Entry Updated', 'success')

    redirect_to = request.form.get('redirect_to')
    if redirect_to == 'tracking':
        return redirect(url_for('tracking'))
    if redirect_to == 'daily_transactions':
        return redirect(url_for('inventory.daily_transactions', date=e.date))
    return redirect(url_for('index'))

