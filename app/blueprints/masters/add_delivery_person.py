from ._common import *  # noqa

@bp.route('/delivery_persons/add', methods=['POST'])
@login_required
def add_delivery_person():
    if not _user_can('can_manage_delivery_persons'):
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))
    name = (request.form.get('name') or '').strip()
    phone = (request.form.get('phone') or '').strip()
    if not name:
        flash('Driver name is required', 'danger')
        return redirect(url_for('delivery_persons_page'))
    get_or_create_delivery_person(name, phone=phone)
    db.session.commit()
    flash('Delivery person saved', 'success')
    return redirect(url_for('delivery_persons_page'))

