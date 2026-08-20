from ._common import *  # noqa

@bp.route('/delivery_persons')
@login_required
def delivery_persons_page():
    if not _user_can('can_manage_delivery_persons'):
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))
    rows = DeliveryPerson.query.order_by(DeliveryPerson.name.asc()).all()
    return render_template('delivery_persons.html', rows=rows)

