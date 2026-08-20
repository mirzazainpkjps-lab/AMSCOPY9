"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/edit_bill/DirectSale/<int:id>', methods=['POST'])
@login_required
def edit_direct_sale(id):
    try:
        def as_bool(val):
            return str(val).strip().lower() in ['1', 'true', 'on', 'yes']
        def _fail_edit(msg):
            flash(msg, 'danger')
            _stash_direct_sale_form_draft(request.form, mode='edit', sale_id=id)
            return redirect(url_for('direct_sales_page', resume='edit', sale_id=id))
        def _is_self_owned_sale_conflict(conflict_row, sale_obj, candidate_bill_no):
            """
            Allow edit when conflict points to records that belong to this same sale
            (linked invoice or derived direct-sale pending row).
            """
            if not conflict_row:
                return False
            src, row_id = conflict_row
            bill_variants = set(_bill_no_variants(candidate_bill_no))
            bill_variants.update(_direct_sale_bill_refs(sale_obj))

            if src == 'Invoice':
                return bool(getattr(sale_obj, 'invoice_id', None) and sale_obj.invoice_id == row_id)

            if src == 'PendingBill':
                pb = db.session.get(PendingBill, row_id)
                if not pb:
                    return False
                pb_bill = (pb.bill_no or '').strip()
                pb_reason = (pb.reason or '').strip().lower()
                same_bill = pb_bill in bill_variants
                # Direct-sale pending row is a derived tracker, not a true duplicate owner.
                return bool(same_bill and pb_reason.startswith('direct sale'))

            return False

        sale = DirectSale.query.get_or_404(id)
        old_refs = _direct_sale_bill_refs(sale)
        old_client_code, old_client_name = _direct_sale_client_identity(sale)
        old_active_entries = Entry.query.filter(
            Entry.bill_no.in_(old_refs),
            Entry.nimbus_no == 'Direct Sale',
            Entry.is_void == False
        )
        old_entry_client_filter = _entry_client_scope_filter(old_client_code, old_client_name)
        if old_entry_client_filter is not None:
            old_active_entries = old_active_entries.filter(old_entry_client_filter)
        old_active_entries = old_active_entries.all()

        category = normalize_sale_category(request.form.get('category', ''), default='Credit Customer')
        client_code = request.form.get('client_code', '').strip()
        client_name_input = request.form.get('client_name', '').strip()
        manual_client_name = request.form.get('manual_client_name', '').strip()
        client = get_client_by_input(client_code) or get_client_by_input(client_name_input)
        if category == 'Open Khata':
            if not manual_client_name:
                return _fail_edit('Open Khata requires manual customer name.')
            sale.client_name = manual_client_name
            client = None
        elif category == 'Cash' and manual_client_name:
            sale.client_name = manual_client_name
            client = None
        else:
            if client:
                sale.client_name = client.name
            elif client_name_input:
                sale.client_name = client_name_input

        # For registered-client sale types, force selection from client master.
        if category in ['Booking Delivery', 'Mixed Transaction', 'Credit Customer'] and not client:
            return _fail_edit('Select a registered client from the client list. Partial/manual client text is not allowed for this sale type.')

        driver_name = (request.form.get('driver_name') or sale.driver_name or '').strip()
        manual_bill_raw = request.form.get('manual_bill_no', '').strip()
        manual_bill_no = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
        paid_amount = _to_float_or_zero(request.form.get('paid_amount', 0))

        payment_method = (request.form.get('payment_method') or 'Cash').strip()
        payment_account_id = request.form.get('payment_account_id')
        bank_name = (request.form.get('bank_name') or '').strip()
        account_name = (request.form.get('account_name') or '').strip()
        account_no = (request.form.get('account_no') or '').strip()
        expected_pay_category = _payment_expected_account_category(payment_method)
        if payment_account_id:
            try:
                payment_account_id = int(payment_account_id)
                account = Account.query.get(payment_account_id)
                if not account:
                    return _fail_edit('Selected payment account not found.')
                if expected_pay_category and (account.category or '').strip().lower() != expected_pay_category:
                    return _fail_edit(f"Selected account must be a {expected_pay_category} account for method '{payment_method}'.")
                bank_name = account.bank_name or ''
                account_name = account.account_holder_name or account.name
                account_no = account.account_number or ''
            except Exception:
                return _fail_edit('Invalid payment account selection.')
        else:
            payment_account_id = None

        if expected_pay_category != 'bank':
            bank_name = ''

        if _user_can('can_manage_sales'):
            try:
                discount, discount_reason = _parse_discount_fields(
                    request.form.get('discount', 0),
                    request.form.get('discount_reason', ''),
                    label='Sale discount',
                    require_reason=False
                )
            except ValueError as ve:
                return _fail_edit(str(ve))
        else:
            discount = sale.discount or 0
            discount_reason = sale.discount_reason or ''
        note = request.form.get('note', '').strip()
        # Normalize posted timestamp via PK resolver for consistent timezone handling.
        sale_posted_at = resolve_posted_datetime(
            request.form.get('sale_date', '').strip(),
            fallback_dt=sale.date_posted or pk_now()
        )
        delivery_rent = _to_float_or_zero(request.form.get('delivery_rent', 0))
        delivery_allocations, delivery_alloc_err, delivery_alloc_total, delivery_bags_total, delivery_primary_name = _parse_delivery_allocations(request.form)
        if delivery_alloc_err:
            return _fail_edit(delivery_alloc_err)
        if delivery_allocations:
            driver_name = delivery_primary_name or driver_name
            delivery_rent = delivery_alloc_total

        if not driver_name:
            return _fail_edit('Delivery person is required for sale dispatch.')
        if not delivery_allocations:
            get_or_create_delivery_person(driver_name)
        if delivery_rent < 0:
            return _fail_edit('Delivery rent cannot be negative.')
        if category in ['Credit Customer', 'Mixed Transaction', 'Open Khata'] and not manual_bill_no:
            return _fail_edit('Manual Bill No is required for billed sales. Please enter it before saving.')

        if manual_bill_no:
            conflict = find_bill_conflict(manual_bill_no, exclude_sale_id=sale.id)
            if conflict and not _is_self_owned_sale_conflict(conflict, sale, manual_bill_no):
                return _fail_edit(f"Bill No '{manual_bill_no}' already exists. Please open the existing bill and edit it instead.")

        materials_list = request.form.getlist('product_name[]')
        alternate_list = request.form.getlist('alternate_material[]')
        material_ids = request.form.getlist('material_id[]')
        alternate_ids = request.form.getlist('alternate_material_id[]')
        qtys = request.form.getlist('qty[]')
        rates = request.form.getlist('unit_rate[]')
        grn_item_ids = request.form.getlist('grn_item_id[]')
        ignore_items = request.form.getlist('ignore_booking_item[]')

        # Compute per-material booking balances for the resolved client so that an
        # edit can split each posted line into a reserved (rate 0) slice and a
        # chargeable slice exactly like the add-sale flow does.  Without this, an
        # existing booked line loses its `is_booking` flag during reconstruction
        # and all booking allocations are silently dropped on save.
        booking_balances = {}
        if client and category in ['Booking Delivery', 'Mixed Transaction']:
            norm_client = func.lower(func.trim(client.name))
            booking_ids = [
                b.id for b in Booking.query.filter(
                    Booking.is_void == False,
                    func.lower(func.trim(Booking.client_name)) == norm_client,
                ).all()
            ]
            booked_totals = {}
            if booking_ids:
                for bit in BookingItem.query.filter(BookingItem.booking_id.in_(booking_ids)).all():
                    key = _material_norm_key(bit.material_name)
                    if key:
                        booked_totals[key] = booked_totals.get(key, 0) + float(bit.qty or 0)

            delivered_totals = {}
            del_query = Entry.query.filter(
                or_(Entry.client_code == client.code,
                    func.lower(func.trim(Entry.client)) == func.lower(func.trim(client.name))),
                Entry.type == 'OUT',
                Entry.is_void == False,
                not_(and_(Entry.nimbus_no == 'Direct Sale',
                          Entry.client_category != 'Booking Delivery'))
            )
            # Exclude entries that belong to the sale being edited: the old
            # allocations/entries are still in the table at this point and
            # would otherwise make the balance appear fully consumed, causing
            # a no-op edit to drop the booking allocation.
            del_query = del_query.filter(
                or_(Entry.source_module != 'sales',
                    Entry.source_id != sale.id)
            )
            del_rows = del_query.all()
            for e in del_rows:
                key = _material_norm_key(e.booked_material or e.material)
                if key:
                    delivered_totals[key] = delivered_totals.get(key, 0) + float(e.qty or 0)

            returned_totals = {}
            ret_query = Entry.query.filter(
                or_(Entry.client_code == client.code,
                    func.lower(func.trim(Entry.client)) == func.lower(func.trim(client.name))),
                Entry.type == 'IN',
                Entry.is_void == False,
                Entry.nimbus_no == 'Material Return',
                Entry.transaction_category == 'Booked Return'
            )
            # Same exclusion rationale as delivered totals above.
            ret_query = ret_query.filter(
                or_(Entry.source_module != 'sales',
                    Entry.source_id != sale.id)
            )
            ret_rows = ret_query.all()
            for e in ret_rows:
                key = _material_norm_key(e.material)
                if key:
                    returned_totals[key] = returned_totals.get(key, 0) + float(e.qty or 0)

            for raw in set(materials_list):
                try:
                    m = resolve_transaction_material(typed_text=raw, require_active=False)
                except ValueError:
                    continue
                if not m:
                    continue
                key = _material_norm_key(m.name)
                booking_balances[key] = max(
                    0.0,
                    float(booked_totals.get(key, 0) or 0)
                    - float(delivered_totals.get(key, 0) or 0)
                    + float(returned_totals.get(key, 0) or 0),
                )

        historical_product_names = [
            row[0]
            for row in db.session.query(DirectSaleItem.product_name).filter_by(sale_id=sale.id).all()
        ]
        parsed_items = []
        max_len = max(len(materials_list), len(alternate_list), len(qtys), len(rates))
        for idx in range(max_len):
            mat = materials_list[idx] if idx < len(materials_list) else ''
            alt = alternate_list[idx] if idx < len(alternate_list) else ''
            qty = qtys[idx] if idx < len(qtys) else ''
            rate = rates[idx] if idx < len(rates) else ''
            mat_name_in = str(mat or '').strip()
            mid = material_ids[idx] if idx < len(material_ids) else ''
            qty_val = _to_float_or_zero(qty)
            if not mat_name_in and not str(mid or '').strip() and qty_val <= 0:
                continue
            try:
                mat_obj = resolve_transaction_material(
                    material_id=mid,
                    typed_text=mat_name_in,
                    require_active=True,
                    allow_inactive_names=historical_product_names,
                )
            except ValueError as ve:
                return _fail_edit(str(ve))
            if not mat_obj:
                return _fail_edit(MATERIAL_NOT_SELECTED_MSG)
            if qty_val <= 0:
                continue
            rate_val = _to_float_or_zero(rate)
            if rate_val < 0:
                rate_val = 0
            alt_name_in = str(alt or '').strip()
            alt_mid = alternate_ids[idx] if idx < len(alternate_ids) else ''
            alt_obj = None
            if alt_name_in or str(alt_mid or '').strip():
                try:
                    alt_obj = resolve_transaction_material(
                        material_id=alt_mid,
                        typed_text=alt_name_in,
                        require_active=True,
                        allow_inactive_names=historical_product_names,
                    )
                except ValueError as ve:
                    return _fail_edit(str(ve))
                if not alt_obj:
                    return _fail_edit(MATERIAL_NOT_SELECTED_MSG)

            grn_item_id = None
            if idx < len(grn_item_ids) and grn_item_ids[idx]:
                try:
                    grn_item_id = int(grn_item_ids[idx])
                except (ValueError, TypeError):
                    grn_item_id = None

            ignore_flag = False
            if idx < len(ignore_items):
                ignore_flag = str(ignore_items[idx] or '').strip().lower() in ['1', 'true', 'on', 'yes']
            # Cash / credit / open khata are chargeable sales — never silently
            # consume booking qty (mirrors add_direct_sale).
            if category in ['Cash', 'Credit Customer', 'Open Khata']:
                ignore_flag = True

            delivered_name = alt_obj.name if alt_obj else mat_obj.name
            is_alt = bool(alt_obj and delivered_name != mat_obj.name)

            if is_alt and rate_val > 0:
                return _fail_edit('Alternate material is only allowed for booked items (rate 0).')

            # Default whole-row chargeable slice (used for pure cash/credit and
            # for any posted excess after the reserved slice is taken).
            qty_booking = 0.0
            qty_sale = qty_val
            mat_key = _material_norm_key(mat_obj.name)
            balance = 0.0 if ignore_flag else float(booking_balances.get(mat_key, 0) or 0)
            if balance > 0:
                qty_booking = min(qty_val, balance)
                qty_sale = qty_val - qty_booking
                booking_balances[mat_key] = balance - qty_booking

            if is_alt and (ignore_flag or qty_booking <= 0):
                return _fail_edit(
                    f'Alternate material is only allowed for booked items. "{mat_obj.name}" has no booking balance.'
                )

            if qty_booking > 0:
                parsed_items.append({
                    'product_name': delivered_name,
                    'booked_material': mat_obj.name if is_alt else None,
                    'qty': qty_booking,
                    'price_at_time': 0,
                    'grn_item_id': None,
                    'is_booking': True,
                    'is_alternate': is_alt,
                })

            if qty_sale > 0:
                # For a pure booked (rate 0) row with no booking balance left
                # there is nothing to charge.
                effective_rate = rate_val
                if effective_rate <= 0:
                    effective_rate = float(mat_obj.unit_price or 0)
                parsed_items.append({
                    'product_name': mat_obj.name,
                    'booked_material': None,
                    'qty': qty_sale,
                    'price_at_time': effective_rate,
                    'grn_item_id': grn_item_id,
                    'is_booking': False,
                    'is_alternate': False,
                })

        if not parsed_items:
            return _fail_edit('No valid material items were captured. Add at least one item with qty > 0.')

        parsed_items = _dedupe_direct_sale_items(parsed_items)
        parsed_items = _expand_chargeable_items_fifo(
            parsed_items,
            as_of_dt=(sale_posted_at.date() if sale_posted_at else None),
            exclude_sale_id=sale.id,
        )

        total_qty = sum(_to_float_or_zero(i.get('qty')) for i in parsed_items)
        if delivery_allocations and delivery_bags_total > (total_qty + 0.0001):
            return _fail_edit('Total delivery bags cannot exceed total material quantity for this sale.')

        any_booking_item = any(bool(i.get('is_booking')) for i in parsed_items)
        any_chargeable_item = any((not bool(i.get('is_booking'))) and float(i.get('price_at_time') or 0) > 0 for i in parsed_items)
        amount = sum((float(i['qty'] or 0) * float(i['price_at_time'] or 0)) for i in parsed_items)
        # Booking Delivery can have zero-priced dispatch (amount=0) while still allowing
        # a financial discount adjustment in client ledger.
        if category != 'Booking Delivery' and discount > (amount + 0.01):
            return _fail_edit('Discount cannot exceed total amount.')

        if category == 'Booking Delivery':
            if any_chargeable_item:
                return _fail_edit('Booked Sale can only contain reserved items (rate 0).')
            amount = 0
            paid_amount = 0
            discount = 0
            create_invoice = False
        elif category == 'Mixed Transaction':
            if not any_booking_item or not any_chargeable_item:
                return _fail_edit('Booked + Credit must contain both booked (rate 0) and non-booked (rate > 0) items.')
        else:
            if any_booking_item:
                return _fail_edit('This sale type cannot include booked items (rate 0).')
            if not any_chargeable_item:
                return _fail_edit('This sale type requires chargeable items with rate > 0.')

        if category == 'Cash' and (paid_amount + discount) < (amount - 0.01):
            return _fail_edit('Cash Sale must be fully paid. Transaction not complete.')

        if paid_amount > 0 and expected_pay_category in ['cash', 'bank'] and not payment_account_id:
            return _fail_edit('Select a cash/bank account for the paid amount to post into Accounts.')
        if paid_amount <= 0:
            payment_method = ''
            payment_account_id = None
            bank_name = ''
            account_name = ''
            account_no = ''

        # Allow fully-paid edits even if category is credit-style to avoid blocking historical edits.
        # (New sales still enforce credit-only rules in add_direct_sale.)

        if category == 'Open Khata' and not sale.client_name:
            sale.client_name = OPEN_KHATA_NAME
        rent_rec = _rent_reconciliation_from_items(
            parsed_items,
            delivery_rent_cost=delivery_rent,
            client_name=sale.client_name,
            client_code=(client.code if client else (OPEN_KHATA_CODE if category == 'Open Khata' else None))
        )

        sale.category = category
        sale.client_code = (client.code if client else (OPEN_KHATA_CODE if category == 'Open Khata' else None))
        sale.driver_name = driver_name
        sale.amount = amount
        sale.discount = discount
        sale.discount_reason = discount_reason
        sale.paid_amount = paid_amount
        sale.rent_item_revenue = rent_rec['rent_item_revenue']
        sale.delivery_rent_cost = rent_rec['delivery_rent_cost']
        sale.rent_variance_loss = rent_rec['rent_variance_loss']
        sale.manual_bill_no = manual_bill_no
        sale.note = note
        sale.date_posted = sale_posted_at
        sale.payment_method = payment_method
        sale.payment_account_id = payment_account_id
        sale.bank_name = bank_name
        sale.account_name = account_name
        sale.account_no = account_no

        sale.photo_url = request.form.get('photo_url', '').strip()
        new_photo = save_photo(request.files.get('photo'))
        if new_photo:
            sale.photo_path = new_photo

        # Preserve alternate-booking mapping from previous active rows when edit form
        # does not explicitly carry alternate source fields.
        old_alt_candidates = []
        for e in old_active_entries:
            bm = (e.booked_material or '').strip()
            if not bm:
                continue
            old_alt_candidates.append({
                'material': (e.material or '').strip(),
                'qty': float(e.qty or 0),
                'booked_material': bm,
                'used': False
            })

        def _take_old_booked_material(delivered_material, qty_val):
            delivered = (delivered_material or '').strip()
            try:
                q = float(qty_val or 0)
            except Exception:
                q = 0.0
            for row in old_alt_candidates:
                if row['used']:
                    continue
                if row['material'] != delivered:
                    continue
                if abs(float(row['qty'] or 0) - q) > 0.0001:
                    continue
                row['used'] = True
                return row['booked_material']
            return None

        # Sale lines are replaced wholesale. Preserve the old derived booking
        # links before removing them so no dangling FK or silent audit loss is
        # created. GRN allocations must be removed before their sale lines too.
        from app.services.allocation_integrity import archive_and_delete_booking_allocations
        old_booking_allocations = BookingAllocation.query.filter_by(sale_id=sale.id).all()
        archive_and_delete_booking_allocations(
            old_booking_allocations,
            reason="direct sale edit replaced source sale lines",
        )
        _delete_sale_grn_allocations(sale)
        DirectSaleItem.query.filter_by(sale_id=id).delete(synchronize_session=False)
        SaleDeliveryPerson.query.filter_by(sale_id=id).delete()

        if delivery_allocations:
            for alloc in delivery_allocations:
                db.session.add(SaleDeliveryPerson(
                    sale_id=sale.id,
                    delivery_person_id=alloc['delivery_person'].id,
                    bags_delivered=alloc['bags_delivered'],
                    rent_amount=alloc['rent_amount'],
                    created_at=sale_posted_at
                ))
        else:
            dp = get_or_create_delivery_person(driver_name)
            if dp:
                db.session.add(SaleDeliveryPerson(
                    sale_id=sale.id,
                    delivery_person_id=dp.id,
                    bags_delivered=0,
                    rent_amount=delivery_rent,
                    created_at=sale_posted_at
                ))

        bill_ref = _direct_sale_default_bill_ref(sale)
        entry_client_obj = client or get_client_by_input(sale.client_name or '')
        sale_item_records = []
        for item in parsed_items:
            dsi = DirectSaleItem(
                sale_id=sale.id,
                product_name=item['product_name'],
                qty=item['qty'],
                price_at_time=item['price_at_time'],
                grn_item_id=item.get('grn_item_id'),
                cost_rate_at_sale=item.get('cost_rate_at_sale'),
            )
            db.session.add(dsi)
            sale_item_records.append((dsi, item))

        db.session.flush()
        _apply_booking_allocations_for_sale(sale, sale_item_records)
        _apply_grn_allocations_for_sale(sale, sale_item_records)

        rebuild_direct_sale_effects(
            sale,
            old_refs=old_refs,
            old_client_code=old_client_code,
            old_client_name=old_client_name,
            rebuild_stock=True
        )
        validate_transaction_consistency('sales', sale.id)
        db.session.commit()
        flash('Direct sale updated and resynced', 'success')
        resolved_client_code = (entry_client_obj.code if entry_client_obj else (OPEN_KHATA_CODE if category == 'Open Khata' else None))
        return redirect(url_for(
            'direct_sales_page',
            download_bill=bill_ref,
            download_src='direct_sale',
            download_src_id=sale.id,
            download_client_code=resolved_client_code,
            download_client_name=sale.client_name
        ))
    except Exception as e:
        db.session.rollback()
        logging.error(f"Direct Sale Edit Error: {str(e)}")
        flash(f"Error updating sale: {str(e)}", "danger")
        _stash_direct_sale_form_draft(request.form, mode='edit', sale_id=id)
        return redirect(url_for('direct_sales_page', resume='edit', sale_id=id))

