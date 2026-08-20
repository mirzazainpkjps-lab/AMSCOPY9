"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/add_direct_sale', methods=['POST'])
@login_required
def add_direct_sale():
  try:
    def as_bool(val):
        return str(val).strip().lower() in ['1', 'true', 'on', 'yes']

    def _fail_sale(msg):
        flash(msg, 'danger')
        _stash_direct_sale_form_draft(request.form, mode='add')
        return redirect(url_for('direct_sales_page', resume='add'))

    client_name = request.form.get('client_name', '').strip() or request.form.get('client_code', '').strip()
    driver_name = (request.form.get('driver_name') or '').strip()
    materials_list = request.form.getlist('product_name[]')
    alternate_list = request.form.getlist('alternate_material[]')
    qtys = request.form.getlist('qty[]')
    rates = request.form.getlist('unit_rate[]')
    grn_item_ids = request.form.getlist('grn_item_id[]')
    ignore_items = request.form.getlist('ignore_booking_item[]')
    # amount = float(request.form.get('amount', 0) or 0) # Recalculated below
    paid_amount = _to_float_or_zero(request.form.get('paid_amount', 0))
    
    # Get payment method data
    payment_method = request.form.get('payment_method', 'Cash')
    payment_account_id = request.form.get('payment_account_id')
    bank_name = request.form.get('bank_name', '').strip()
    account_name = request.form.get('account_name', '').strip()
    account_no = request.form.get('account_no', '').strip()
    expected_pay_category = _payment_expected_account_category(payment_method)
    
    if payment_account_id:
        try:
            payment_account_id = int(payment_account_id)
            account = Account.query.get(payment_account_id)
            if account:
                if expected_pay_category and (account.category or '').strip().lower() != expected_pay_category:
                    raise ValueError(f"Selected account must be a {expected_pay_category} account for method '{payment_method}'.")
                bank_name = account.bank_name or ''
                account_name = account.account_holder_name or account.name
                account_no = account.account_number or ''
            else:
                payment_account_id = None
        except (ValueError, TypeError) as ve:
            return _fail_sale(str(ve))
    
    if expected_pay_category != 'bank':
        bank_name = ''

    if paid_amount > 0 and expected_pay_category in ['cash', 'bank'] and not payment_account_id:
        return _fail_sale('Select a cash/bank account for the paid amount to post into Accounts.')

    # If nothing is paid now, do not store payment linkage details.
    if paid_amount <= 0:
        payment_account_id = None
        bank_name = ''
        account_name = ''
        account_no = ''
    if _user_can('can_manage_sales'):
        try:
            discount, discount_reason = _parse_discount_fields(
                request.form.get('discount', 0),
                request.form.get('discount_reason', ''),
                label='Sale discount',
                require_reason=False
            )
        except ValueError as ve:
            return _fail_sale(str(ve))
    else:
        discount = 0
        discount_reason = ''
    manual_bill_raw = request.form.get('manual_bill_no', '').strip()
    manual_bill_no = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
    allow_negative_stock = as_bool(request.form.get('allow_negative_stock'))
    note = request.form.get('note', '').strip()
    create_invoice = as_bool(request.form.get('create_invoice'))
    track_as_cash = as_bool(request.form.get('track_as_cash'))
    delivery_rent = _to_float_or_zero(request.form.get('delivery_rent', 0))
    delivery_allocations, delivery_alloc_err, delivery_alloc_total, delivery_bags_total, delivery_primary_name = _parse_delivery_allocations(request.form)
    if delivery_alloc_err:
        return _fail_sale(delivery_alloc_err)
    if delivery_allocations:
        driver_name = delivery_primary_name or driver_name
        delivery_rent = delivery_alloc_total
    draft_id = _safe_int(request.form.get('draft_id'))

    # Idempotency guard: the form mints a fresh key each time the sale sheet is
    # opened. A double-click / network retry re-submits the same key, so treat
    # it as an already-saved sale instead of creating a second transaction.
    idem_key = (request.form.get('idempotency_key') or '').strip() or None
    if idem_key:
        prior_sale = DirectSale.query.filter_by(idempotency_key=idem_key).order_by(DirectSale.id.desc()).first()
        if prior_sale:
            prior_bill = _direct_sale_default_bill_ref(prior_sale)
            flash('This sale was already saved (duplicate submission ignored).', 'info')
            return redirect(url_for(
                'direct_sales_page',
                download_bill=prior_bill,
                download_src='direct_sale',
                download_src_id=prior_sale.id,
                download_client_code=prior_sale.client_code,
                download_client_name=prior_sale.client_name,
            ))

    # Check for global setting
    settings = Settings.query.first()
    global_negative_stock_allowed = settings.allow_global_negative_stock if settings else False

    photo_path = save_photo(request.files.get('photo'))
    photo_url = request.form.get('photo_url', '').strip()

    category_input = request.form.get('category', '').strip()
    category = normalize_sale_category(category_input)
    sale_date_raw = request.form.get('sale_date', '').strip()
    sale_posted_at = resolve_posted_datetime(sale_date_raw, fallback_dt=pk_now())
    # Held drafts must use the final save time, not the original hold time.
    if draft_id:
        sale_posted_at = pk_now()

    if not driver_name:
        return _fail_sale('Delivery person is required for sale dispatch.')
    if not delivery_allocations:
        get_or_create_delivery_person(driver_name)
    if delivery_rent < 0:
        return _fail_sale('Delivery rent cannot be negative.')
    # Find client by name or code
    client = get_client_by_input(client_name)

    if client:
        client_name = client.name

    # 1. Calculate Booking Balances
    booking_balances = {}
    if client:
        booking_ids = [
            b.id for b in Booking.query.filter(
                func.lower(func.trim(Booking.client_name)) == func.lower(func.trim(client.name)),
                Booking.is_void == False
            ).all()
        ]
        booked_totals = {}
        if booking_ids:
            for item in BookingItem.query.filter(BookingItem.booking_id.in_(booking_ids)).all():
                key = _material_norm_key(item.material_name)
                if not key:
                    continue
                booked_totals[key] = booked_totals.get(key, 0) + float(item.qty or 0)

        delivered_totals = {}
        entries = Entry.query.filter(
            or_(Entry.client_code == client.code,
                func.lower(func.trim(Entry.client)) == func.lower(func.trim(client.name))),
            Entry.type == 'OUT',
            Entry.is_void == False,
            # Exclude Direct Sales that are NOT Booking Deliveries (i.e. Cash/Credit sales)
            # This prevents regular sales from reducing the booking balance
            not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
        ).all()
        for entry in entries:
            key = _material_norm_key(entry.booked_material or entry.material)
            if not key:
                continue
            delivered_totals[key] = delivered_totals.get(key, 0) + float(entry.qty or 0)

        # Include returned booked material quantities so that after a Booked Return,
        # the available booked material is correctly restored: available = booked - delivered + returned
        returned_booked_totals = {}
        return_entries = Entry.query.filter(
            or_(Entry.client_code == client.code,
                func.lower(func.trim(Entry.client)) == func.lower(func.trim(client.name))),
            Entry.type == 'IN',
            Entry.is_void == False,
            Entry.nimbus_no == 'Material Return',
            Entry.transaction_category == 'Booked Return'
        ).all()
        for entry in return_entries:
            key = _material_norm_key(entry.material)
            if not key:
                continue
            returned_booked_totals[key] = returned_booked_totals.get(key, 0) + float(entry.qty or 0)

        for mat_in in set(materials_list):
            try:
                mat_obj = resolve_transaction_material(typed_text=mat_in, require_active=True)
            except ValueError:
                continue
            if not mat_obj:
                continue
            key = _material_norm_key(mat_obj.name)
            booking_balances[key] = max(0, booked_totals.get(key, 0) - delivered_totals.get(key, 0) + returned_booked_totals.get(key, 0))

    # 2. Process Items (Auto-Split Booking vs Sale)
    processed_items = []
    calculated_amount = 0
    missing_rate_non_booked = []

    material_ids = request.form.getlist('material_id[]')
    alternate_ids = request.form.getlist('alternate_material_id[]')

    for idx, mat in enumerate(materials_list):
        qty_val = qtys[idx] if idx < len(qtys) else ''
        rate_val = rates[idx] if idx < len(rates) else ''
        mat_name_in = str(mat or '').strip()
        mid = material_ids[idx] if idx < len(material_ids) else ''
        qty = _to_float_or_zero(qty_val)
        if not mat_name_in and not str(mid or '').strip() and qty <= 0:
            continue
        try:
            mat_obj = resolve_transaction_material(
                material_id=mid,
                typed_text=mat_name_in,
                require_active=True,
            )
        except ValueError as ve:
            return _fail_sale(str(ve))
        if not mat_obj or qty <= 0:
            continue
        mat_name = mat_obj.name
        rate = _to_float_or_zero(rate_val)

        ignore_item = False
        if idx < len(ignore_items):
            ignore_item = str(ignore_items[idx] or '').strip().lower() in ['1', 'true', 'on', 'yes']
        # Cash / credit / open khata are chargeable sales — never silently
        # consume booking qty (that made amount=0 and voided stock).
        if category in ['Cash', 'Credit Customer', 'Open Khata']:
            ignore_item = True
        mat_key = _material_norm_key(mat_name)
        balance = 0 if ignore_item else booking_balances.get(mat_key, 0)
        qty_booking = 0
        qty_sale = qty

        alt_input = (alternate_list[idx] if idx < len(alternate_list) else '').strip()
        alt_mid = alternate_ids[idx] if idx < len(alternate_ids) else ''
        alt_obj = None
        if alt_input or str(alt_mid or '').strip():
            try:
                alt_obj = resolve_transaction_material(
                    material_id=alt_mid,
                    typed_text=alt_input,
                    require_active=True,
                )
            except ValueError as ve:
                return _fail_sale(str(ve))
            if not alt_obj:
                return _fail_sale(MATERIAL_NOT_SELECTED_MSG)

        if balance > 0:
            qty_booking = min(qty, balance)
            qty_sale = qty - qty_booking
            booking_balances[mat_key] -= qty_booking

        if alt_input and (ignore_item or qty_booking <= 0):
            return _fail_sale(f'Alternate material is only allowed for booked items. "{mat_name}" has no booking balance.')

        if qty_booking > 0:
            delivered_mat = alt_obj.name if alt_obj else mat_name
            processed_items.append({
                'product_name': delivered_mat,
                'booked_material': mat_name,
                'qty': qty_booking,
                'price_at_time': 0,
                'grn_item_id': None,  # Booking items don't need GRN tracking
                'is_booking': True,
                'is_alternate': bool(alt_obj and delivered_mat != mat_name)
            })

        if qty_sale > 0:
            if rate <= 0:
                rate = float(mat_obj.unit_price or 0)
            if rate <= 0:
                missing_rate_non_booked.append(mat_name)
            grn_item_id = None
            if idx < len(grn_item_ids) and grn_item_ids[idx]:
                try:
                    grn_item_id = int(grn_item_ids[idx])
                except (ValueError, TypeError):
                    grn_item_id = None
            processed_items.append({
                'product_name': mat_name,
                'booked_material': None,
                'qty': qty_sale,
                'price_at_time': rate,
                'grn_item_id': grn_item_id,
                'is_booking': False,
                'is_alternate': False
            })
            calculated_amount += (qty_sale * rate)

    if not processed_items:
        return _fail_sale('No valid material items were captured. Add at least one item with qty > 0.')

    processed_items = _dedupe_direct_sale_items(processed_items)
    processed_items = _expand_chargeable_items_fifo(
        processed_items,
        as_of_dt=(sale_posted_at.date() if sale_posted_at else None),
    )

    if missing_rate_non_booked:
        mats = ', '.join(sorted(set(missing_rate_non_booked)))
        return _fail_sale(f'Rate is required for non-booked items: {mats}')

    # 3. Stock Validation (Only for non-booked items)
    # Aggregate required quantities first to prevent cumulative overrun
    required_stock = {}
    required_alt_stock = {}
    for item in processed_items:
        if not item['is_booking']:
            mat = item['product_name']
            required_stock[mat] = required_stock.get(mat, 0) + item['qty']
        elif item.get('is_alternate'):
            mat = item.get('booked_material') or item['product_name']
            required_alt_stock[mat] = required_alt_stock.get(mat, 0) + item['qty']

    for mat, req_qty in required_stock.items():
        mat_obj = Material.query.filter_by(name=mat).first()
        if mat_obj:
            available = mat_obj.total or 0
            if not allow_negative_stock and not global_negative_stock_allowed and available < req_qty:
                raise ValueError(f"Insufficient stock for {mat}. Available: {available}, Required: {req_qty} (Non-booked). Enable 'Allow Negative Stock' or global setting to bypass.")
    for mat, req_qty in required_alt_stock.items():
        mat_obj = Material.query.filter_by(name=mat).first()
        if mat_obj:
            available = mat_obj.total or 0
            if not allow_negative_stock and not global_negative_stock_allowed and available < req_qty:
                raise ValueError(f"Insufficient stock for {mat}. Available: {available}, Required: {req_qty} (Alternate booking from original). Enable 'Allow Negative Stock' or global setting to bypass.")

    total_qty = sum(_to_float_or_zero(item.get('qty')) for item in processed_items)
    if delivery_allocations and delivery_bags_total > (total_qty + 0.0001):
        return _fail_sale('Total delivery bags cannot exceed total material quantity for this sale.')

    # Compute whether this client has any active booking balance (across all materials).
    # Available booked material = booked - delivered + returned.
    has_client_booking_balance = False
    if client:
        norm_name = (client.name or '').strip().lower()
        booked_rows_all = db.session.query(
            BookingItem.material_name,
            func.sum(BookingItem.qty)
        ).join(Booking, BookingItem.booking_id == Booking.id).filter(
            Booking.is_void == False,
            func.lower(func.trim(Booking.client_name)) == norm_name
        ).group_by(BookingItem.material_name).all()
        if booked_rows_all:
            booked_map_all = {}
            for mat, qty in booked_rows_all:
                key = _material_norm_key(mat)
                if key:
                    booked_map_all[key] = booked_map_all.get(key, 0) + float(qty or 0)
            delivered_rows_all = db.session.query(
                func.coalesce(Entry.booked_material, Entry.material),
                func.sum(Entry.qty)
            ).filter(
                Entry.type == 'OUT',
                Entry.is_void == False,
                or_(
                    Entry.client_code == client.code,
                    func.lower(func.trim(Entry.client)) == norm_name
                ),
                not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
            ).group_by(func.coalesce(Entry.booked_material, Entry.material)).all()
            delivered_map_all = {}
            for mat, qty in delivered_rows_all:
                key = _material_norm_key(mat)
                if key:
                    delivered_map_all[key] = delivered_map_all.get(key, 0) + float(qty or 0)
            # Also include returned booked material quantities
            returned_booked_rows = db.session.query(
                func.trim(Entry.material),
                func.sum(Entry.qty)
            ).filter(
                Entry.type == 'IN',
                Entry.is_void == False,
                Entry.nimbus_no == 'Material Return',
                Entry.transaction_category == 'Booked Return',
                or_(
                    Entry.client_code == client.code,
                    func.lower(func.trim(Entry.client)) == norm_name
                )
            ).group_by(func.trim(Entry.material)).all()
            returned_booked_map = {}
            for mat, qty in returned_booked_rows:
                key = _material_norm_key(mat)
                if key:
                    returned_booked_map[key] = returned_booked_map.get(key, 0) + float(qty or 0)
            has_client_booking_balance = any(
                booked_qty - delivered_map_all.get(mat_key, 0) + returned_booked_map.get(mat_key, 0) > 0
                for mat_key, booked_qty in booked_map_all.items()
            )

    # 4. Determine Final Category & Amount
    amount = calculated_amount
    all_booking = all(item['is_booking'] for item in processed_items)
    any_booking = any(item['is_booking'] for item in processed_items)
    category_input_l = category.lower()

    # Enforce selected sale-type policy.
    if category_input_l in ['booking delivery', 'mixed transaction', 'credit customer'] and not client:
        return _fail_sale('Select a registered client from the client list for this sale type.')

    if category == 'Booking Delivery':
        if not has_client_booking_balance or not all_booking:
            return _fail_sale('Booked Sale is only for clients with booking balance and booked materials only.')
        # Booked dispatch is fulfillment only — never a credit/due sale.
        amount = 0
        paid_amount = 0
        discount = 0
        create_invoice = False
        track_as_cash = False
    elif category == 'Mixed Transaction':
        if not has_client_booking_balance or not any_booking:
            return _fail_sale('Booked + Credit is only for clients with booking balance and must include booked items.')
        if all_booking or amount <= 0:
            return _fail_sale('Booked + Credit must include a non-booked credit portion with amount > 0.')
    elif category == 'Credit Customer':
        # Due sale can be used for any registered client; it must remain pure chargeable.
        if any_booking:
            return _fail_sale('Credit Sale cannot include booked-material fulfillment.')
    elif category == 'Open Khata':
        # Open Khata is for unregistered walk-in style credit.
        if client and client.code != OPEN_KHATA_CODE:
            return _fail_sale('Open Khata is only for unregistered customers (not selected from client list).')
    else:
        category = 'Cash'

    # Booking Delivery can have zero-priced dispatch (amount=0) while still allowing
    # a financial discount adjustment in client ledger.
    if category != 'Booking Delivery' and discount > (amount + 0.01):
        return _fail_sale('Discount cannot exceed total amount.')

    # Validation: Unbilled Cash Sale must be fully paid
    if category == 'Cash' and (paid_amount + discount) < (amount - 0.01):
        return _fail_sale('Cash Sale must be fully paid. Transaction not complete.')
    
    # Auto bill is enough when no manual number is provided.

    # Validation: Manual bill must be unique
    if manual_bill_no:
        conflict = find_bill_conflict(manual_bill_no)
        if conflict:
            return _fail_sale(f"Bill No '{manual_bill_no}' already exists. Please open the existing bill and edit it instead.")

    # Handle Cash category (Manual overrides)
    if category == 'Cash':
        manual_client_name = request.form.get('manual_client_name', '').strip()
        if manual_client_name:
            client_name = manual_client_name
    elif category == 'Open Khata':
        manual_client_name = request.form.get('manual_client_name', '').strip()
        if not manual_client_name:
            return _fail_sale('Open Khata requires manual customer name.')
        client_name = manual_client_name
        create_invoice = False
        track_as_cash = False

    # Force manual bill requirement for non-cash sales if not provided
    if category != 'Cash' and not manual_bill_no and not create_invoice:
        # We allow it but it will be auto-generated or marked as system bill
        pass

    hv = request.form.get('has_bill')
    has_bill = True if hv is None else hv in ['on', '1', 'true', 'True']

    pending_amount = max(0.0, amount - discount - paid_amount)
    sale_client_code = client.code if client else None
    if category == 'Open Khata' and not sale_client_code:
        sale_client_code = OPEN_KHATA_CODE
    rent_rec = _rent_reconciliation_from_items(
        processed_items,
        delivery_rent_cost=delivery_rent,
        client_name=client_name,
        client_code=sale_client_code
    )

    if (
        category in ['Mixed Transaction', 'Credit Customer', 'Open Khata']
        and pending_amount <= 0
        and discount <= 0
    ):
        return _fail_sale('This sale type is for credit only. Use Cash Sale if fully paid.')

    # Handle invoice creation
    inv = None
    invoice_no = None
    if create_invoice:
        if manual_bill_no:
            invoice_no = manual_bill_no
            is_manual = True
        else:
            # Invoice without manual bill no (auto)
            invoice_no = f"INV-{pk_now().strftime('%Y%m%d%H%M%S')}"
            is_manual = False

        existing_global = Invoice.query.filter_by(invoice_no=invoice_no).first()
        if existing_global:
            if is_manual:
                return _fail_sale(f'Invoice number "{invoice_no}" is already used. Please use a different number.')
            # Auto invoice: ensure uniqueness instead of reusing/updating
            while Invoice.query.filter_by(invoice_no=invoice_no).first():
                invoice_no = f"INV-{pk_now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"

        balance = max(0.0, amount - discount - paid_amount)
        status = 'PAID' if balance <= 0 else ('PARTIAL' if paid_amount > 0 else 'OPEN')

        inv = Invoice(client_code=sale_client_code,
                      client_name=client.name if client else client_name,
                      invoice_no=invoice_no,
                      is_manual=is_manual,
                      date=sale_posted_at.date(),
                      total_amount=amount,
                      # Note: Invoice model might not have discount column, so we reflect it in balance
                      balance=balance,
                      status=status,
                      is_cash=track_as_cash,
                      note=note,
                      created_at=sale_posted_at.strftime('%Y-%m-%d %H:%M'),
                      created_by=current_user.username)
        db.session.add(inv)
        db.session.flush()

    auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES['DIRECT_SALE'])

    sale = DirectSale(idempotency_key=idem_key,
                      client_name=client_name,
                      client_code=sale_client_code,
                      amount=amount,
                      paid_amount=paid_amount,
                      discount=discount,
                      discount_reason=discount_reason,
                      manual_bill_no=manual_bill_no,
                      auto_bill_no=auto_bill_no,
                      photo_path=photo_path,
                      photo_url=photo_url,
                      category=category,
                      note=note,
                      driver_name=driver_name,
                      rent_item_revenue=rent_rec['rent_item_revenue'],
                      delivery_rent_cost=rent_rec['delivery_rent_cost'],
                      rent_variance_loss=rent_rec['rent_variance_loss'],
                      payment_method=payment_method,
                      payment_account_id=payment_account_id,
                      bank_name=bank_name,
                      account_name=account_name,
                      account_no=account_no,
                      date_posted=sale_posted_at)
    db.session.add(sale)
    db.session.flush()

    if create_invoice and inv:
        sale.invoice_id = inv.id

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

    # Create DirectSaleItems and Entries
    now = sale_posted_at
    sale_item_records = []
    for item in processed_items:
        # Create Sale Item
        dsi = DirectSaleItem(sale_id=sale.id,
                           product_name=item['product_name'],
                           qty=item['qty'],
                           price_at_time=item['price_at_time'],
                           grn_item_id=item.get('grn_item_id'),
                           cost_rate_at_sale=item.get('cost_rate_at_sale'))
        db.session.add(dsi)
        sale_item_records.append((dsi, item))

        # Create Entry
        ledger_bill_ref = manual_bill_no or (inv.invoice_no if (create_invoice and inv) else (sale.auto_bill_no or ("UNBILLED-" + str(sale.id))))

        # Determine category per item for mixed transactions
        item_category = category
        if item['is_booking']:
            item_category = 'Booking Delivery'
        elif category == 'Mixed Transaction':
            item_category = 'Credit Customer'
        elif category == 'Booking Delivery': # Fallback if main cat is Booking but this item isn't (shouldn't happen with split logic)
            item_category = 'Credit Customer'

        entry_note = note
        if item.get('is_booking') and item.get('is_alternate') and item.get('booked_material'):
            extra = f"Alternate Material for Booked Sale (Original: {item['booked_material']})"
            entry_note = f"{note} | {extra}" if note else extra
        entry = Entry(date=now.strftime('%Y-%m-%d'),
                      time=now.strftime('%H:%M:%S'),
                      type='OUT',
                      material=item['product_name'],
                      booked_material=(item.get('booked_material') if item.get('is_alternate') else None),
                      client=client_name,
                      client_code=sale_client_code,
                      qty=item['qty'],
                      bill_no=ledger_bill_ref,
                      nimbus_no='Direct Sale',
                      created_by=current_user.username,
                      client_category=item_category,
                      transaction_category=('Unbilled' if category == 'Cash' else 'Billed'),
                      driver_name=driver_name,
                      note=entry_note,
                      is_alternate=bool(item.get('is_alternate')))
        _stamp_source(entry, 'sales', 'direct_sale', sale.id, ledger_bill_ref, item_category)
        db.session.add(entry)

        # Update Material stock (reduce In Hand)
        stock_name = item.get('booked_material') if item.get('is_alternate') else item['product_name']
        mat_obj = Material.query.filter_by(name=stock_name).first()
        if mat_obj:
            mat_obj.total = (mat_obj.total or 0) - item['qty']

    db.session.flush()
    _apply_booking_allocations_for_sale(sale, sale_item_records)
    _apply_grn_allocations_for_sale(sale, sale_item_records)

    # finalize_transaction() → rebuild_direct_sale_effects() already synchronises
    # pending bill, delivery rent, waive-off and accounts for this sale, then
    # recalculates stock — all inside the same DB transaction.  Calling those
    # four syncs here too was redundant duplicate work per submit.
    finalize_transaction('sales', sale.id)

    db.session.commit()
    if draft_id:
        try:
            draft_row = DirectSaleDraft.query.get(draft_id)
            if draft_row:
                db.session.delete(draft_row)
                db.session.commit()
        except Exception:
            db.session.rollback()
    msg = 'Direct sale added successfully'
    if create_invoice and inv:
        msg += f" â€” Invoice: {inv.invoice_no}"
    flash(msg, 'success')
  except Exception as e:
    db.session.rollback()
    logging.error(f"Direct Sale Error: {str(e)}")
    flash(f"Error processing sale: {str(e)}", "danger")
    _stash_direct_sale_form_draft(request.form, mode='add')
    return redirect(url_for('direct_sales_page', resume='add'))

  # Success redirect
  if manual_bill_no:
      bill_ref = manual_bill_no
  elif create_invoice and inv:
      bill_ref = inv.invoice_no
  elif sale.auto_bill_no:
      bill_ref = sale.auto_bill_no
  else:
      bill_ref = f"CSH-{sale.id}" if category == 'Cash' else f"DS-{sale.id}"

  return redirect(url_for(
      'direct_sales_page',
      download_bill=bill_ref,
      download_src='direct_sale',
      download_src_id=sale.id,
      download_client_code=sale_client_code,
      download_client_name=sale.client_name
  ))

