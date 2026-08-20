from ._common import *  # noqa

@bp.route('/supplier_opening_balance/<int:id>', methods=['POST'])
@login_required
def supplier_opening_balance(id):
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('supplier_ledger', id=id))
    s = db.session.get(Supplier, id)
    if not s:
        flash('Supplier not found', 'danger')
        return redirect(url_for('suppliers'))
    s.opening_balance = _to_float_or_zero(request.form.get('opening_balance', s.opening_balance))
    s.opening_balance_date = _resolve_opening_balance_date(
        request.form.get('opening_balance_date'),
        fallback_dt=(s.opening_balance_date or s.created_at)
    )
    db.session.commit()
    flash('Opening balance updated', 'success')
    return redirect(url_for('supplier_ledger', id=id))

