"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/edit_ledger_transaction/<string:trans_type>/<int:trans_id>', methods=['POST'])
@login_required
def edit_ledger_transaction(trans_type, trans_id):
    """Edit transaction from Client Ledger view. Routes to appropriate edit handler."""
    if trans_type == 'Payment':
        if not _user_can('can_manage_payments'):
            flash('Permission denied', 'danger')
            return redirect(request.referrer or url_for('index'))
        
        payment = Payment.query.get_or_404(trans_id)
        client = get_client_by_input(payment.client_name or '')
        
        # Update payment fields
        payment.amount = float(request.form.get('amount', 0) or 0)
        payment.method = request.form.get('method', 'Cash')
        payment.bank_name = request.form.get('bank_name', '').strip()
        payment.account_name = request.form.get('account_name', '').strip()
        payment.account_no = request.form.get('account_no', '').strip()
        manual_bill_raw = request.form.get('manual_bill_no', '').strip()
        payment.manual_bill_no = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
        payment.note = request.form.get('note', '').strip()
        
        date_str = (request.form.get('date_posted') or '').strip()
        if date_str:
            payment.date_posted = resolve_posted_datetime(date_str, fallback_dt=payment.date_posted or pk_now())
        
        # Validate bill uniqueness
        if payment.manual_bill_no:
            conflict = find_bill_conflict(payment.manual_bill_no)
            if conflict and not (conflict[0] == 'Payment' and conflict[1] == payment.id):
                flash(f"Manual bill '{payment.manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
                if client:
                    return redirect(url_for('financial_ledger', client_id=client.id))
                return redirect(request.referrer or url_for('index'))
        
        # Sync payment accounting and rebuild ledger
        _sync_payment_waive_off(payment)
        _sync_payment_accounting(payment)
        if client:
            rebuild_pending_bills(client_id=client.id)
        
        db.session.commit()
        flash('Payment updated successfully in Client Ledger', 'success')
        if client:
            return redirect(url_for('financial_ledger', client_id=client.id))
    
    elif trans_type == 'Booking':
        if not _user_can('can_manage_sales'):
            flash('Permission denied', 'danger')
            return redirect(request.referrer or url_for('index'))
        
        booking = Booking.query.get_or_404(trans_id)
        client = get_client_by_input(booking.client_name or '')
        
        # Update booking fields
        booking.manual_bill_no = (request.form.get('manual_bill_no', '') or '').strip()
        try:
            booking.discount, booking.discount_reason = _parse_discount_fields(
                request.form.get('discount', 0),
                request.form.get('discount_reason', ''),
                label='Booking discount',
                require_reason=False
            )
        except ValueError as ve:
            flash(str(ve), 'danger')
            if client:
                return redirect(url_for('financial_ledger', client_id=client.id))
            return redirect(request.referrer or url_for('index'))
        booking.note = request.form.get('note', '').strip()
        
        date_str = (request.form.get('date_posted') or '').strip()
        if date_str:
            booking.date_posted = resolve_posted_datetime(date_str, fallback_dt=booking.date_posted or pk_now())
        
        # Sync booking and rebuild ledger
        _sync_booking_pending_bill(booking)
        if client:
            rebuild_pending_bills(client_id=client.id)
        
        db.session.commit()
        flash('Booking updated successfully in Client Ledger', 'success')
        if client:
            return redirect(url_for('financial_ledger', client_id=client.id))
    
    elif trans_type == 'DirectSale':
        if not _user_can('can_manage_sales'):
            flash('Permission denied', 'danger')
            return redirect(request.referrer or url_for('index'))
        
        sale = DirectSale.query.get_or_404(trans_id)
        client = get_client_by_input(sale.client_name or '')
        
        # Update sale fields
        sale.amount = float(request.form.get('amount', 0) or 0)
        sale.manual_bill_no = (request.form.get('manual_bill_no', '') or '').strip()
        sale.category = (request.form.get('category', '') or '').strip()
        sale.payment_method = (request.form.get('payment_method', '') or '').strip()
        sale.note = request.form.get('note', '').strip()
        
        date_str = (request.form.get('date_posted') or '').strip()
        if date_str:
            sale.date_posted = resolve_posted_datetime(date_str, fallback_dt=sale.date_posted or pk_now())
        
        # Validate bill uniqueness
        if sale.manual_bill_no:
            conflict = find_bill_conflict(sale.manual_bill_no)
            if conflict and not (conflict[0] == 'DirectSale' and conflict[1] == sale.id):
                flash(f"Manual bill '{sale.manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
                if client:
                    return redirect(url_for('financial_ledger', client_id=client.id))
                return redirect(request.referrer or url_for('index'))
        
        # Sync sale and rebuild ledger
        _sync_direct_sale_pending_bill(sale)
        if client:
            rebuild_pending_bills(client_id=client.id)
        
        db.session.commit()
        flash('Direct Sale updated successfully in Client Ledger', 'success')
        if client:
            return redirect(url_for('financial_ledger', client_id=client.id))
    
    flash('Invalid transaction type', 'danger')
    return redirect(request.referrer or url_for('index'))

