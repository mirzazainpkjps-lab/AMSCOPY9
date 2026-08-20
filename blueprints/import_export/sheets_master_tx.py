"""sheets_master — split from import_export.py."""
from ._common import *  # noqa

def _process_dispatch(df, strategy, missing_client_strategy, report, allow_missing=False):
    for _, row in df.iterrows():
        # Helper to get stripped string value, returns empty string for NaN/None
        def get_val(key):
            val = row.get(key)
            return str(val).strip() if pd.notna(val) and val is not None else ''

        item = get_val('item')
        qty_str = get_val('qty')

        # Skip row if both material and qty are missing or qty is 0.
        if not item and (not qty_str or float(qty_str or 0) == 0):
            continue

        try:
            qty = float(qty_str) if qty_str else 0.0
        except ValueError:
            report['errors'] += 1
            report['error_details'].append(f"Invalid Qty '{qty_str}' for item '{item}'")
            continue

        client_code = get_val('client_code')
        client_name = get_val('customer')
        client_category = get_val('client_category')
        transaction_category = get_val('transaction_category').upper()
        bill_no = get_val('bill_no')
        entry_date_str = get_val('date')
        nimbus_no = get_val('nimbus_no')

        # Normalize key categories so a single template can represent all types.
        upper_client_cat = client_category.upper()
        is_open_khata_row = (
            upper_client_cat in ['OPEN KHATA', 'OPEN_KHATA'] or
            transaction_category in ['OPEN KHATA', 'OPEN_KHATA']
        )
        is_cash_unbilled_row = (
            transaction_category in ['UNBILLED', 'CASH'] or
            upper_client_cat in ['CASH', 'UNBILLED']
        )

        if is_open_khata_row:
            client_code = client_code or 'OPEN-KHATA'
            client_name = client_name or 'OPEN KHATA'
            if not client_category:
                client_category = 'Open Khata'

        if is_cash_unbilled_row and str(bill_no).upper() in ['NOT BILLED', 'CASH', '']:
            # Keep unbilled cash entries without pending-bill linkage.
            bill_no = ''

        # Handle date format
        entry_date = None
        if entry_date_str:
            try:
                entry_date = pd.to_datetime(entry_date_str).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                pass 

        if not entry_date:
            report['skipped'] += 1
            report['error_details'].append(f"Skipped: Missing or invalid date for item '{item}' (Bill: {bill_no})")
            continue

        # Check Client Dependency
        client = Client.query.filter_by(code=client_code).first()
        
        if not client:
            if client_name:
                client = Client.query.filter_by(name=client_name).first()

            if not client and (client_code or client_name):
                if missing_client_strategy == 'create':
                    new_code = client_code if client_code else generate_client_code()
                    if not Client.query.filter_by(code=new_code).first():
                        client = Client(
                            code=new_code,
                            name=client_name or 'Imported Client',
                            category=_clean_category(client_category),
                            is_active=True
                        )
                        db.session.add(client)
                        db.session.flush()
                elif missing_client_strategy == 'stop':
                    raise Exception(f"Missing client '{client_code or client_name}' for dispatch")
                else: # 'skip' is the default
                    if not allow_missing:
                        report['skipped'] += 1
                        report['error_details'].append(f"Skipped: Client '{client_code or client_name}' not found.")
                        continue
                    _record_discrepancy(report, f"Dispatch: Missing client '{client_code or client_name}' (imported as-is)")
        elif client_category:
            # Keep client master category aligned when import contains a category.
            client.category = client_category
        
        final_client_code = client.code if client else client_code
        final_client_name = client.name if client else client_name
        final_client_category = client_category or (client.category if client and client.category else '')

        # Ensure Material Exists
        mat = None
        if item:
            mat = Material.query.filter(func.lower(Material.name) == item.lower()).first()
            if not mat:
                mat = Material(name=item, code=f"MAT-{pk_now().strftime('%f')}", category_id=_default_material_category_id())
                db.session.add(mat)
                db.session.flush()
        
        # --- Create Entry ---
        entry = Entry(
            date=entry_date,
            time=pk_now().strftime('%H:%M:%S'),
            type='OUT',
            material=mat.name if mat else None,
            client=final_client_name,
            client_code=final_client_code,
            client_category=final_client_category,
            transaction_category=transaction_category or None,
            qty=qty,
            bill_no=bill_no,
            nimbus_no=nimbus_no,
            created_by=_actor_username()
        )
        db.session.add(entry)
        
        if mat and qty > 0:
            mat.total = (mat.total or 0) - qty
        
        # --- Sync Pending Bill ---
        # If data matches with client names and codes and bill no in pending bills it must sync
        if bill_no and str(bill_no).upper() not in ['CASH', 'NOT BILLED', ''] and transaction_category not in ['UNBILLED', 'CASH']:
            pb = PendingBill.query.filter_by(bill_no=bill_no).first()
            if pb:
                # Sync client details if there is a mismatch
                if pb.client_code != final_client_code:
                    pb.client_code = final_client_code
                    pb.client_name = final_client_name
            else:
                # Create new Pending Bill if it doesn't exist
                unit_price = mat.unit_price if mat else 0
                amount = qty * unit_price
                
                new_pb = PendingBill(
                    client_code=final_client_code,
                    client_name=final_client_name,
                    bill_no=bill_no,
                    nimbus_no=nimbus_no,
                    amount=amount,
                    reason=f"Imported Dispatch: {qty} {item}",
                    is_paid=False,
                    created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                    created_by=_actor_username(),
                    is_manual=True
                )
                db.session.add(new_pb)
        
        report['imported'] += 1


