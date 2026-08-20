from ._common import *  # noqa

@bp.route('/client_opening_balance/<int:id>', methods=['POST'])
@login_required
def client_opening_balance(id):
    if not _user_can('can_manage_clients'):
        flash('Permission denied', 'danger')
        return redirect(url_for('financial_ledger', client_id=id))
    c = db.session.get(Client, id)
    if not c:
        flash('Client not found', 'danger')
        return redirect(url_for('clients'))
    c.opening_balance = _to_float_or_zero(request.form.get('opening_balance', c.opening_balance))
    c.opening_balance_date = _resolve_opening_balance_date(
        request.form.get('opening_balance_date'),
        fallback_dt=(c.opening_balance_date or c.created_at)
    )
    db.session.commit()
    flash('Opening balance updated', 'success')
    return redirect(url_for('financial_ledger', client_id=id))

