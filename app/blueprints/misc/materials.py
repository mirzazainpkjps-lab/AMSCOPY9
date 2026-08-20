"""materials — split from misc.py."""
from ._common import *  # noqa

@bp.route('/merge_materials', methods=['POST'])
@login_required
def merge_materials():
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

    def _cascade_rename_material_label(old_value, new_value):
        old_value = (old_value or '').strip()
        new_value = (new_value or '').strip()
        if not old_value or not new_value or old_value.lower() == new_value.lower():
            return

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
            model.query.filter(_rename_filter(col, old_value)).update(
                {col: new_value},
                synchronize_session=False
            )

    source_id = request.form.get('source_material_id')
    target_id = request.form.get('target_material_id')

    if not source_id or not target_id or source_id == target_id:
        flash('Invalid selection for merging', 'danger')
        return redirect(url_for('materials'))

    source_mat = db.session.get(Material, int(source_id))
    target_mat = db.session.get(Material, int(target_id))

    if not source_mat or not target_mat:
        flash('One or both materials not found', 'danger')
        return redirect(url_for('materials'))

    try:
        source_name = source_mat.name
        target_name = target_mat.name

        _cascade_rename_material_label(source_name, target_name)

        # 7. Delete the source material
        db.session.delete(source_mat)

        db.session.commit()
        flash(f'Successfully merged "{source_name}" into "{target_name}". All records updated.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Merge failed: {str(e)}', 'danger')

    return redirect(url_for('materials'))


@bp.route('/add_material', methods=['POST'])
@login_required
def add_material():
    name = request.form.get('material_name', '').strip()
    code = request.form.get('material_code', '').strip()
    category_id = (request.form.get('category_id') or '').strip()
    unit = request.form.get('material_unit', '').strip() or 'Bags'
    if not name:
        flash('Material name is required', 'danger')
        return redirect(url_for('materials'))
    existing_name = Material.query.filter(
        func.lower(func.trim(Material.name)) == name.lower()
    ).first()
    if existing_name:
        flash(f'Material name "{name}" already exists', 'danger')
        return redirect(url_for('materials'))
    category = None
    if category_id:
        try:
            category = db.session.get(MaterialCategory, int(category_id))
        except Exception:
            category = None
    if not category:
        category = get_or_create_material_category('General')
    if not code:
        code = _next_material_code_for_category(category, material_name=name)
    if Material.query.filter_by(code=code).first():
        flash(f'Material code "{code}" already exists', 'danger')
        return redirect(url_for('materials'))
    new_mat = Material(name=name, code=code, category_id=category.id if category else None, unit=unit)
    db.session.add(new_mat)
    db.session.commit()
    if request.args.get('ajax'):
        return jsonify({'success': True, 'id': new_mat.id, 'name': new_mat.name, 'code': new_mat.code, 'price': new_mat.unit_price, 'unit': new_mat.unit})
    flash('Brand Added', 'success')
    return redirect(url_for('materials'))


@bp.route('/edit_material/<int:id>', methods=['POST'])
@login_required
def edit_material(id):
    m = db.session.get(Material, id)
    if m:
        new_code = request.form.get('material_code', '').strip()
        new_name = request.form.get('material_name', '').strip()
        category_id = (request.form.get('category_id') or '').strip()
        new_unit = request.form.get('material_unit', '').strip() or 'Bags'
        if not new_code:
            flash('Material code is required', 'danger')
            return redirect(url_for('materials'))
        if not new_name:
            flash('Material name is required', 'danger')
            return redirect(url_for('materials'))
        existing_name = Material.query.filter(
            func.lower(func.trim(Material.name)) == new_name.lower()
        ).first()
        if existing_name and existing_name.id != id:
            flash(f'Material name "{new_name}" already exists', 'danger')
            return redirect(url_for('materials'))
        existing = Material.query.filter_by(code=new_code).first()
        if existing and existing.id != id:
            flash(f'Material code "{new_code}" already exists', 'danger')
            return redirect(url_for('materials'))

        old_name = (m.name or '').strip()
        new_name = (new_name or '').strip()

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

        def _cascade_rename(old_value, new_value):
            if not old_value or not new_value or old_value.strip().lower() == new_value.strip().lower():
                return

            updates = [
                (Entry, Entry.material, 'Entry.material'),
                (Entry, Entry.booked_material, 'Entry.booked_material'),
                (BookingItem, BookingItem.material_name, 'BookingItem.material_name'),
                (DirectSaleItem, DirectSaleItem.product_name, 'DirectSaleItem.product_name'),
                (GRNItem, GRNItem.mat_name, 'GRNItem.mat_name'),
                (MaterialReturnItem, MaterialReturnItem.material_name, 'MaterialReturnItem.material_name'),
                (DeliveryItem, DeliveryItem.product, 'DeliveryItem.product'),
                (ReconBasket, ReconBasket.inv_material, 'ReconBasket.inv_material'),
            ]

            for model, col, _label in updates:
                try:
                    model.query.filter(_rename_filter(col, old_value)).update(
                        {col: new_value},
                        synchronize_session=False
                    )
                except Exception:
                    db.session.rollback()
                    raise

        _cascade_rename(old_name, new_name)

        m.name = new_name
        m.code = new_code
        m.unit = new_unit
        if category_id:
            try:
                cat = db.session.get(MaterialCategory, int(category_id))
            except Exception:
                cat = None
            if cat:
                m.category_id = cat.id
        db.session.commit()
        flash('Brand Updated', 'info')
    return redirect(url_for('materials'))