def _process_bookings(df, strategy, report, allow_missing=False):
    for _, row in df.iterrows():
        client_name = str(row.get('client_name', '')).strip()
        if not client_name:
            report['errors'] += 1
            _record_discrepancy(report, "Bookings: Missing client_name (imported as-is)")
            if not allow_missing:
                continue

        manual_bill_no = str(row.get('manual_bill_no', '')).strip()
        amount = float(row.get('amount', 0) or 0)
        paid_amount = float(row.get('paid_amount', 0) or 0)
        rent_item_revenue = float(row.get('rent_item_revenue', 0) or 0)
        delivery_rent_cost = float(row.get('delivery_rent_cost', 0) or 0)
        rent_variance_loss = float(row.get('rent_variance_loss', 0) or 0)
        note = str(row.get('note', '')).strip()
        date_posted = _parse_dt(row.get('date_posted')) or pk_now()

        existing = Booking.query.filter_by(client_name=client_name, manual_bill_no=manual_bill_no).first() if manual_bill_no else None
        if existing:
            # Keep pending bills in sync even when duplicate booking rows are skipped.
            _upsert_pending_bill_from_booking(client_name, manual_bill_no, amount, paid_amount, note)
            if strategy == 'update':
                existing.amount = amount
                existing.paid_amount = paid_amount
                existing.rent_item_revenue = rent_item_revenue
                existing.delivery_rent_cost = delivery_rent_cost
                existing.rent_variance_loss = rent_variance_loss
                existing.note = note
                existing.date_posted = date_posted
                report['updated'] += 1
            else:
                report['skipped'] += 1
            continue

        b = Booking(
            client_name=client_name,
            manual_bill_no=manual_bill_no or None,
            amount=amount,
            paid_amount=paid_amount,
            date_posted=date_posted,
            note=note
        )
        db.session.add(b)
        _upsert_pending_bill_from_booking(client_name, manual_bill_no, amount, paid_amount, note)
        report['imported'] += 1


def _process_booking_items(df, strategy, report):
    for _, row in df.iterrows():
        bill_no = str(row.get('booking_bill_no', '')).strip()
        client_name = str(row.get('booking_client_name', '')).strip()
        material_name = str(row.get('material_name', '')).strip()
        if not material_name:
            report['errors'] += 1
            continue

        booking = None
        if bill_no:
            booking = Booking.query.filter_by(manual_bill_no=bill_no, client_name=client_name).first()
        if not booking and client_name:
            booking = Booking.query.filter_by(client_name=client_name).order_by(Booking.id.desc()).first()
        if not booking:
            report['skipped'] += 1
            continue

        qty = float(row.get('qty', 0) or 0)
        price = float(row.get('price_at_time', 0) or 0)

        # Ensure material exists in master list so future forms can select it.
        mat = Material.query.filter(func.lower(Material.name) == material_name.lower()).first()
        if not mat:
            mat = Material(name=material_name, code=f"MAT-{pk_now().strftime('%f')}", unit_price=price or 0, category_id=_default_material_category_id())
            db.session.add(mat)
            db.session.flush()

        exists = BookingItem.query.filter_by(
            booking_id=booking.id,
            material_name=material_name,
            qty=qty,
            price_at_time=price
        ).first()
        if exists:
            report['skipped'] += 1
            continue

        db.session.add(BookingItem(
            booking_id=booking.id,
            material_name=material_name,
            qty=qty,
            price_at_time=price
        ))
        report['imported'] += 1


