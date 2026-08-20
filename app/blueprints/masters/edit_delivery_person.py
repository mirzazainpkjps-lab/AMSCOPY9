from ._common import *  # noqa

@bp.route('/delivery_persons/edit/<int:id>', methods=['POST'])
@login_required
def edit_delivery_person(id):
    if not _user_can('can_manage_delivery_persons'):
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))

    row = db.session.get(DeliveryPerson, id)
    if not row:
        flash('Delivery person not found', 'danger')
        return redirect(url_for('delivery_persons_page'))

    new_name = (request.form.get('name') or '').strip()
    new_phone = (request.form.get('phone') or '').strip()
    if not new_name:
        flash('Driver name is required', 'danger')
        return redirect(url_for('delivery_persons_page'))

    existing = DeliveryPerson.query.filter(
        DeliveryPerson.id != row.id,
        func.lower(func.trim(DeliveryPerson.name)) == new_name.lower()
    ).first()
    if existing:
        flash('Driver name already exists', 'danger')
        return redirect(url_for('delivery_persons_page'))

    old_name = (row.name or '').strip()
    row.name = new_name
    row.phone = new_phone or None

    if old_name and old_name.lower() != new_name.lower():
        DeliveryRent.query.filter(
            func.lower(func.trim(DeliveryRent.delivery_person_name)) == old_name.lower()
        ).update({'delivery_person_name': new_name}, synchronize_session=False)

    db.session.commit()
    flash('Delivery person updated', 'success')
    return redirect(url_for('delivery_persons_page'))

