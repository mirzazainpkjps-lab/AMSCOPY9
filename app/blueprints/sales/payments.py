"""payments — split from sales.py."""
from ._common import *  # noqa

@bp.route('/payments')
@login_required
def payments_page():
    payments_readonly = True
    party = (request.args.get('party', 'customer') or 'customer').strip().lower()
    if party not in ['customer', 'supplier', 'all']:
        party = 'customer'
    show_mode = (request.args.get('show', 'active') or 'active').strip().lower()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    client_filter = (request.args.get('client') or '').strip()
    method_filter = (request.args.get('method') or '').strip()
    amount_min_raw = (request.args.get('amount_min') or '').strip()
    amount_max_raw = (request.args.get('amount_max') or '').strip()

    def _parse_amount(val):
        if val in (None, ''):
            return None
        try:
            return float(val)
        except Exception:
            return None

    amount_min = _parse_amount(amount_min_raw)
    amount_max = _parse_amount(amount_max_raw)
    page_customer = request.args.get('page_customer', 1, type=int)
    page_supplier = request.args.get('page_supplier', 1, type=int)
    per_page_customer = request.args.get('per_page_customer', 10, type=int)
    per_page_supplier = request.args.get('per_page_supplier', 10, type=int)
    per_page_customer = min(max(per_page_customer, 10), 50)
    per_page_supplier = min(max(per_page_supplier, 10), 50)
    payments = []
    supplier_payments = []
    customer_pagination = None
    supplier_pagination = None

    if party in ['customer', 'all']:
        payments_q = Payment.query
        if show_mode == 'voided':
            payments_q = payments_q.filter(Payment.is_void == True)
        elif show_mode == 'all':
            payments_q = payments_q
        else:
            show_mode = 'active'
            payments_q = payments_q.filter(Payment.is_void == False)
        if client_filter:
            resolved_client = get_client_by_input(client_filter)
            if resolved_client:
                payments_q = payments_q.filter(or_(
                    Payment.client_id == resolved_client.id,
                    and_(Payment.client_id.is_(None),
                         func.lower(func.trim(Payment.client_name)) == resolved_client.name.strip().lower()),
                ))
            else:
                payments_q = payments_q.filter(Payment.client_name.ilike(f"%{client_filter}%"))
        if method_filter:
            payments_q = payments_q.filter(Payment.method == method_filter)
        if date_from:
            payments_q = payments_q.filter(func.date(Payment.date_posted) >= date_from)
        if date_to:
            payments_q = payments_q.filter(func.date(Payment.date_posted) <= date_to)
        if amount_min is not None:
            payments_q = payments_q.filter(Payment.amount >= amount_min)
        if amount_max is not None:
            payments_q = payments_q.filter(Payment.amount <= amount_max)
        customer_pagination = payments_q.order_by(Payment.date_posted.desc()).paginate(
            page=page_customer, per_page=per_page_customer, error_out=False
        )
        payments = customer_pagination.items

    if party in ['supplier', 'all']:
        supplier_q = SupplierPayment.query
        if show_mode == 'voided':
            supplier_q = supplier_q.filter(SupplierPayment.is_void == True)
        elif show_mode == 'all':
            supplier_q = supplier_q
        else:
            show_mode = 'active'
            supplier_q = supplier_q.filter(SupplierPayment.is_void == False)
        supplier_pagination = supplier_q.order_by(SupplierPayment.date_posted.desc()).paginate(
            page=page_supplier, per_page=per_page_supplier, error_out=False
        )
        supplier_payments = supplier_pagination.items

    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()
    accounts = Account.query.filter(func.coalesce(Account.is_active, True) == True).order_by(Account.name.asc()).all()
    next_auto = peek_next_bill_no(AUTO_BILL_NAMESPACES['PAYMENT'])
    return render_template('payments.html',
                           payments=payments,
                           supplier_payments=supplier_payments,
                           clients=clients,
                           suppliers=suppliers,
                           accounts=accounts,
                           next_auto=next_auto,
                           payments_readonly=payments_readonly,
                           show_mode=show_mode,
                           party=party,
                           today_date=pk_today().strftime('%Y-%m-%d'),
                           date_from=date_from,
                           date_to=date_to,
                           client_filter=client_filter,
                           method_filter=method_filter,
                           amount_min=amount_min_raw,
                           amount_max=amount_max_raw,
                           customer_pagination=customer_pagination,
                           supplier_pagination=supplier_pagination,
                           page_customer=page_customer,
                           page_supplier=page_supplier,
                           per_page_customer=per_page_customer,
                           per_page_supplier=per_page_supplier)


