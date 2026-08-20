"""client — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/ledger/<int:client_id>')
@login_required
def financial_ledger(client_id):
    client = Client.query.get_or_404(client_id)
    client_name_norm = (client.name or '').strip().lower()

    def _fmt_dt(dt_val):
        if not dt_val:
            return ''
        if isinstance(dt_val, str):
            return dt_val
        try:
            return dt_val.strftime('%Y-%m-%d %H:%M')
        except Exception:
            return str(dt_val)

    def _parse_dt(dt_val):
        if isinstance(dt_val, datetime):
            return dt_val
        if isinstance(dt_val, date):
            return datetime.combine(dt_val, datetime.min.time())
        if isinstance(dt_val, str):
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    return datetime.strptime(dt_val, fmt)
                except ValueError:
                    continue
        return datetime.min

    # 1. Fetch Pending Bills
    pending_bills = PendingBill.query.filter_by(client_code=client.code, is_void=False).order_by(PendingBill.id.desc()).all()

    # Sanitize pending bills for template to avoid NoneType error
    for pb in pending_bills:
        if pb.reason is None: pb.reason = ""

    # 2. Financial Ledger
    bookings = Booking.query.filter(func.lower(func.trim(Booking.client_name)) == client_name_norm).all()
    payments = Payment.query.filter(or_(
        Payment.client_id == client.id,
        and_(Payment.client_id.is_(None), func.lower(func.trim(Payment.client_name)) == client_name_norm),
    )).all()
    # Use case-insensitive match for Direct Sales to ensure we catch them all
    direct_sales = DirectSale.query.filter(func.lower(func.trim(DirectSale.client_name)) == client_name_norm).all()

    # Financial History (Bookings, Payments, Direct Sales) - NO Material Entries
    financial_history = []
    booking_bill_refs = set()
    direct_sale_bill_refs = set()

    cancel_bill_refs = set()
    cancel_amount_by_bill = {}
    cancel_bill_rows = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).all()
    for ce in cancel_bill_rows:
        bno = (ce.bill_no or '').strip()
        ano = (ce.auto_bill_no or '').strip()
        if bno:
            cancel_bill_refs.add(bno)
        if ano:
            cancel_bill_refs.add(ano)
        bill_ref = bno or ano
        if bill_ref:
            qty = float(ce.qty or 0)
            mat_ref = (ce.material or ce.booked_material or '').strip()
            amount = _resolve_cancel_display_amount(
                client_name_norm=client_name_norm,
                bill_ref=bill_ref,
                mat_ref=mat_ref,
                qty=qty,
                note=getattr(ce, 'note', None)
            )
            if amount is not None and amount > 0:
                cancel_amount_by_bill[bill_ref] = float(cancel_amount_by_bill.get(bill_ref, 0) or 0) + float(amount)

    for b in bookings:
        if b.is_void: continue
        if b.manual_bill_no:
            booking_bill_refs.add(b.manual_bill_no)
        if b.auto_bill_no:
            booking_bill_refs.add(b.auto_bill_no)
        booking_bill_refs.add(f"BK-{b.id}")
        booking_bill_ref = b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}"
        debit = _booking_ledger_gross_due(
            b,
            cancel_value=cancel_amount_by_bill.get(booking_bill_ref, 0),
            allow_legacy_lift=(booking_bill_ref not in cancel_bill_refs)
        )
        credit = b.paid_amount or 0
        discount = getattr(b, 'discount', 0) or 0
        financial_history.append({
            'date': b.date_posted,
            'date_display': _fmt_dt(b.date_posted),
            'description': 'Booking',
            'bill_no': booking_bill_ref,
            'debit': debit,
            'credit': credit,
            'type': 'Booking',
            'id': b.id
        })
        if float(discount or 0) > 0:
            discount_reason = (getattr(b, 'discount_reason', None) or '').strip()
            discount_desc = 'DISCOUNT WAIVE OFF'
            if discount_reason:
                discount_desc = f'DISCOUNT WAIVE OFF ({discount_reason})'
            financial_history.append({
                'date': b.date_posted,
                'date_display': _fmt_dt(b.date_posted),
                'description': discount_desc,
                'bill_no': booking_bill_ref,
                'debit': 0,
                'credit': float(discount or 0),
                'type': None,
                'id': None
            })

    waive_rows = WaiveOff.query.filter(
        func.lower(func.trim(WaiveOff.client_name)) == client_name_norm,
        WaiveOff.is_void == False
    ).filter(
        ~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%')
    ).order_by(WaiveOff.date_posted.asc(), WaiveOff.id.asc()).all()
    waive_by_payment = {}
    standalone_waive_rows = []
    for w in waive_rows:
        if w.payment_id:
            waive_by_payment.setdefault(w.payment_id, []).append(w)
        else:
            standalone_waive_rows.append(w)

    for p in payments:
        if p.is_void: continue
        amt = p.amount or 0
        method_label = p.method or "Cash"
        pay_details = []
        if getattr(p, 'bank_name', None):
            pay_details.append(f"Bank: {p.bank_name}")
        if getattr(p, 'account_name', None):
            pay_details.append(f"A/C Name: {p.account_name}")
        if getattr(p, 'account_no', None):
            pay_details.append(f"A/C No: {p.account_no}")
        details_suffix = f" - {' | '.join(pay_details)}" if pay_details else ''
        if amt >= 0:
            debit = 0
            credit = amt
            payment_desc = f'Payment ({method_label}){details_suffix}'
        else:
            debit = abs(amt)
            credit = 0
            payment_desc = f'Repayment ({method_label}){details_suffix}'

        # Payment row: only actual cash/bank amount.
        financial_history.append({
            'date': p.date_posted,
            'date_display': _fmt_dt(p.date_posted),
            'description': payment_desc,
            'bill_no': p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}",
            'debit': debit,
            'credit': credit,
            'type': 'Payment',
            'id': p.id
        })

        linked_waive_rows = waive_by_payment.get(p.id, [])
        if linked_waive_rows:
            for w in linked_waive_rows:
                w_desc = 'Waive-Off (Loss)'
                if (w.reason or '').strip():
                    w_desc = f'Waive-Off (Loss) ({w.reason.strip()})'
                financial_history.append({
                    'date': w.date_posted or p.date_posted,
                    'date_display': _fmt_dt(w.date_posted or p.date_posted),
                    'description': w_desc,
                    'bill_no': w.bill_no or p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}",
                    'debit': 0,
                    'credit': float(w.amount or 0),
                    'type': None,
                    'id': None
                })
        else:
            # Legacy fallback for older records where waive_off row does not exist.
            p_discount = float(getattr(p, 'discount', 0) or 0)
            if p_discount > 0:
                discount_reason = (getattr(p, 'discount_reason', None) or '').strip()
                discount_desc = 'Waive-Off (Loss)'
                if discount_reason:
                    discount_desc = f'Waive-Off (Loss) ({discount_reason})'
                financial_history.append({
                    'date': p.date_posted,
                    'date_display': _fmt_dt(p.date_posted),
                    'description': discount_desc,
                    'bill_no': p.manual_bill_no or p.auto_bill_no or f"PAY-{p.id}",
                    'debit': 0,
                    'credit': p_discount,
                    'type': None,
                    'id': None
                })

    def _waive_bill_ref(row):
        ref = (getattr(row, 'bill_no', None) or '').strip()
        if ref:
            return ref
        marker = (getattr(row, 'note', None) or '').strip()
        m = re.match(r'^\[direct_sale_discount:(\d+)\]$', marker, re.IGNORECASE)
        if m:
            sale = db.session.get(DirectSale, int(m.group(1)))
            if sale:
                return (sale.manual_bill_no or sale.auto_bill_no or f"DS-{sale.id}")
        return ''

    for w in standalone_waive_rows:
        w_desc = 'Waive-Off (Loss)'
        if (w.reason or '').strip():
            w_desc = f'Waive-Off (Loss) ({w.reason.strip()})'
        financial_history.append({
            'date': w.date_posted,
            'date_display': _fmt_dt(w.date_posted),
            'description': w_desc,
            'bill_no': _waive_bill_ref(w),
            'debit': 0,
            'credit': float(w.amount or 0),
            'type': None,
            'id': None
        })

    for s in direct_sales:
        if s.is_void: continue
        sale_bill_ref = (
            (s.invoice.invoice_no if getattr(s, 'invoice', None) else None)
            or s.manual_bill_no
            or s.auto_bill_no
            or f"DS-{s.id}"
        )
        if s.manual_bill_no:
            direct_sale_bill_refs.add(s.manual_bill_no)
        if s.auto_bill_no:
            direct_sale_bill_refs.add(s.auto_bill_no)
        if getattr(s, 'invoice', None) and s.invoice and s.invoice.invoice_no:
            direct_sale_bill_refs.add(s.invoice.invoice_no)
        direct_sale_bill_refs.add(f"UNBILLED-{s.id}")
        direct_sale_bill_refs.add(f"DS-{s.id}")
        direct_sale_bill_refs.add(f"CSH-{s.id}")
        debit = s.amount or 0
        credit = s.paid_amount or 0
        discount = getattr(s, 'discount', 0) or 0
        # A Direct Sale with no financial value is just a dispatch, not a financial event.
        # It should only appear in the material ledger.
        if debit > 0 or credit > 0:
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': 'Direct Sale',
                'bill_no': sale_bill_ref,
                'debit': debit,
                'credit': credit,
                'type': 'DirectSale',
                'id': s.id
            })
        if float(discount or 0) > 0:
            discount_reason = (getattr(s, 'discount_reason', None) or '').strip()
            discount_desc = 'DISCOUNT WAIVE OFF (Direct Sale)'
            if discount_reason:
                discount_desc = f'DISCOUNT WAIVE OFF (Direct Sale) ({discount_reason})'
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': discount_desc,
                'bill_no': sale_bill_ref,
                'debit': 0,
                'credit': float(discount or 0),
                'type': None,
                'id': None
            })
        # Company-side delivery rent variance should be visible in client financial timeline
        # as an informational row, but must not alter client running balance.
        rent_loss = float(getattr(s, 'rent_variance_loss', 0) or 0)
        if rent_loss > 0:
            financial_history.append({
                'date': s.date_posted,
                'date_display': _fmt_dt(s.date_posted),
                'description': f'Delivery Rent Variance (Company Loss) Rs.{rent_loss:.2f}',
                'bill_no': sale_bill_ref,
                'debit': 0,
                'credit': 0,
                'type': None,
                'id': None
            })

    # Explicit booking-cancellation rows for readability in financial ledger.
    cancel_entries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'CANCEL',
        Entry.is_void == False
    ).order_by(Entry.date.asc(), Entry.time.asc(), Entry.id.asc()).all()
    for ce in cancel_entries:
        qty = float(ce.qty or 0)
        bill_ref = (ce.bill_no or ce.auto_bill_no or '').strip()
        mat_ref = (ce.material or ce.booked_material or '').strip()
        amount = _resolve_cancel_display_amount(
            client_name_norm=client_name_norm,
            bill_ref=bill_ref,
            mat_ref=mat_ref,
            qty=qty,
            note=getattr(ce, 'note', None)
        )
        desc = f"Booking Cancel ({(ce.material or ce.booked_material or '-')} x {qty:.3f})"
        financial_history.append({
            'date': _parse_ledger_entry_dt(ce.date, ce.time),
            'date_display': _fmt_dt(_parse_ledger_entry_dt(ce.date, ce.time)),
            'description': desc,
            'bill_no': ce.bill_no or '',
            'debit': 0,
            'credit': float(amount or 0),
            'type': 'Entry',
            'id': ce.id,
            'is_cancel_entry': True,
            'cancel_amount': amount
        })

    # Sort by date (oldest first)
    opening_balance = _to_float_or_zero(getattr(client, 'opening_balance', 0))
    if opening_balance != 0:
        opening_dt = (
            getattr(client, 'opening_balance_date', None)
            or getattr(client, 'created_at', None)
            or datetime.min
        )
        financial_history.append({
            'date': opening_dt,
            'date_display': _fmt_dt(opening_dt),
            'description': 'Opening Balance',
            'bill_no': 'OPENING',
            'debit': opening_balance if opening_balance > 0 else 0,
            'credit': abs(opening_balance) if opening_balance < 0 else 0,
            'type': None,
            'id': None
        })

    opening_rows = [row for row in financial_history if row.get('bill_no') == 'OPENING']
    other_rows = [row for row in financial_history if row.get('bill_no') != 'OPENING']
    other_rows.sort(key=lambda r: _parse_dt(r.get('date')))
    financial_history = opening_rows + other_rows

    # Initial running balance for baseline financial events (Decimal for accuracy).
    running_balance = Decimal('0.00')
    for item in financial_history:
        item['debit'] = _money_round(item.get('debit', 0))
        item['credit'] = _money_round(item.get('credit', 0))
        running_balance += Decimal(str(item['debit'])) - Decimal(str(item['credit']))
        bal = running_balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if bal == Decimal('-0.00'):
            bal = Decimal('0.00')
        item['balance'] = float(bal)

    # 3. Material Ledger
    # Booking-reserved material ledger only:
    # - OUT/CANCEL against booked qty
    # - IN only when the return is a booked-material return (not cash/credit stock return)
    deliveries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        or_(
            Entry.type.in_(['OUT', 'CANCEL']),
            and_(
                Entry.type == 'IN',
                Entry.nimbus_no == 'Material Return',
                or_(
                    Entry.client_category == 'Booked Return',
                    Entry.transaction_category == 'Booked Return',
                ),
            )
        )
    ).order_by(Entry.date.asc(), Entry.time.asc()).all()

    material_history = []
    seen_material_bills = set()
    unresolved_dispatches = []

    # Add Bookings to Material Ledger
    bookings = Booking.query.filter(func.lower(func.trim(Booking.client_name)) == client_name_norm).order_by(Booking.date_posted.asc()).all()
    for b in bookings:
        if b.is_void: continue
        for item in b.items:
            created_at = getattr(b, 'created_at', None)
            date_sort = b.date_posted if b.date_posted else None
            if not date_sort and created_at:
                try:
                    date_sort = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
                except Exception:
                    try:
                        date_sort = datetime.strptime(created_at[:10], '%Y-%m-%d')
                    except Exception:
                        date_sort = None
            material_history.append({
                'date': b.date_posted.strftime('%Y-%m-%d') if b.date_posted else (created_at[:10] if created_at else ''),
                'date_sort': date_sort,
                'material': item.material_name,
                'material_group': item.material_name,
                'material_display': item.material_name,
                'qty_added': item.qty,
                'qty_dispatched': 0,
                'bill_no': b.manual_bill_no or b.auto_bill_no or f"BK-{b.id}",
                'nimbus_no': 'Booking',
                'type': 'Booking'
            })

    # Process Deliveries/Entries
    for d in deliveries:
        if d.is_void:
            continue
        bill_ref = d.bill_no or d.auto_bill_no

        if d.type == 'IN' and (d.nimbus_no == 'Material Return'):
            mat_name = d.material or d.booked_material
            if not mat_name:
                continue
            date_sort = None
            try:
                if d.date and d.time:
                    date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
                elif d.date:
                    date_sort = datetime.strptime(d.date, '%Y-%m-%d')
            except Exception:
                date_sort = None
            material_history.append({
                'date': d.date,
                'date_sort': date_sort,
                'material': mat_name,
                'material_group': mat_name,
                'material_display': mat_name,
                'qty_added': d.qty,
                'qty_dispatched': 0,
                'bill_no': bill_ref,
                'nimbus_no': d.nimbus_no or 'Material Return',
                'type': 'Return'
            })
            continue

        if d.type == 'CANCEL':
            mat_name = d.material or d.booked_material
            if not mat_name:
                continue
            date_sort = None
            try:
                if d.date and d.time:
                    date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
                elif d.date:
                    date_sort = datetime.strptime(d.date, '%Y-%m-%d')
            except Exception:
                date_sort = None
            material_history.append({
                'date': d.date,
                'date_sort': date_sort,
                'material': mat_name,
                'material_group': mat_name,
                'material_display': mat_name,
                'qty_added': 0,
                'qty_dispatched': d.qty,
                'bill_no': bill_ref,
                'nimbus_no': d.nimbus_no or 'Booking Cancel',
                'type': 'Cancel'
            })
            continue

        is_booking_delivery = (d.client_category == 'Booking Delivery') or (bill_ref in booking_bill_refs)

        if is_booking_delivery:
            group_mat = d.booked_material or d.material
            display_mat = group_mat
            if d.booked_material and d.material and d.booked_material != d.material:
                display_mat = f"{d.booked_material}>ALT>{d.material}"
            date_sort = None
            try:
                if d.date and d.time:
                    date_sort = datetime.strptime(f"{d.date} {d.time}", '%Y-%m-%d %H:%M:%S')
                elif d.date:
                    date_sort = datetime.strptime(d.date, '%Y-%m-%d')
            except Exception:
                date_sort = None
            material_history.append({
                'date': d.date,
                'date_sort': date_sort,
                'material': group_mat,
                'material_group': group_mat,
                'material_display': display_mat,
                'qty_added': 0,
                'qty_dispatched': d.qty,
                'bill_no': bill_ref,
                'nimbus_no': d.nimbus_no,
                'type': 'Dispatch'
            })
            if bill_ref:
                seen_material_bills.add(bill_ref)
        else:
            # Material ledger is booking-reserved only.
            # Non-booking dispatches (cash/credit direct sales etc.) must never appear here.
            # They remain visible in financial views/reports as applicable.
            continue

    for s in direct_sales:
        if s.is_void: continue
        sale_ref_candidates = set()
        if s.manual_bill_no:
            sale_ref_candidates.add(s.manual_bill_no)
        if s.auto_bill_no:
            sale_ref_candidates.add(s.auto_bill_no)
        if getattr(s, 'invoice', None) and s.invoice and s.invoice.invoice_no:
            sale_ref_candidates.add(s.invoice.invoice_no)

        # If any sale reference is already present from Entry rows, this sale dispatch is
        # already represented and must not be appended again under another bill number.
        if sale_ref_candidates & seen_material_bills:
            continue

        # Also skip standalone Direct Sales in this fallback loop
        if s.category != 'Booking Delivery':
            continue

        bill_ref = (
            (s.invoice.invoice_no if getattr(s, 'invoice', None) and s.invoice else None)
            or s.manual_bill_no
            or s.auto_bill_no
            or f"DS-{s.id}"
        )

        for item in s.items:
            # Skip non-booked items (Price > 0) in mixed transactions
            if item.price_at_time > 0:
                continue

            date_sort = s.date_posted if s.date_posted else None
            material_history.append({
                'date': s.date_posted.strftime('%Y-%m-%d') if s.date_posted else '',
                'date_sort': date_sort,
                'material': item.product_name,
                'material_group': item.product_name,
                'material_display': item.product_name,
                'qty_added': 0,
                'qty_dispatched': item.qty,
                'bill_no': bill_ref,
                'nimbus_no': 'Direct Sale',
                'type': 'Dispatch'
            })

    # Financial rows can also be appended during delivery processing (e.g., non-booking
    # dispatches). Recompute running balances so every rendered row has `balance`.
    opening_rows = [row for row in financial_history if row.get('bill_no') == 'OPENING']
    other_rows = [row for row in financial_history if row.get('bill_no') != 'OPENING']
    other_rows.sort(key=lambda r: _parse_dt(r.get('date')))
    financial_history = opening_rows + other_rows
    running_balance = Decimal('0.00')
    for item in financial_history:
        item['debit'] = _money_round(item.get('debit', 0))
        item['credit'] = _money_round(item.get('credit', 0))
        running_balance += Decimal(str(item['debit'])) - Decimal(str(item['credit']))
        bal = running_balance.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if bal == Decimal('-0.00'):
            bal = Decimal('0.00')
        item['balance'] = float(bal)

    # Sort by date (oldest first)
    # Sort by date, then by type priority (Booking/Add before Dispatch) to ensure balance doesn't dip
    def mat_sort_key(x):
        d = x.get('date_sort') or datetime.min
        t = x['type']
        # When timestamps exist, sort strictly by time to preserve real order.
        # Only use type priority for rows missing a timestamp.
        if d != datetime.min:
            p = 0
        else:
            if t == 'Booking':
                p = 0
            elif t == 'Cancel':
                p = 1
            elif t == 'Direct Sale':
                p = 2
            else:
                p = 3
        return (d, p)

    material_history.sort(key=mat_sort_key)

    # Running balance per material
    mat_balances = {}
    for item in material_history:
        mat = item.get('material_group') or item['material']
        if mat not in mat_balances:
            mat_balances[mat] = 0
        # Cancellation rows are informational only; do not alter running balance.
        if item.get('type') != 'Cancel':
            mat_balances[mat] += (item.get('qty_added', 0) - item.get('qty_dispatched', 0))
        item['balance'] = mat_balances[mat]

    # Group material history by material so UI can render separate sections.
    material_history_grouped = {}
    for item in material_history:
        mat_name = item.get('material_group') or item.get('material') or 'Unknown'
        material_history_grouped.setdefault(mat_name, []).append(item)

    # Calculate totals
    total_debit = sum(Decimal(str(item.get('debit', 0))) for item in financial_history)
    total_credit = sum(Decimal(str(item.get('credit', 0))) for item in financial_history)
    total_balance = total_debit - total_credit
    total_debit = _money_round(total_debit)
    total_credit = _money_round(total_credit)
    total_balance = _money_round(total_balance)

    # Cancellation preview for remaining bookings (LIFO by booking date).
    # Shared plan: remaining = booked - delivered + returned_booked, so qty
    # credited back by a booked material return is cancellable again.
    from app.services.booking_cancel_plan import build_client_booking_cancel_plan

    plan_rows, cancel_total, cancel_total_qty = build_client_booking_cancel_plan(client)
    cancel_rows = [
        {
            'item_id': row['item_id'],
            'material': row['material'],
            'booking_date': row['booking_date'],
            'bill_no': row['bill_no'],
            'qty_remaining': row['remaining_qty'],
            'rate': row['rate'],
            'amount': row['amount'],
        }
        for row in plan_rows
    ]

    cancel_rows.sort(
        key=lambda x: (x.get('material') or '', x.get('booking_date') or ''),
    )
    cancel_total = _money_round(cancel_total)
    cancel_new_balance = _money_round(Decimal(str(total_balance)) - Decimal(str(cancel_total)))
    cancel_client_due = max(0, cancel_new_balance)
    cancel_company_due = max(0, -cancel_new_balance)

    all_clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()

    # Build transaction objects for modals
    transactions_map = {}
    for b in bookings:
        if not b.is_void:
            transactions_map[f"Booking{b.id}"] = b
    for p in payments:
        if not p.is_void:
            transactions_map[f"Payment{p.id}"] = p
    for s in direct_sales:
        if not s.is_void:
            transactions_map[f"DirectSale{s.id}"] = s

    return render_template('client_ledger.html',
                           client=client,
                           pending_bills=pending_bills,
                           financial_history=financial_history,
                           material_history=material_history,
                           material_history_grouped=material_history_grouped,
                           unresolved_dispatches=unresolved_dispatches,
                           total_debit=total_debit,
                           total_credit=total_credit,
                           total_balance=total_balance,
                           cancel_rows=cancel_rows,
                           cancel_total=cancel_total,
                           cancel_total_qty=cancel_total_qty,
                           cancel_new_balance=cancel_new_balance,
                           cancel_client_due=cancel_client_due,
                           cancel_company_due=cancel_company_due,
                           clients=all_clients,
                           materials=materials,
                           transactions_map=transactions_map)

