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
        new_name = (request.form.get('name', '') or '').strip()
        if new_name:
            # Duplicate supplier names break every name-based lookup/ledger
            # join (GRN.supplier, Entry.client, get_supplier_by_input).
            duplicate = Supplier.query.filter(
                func.lower(func.trim(Supplier.name)) == new_name.lower(),
                Supplier.id != supplier.id,
            ).first()
            if duplicate:
                flash(
                    f"Supplier already exists with name '{new_name}' (#{duplicate.id}). "
                    "Use a different name — the supplier ledger joins GRNs by name for legacy rows.",
                    'danger'
                )
                return redirect(url_for('suppliers'))
            old_name = supplier.name
            supplier.name = new_name
            if old_name and new_name and old_name.strip().lower() != new_name.lower():
                # Keep the denormalised display strings in sync so the GRN
                # list/search and legacy name-matched ledger rows follow the
                # rename (rows with supplier_id keep matching by id anyway).
                grns = GRN.query.filter(
                    or_(
                        GRN.supplier_id == supplier.id,
                        and_(
                            GRN.supplier_id.is_(None),
                            func.lower(func.trim(GRN.supplier)) == old_name.strip().lower(),
                        ),
                    )
                ).all()
                for g in grns:
                    g.supplier = new_name
                grn_bills = [g.auto_bill_no for g in grns if g.auto_bill_no]
                if grn_bills:
                    Entry.query.filter(
                        Entry.auto_bill_no.in_(grn_bills),
                        Entry.type == 'IN'
                    ).update({'client': new_name}, synchronize_session=False)
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