def _process_payments(df, strategy, report, allow_missing=False):
    for _, row in df.iterrows():
        client_name = str(row.get('client_name', '')).strip()
        amount = float(row.get('amount', 0) or 0)
        if not client_name:
            report['errors'] += 1
            _record_discrepancy(report, f"Payments: Missing client_name (amount={amount})")
            if not allow_missing and amount <= 0:
                continue
        manual_bill_no = str(row.get('manual_bill_no', '')).strip()
        method = str(row.get('method', 'Cash')).strip() or 'Cash'
        note = str(row.get('note', '')).strip()
        date_posted = _parse_dt(row.get('date_posted')) or pk_now()

        existing = Payment.query.filter_by(client_name=client_name, manual_bill_no=manual_bill_no, amount=amount).first() if manual_bill_no else None
        if existing:
            if strategy == 'update':
                existing.method = method
                existing.note = note
                existing.date_posted = date_posted
                report['updated'] += 1
            else:
                report['skipped'] += 1
            continue

        db.session.add(Payment(
            client_name=client_name,
            amount=amount,
            method=method,
            manual_bill_no=manual_bill_no or None,
            date_posted=date_posted,
            note=note
        ))
        report['imported'] += 1


def _process_sales(df, strategy, report, allow_missing=False):
    for _, row in df.iterrows():
        client_name = str(row.get('client_name', '')).strip()
        if not client_name:
            report['errors'] += 1
            _record_discrepancy(report, "Sales: Missing client_name (imported as-is)")
            if not allow_missing:
                continue
        manual_bill_no = str(row.get('manual_bill_no', '')).strip()
        auto_bill_no = str(row.get('auto_bill_no', '')).strip()
        category = str(row.get('category', 'Credit Customer')).strip() or 'Credit Customer'
        amount = float(row.get('amount', 0) or 0)
        paid_amount = float(row.get('paid_amount', 0) or 0)
        rent_item_revenue = float(row.get('rent_item_revenue', 0) or 0)
        delivery_rent_cost = float(row.get('delivery_rent_cost', 0) or 0)
        rent_variance_loss = float(row.get('rent_variance_loss', 0) or 0)
        note = str(row.get('note', '')).strip()
        date_posted = _parse_dt(row.get('date_posted')) or pk_now()

        existing = None
        if manual_bill_no:
            existing = DirectSale.query.filter_by(client_name=client_name, manual_bill_no=manual_bill_no).first()
        elif auto_bill_no:
            existing = DirectSale.query.filter_by(client_name=client_name, auto_bill_no=auto_bill_no).first()

        if existing:
            if strategy == 'update':
                existing.category = category
                existing.amount = amount
                existing.paid_amount = paid_amount
                existing.rent_item_revenue = rent_item_revenue
                existing.delivery_rent_cost = delivery_rent_cost
                existing.rent_variance_loss = rent_variance_loss
                existing.note = note
                existing.date_posted = date_posted
                report['updated'] += 1
            else:
                report['skipped'] += 1
            continue

        db.session.add(DirectSale(
            client_name=client_name,
            manual_bill_no=manual_bill_no or None,
            auto_bill_no=auto_bill_no or None,
            category=category,
            amount=amount,
            paid_amount=paid_amount,
            rent_item_revenue=rent_item_revenue,
            delivery_rent_cost=delivery_rent_cost,
            rent_variance_loss=rent_variance_loss,
            date_posted=date_posted,
            note=note
        ))
        report['imported'] += 1


def _process_sale_items(df, strategy, report):
    for _, row in df.iterrows():
        bill_no = str(row.get('sale_bill_no', '')).strip()
        client_name = str(row.get('sale_client_name', '')).strip()
        product_name = str(row.get('product_name', '')).strip()
        if not product_name:
            report['errors'] += 1
            continue
        sale = None
        if bill_no:
            sale = DirectSale.query.filter(
                DirectSale.client_name == client_name,
                or_(DirectSale.manual_bill_no == bill_no, DirectSale.auto_bill_no == bill_no)
            ).first()
        if not sale and client_name:
            sale = DirectSale.query.filter_by(client_name=client_name).order_by(DirectSale.id.desc()).first()
        if not sale:
            report['skipped'] += 1
            continue

        qty = float(row.get('qty', 0) or 0)
        price = float(row.get('price_at_time', 0) or 0)

        # Ensure product exists in material master for consistent downstream behavior.
        mat = Material.query.filter(func.lower(Material.name) == product_name.lower()).first()
        if not mat:
            mat = Material(name=product_name, code=f"MAT-{pk_now().strftime('%f')}", unit_price=price or 0, category_id=_default_material_category_id())
            db.session.add(mat)
            db.session.flush()

        exists = DirectSaleItem.query.filter_by(
            sale_id=sale.id,
            product_name=product_name,
            qty=qty,
            price_at_time=price
        ).first()
        if exists:
            report['skipped'] += 1
            continue

        db.session.add(DirectSaleItem(
            sale_id=sale.id,
            product_name=product_name,
            qty=qty,
            price_at_time=price
        ))
        report['imported'] += 1


