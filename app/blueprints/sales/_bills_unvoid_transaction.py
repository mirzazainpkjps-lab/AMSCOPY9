"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/unvoid_transaction/<string:type>/<int:id>', methods=['POST'])
@login_required
def unvoid_transaction(type, id):
    if not _user_can('can_manage_sales'):
        flash('Permission denied', 'danger')
        return redirect(request.referrer or url_for('index'))

    if type == 'Entry':
        entry = db.session.get(Entry, id)
        if _set_entry_void_state(entry, False):
            flash('Entry restored and stock reapplied', 'success')

    elif type == 'DirectSale':
        sale = db.session.get(DirectSale, id)
        # Use atomic restore operation with tracking
        if sale and _atomic_restore_direct_sale_with_tracking(sale):
            flash('Sale restored consistently (atomic transaction)', 'success')
        elif sale and _set_direct_sale_void_state(sale, False):
            flash('Sale restored', 'success')

    elif type == 'MaterialReturn':
        ret = db.session.get(MaterialReturn, id)
        if _set_material_return_void_state(ret, False):
            flash('Material return restored', 'success')

    elif type == 'Booking':
        bk = db.session.get(Booking, id)
        if _set_booking_void_state(bk, False):
            flash('Booking restored', 'success')

    elif type == 'Payment':
        pay = db.session.get(Payment, id)
        if _set_payment_void_state(pay, False):
            client = get_client_by_input(pay.client_name or '') if pay else None
            if client:
                rebuild_pending_bills(client_id=client.id)
            flash('Payment restored', 'success')

    db.session.add(AuditLog(
        user_id=getattr(current_user, 'id', None),
        action=f'transaction.unvoid.{type}',
        details=f'id={id}'
    ))
    db.session.commit()
    return redirect(request.referrer or url_for('index'))

