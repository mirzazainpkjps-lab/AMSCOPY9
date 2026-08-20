from ._common import *  # noqa


@bp.route('/edit_supplier/<int:id>', methods=['POST'])
@login_required
def edit_supplier(id):
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))
    supplier = db.session.get(Supplier, id)
    if supplier:
        before = {
            'id': supplier.id, 'name': supplier.name, 'phone': supplier.phone,
            'address': supplier.address, 'opening_balance': supplier.opening_balance,
            'is_active': bool(supplier.is_active),
        }
        supplier.name = request.form.get('name', '').strip()
        supplier.phone = request.form.get('phone', '')
        supplier.address = request.form.get('address', '')
        supplier.opening_balance = _to_float_or_zero(request.form.get('opening_balance', supplier.opening_balance))
        supplier.opening_balance_date = _resolve_opening_balance_date(
            request.form.get('opening_balance_date'),
            fallback_dt=(supplier.opening_balance_date or supplier.created_at)
        )
        supplier.is_active = 'is_active' in request.form
        after = {
            'id': supplier.id, 'name': supplier.name, 'phone': supplier.phone,
            'address': supplier.address, 'opening_balance': supplier.opening_balance,
            'is_active': bool(supplier.is_active),
        }
        from utils.accounting_audit import record_accounting_audit
        action = 'Activate' if not before['is_active'] and after['is_active'] else (
            'Suspend' if before['is_active'] and not after['is_active'] else 'Edit'
        )
        record_accounting_audit(
            current_user, action=action, entity_type='Supplier', entity_id=supplier.id,
            before=before, after=after, party_before_id=supplier.id, party_after_id=supplier.id,
            reason='Supplier master updated', module='suppliers',
        )
        db.session.commit()
        flash('Supplier updated.', 'success')
    return redirect(url_for('suppliers'))
