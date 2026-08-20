from ._common import *  # noqa

@bp.route('/materials/activate_all', methods=['POST'])
@login_required
def activate_all_materials():
    if not _user_can('can_manage_materials'):
        flash('Permission denied', 'danger')
        return redirect(url_for('materials'))
    count = Material.query.filter_by(is_active=False).update({'is_active': True}, synchronize_session=False)
    db.session.commit()
    flash(f'Activated {count} suspended materials.', 'success')
    return redirect(url_for('materials'))

