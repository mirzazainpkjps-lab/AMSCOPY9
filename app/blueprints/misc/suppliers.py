"""suppliers — split from misc.py."""
from ._common import *  # noqa

@bp.route('/pay_supplier', methods=['GET'])
@login_required
def pay_supplier_page():
    return redirect(url_for('accounts.supplier_payments'))


@bp.route('/supplier_ledger/<int:id>')
@login_required
def supplier_ledger(id):
    """Supplier ledger using the same projection/filter contract as clients."""
    supplier = Supplier.query.get_or_404(id)
    requested_supplier = (request.args.get('supplier_search') or '').strip()
    if requested_supplier:
        alternate = get_supplier_by_input(requested_supplier)
        if alternate and alternate.id != supplier.id:
            preserved = request.args.to_dict()
            preserved.pop('supplier_search', None)
            return redirect(url_for('supplier_ledger', id=alternate.id, **preserved))
    filters = {
        'start_date': (request.args.get('start_date') or request.args.get('date_from') or '').strip(),
        'end_date': (request.args.get('end_date') or request.args.get('date_to') or '').strip(),
        'type_filter': (request.args.get('type') or request.args.get('transaction_type') or '').strip(),
        'query': (request.args.get('q') or request.args.get('search') or '').strip(),
        'amount_min': (request.args.get('amount_min') or '').strip(),
        'amount_max': (request.args.get('amount_max') or '').strip(),
        'account_filter': (request.args.get('account') or '').strip(),
        'status_filter': (request.args.get('status') or '').strip(),
    }
    from app.services.financial_ledgers import build_supplier_financial_ledger
    ledger = build_supplier_financial_ledger(supplier, **filters)
    selected = ledger['filtered_rows']
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    per_page = min(max(request.args.get('per_page', 25, type=int) or 25, 10), 100)
    total = len(selected)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    page_rows = selected[(page - 1) * per_page: page * per_page]
    closing = page_rows[-1]['balance'] if page_rows else ledger['closing_balance']
    suppliers = Supplier.query.filter(Supplier.is_active == True).order_by(Supplier.name.asc(), Supplier.id.asc()).all()
    return render_template(
        'financial_ledger.html',
        entity=supplier,
        entity_type='supplier',
        ledger=ledger,
        rows=page_rows,
        all_rows=ledger['rows'],
        obligations=[],
        filters=filters,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        filtered_closing=closing,
        selector_entities=suppliers,
        current_payable=max(0.0, float(ledger['closing_balance'] or 0)),
        back_url=url_for('suppliers'),
        opening_url=url_for('supplier_opening_balance', id=supplier.id),
        today_date=pk_today().strftime('%Y-%m-%d'),
    )


@bp.route('/download_supplier_ledger/<int:id>')
@login_required
def download_supplier_ledger(id):
    """Export all filtered supplier movements (not only the visible page)."""
    supplier = Supplier.query.get_or_404(id)
    filters = {
        'start_date': (request.args.get('start_date') or '').strip(),
        'end_date': (request.args.get('end_date') or '').strip(),
        'type_filter': (request.args.get('type') or '').strip(),
        'query': (request.args.get('q') or '').strip(),
        'amount_min': (request.args.get('amount_min') or '').strip(),
        'amount_max': (request.args.get('amount_max') or '').strip(),
        'account_filter': (request.args.get('account') or '').strip(),
        'status_filter': (request.args.get('status') or '').strip(),
    }
    from app.services.financial_ledgers import build_supplier_financial_ledger
    ledger = build_supplier_financial_ledger(supplier, **filters)
    action = (request.args.get('action') or 'download').lower()
    if action != 'print':
        import csv
        from io import StringIO
        out = StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['Date', 'Type', 'Reference', 'Description', 'Debit', 'Credit', 'Balance', 'Account', 'Notes'])
        for row in ledger['filtered_rows']:
            writer.writerow([
                row['date'].strftime('%Y-%m-%d %H:%M') if row.get('date') and row['date'] != datetime.min else '',
                row.get('type', ''), row.get('reference', ''), row.get('description', ''),
                f"{row.get('debit', 0):.2f}", f"{row.get('credit', 0):.2f}",
                f"{row.get('balance', 0):.2f}", row.get('account', ''), row.get('note', ''),
            ])
        response = Response(out.getvalue(), mimetype='text/csv; charset=utf-8')
        response.headers['Content-Disposition'] = 'attachment; filename=supplier-ledger.csv'
        return response
    rendered = render_template(
        'supplier_ledger_print.html',
        supplier=supplier, ledger=ledger['filtered_rows'],
        final_balance=ledger['closing_balance'], total_bill=ledger['total_credit'],
        total_paid=ledger['total_debit'], generated_at=pk_now(), auto_print=True
    )
    response = make_response(rendered)
    response.headers['Content-Disposition'] = 'inline; filename=supplier-ledger.html'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


