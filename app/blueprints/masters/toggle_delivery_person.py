from ._common import *  # noqa

@bp.route('/delivery_persons/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_delivery_person(id):
    if not _user_can('can_manage_delivery_persons'):
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))
    row = db.session.get(DeliveryPerson, id)
    if row:
        row.is_active = not row.is_active
        db.session.commit()
        flash('Delivery person status updated', 'success')
    return redirect(url_for('delivery_persons_page'))

