"""Client activation/suspension with atomic structured audit."""
from ._common import *  # noqa


@bp.route('/client_toggle_active/<int:client_id>', methods=['POST'])
@login_required
def client_toggle_active(client_id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(request.referrer or url_for('clients'))
    client = db.session.get(Client, client_id)
    if not client:
        flash('Client not found', 'danger')
        return redirect(url_for('clients'))
    before = {'id': client.id, 'code': client.code, 'name': client.name,
              'is_active': bool(client.is_active)}
    client.is_active = not bool(client.is_active)
    from utils.accounting_audit import record_accounting_audit
    record_accounting_audit(
        current_user, action='Activate' if client.is_active else 'Suspend',
        entity_type='Client', entity_id=client.id, before=before,
        after={**before, 'is_active': bool(client.is_active)},
        party_before_id=client.id, party_after_id=client.id,
        reason='Client activation status changed', module='clients',
    )
    db.session.commit()
    if client.is_active:
        flash('Client reactivated and is selectable for new transactions.', 'success')
    else:
        flash('Client suspended and hidden from new transaction selectors.', 'warning')
    return redirect(request.referrer or url_for('clients'))