@bp.route('/bulk_update_material_unit', methods=['POST'])
@login_required
def bulk_update_material_unit():
    category_id = request.form.get('category_id')
    new_unit = request.form.get('new_unit', '').strip()
    
    if not new_unit:
        flash('Unit is required', 'danger')
        return redirect(url_for('materials'))

    query = Material.query
    if category_id:
        query = query.filter_by(category_id=int(category_id))
    
    count = query.update({Material.unit: new_unit}, synchronize_session=False)
    db.session.commit()
    flash(f'Updated unit to "{new_unit}" for {count} materials.', 'success')
    return redirect(url_for('materials'))


@bp.route('/material_categories/add', methods=['POST'])
@login_required
def add_material_category():
    if not _can_manage_categories():
        flash('Unauthorized', 'danger')
        return redirect(url_for('settings'))
    name = (request.form.get('category_name') or '').strip()
    if not name:
        flash('Category name is required', 'danger')
        return redirect(url_for('settings'))
    existing = MaterialCategory.query.filter(
        func.lower(func.trim(MaterialCategory.name)) == name.lower()
    ).first()
    if existing:
        flash('Category already exists', 'danger')
        return redirect(url_for('settings'))
    db.session.add(MaterialCategory(name=name, is_active=True))
    db.session.commit()
    flash('Category added', 'success')
    return redirect(url_for('settings'))


@bp.route('/material_categories/<int:id>/rename', methods=['POST'])
@login_required
def rename_material_category(id):
    if not _can_manage_categories():
        flash('Unauthorized', 'danger')
        return redirect(url_for('settings'))
    name = (request.form.get('category_name') or '').strip()
    if not name:
        flash('Category name is required', 'danger')
        return redirect(url_for('settings'))
    cat = db.session.get(MaterialCategory, id)
    if not cat:
        flash('Category not found', 'danger')
        return redirect(url_for('settings'))
    if cat.name.lower() == 'general' and name.lower() != 'general':
        flash('Default category cannot be renamed', 'danger')
        return redirect(url_for('settings'))
    existing = MaterialCategory.query.filter(
        func.lower(func.trim(MaterialCategory.name)) == name.lower()
    ).first()
    if existing and existing.id != cat.id:
        flash('Category already exists', 'danger')
        return redirect(url_for('settings'))
    cat.name = name
    db.session.commit()
    flash('Category updated', 'success')
    return redirect(url_for('settings'))


@bp.route('/material_categories/<int:id>/toggle', methods=['POST'])
@login_required
def toggle_material_category(id):
    if not _can_manage_categories():
        flash('Unauthorized', 'danger')
        return redirect(url_for('settings'))
    cat = db.session.get(MaterialCategory, id)
    if not cat:
        flash('Category not found', 'danger')
        return redirect(url_for('settings'))
    if cat.name.lower() == 'general' and cat.is_active:
        flash('Default category cannot be disabled', 'danger')
        return redirect(url_for('settings'))
    cat.is_active = not cat.is_active
    db.session.commit()
    flash('Category status updated', 'success')
    return redirect(url_for('settings'))


@bp.route('/delete_material/<int:id>', methods=['POST'])
@login_required
def delete_material(id):
    if not _user_can('can_manage_materials'):
        flash('Permission denied', 'danger')
        return redirect(url_for('materials'))
    m = db.session.get(Material, id)
    if m:
        m.is_active = not bool(m.is_active)
        db.session.commit()
        flash('Material status updated', 'warning')
    return redirect(url_for('materials'))


