from ._common import *  # noqa


@bp.route('/delete_supplier/<int:id>', methods=['POST'])
@login_required
def delete_supplier(id):
    """Archive a supplier; never sever GRN/payment history."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('suppliers'))
    supplier = db.session.get(Supplier, id)
    if supplier:
        from utils.accounting_audit import record_accounting_audit
        before = {'id': supplier.id, 'name': supplier.name, 'is_active': bool(supplier.is_active)}
        supplier.is_active = False
        record_accounting_audit(
            current_user, action='Suspend', entity_type='Supplier', entity_id=supplier.id,
            before=before, after={**before, 'is_active': False},
            party_before_id=supplier.id, party_after_id=supplier.id,
            reason='Supplier archived; historical GRNs/payments preserved', module='suppliers',
        )
        db.session.commit()
        flash('Supplier suspended; historical GRNs and payments were preserved.', 'warning')
    return redirect(url_for('suppliers'))
