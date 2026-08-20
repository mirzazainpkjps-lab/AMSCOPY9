from ._common import *  # noqa

@bp.route('/reclaim_client/<int:id>', methods=['POST'])
@login_required
def reclaim_client(id):
    source_client = db.session.get(Client, id)
    if not source_client or source_client.is_active or not source_client.transferred_to_id:
        flash('Invalid reclaim request', 'danger')
        return redirect(url_for('clients', show_inactive=1))

    target_client = db.session.get(Client, source_client.transferred_to_id)
    if not target_client:
        flash('Target client not found', 'danger')
        return redirect(url_for('clients', show_inactive=1))

    source_client.is_active = True

    entries_reclaimed = Entry.query.filter_by(
        client_code=target_client.code, client=target_client.name).update({
            'client': source_client.name,
            'client_code': source_client.code
        })
    bills_reclaimed = PendingBill.query.filter_by(
        client_code=target_client.code, client_name=target_client.name).update({
            'client_name': source_client.name,
            'client_code': source_client.code
        })
    waive_reclaimed = WaiveOff.query.filter_by(
        client_code=target_client.code, client_name=target_client.name).update({
            'client_name': source_client.name,
            'client_code': source_client.code
        })

    source_client.transferred_to_id = None
    db.session.commit()

    flash(f'Reclaimed {entries_reclaimed} entries, {bills_reclaimed} bills, and {waive_reclaimed} waive-off rows.', 'success')
    return redirect(url_for('clients'))