@bp.route('/add_payment', methods=['POST'])
@login_required
def add_payment():
    """Legacy-compatible endpoint backed by the canonical Accounts service."""
    if not _user_can('can_manage_payments'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.client_payments'))
    try:
        from app.services.payments_crud import save_client_payment
        from utils.retry_commit import retry_on_conflict
        # The upload is side-effecting and must not be repeated per attempt.
        uploaded = save_photo(request.files.get('photo'))
        form = request.form

        def _save():
            # `Account` uses optimistic locking (version_id_col='revision'), so
            # two payments hitting the same account concurrently make the loser
            # raise StaleDataError. Re-running the whole unit of work re-reads
            # the account at its new revision and recomputes the balance from
            # committed state; retrying only the COMMIT would re-apply a stale
            # in-memory balance and silently lose the other payment.
            payment, _ = save_client_payment(
                client_code=form.get('client_code', ''),
                client_name=form.get('client_name', ''),
                amount=form.get('amount', 0),
                discount=form.get('discount', 0),
                discount_reason=form.get('discount_reason', ''),
                payment_type=form.get('payment_type', 'Receipt'),
                method=form.get('method', 'Cash'),
                payment_account_id=form.get('payment_account_id'),
                manual_bill_no=form.get('manual_bill_no', ''),
                date_posted=form.get('date', ''),
                note=form.get('note', ''),
                photo_path=uploaded,
                photo_url=form.get('photo_url', ''),
                idempotency_key=form.get('idempotency_key'),
                actor=current_user,
            )
            db.session.commit()
            return payment

        retry_on_conflict(_save, label='add_payment')
        flash('Payment received successfully.', 'success')
        return redirect(url_for('accounts.client_payments', show='active'))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger('payments').exception('Payment create failed')
        from utils.retry_commit import _is_transient
        if _is_transient(exc):
            # Don't misattribute a lock conflict to bad input - the data was
            # fine, the account was just busy.
            flash('This account is busy with another transaction. '
                  'Nothing was saved - please submit the payment again.', 'warning')
        else:
            flash('Unable to save payment: the payment could not be saved. Please check the details and try again.', 'danger')
    return redirect(url_for('accounts.client_payments'))


@bp.route('/edit_bill/Payment/<int:id>', methods=['POST'])
@login_required
def edit_payment(id):
    """Compatibility Edit endpoint using the same service as Create."""
    if not _user_can('can_manage_payments'):
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.client_payments'))
    Payment.query.get_or_404(id)
    try:
        from app.services.payments_crud import _client_payment_kind, save_client_payment
        from utils.retry_commit import retry_on_conflict

        # Side effects that must NOT be repeated on retry are done once, up
        # front, outside the retried unit of work.
        uploaded = save_photo(request.files.get('photo'))
        form = request.form

        def _save():
            # Re-read the row inside the attempt: retry_on_conflict rolls the
            # session back between attempts, so any instance loaded earlier is
            # detached/stale and its attributes cannot be used as defaults.
            current = Payment.query.get_or_404(id)
            save_client_payment(
                payment_id=id,
                client_code=form.get('client_code', ''),
                client_name=form.get('client_name', current.client_name or ''),
                amount=form.get('amount', abs(float(current.amount or 0))),
                discount=form.get('discount', current.discount or 0),
                discount_reason=form.get('discount_reason', current.discount_reason or ''),
                payment_type=form.get('payment_type', _client_payment_kind(current)),
                method=form.get('method', current.method or 'Cash'),
                payment_account_id=form.get('payment_account_id') or current.payment_account_id,
                manual_bill_no=form.get('manual_bill_no', current.manual_bill_no or ''),
                date_posted=form.get('date', ''),
                note=form.get('note', current.note or ''),
                photo_path=uploaded,
                photo_url=form.get('photo_url', current.photo_url or ''),
                expected_revision=form.get('revision'),
                actor=current_user,
            )
            db.session.commit()

        retry_on_conflict(_save, label='edit_payment')
        flash('Payment updated. All balances were recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger('payments').exception('Payment edit failed')
        from utils.retry_commit import _is_transient
        if _is_transient(exc):
            flash('This account is busy with another transaction. '
                  'Nothing was saved - please submit the change again.', 'warning')
        else:
            flash('Unable to update payment: the payment could not be saved. Please check the details and try again.', 'danger')
    return redirect(url_for('accounts.client_payments', show='active'))
