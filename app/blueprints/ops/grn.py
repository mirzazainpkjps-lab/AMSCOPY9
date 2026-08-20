"""grn — split from ops.py."""
from ._common import *  # noqa
from app.services.void_rebuild import hard_delete_transaction

@bp.route('/grn', methods=['GET', 'POST'])
@login_required
def grn():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            _t0 = time.time()
            logger = logging.getLogger('grn')
            _actor = getattr(current_user, 'username', None) or 'anon'
            logger.info('GRN_CREATE_START action=add user=%s', _actor)
            supplier_input = request.form.get('supplier', '').strip()
            supplier_id = request.form.get('supplier_id')
            
            supplier_obj = None
            if supplier_id:
                supplier_obj = db.session.get(Supplier, int(supplier_id))
            elif supplier_input:
                supplier_obj = get_supplier_by_input(supplier_input)
                if not supplier_obj:
                    # Auto-create supplier if not found
                    supplier_obj = Supplier(name=supplier_input, is_active=True)
                    db.session.add(supplier_obj)
                    db.session.flush()
            
            supplier_name = supplier_obj.name if supplier_obj else supplier_input

            manual_bill_raw = request.form.get('manual_bill_no', '').strip()
            manual_bill = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
            auto_bill = get_next_bill_no(AUTO_BILL_NAMESPACES['GRN'])
            note = request.form.get('note', '').strip()
            photo = save_photo(request.files.get('photo'))
            photo_url = request.form.get('photo_url', '').strip()
            loading_cost = float(request.form.get('loading_cost', 0) or 0)
            freight_cost = float(request.form.get('freight_cost', 0) or 0)
            other_expense = float(request.form.get('other_expense', 0) or 0)
            adjustment_amount = float(request.form.get('adjustment_amount', 0) or 0)
            try:
                discount, _ = _parse_discount_fields(
                    request.form.get('discount', 0),
                    '',
                    label='GRN discount',
                    require_reason=False
                )
            except ValueError as ve:
                flash(str(ve), 'danger')
                return redirect(url_for('grn'))
            paid_amount = float(request.form.get('paid_amount', 0) or 0)
            payment_type = request.form.get('payment_type', '').strip()
            payment_account_id = request.form.get('payment_account_id')
            bank_name = request.form.get('bank_name', '').strip()
            account_name = request.form.get('account_name', '').strip()
            account_no = request.form.get('account_no', '').strip()
            tax_percent = float(request.form.get('tax_percent', 0) or 0)
            tax_amount = float(request.form.get('tax_amount', 0) or 0)
            tax_type = request.form.get('tax_type', '').strip()
            supplier_invoice_no = request.form.get('supplier_invoice_no', '').strip()
            due_date_str = request.form.get('due_date')
            bill_date_str = request.form.get('bill_date')

            date_str = request.form.get('date')
            if date_str:
                try:
                    date_posted = datetime.strptime(date_str, '%Y-%m-%d')
                    if date_posted.date() == pk_today():
                        date_posted = pk_now()
                except ValueError:
                    date_posted = pk_now()
            else:
                date_posted = pk_now()

            restricted = _enforce_grn_backdate_policy(date_posted, 'Add GRN')
            if restricted:
                return restricted
            
            due_date = None
            if due_date_str:
                try: due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                except: pass
                
            bill_date = None
            if bill_date_str:
                try: bill_date = datetime.strptime(bill_date_str, '%Y-%m-%d').date()
                except: pass

            if manual_bill:
                conflict = find_bill_conflict(manual_bill)
                if conflict:
                    flash(f"Manual bill '{manual_bill}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
                    return redirect(url_for('grn'))

            expected_pay_category = _payment_expected_account_category(payment_type)
            pay_account = None
            if payment_account_id:
                try:
                    payment_account_id = int(payment_account_id)
                except Exception:
                    payment_account_id = None

            if paid_amount > 0 and expected_pay_category in ['cash', 'bank'] and not payment_account_id:
                flash('Select a cash/bank account to post the GRN paid amount into Accounts.', 'danger')
                return redirect(url_for('grn'))

            if payment_account_id:
                pay_account = Account.query.get(payment_account_id)
                if not pay_account or bool(getattr(pay_account, 'is_active', True)) is False:
                    flash('Please select a valid payment account.', 'danger')
                    return redirect(url_for('grn'))
                if expected_pay_category and (pay_account.category or '').strip().lower() != expected_pay_category:
                    flash(f"Selected payment account must be a {expected_pay_category} account for payment type '{payment_type}'.", 'danger')
                    return redirect(url_for('grn'))
                if float(pay_account.balance or 0) + 0.00001 < float(paid_amount or 0):
                    flash('Insufficient balance in selected payment account.', 'danger')
                    return redirect(url_for('grn'))

                if expected_pay_category == 'bank':
                    bank_name = pay_account.bank_name or ''
                    account_name = pay_account.account_holder_name or pay_account.name or ''
                    account_no = pay_account.account_number or ''
                else:
                    bank_name = ''

            new_grn = GRN(
                supplier=supplier_name, 
                supplier_id=supplier_obj.id if supplier_obj else None,
                manual_bill_no=manual_bill,
                auto_bill_no=auto_bill, 
                photo_path=photo, 
                photo_url=photo_url, 
                note=note,
                loading_cost=loading_cost,
                freight_cost=freight_cost,
                other_expense=other_expense,
                adjustment_amount=adjustment_amount,
                discount=discount,
                paid_amount=paid_amount,
                payment_type=payment_type,
                payment_account_id=(payment_account_id if (paid_amount > 0 and expected_pay_category in ['cash', 'bank']) else None),
                bank_name=bank_name,
                account_name=account_name,
                account_no=account_no,
                tax_percent=tax_percent,
                tax_amount=tax_amount,
                tax_type=tax_type,
                supplier_invoice_no=supplier_invoice_no,
                due_date=due_date,
                bill_date=bill_date,
                date_posted=date_posted
            )
            db.session.add(new_grn)
            db.session.flush()

            mat_names = request.form.getlist('mat_name[]')
            qtys = request.form.getlist('qty[]')
            prices = request.form.getlist('price[]')

            for name, qty, price in zip(mat_names, qtys, prices):
                if not name:
                    continue
                try:
                    qty_val = float(qty or 0)
                except (TypeError, ValueError):
                    qty_val = 0.0
                # Server-side inventory guard: zero/negative received
                # quantities must never create a stock movement (mirrors the
                # direct-sale rule qty <= 0 => skip).
                if qty_val <= 0:
                    logger.warning(
                        'GRN_ITEM_SKIPPED_INVALID_QTY mat=%s qty=%r user=%s',
                        name, qty, _actor,
                    )
                    continue
                price_val = float(price) if price else 0
                item = GRNItem(grn_id=new_grn.id, mat_name=name, qty=qty_val, price_at_time=price_val)
                db.session.add(item)

                mat = Material.query.filter_by(name=name).first()
                if mat:
                    mat.total += qty_val

                entry = Entry(
                    date=date_posted.strftime('%Y-%m-%d'),
                    time=date_posted.strftime('%H:%M:%S'),
                    type='IN',
                    material=name,
                    client=supplier_name,
                    qty=qty_val,
                    bill_no=manual_bill or '',
                    auto_bill_no=auto_bill,
                    created_by=current_user.username,
                    note=note
                )
                db.session.add(entry)

            _sync_grn_auto_supplier_payment(new_grn)

            try:
                db.session.commit()
            except IntegrityError as _dup:
                # DB-level duplicate-submission guard (uq_grn_manual_bill_no):
                # the same manual bill posted twice (double click / retry /
                # refresh) must not create a second GRN or stock movement.
                db.session.rollback()
                logger.warning(
                    'GRN_DUPLICATE_REJECTED manual_bill=%s user=%s elapsed_ms=%.1f',
                    manual_bill, _actor, (time.time() - _t0) * 1000.0,
                )
                flash('GRN not saved: this bill number was already received (duplicate submission ignored).', 'danger')
                return redirect(url_for('grn'))
            logger.info(
                'GRN_COMMIT id=%s bill=%s items=%d elapsed_ms=%.1f',
                new_grn.id, auto_bill, len(mat_names), (time.time() - _t0) * 1000.0,
            )
            flash('GRN added successfully!', 'success')

        elif action == 'delete':
            grn_id = request.form.get('id')
            grn_obj = db.session.get(GRN, grn_id)
            if grn_obj:
                restricted = _enforce_grn_backdate_policy(grn_obj.date_posted, 'Delete GRN')
                if restricted:
                    return restricted
                if _grn_has_locked_lots(grn_obj):
                    flash('Cannot delete GRN: lots are locked by cash/credit sales. Delete those sales first.', 'danger')
                    return redirect(url_for('grn'))
                try:
                    if hard_delete_transaction('GRN', grn_obj.id):
                        db.session.commit()
                        flash('GRN deleted and stock reversed.', 'success')
                    else:
                        flash('GRN not found.', 'warning')
                except ValueError as ve:
                    db.session.rollback()
                    flash(str(ve), 'danger')

        return redirect(url_for('grn'))

    search = request.args.get('search', '').strip()
    sort_by = request.args.get('sort', 'date')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = GRN.query
    if search:
        query = query.filter(or_(
            GRN.supplier.ilike(f'%{search}%'),
            GRN.manual_bill_no.ilike(f'%{search}%'),
            GRN.auto_bill_no.ilike(f'%{search}%')
        ))
    if start_date:
        query = query.filter(func.date(GRN.date_posted) >= start_date)
    if end_date:
        query = query.filter(func.date(GRN.date_posted) <= end_date)
    
    if sort_by == 'supplier':
        grns = query.options(selectinload(GRN.items)).order_by(GRN.supplier.asc()).all()
    else:
        grns = query.options(selectinload(GRN.items)).order_by(GRN.date_posted.desc()).all()

    materials = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    suppliers_list = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()

    settings = Settings.query.first()
    next_auto = peek_next_bill_no(AUTO_BILL_NAMESPACES['GRN'])

    accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True
    ).order_by(Account.category.asc(), Account.name.asc()).all()

    return render_template('grn_wizard.html', grns=grns, materials=materials, settings=settings, next_auto=next_auto, clients=clients, suppliers=suppliers_list, accounts=accounts, today_date=pk_today().strftime('%Y-%m-%d'), search=search, sort=sort_by, start_date=start_date, end_date=end_date)


