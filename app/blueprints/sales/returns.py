"""returns — split from sales.py."""
from ._common import *  # noqa

@bp.route('/material_returns')
@login_required
def material_returns_page():
    show_mode = (request.args.get('show', 'active') or 'active').strip().lower()
    client_filter = (request.args.get('client') or '').strip()
    bill_filter = (request.args.get('bill_no') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    material_filter = (request.args.get('material') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(max(per_page, 10), 100)
    edit_id = (request.args.get('edit_id') or '').strip()

    returns_q = MaterialReturn.query.options(
        selectinload(MaterialReturn.items),
        selectinload(MaterialReturn.payment)
    )
    if show_mode == 'voided':
        returns_q = returns_q.filter(MaterialReturn.is_void == True)
    elif show_mode == 'all':
        returns_q = returns_q
    else:
        show_mode = 'active'
        returns_q = returns_q.filter(MaterialReturn.is_void == False)

    resolved_client = get_client_by_input(client_filter) if client_filter else None
    if client_filter:
        if resolved_client:
            returns_q = returns_q.filter(
                func.lower(func.trim(MaterialReturn.client_name)) == resolved_client.name.strip().lower()
            )
        else:
            returns_q = returns_q.filter(MaterialReturn.client_name.ilike(f'%{client_filter}%'))
    if bill_filter:
        returns_q = returns_q.filter(or_(
            MaterialReturn.manual_bill_no.ilike(f'%{bill_filter}%'),
            MaterialReturn.auto_bill_no.ilike(f'%{bill_filter}%')
        ))
    if date_from:
        returns_q = returns_q.filter(func.date(MaterialReturn.date_posted) >= date_from)
    if date_to:
        returns_q = returns_q.filter(func.date(MaterialReturn.date_posted) <= date_to)
    if material_filter:
        returns_q = returns_q.filter(
            MaterialReturn.items.any(MaterialReturnItem.material_name.ilike(f'%{material_filter}%'))
        )

    returns_pagination = returns_q.order_by(MaterialReturn.date_posted.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    next_auto = peek_next_bill_no(AUTO_BILL_NAMESPACES['MATERIAL_RETURN'])

    edit_return = None
    if edit_id.isdigit():
        edit_return = MaterialReturn.query.options(selectinload(MaterialReturn.items)).get(int(edit_id))
        if edit_return and edit_return.is_void:
            edit_return = None
    # Historical inactive materials remain visible while editing their original
    # return, but are never offered in the new-return selector.
    if edit_return:
        active_ids = {m.id for m in materials}
        historical_names = {(item.material_name or '').strip().lower() for item in edit_return.items}
        historical = Material.query.filter(
            func.lower(func.trim(Material.name)).in_(historical_names)
        ).order_by(Material.name.asc()).all() if historical_names else []
        materials.extend([m for m in historical if m.id not in active_ids])
    return render_template(
        'material_returns.html',
        returns=returns_pagination.items,
        clients=clients,
        materials=materials,
        show_mode=show_mode,
        client_filter=client_filter,
        bill_filter=bill_filter,
        date_from=date_from,
        date_to=date_to,
        material_filter=material_filter,
        pagination=returns_pagination,
        per_page=per_page,
        next_auto=next_auto,
        edit_return=edit_return
    )


@bp.route('/add_material_return', methods=['POST'])
@login_required
def add_material_return():
    try:
        client_input = (request.form.get('client_code') or request.form.get('client_name') or '').strip()
        client = get_client_by_input(client_input)
        if not client or not client.is_active:
            flash('Select a valid active client for material return.', 'danger')
            return redirect(url_for('material_returns_page'))

        date_str = (request.form.get('date') or '').strip()
        note = (request.form.get('note') or '').strip()
        manual_bill_raw = (request.form.get('manual_bill_no') or '').strip()
        manual_bill_no = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
        if manual_bill_no:
            conflict = find_bill_conflict(manual_bill_no)
            if conflict:
                flash(f"Manual bill '{manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
                return redirect(url_for('material_returns_page'))

        materials_list = request.form.getlist('material_name[]')
        material_ids = request.form.getlist('material_id[]')
        qtys = request.form.getlist('qty[]')
        try:
            return_type = _parse_transaction_return_type(request.form, default='normal')
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(url_for('material_returns_page'))
        unit_rates = request.form.getlist('unit_rate[]')
        rent_rates = request.form.getlist('rent_rate[]')
        parsed_items = []
        for mat_raw, mid_raw, qty_raw, unit_raw, rent_raw in zip_longest(
            materials_list, material_ids, qtys, unit_rates, rent_rates, fillvalue=''
        ):
            mat_txt = (mat_raw or '').strip()
            qty_val = _to_float_or_zero(qty_raw)
            if not mat_txt and not str(mid_raw or '').strip() and qty_val <= 0:
                continue
            try:
                mat_obj = resolve_transaction_material(
                    material_id=mid_raw,
                    typed_text=mat_txt,
                    require_active=True,
                )
            except ValueError as ve:
                flash(str(ve), 'danger')
                return redirect(url_for('material_returns_page'))
            if not mat_obj:
                flash(MATERIAL_NOT_SELECTED_MSG, 'danger')
                return redirect(url_for('material_returns_page'))
            if qty_val <= 0:
                continue

            unit_rate_val = 0.0
            rent_rate_val = 0.0
            if return_type == 'booked':
                rent_rate_val = _to_float_or_zero(rent_raw)
            else:
                unit_rate_val = _to_float_or_zero(unit_raw)
                if unit_rate_val <= 0:
                    unit_rate_val = float(mat_obj.unit_price or 0)
                if unit_rate_val <= 0:
                    flash(f'Unit price is required for "{mat_obj.name}".', 'danger')
                    return redirect(url_for('material_returns_page'))
                rent_rate_val = _to_float_or_zero(rent_raw)
            parsed_items.append({
                'material_id': mat_obj.id,
                'material_name': mat_obj.name,
                'qty': qty_val,
                'unit_rate': unit_rate_val,
                'rent_rate': rent_rate_val,
                'return_type': return_type,
            })

        if not parsed_items:
            flash('Add at least one valid return item with qty > 0.', 'danger')
            return redirect(url_for('material_returns_page'))

        if any(item.get('return_type') != return_type for item in parsed_items):
            flash('Return Type must be the same for every selected item in this return.', 'danger')
            return redirect(url_for('material_returns_page'))

        returnable_map = (
            _client_booked_material_returnable_qty_map(client)
            if return_type == 'booked'
            else _client_material_returnable_qty_map(client)
        )
        requested_by_name = {}
        for item in parsed_items:
            requested_by_name[item['material_name']] = (
                float(requested_by_name.get(item['material_name'], 0) or 0) + float(item['qty'] or 0)
            )
        for mat_name, qty_needed in requested_by_name.items():
            allowed = float(_returnable_qty_for_name(returnable_map, mat_name) or 0)
            if qty_needed > (allowed + 0.0001):
                flash(
                    f"Cannot return {qty_needed} of {mat_name}. "
                    f"Maximum returnable is {round(allowed, 2)}.",
                    'danger'
                )
                return redirect(url_for('material_returns_page'))

        posted_at = resolve_posted_datetime(date_str)
        auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES['MATERIAL_RETURN'])
        if return_type == 'booked':
            total_amount = sum(float(i['qty']) * float(i['rent_rate']) for i in parsed_items)
        else:
            total_amount = sum(float(i['qty']) * (float(i['unit_rate']) + float(i['rent_rate'])) for i in parsed_items)

        ret = MaterialReturn(
            client_name=client.name,
            return_type=return_type,
            amount=total_amount,
            manual_bill_no=manual_bill_no,
            auto_bill_no=auto_bill_no,
            date_posted=posted_at,
            note=note
        )
        db.session.add(ret)
        db.session.flush()

        bill_ref = ret.manual_bill_no or ret.auto_bill_no or f"RTN-{ret.id}"
        pay = Payment(
            client_id=client.id,
            client_name=client.name,
            amount=total_amount,
            payment_type='Material Return',
            source_type='MaterialReturn',
            source_id=ret.id,
            method='Material Return',
            manual_bill_no='',
            auto_bill_no=get_next_bill_no(AUTO_BILL_NAMESPACES['PAYMENT']),
            date_posted=posted_at,
            note=(f"[MATERIAL_RETURN:{ret.id}] {bill_ref}" + (f" | {note}" if note else ''))
        )
        db.session.add(pay)
        db.session.flush()
        ret.payment_id = pay.id

        entry_txn_category = 'Booked Return' if return_type == 'booked' else 'Return'
        entry_client_category = 'Booked Return' if return_type == 'booked' else 'Material Return'
        for item in parsed_items:
            # Keep legacy `price_at_time` meaningful:
            # - Booked return: rent_rate
            # - Normal return: unit_rate (rent stored separately)
            legacy_rate = float(item['rent_rate']) if return_type == 'booked' else float(item['unit_rate'])
            db.session.add(MaterialReturnItem(
                material_return_id=ret.id,
                material_name=item['material_name'],
                qty=item['qty'],
                unit_rate=item['unit_rate'],
                rent_rate=item['rent_rate'],
                price_at_time=legacy_rate
            ))
            db.session.add(Entry(
                date=posted_at.strftime('%Y-%m-%d'),
                time=posted_at.strftime('%H:%M:%S'),
                type='IN',
                material=item['material_name'],
                client=client.name,
                client_code=client.code,
                client_category=entry_client_category,
                qty=item['qty'],
                bill_no=bill_ref,
                nimbus_no='Material Return',
                created_by=current_user.username,
                transaction_category=entry_txn_category,
                note=note
            ))
            mat_obj = Material.query.filter_by(name=item['material_name']).first()
            if mat_obj:
                mat_obj.total = float(mat_obj.total or 0) + float(item['qty'] or 0)

        _sync_payment_waive_off(pay)
        if client and float(total_amount or 0) > 0:
            _apply_settlement_to_pending_bills_for_client(client, float(total_amount or 0), 0)
        db.session.commit()
        flash('Material return saved successfully.', 'success')
        return redirect(url_for('material_returns_page'))
    except Exception as e:
        db.session.rollback()
        logging.error(f"Material Return Error: {str(e)}")
        flash(f'Error processing material return: {str(e)}', 'danger')
        return redirect(url_for('material_returns_page'))


@bp.route('/edit_material_return/<int:id>', methods=['POST'])
@login_required
def edit_material_return(id):
    if current_user.role != 'admin':
        flash('Unauthorized', 'danger')
        return redirect(url_for('material_returns_page'))

    try:
        ret = MaterialReturn.query.options(selectinload(MaterialReturn.items)).get(id)
        if not ret:
            flash('Return not found.', 'danger')
            return redirect(url_for('material_returns_page'))
        if ret.is_void:
            flash('Deleted return cannot be edited.', 'danger')
            return redirect(url_for('material_returns_page'))

        client = get_client_by_input(ret.client_name or '')
        if not client:
            flash('Client not found for this return.', 'danger')
            return redirect(url_for('material_returns_page'))

        # Editing return-type is risky because it changes which dispatch pool it validates against.
        existing_type = (getattr(ret, 'return_type', None) or 'normal').strip().lower()
        try:
            new_type = _parse_transaction_return_type(request.form, default=existing_type)
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(url_for('material_returns_page', edit_id=ret.id))
        if new_type != existing_type:
            flash('Return type cannot be changed on edit. Delete and create a new return.', 'danger')
            return redirect(url_for('material_returns_page', edit_id=ret.id))

        date_str = (request.form.get('date') or '').strip()
        note = (request.form.get('note') or '').strip()

        materials_list = request.form.getlist('material_name[]')
        material_ids = request.form.getlist('material_id[]')
        qtys = request.form.getlist('qty[]')
        unit_rates = request.form.getlist('unit_rate[]')
        rent_rates = request.form.getlist('rent_rate[]')
        historical_names = {(it.material_name or '').strip().lower() for it in (ret.items or [])}

        parsed_items = []
        for mat_raw, mid_raw, qty_raw, unit_raw, rent_raw in zip_longest(
            materials_list, material_ids, qtys, unit_rates, rent_rates, fillvalue=''
        ):
            mat_txt = (mat_raw or '').strip()
            qty_val = _to_float_or_zero(qty_raw)
            if not mat_txt and not str(mid_raw or '').strip() and qty_val <= 0:
                continue
            try:
                mat_obj = resolve_transaction_material(
                    material_id=mid_raw,
                    typed_text=mat_txt,
                    require_active=True,
                    allow_inactive_names=historical_names,
                )
            except ValueError as ve:
                flash(str(ve), 'danger')
                return redirect(url_for('material_returns_page', edit_id=ret.id))
            if not mat_obj:
                flash(MATERIAL_NOT_SELECTED_MSG, 'danger')
                return redirect(url_for('material_returns_page', edit_id=ret.id))
            if qty_val <= 0:
                continue

            unit_rate_val = 0.0
            rent_rate_val = 0.0
            if existing_type == 'booked':
                rent_rate_val = _to_float_or_zero(rent_raw)
            else:
                unit_rate_val = _to_float_or_zero(unit_raw)
                if unit_rate_val <= 0:
                    unit_rate_val = float(mat_obj.unit_price or 0)
                if unit_rate_val <= 0:
                    flash(f'Unit price is required for "{mat_obj.name}".', 'danger')
                    return redirect(url_for('material_returns_page', edit_id=ret.id))
                rent_rate_val = _to_float_or_zero(rent_raw)

            parsed_items.append({
                'material_id': mat_obj.id,
                'material_name': mat_obj.name,
                'qty': qty_val,
                'unit_rate': unit_rate_val,
                'rent_rate': rent_rate_val,
                'return_type': existing_type,
            })

        if not parsed_items:
            flash('Add at least one valid return item with qty > 0.', 'danger')
            return redirect(url_for('material_returns_page', edit_id=ret.id))

        if any(item.get('return_type') != existing_type for item in parsed_items):
            flash('Return Type must be the same for every selected item in this return.', 'danger')
            return redirect(url_for('material_returns_page', edit_id=ret.id))

        # Returnable qty check (add back this return's previous qty so edits can reduce/increase safely).
        if existing_type == 'booked':
            returnable_map = _client_booked_material_returnable_qty_map(client)
        else:
            returnable_map = _client_material_returnable_qty_map(client)
        old_qty_by_material = {}
        for it in (ret.items or []):
            name = (getattr(it, 'material_name', None) or '').strip()
            if not name:
                continue
            old_qty_by_material[name] = float(old_qty_by_material.get(name, 0) or 0) + float(getattr(it, 'qty', 0) or 0)
        for k, v in old_qty_by_material.items():
            returnable_map[k] = float(returnable_map.get(k, 0) or 0) + float(v or 0)

        requested_by_name = {}
        for item in parsed_items:
            requested_by_name[item['material_name']] = (
                float(requested_by_name.get(item['material_name'], 0) or 0) + float(item['qty'] or 0)
            )
        for mat_name, qty_needed in requested_by_name.items():
            allowed = float(_returnable_qty_for_name(returnable_map, mat_name) or 0)
            if qty_needed > (allowed + 0.0001):
                flash(
                    f"Cannot return {qty_needed} of {mat_name}. "
                    f"Maximum returnable is {round(allowed, 2)}.",
                    'danger'
                )
                return redirect(url_for('material_returns_page', edit_id=ret.id))

        posted_at = resolve_posted_datetime(date_str) if date_str else (ret.date_posted or pk_now())
        if existing_type == 'booked':
            total_amount = sum(float(i['qty']) * float(i['rent_rate']) for i in parsed_items)
        else:
            total_amount = sum(float(i['qty']) * (float(i['unit_rate']) + float(i['rent_rate'])) for i in parsed_items)

        # Reverse stock impact of old entries, then recreate entries + items from the edited form.
        refs = _material_return_bill_refs(ret)
        old_entries = Entry.query.filter(Entry.bill_no.in_(refs), Entry.nimbus_no == 'Material Return', Entry.is_void == False).all()
        for e in old_entries:
            mat = Material.query.filter_by(name=e.material).first()
            if mat:
                mat.total = float(mat.total or 0) - float(e.qty or 0)
            db.session.delete(e)

        # Replace items
        for it in list(ret.items or []):
            db.session.delete(it)
        db.session.flush()

        ret.amount = total_amount
        ret.note = note
        ret.date_posted = posted_at

        bill_ref = ret.manual_bill_no or ret.auto_bill_no or f"RTN-{ret.id}"
        entry_txn_category = 'Booked Return' if existing_type == 'booked' else 'Return'
        entry_client_category = 'Booked Return' if existing_type == 'booked' else 'Material Return'

        for item in parsed_items:
            legacy_rate = float(item['rent_rate']) if existing_type == 'booked' else float(item['unit_rate'])
            db.session.add(MaterialReturnItem(
                material_return_id=ret.id,
                material_name=item['material_name'],
                qty=item['qty'],
                unit_rate=item['unit_rate'],
                rent_rate=item['rent_rate'],
                price_at_time=legacy_rate
            ))
            db.session.add(Entry(
                date=posted_at.strftime('%Y-%m-%d'),
                time=posted_at.strftime('%H:%M:%S'),
                type='IN',
                material=item['material_name'],
                client=client.name,
                client_code=client.code,
                client_category=entry_client_category,
                qty=item['qty'],
                bill_no=bill_ref,
                nimbus_no='Material Return',
                created_by=current_user.username,
                transaction_category=entry_txn_category,
                note=note
            ))
            mat_obj = Material.query.filter_by(name=item['material_name']).first()
            if mat_obj:
                mat_obj.total = float(mat_obj.total or 0) + float(item['qty'] or 0)

        pay = db.session.get(Payment, ret.payment_id) if ret.payment_id else None
        if pay and not pay.is_void:
            pay.client_id = client.id
            pay.payment_type = 'Material Return'
            pay.source_type = 'MaterialReturn'
            pay.source_id = ret.id
            pay.amount = total_amount
            pay.date_posted = posted_at
            pay.note = (pay.note or '').split('|')[0].strip() or pay.note
            pay.note = (f"[MATERIAL_RETURN:{ret.id}] {bill_ref}" + (f" | {note}" if note else ''))
            _sync_payment_waive_off(pay)

        if client:
            rebuild_pending_bills(client.id)

        db.session.commit()
        flash('Material return updated successfully.', 'success')
        return redirect(url_for('material_returns_page'))
    except Exception as e:
        db.session.rollback()
        logging.error(f"Material Return Edit Error: {str(e)}")
        flash(f'Error updating material return: {str(e)}', 'danger')
        return redirect(url_for('material_returns_page', edit_id=id))