@bp.route('/download_supplier_payment/<int:payment_id>')
@login_required
def download_supplier_payment(payment_id):
    payment = SupplierPayment.query.get_or_404(payment_id)
    supplier = db.session.get(Supplier, payment.supplier_id)
    supplier_name = supplier.name if supplier else 'Supplier'

    bill_view = SimpleNamespace(
        manual_bill_no=f"PAY-{payment.id}",
        auto_bill_no='',
        invoice_no='',
        date_posted=payment.date_posted,
        client_name=supplier_name,
        supplier=supplier_name,
        amount=payment.amount or 0,
        paid_amount=0,
        method=payment.method or '',
        bank_name=payment.bank_name or '',
        account_name=payment.account_name or '',
        account_no=payment.account_no or '',
        note=payment.note or ''
    )

    action = (request.args.get('action') or 'download').lower()
    disposition = 'inline' if action == 'print' else 'attachment'

    rendered = render_template(
        'view_bill.html',
        bill=bill_view,
        type='Payment',
        items=[],
        client=None,
        client_balance=0,
        previous_balance=0,
        recent_deliveries=[],
        material_ledger_recent=[],
        material_stock_summary=[],
        auto_print=(action == 'print')
    )
    if action == 'download':
        pdf_response = _try_render_weasy_pdf(
            rendered,
            _download_filename('SUPPLIERPAYMENT', 'pdf'),
            disposition=disposition
        )
        if pdf_response:
            return pdf_response

    response = make_response(rendered)
    response.headers['Content-Disposition'] = f'{disposition}; filename={_download_filename("SUPPLIERPAYMENT", "html")}'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response


@bp.route('/add_supplier_payment', methods=['POST'])
@login_required
def add_supplier_payment():
    """Compatibility endpoint delegating to the canonical Accounts service."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    supplier_id = request.form.get('supplier_id')
    try:
        from app.services.payments_crud import save_supplier_payment
        payment, _ = save_supplier_payment(
            supplier_id=supplier_id,
            amount=request.form.get('amount', 0),
            method=request.form.get('method', 'Cash'),
            payment_account_id=request.form.get('payment_account_id'),
            manual_bill_no=request.form.get('manual_bill_no', ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', ''),
            idempotency_key=request.form.get('idempotency_key'),
            actor=current_user,
        )
        db.session.commit()
        flash('Supplier payment recorded.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment create failed')
        flash(f'Unable to record supplier payment: {exc}', 'danger')
    return_to = (request.form.get('return_to') or '').strip().lower()
    if return_to == 'payments':
        return redirect(url_for('accounts.supplier_payments'))
    if str(supplier_id or '').isdigit():
        return redirect(url_for('supplier_ledger', id=int(supplier_id)))
    return redirect(url_for('suppliers'))


@bp.route('/edit_supplier_payment/<int:id>', methods=['POST'])
@login_required
def edit_supplier_payment(id):
    """Compatibility endpoint using exactly the same service as Create/Edit."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import save_supplier_payment
        save_supplier_payment(
            payment_id=id,
            supplier_id=request.form.get('supplier_id') or payment.supplier_id,
            amount=request.form.get('amount', payment.amount),
            method=request.form.get('method', payment.method or 'Cash'),
            payment_account_id=request.form.get('payment_account_id') or payment.payment_account_id,
            manual_bill_no=request.form.get('manual_bill_no', payment.manual_bill_no or ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', payment.note or ''),
            expected_revision=request.form.get('revision'),
            actor=current_user,
        )
        db.session.commit()
        flash('Supplier payment updated. All balances were recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment edit failed')
        flash(f'Unable to update supplier payment: {exc}', 'danger')
    return redirect(url_for('accounts.supplier_payments', show='all'))


@bp.route('/delete_supplier_payment/<int:id>', methods=['POST'])
@login_required
def delete_supplier_payment(id):
    """Compatibility soft-delete; never hard-deletes accounting history."""
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import delete_supplier_payment as do_delete
        if do_delete(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment deleted and accounting effects reversed.', 'success')
        else:
            flash('Supplier payment is already deleted.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment delete failed')
        flash(f'Unable to delete supplier payment: {exc}', 'danger')
    return redirect(url_for('accounts.supplier_payments', show='all'))


@bp.route('/restore_supplier_payment/<int:id>', methods=['POST'])
@login_required
def restore_supplier_payment(id):
    if not _user_can('can_manage_suppliers'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import restore_supplier_payment as do_restore
        if do_restore(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment restored and balances re-applied.', 'success')
        else:
            flash('Supplier payment is already active.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.exception('Supplier payment restore failed')
        flash(f'Unable to restore supplier payment: {exc}', 'danger')
    return redirect(url_for('accounts.supplier_payments', show='all'))
