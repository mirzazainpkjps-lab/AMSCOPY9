from ._common import *  # noqa

@bp.route('/materials/rename_label', methods=['POST'])
@login_required
def rename_material_label():
    """
    Update historical records that store material as plain text.
    Useful when a material was renamed but older rows still carry the old label.
    """
    old_label = (request.form.get('old_label') or '').strip()
    target_id = (request.form.get('target_material_id') or '').strip()
    if not old_label or not target_id:
        flash('Old label and target material are required.', 'danger')
        return redirect(url_for('materials'))

    target_mat = db.session.get(Material, int(target_id))
    if not target_mat:
        flash('Target material not found.', 'danger')
        return redirect(url_for('materials'))

    def _rename_filter(col, old_value):
        old_value = (old_value or '').strip()
        if not old_value:
            return False
        old_lower = old_value.lower()
        old_nospace = old_lower.replace(' ', '')
        return or_(
            func.lower(func.trim(col)) == old_lower,
            func.lower(func.replace(func.trim(col), ' ', '')) == old_nospace
        )

    try:
        per_table = {}
        updates = [
            (Entry, Entry.material),
            (Entry, Entry.booked_material),
            (BookingItem, BookingItem.material_name),
            (DirectSaleItem, DirectSaleItem.product_name),
            (GRNItem, GRNItem.mat_name),
            (MaterialReturnItem, MaterialReturnItem.material_name),
            (DeliveryItem, DeliveryItem.product),
            (ReconBasket, ReconBasket.inv_material),
        ]
        for model, col in updates:
            updated = model.query.filter(_rename_filter(col, old_label)).update(
                {col: target_mat.name},
                synchronize_session=False
            )
            per_table[f"{model.__tablename__}.{col.key}"] = int(updated or 0)
        db.session.commit()
        total = sum(per_table.values())
        details = ", ".join([f"{k}={v}" for k, v in per_table.items() if v]) or "no matching rows"
        flash(f'Updated "{old_label}" → "{target_mat.name}" ({total} rows). {details}', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Label rename failed: {exc}', 'danger')

    return redirect(url_for('materials'))

