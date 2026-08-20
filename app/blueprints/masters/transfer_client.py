from ._common import *  # noqa

@bp.route('/transfer_client/<int:id>', methods=['POST'])
@login_required
def transfer_client(id):
    source_client = db.session.get(Client, id)
    target_client_id = request.form.get('target_client_id')
    if not source_client or not target_client_id:
        flash('Invalid transfer request', 'danger')
        return redirect(url_for('clients'))

    target_client = db.session.get(Client, int(target_client_id))
    if not target_client:
        flash('Target client not found', 'danger')
        return redirect(url_for('clients'))

    if target_client.id == source_client.id:
        flash('Cannot transfer to the same client', 'danger')
        return redirect(url_for('clients'))

    if not target_client.is_active:
        flash('Cannot transfer to an inactive client', 'danger')
        return redirect(url_for('clients'))

    entries_updated = Entry.query.filter_by(client_code=source_client.code).update({
        'client': target_client.name,
        'client_code': target_client.code
    })
    bills_updated = PendingBill.query.filter_by(client_code=source_client.code).update({
        'client_name': target_client.name,
        'client_code': target_client.code
    })
    waive_updated = WaiveOff.query.filter_by(client_code=source_client.code).update({
        'client_name': target_client.name,
        'client_code': target_client.code
    })

    source_client.is_active = False
    source_client.transferred_to_id = target_client.id
    db.session.commit()

    flash(f'Transferred {entries_updated} entries, {bills_updated} bills, and {waive_updated} waive-off rows.', 'success')
    return redirect(url_for('clients'))

