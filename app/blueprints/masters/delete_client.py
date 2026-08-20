from ._common import *  # noqa

@bp.route('/delete_client/<int:id>', methods=['POST'])
@login_required
def delete_client(id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))
    c = db.session.get(Client, id)
    if c:
        from utils.accounting_audit import record_accounting_audit
        before = {'id': c.id, 'code': c.code, 'name': c.name, 'is_active': bool(c.is_active)}
        c.is_active = False
        record_accounting_audit(
            current_user, action='Suspend', entity_type='Client', entity_id=c.id,
            before=before, after={**before, 'is_active': False},
            party_before_id=c.id, party_after_id=c.id, reason='Client suspended', module='clients',
        )
        db.session.commit()
        flash('Client suspended; historical transactions were preserved.', 'warning')
    return redirect(url_for('clients'))

