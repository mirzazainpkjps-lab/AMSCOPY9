from ._common import *  # noqa

@bp.route('/clients/activate_all', methods=['POST'])
@login_required
def activate_all_clients():
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('clients'))
    count = Client.query.filter_by(is_active=False).update({'is_active': True}, synchronize_session=False)
    db.session.commit()
    flash(f'Activated {count} suspended clients.', 'success')
    return redirect(url_for('clients'))

