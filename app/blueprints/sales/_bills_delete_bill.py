"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/delete_bill/<string:type>/<int:id>', methods=['POST'])
@login_required
def delete_bill(type, id):
    if current_user.role != 'admin':
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))

    try:
        ok = hard_delete_transaction(type, id)
        if ok:
            db.session.commit()
            flash(f'{type} deleted', 'success')
        else:
            flash('Record not found', 'danger')
    except Exception as exc:
        db.session.rollback()
        flash(f'Unable to delete: {exc}', 'danger')

    if type == 'Booking':
        return redirect(url_for('bookings_page'))
    if type == 'Payment':
        return redirect(url_for('payments_page'))
    if type == 'DirectSale':
        return redirect(url_for('direct_sales_page'))
    if type == 'MaterialReturn':
        return redirect(url_for('material_returns_page'))
    return redirect(url_for('index'))
