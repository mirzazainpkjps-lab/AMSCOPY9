"""pending — split from misc.py."""
from ._common import *  # noqa

@bp.route('/add_pending_bill', methods=['POST'])
@login_required
def add_pending_bill():
    note = request.form.get('note', '').strip()
    client_code = request.form.get('client_code', '').strip()
    client_obj = get_client_by_input(client_code)
    photo_path = save_photo(request.files.get('photo'))

    if not client_obj:
        flash('Invalid Client Code.', 'danger')
        return redirect(url_for('pending_bills'))

    raw_bill_no = request.form.get('bill_no', '').strip()
    normalized_bill_no = normalize_manual_bill(raw_bill_no)
    if not normalized_bill_no:
        flash('Bill number is required', 'danger')
        return redirect(url_for('pending_bills'))

    conflict = find_bill_conflict(normalized_bill_no)
    if conflict:
        flash(f"Bill '{normalized_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
        return redirect(url_for('pending_bills'))

    bill = PendingBill(client_code=client_code,
                       client_name=client_obj.name,
                       bill_no=normalized_bill_no,
                       bill_kind='MB',
                       is_manual=True,
                       nimbus_no=request.form.get('nimbus_no', '').strip(),
                       amount=float(request.form.get('amount') or 0),
                       reason=request.form.get('reason', '').strip(),
                       photo_url=request.form.get('photo_url', '').strip(),
                       photo_path=photo_path,
                       created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                       created_by=current_user.username,
                       note=note)
    db.session.add(bill)
    db.session.commit()
    flash('Pending bill added', 'success')
    return redirect(url_for('pending_bills'))


@bp.route('/edit_pending_bill/<int:id>', methods=['POST'])
@login_required
def edit_pending_bill(id):
    bill = db.session.get(PendingBill, id)
    if bill:
        old_bill_no = bill.bill_no
        old_client_code = bill.client_code

        client_code = request.form.get('client_code', '').strip()
        client_obj = get_client_by_input(client_code)

        if not client_obj:
            flash('Invalid Client Code.', 'danger')
            return redirect(url_for('pending_bills'))

        bill.client_code = client_code
        bill.client_name = client_obj.name
        raw_bill_no = request.form.get('bill_no', '').strip()
        bill.bill_no = normalize_manual_bill(raw_bill_no) if raw_bill_no else ''
        bill.bill_kind = parse_bill_kind(bill.bill_no)
        bill.is_manual = (bill.bill_kind == 'MB')
        if bill.bill_no:
            conflict = find_bill_conflict(bill.bill_no)
            if conflict and not (conflict[0] == 'PendingBill' and conflict[1] == bill.id):
                flash(f"Bill '{bill.bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
                return redirect(url_for('pending_bills'))
        bill.nimbus_no = request.form.get('nimbus_no', '').strip()
        bill.amount = float(request.form.get('amount') or 0)
        bill.reason = request.form.get('reason', '').strip()
        bill.photo_url = request.form.get('photo_url', '').strip()
        bill.note = request.form.get('note', '').strip()
        
        new_photo = save_photo(request.files.get('photo'))
        if new_photo:
            bill.photo_path = new_photo

        update_data = {
            'bill_no': bill.bill_no,
            'client': bill.client_name,
            'client_code': bill.client_code
        }
        Entry.query.filter_by(bill_no=old_bill_no, client_code=old_client_code).update(update_data)

        db.session.commit()
        flash('Bill updated', 'success')
    return redirect(url_for('pending_bills'))


@bp.route('/delete_pending_bill/<int:id>', methods=['POST'])
@login_required
def delete_pending_bill(id):
    bill = db.session.get(PendingBill, id)
    if bill:
        bill.is_void = True
        db.session.commit()
        flash('Bill deleted', 'warning')
    return redirect(url_for('pending_bills'))


@bp.route('/export_pending_bills')
@login_required
def export_pending_bills():
    if current_user.role not in ['admin', 'root']:
        flash('Only tenant admin or root can run import/export operations.', 'danger')
        return redirect(url_for('index'))
    """Redirects to the generic export function for pending bills."""
    # This is a convenience route to fix a template error.
    # It redirects to the actual export endpoint in the import_export blueprint.
    args = request.args.to_dict()
    args['dataset'] = 'pending_bills'
    return redirect(url_for('import_export.export_data', **args))


@bp.route('/import_pending_bills', methods=['GET', 'POST'])
@login_required
def import_pending_bills():
    if current_user.role not in ['admin', 'root']:
        flash('Only tenant admin or root can run import/export operations.', 'danger')
        return redirect(url_for('index'))
    """Legacy pending-bills import endpoint (GET+POST compatibility)."""
    if request.method == 'GET':
        return redirect(url_for('import_export.import_export_page'))

    file = request.files.get('file')
    if not file:
        flash('No file selected for import.', 'danger')
        return redirect(url_for('pending_bills'))

    try:
        import pandas as pd
        from blueprints.import_export import backup_database, _process_pending_bills

        ok, msg = backup_database()
        if not ok:
            flash(f'Backup failed: {msg}', 'danger')
            return redirect(url_for('pending_bills'))

        if file.filename.lower().endswith('.csv'):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]
        df = df.fillna('')
        report = {'imported': 0, 'updated': 0, 'skipped': 0, 'errors': 0, 'error_details': [], 'discrepancies': []}
        _process_pending_bills(
            df=df,
            strategy='update',
            missing_client_strategy='create',
            report=report,
            allow_missing=False
        )
        db.session.commit()
        flash(
            f"Pending Bills import complete. Imported: {report['imported']}, Updated: {report['updated']}, "
            f"Skipped: {report['skipped']}, Errors: {report['errors']}",
            'success'
        )
    except Exception as e:
        db.session.rollback()
        flash(f'Import failed: {str(e)}', 'danger')

    return redirect(url_for('pending_bills'))


@bp.route('/edit_grn/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_grn(id):
    grn_obj = GRN.query.get_or_404(id)
    restricted = _enforce_grn_backdate_policy(grn_obj.date_posted, 'Edit GRN')
    if restricted:
        return restricted
    
    if request.method == 'POST':
        if _grn_has_locked_lots(grn_obj):
            flash('This GRN has locked lots used by cash/credit sales. Delete those sales before changing item qty/rate.', 'danger')
            return redirect(url_for('edit_grn', id=grn_obj.id))
        old_supplier_id = grn_obj.supplier_id
        old_auto_pay = _find_grn_auto_supplier_payment(grn_obj)
        old_pay_account_id = None
        old_pay_amount = 0.0
        if old_auto_pay and not bool(getattr(old_auto_pay, 'is_void', False)):
            old_pay_account_id = getattr(old_auto_pay, 'payment_account_id', None)
            old_pay_amount = float(getattr(old_auto_pay, 'amount', 0) or 0)
        # 1. Reverse Stock for existing items (only active items)
        for item in [i for i in (grn_obj.items or []) if not bool(getattr(i, 'is_void', False))]:
            mat = Material.query.filter_by(name=item.mat_name).first()
            if mat:
                mat.total = (mat.total or 0) - (item.qty or 0)
        
        # 2. Void old items and entries (preserve audit trail)
        for item in (grn_obj.items or []):
            item.is_void = True
        for e in Entry.query.filter(Entry.auto_bill_no == grn_obj.auto_bill_no, Entry.type == 'IN').all():
            e.is_void = True
        
        # 3. Update GRN fields
        supplier_input = request.form.get('supplier', '').strip()
        supplier_id_input = (request.form.get('supplier_id') or '').strip()
        supplier_obj = None
        if supplier_id_input.isdigit():
            supplier_obj = db.session.get(Supplier, int(supplier_id_input))
        if not supplier_obj and supplier_input:
            supplier_obj = get_supplier_by_input(supplier_input)
        if not supplier_obj and supplier_input:
            supplier_obj = Supplier(name=supplier_input, is_active=True)
            db.session.add(supplier_obj)
            db.session.flush()

        grn_obj.supplier = supplier_obj.name if supplier_obj else supplier_input
        grn_obj.supplier_id = supplier_obj.id if supplier_obj else None
        manual_bill_raw = request.form.get('manual_bill_no', '').strip()
        grn_obj.manual_bill_no = normalize_manual_bill(manual_bill_raw) if manual_bill_raw else ''
        grn_obj.note = request.form.get('note', '').strip()
        grn_obj.photo_url = request.form.get('photo_url', '').strip()
        
        new_photo = save_photo(request.files.get('photo'))
        if new_photo:
            grn_obj.photo_path = new_photo
            
        grn_obj.loading_cost = float(request.form.get('loading_cost', 0) or 0)
        grn_obj.freight_cost = float(request.form.get('freight_cost', 0) or 0)
        grn_obj.other_expense = float(request.form.get('other_expense', 0) or 0)
        grn_obj.adjustment_amount = float(request.form.get('adjustment_amount', 0) or 0)
        try:
            grn_obj.discount, _ = _parse_discount_fields(
                request.form.get('discount', 0),
                '',
                label='GRN discount',
                require_reason=False
            )
        except ValueError as ve:
            flash(str(ve), 'danger')
            return redirect(url_for('edit_grn', id=grn_obj.id))
        grn_obj.paid_amount = float(request.form.get('paid_amount', 0) or 0)
        grn_obj.payment_type = request.form.get('payment_type', '').strip()
        payment_account_id = request.form.get('payment_account_id')
        grn_obj.bank_name = request.form.get('bank_name', '').strip()
        grn_obj.account_name = request.form.get('account_name', '').strip()
        grn_obj.account_no = request.form.get('account_no', '').strip()
        grn_obj.tax_percent = float(request.form.get('tax_percent', 0) or 0)
        grn_obj.tax_amount = float(request.form.get('tax_amount', 0) or 0)
        grn_obj.tax_type = request.form.get('tax_type', '').strip()
        grn_obj.supplier_invoice_no = request.form.get('supplier_invoice_no', '').strip()

        expected_pay_category = _payment_expected_account_category(grn_obj.payment_type)
        pay_account = None
        if payment_account_id:
            try:
                payment_account_id = int(payment_account_id)
            except Exception:
                payment_account_id = None

        if float(grn_obj.paid_amount or 0) > 0 and expected_pay_category in ['cash', 'bank'] and not payment_account_id:
            flash('Select a cash/bank account to post the GRN paid amount into Accounts.', 'danger')
            return redirect(url_for('edit_grn', id=grn_obj.id))

        if payment_account_id:
            pay_account = Account.query.get(payment_account_id)
            if not pay_account or bool(getattr(pay_account, 'is_active', True)) is False:
                flash('Please select a valid payment account.', 'danger')
                return redirect(url_for('edit_grn', id=grn_obj.id))
            if expected_pay_category and (pay_account.category or '').strip().lower() != expected_pay_category:
                flash(f"Selected payment account must be a {expected_pay_category} account for payment type '{grn_obj.payment_type}'.", 'danger')
                return redirect(url_for('edit_grn', id=grn_obj.id))

            new_paid = float(grn_obj.paid_amount or 0)
            if payment_account_id == old_pay_account_id:
                delta = new_paid - float(old_pay_amount or 0)
                if delta > 0 and float(pay_account.balance or 0) + 0.00001 < delta:
                    flash('Insufficient balance in selected payment account for the increased paid amount.', 'danger')
                    return redirect(url_for('edit_grn', id=grn_obj.id))
            else:
                if new_paid > 0 and float(pay_account.balance or 0) + 0.00001 < new_paid:
                    flash('Insufficient balance in selected payment account.', 'danger')
                    return redirect(url_for('edit_grn', id=grn_obj.id))

            if expected_pay_category == 'bank':
                grn_obj.bank_name = pay_account.bank_name or ''
                grn_obj.account_name = pay_account.account_holder_name or pay_account.name or ''
                grn_obj.account_no = pay_account.account_number or ''
            else:
                grn_obj.bank_name = ''
                grn_obj.account_name = ''
                grn_obj.account_no = ''

        grn_obj.payment_account_id = (payment_account_id if (float(grn_obj.paid_amount or 0) > 0 and expected_pay_category in ['cash', 'bank']) else None)
        
        if grn_obj.manual_bill_no:
            conflict = find_bill_conflict(grn_obj.manual_bill_no)
            if conflict and not (conflict[0] == 'GRN' and conflict[1] == grn_obj.id):
                flash(f"Manual bill '{grn_obj.manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.", 'danger')
                return redirect(url_for('edit_grn', id=grn_obj.id))

        date_str = request.form.get('date')
        if date_str:
            try:
                date_posted = datetime.strptime(date_str, '%Y-%m-%d')
                restricted = _enforce_grn_backdate_policy(date_posted, 'Edit GRN')
                if restricted:
                    return restricted
                if date_posted.date() == pk_today():
                    grn_obj.date_posted = pk_now()
                else:
                    grn_obj.date_posted = date_posted
            except ValueError:
                pass

        # 4. Add / update items (preserve GRNItem IDs so linked DirectSaleItem.grn_item_id stays valid)
        item_ids = request.form.getlist('grn_item_id[]')
        mat_names = request.form.getlist('mat_name[]')
        qtys = request.form.getlist('qty[]')
        prices = request.form.getlist('price[]')

        existing_by_id = {
            int(i.id): i
            for i in (grn_obj.items or [])
            if getattr(i, 'id', None) is not None
        }

        for idx in range(max(len(mat_names), len(qtys), len(prices), len(item_ids))):
            name = (mat_names[idx] if idx < len(mat_names) else '') or ''
            qty = (qtys[idx] if idx < len(qtys) else '') or ''
            price = (prices[idx] if idx < len(prices) else '') or ''
            raw_id = (item_ids[idx] if idx < len(item_ids) else '') or ''

            name = name.strip()
            try:
                qty_val = float(qty or 0)
            except Exception:
                qty_val = 0.0
            try:
                price_val = float(price or 0)
            except Exception:
                price_val = 0.0

            if not name or qty_val <= 0:
                continue

            item_obj = None
            if str(raw_id).strip().isdigit():
                iid = int(str(raw_id).strip())
                candidate = existing_by_id.get(iid)
                if candidate and candidate.grn_id == grn_obj.id:
                    item_obj = candidate

            if item_obj is None:
                item_obj = GRNItem(grn_id=grn_obj.id)
                db.session.add(item_obj)

            item_obj.is_void = False
            item_obj.mat_name = name
            item_obj.qty = qty_val
            item_obj.price_at_time = price_val

            mat = Material.query.filter_by(name=name).first()
            if mat:
                mat.total = (mat.total or 0) + qty_val

            entry = Entry(
                date=grn_obj.date_posted.strftime('%Y-%m-%d'),
                time=grn_obj.date_posted.strftime('%H:%M:%S'),
                type='IN',
                material=name,
                client=grn_obj.supplier,
                qty=qty_val,
                bill_no=grn_obj.manual_bill_no or '',
                auto_bill_no=grn_obj.auto_bill_no,
                created_by=current_user.username,
                note=grn_obj.note
            )
            db.session.add(entry)

        _sync_grn_auto_supplier_payment(grn_obj, old_supplier_id=old_supplier_id)
        
        db.session.commit()
        flash('GRN updated successfully', 'success')
        return redirect(url_for('grn'))

    grns = GRN.query.options(selectinload(GRN.items)).order_by(GRN.date_posted.desc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    suppliers_list = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()
    settings = Settings.query.first()
    
    accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True
    ).order_by(Account.category.asc(), Account.name.asc()).all()

    return render_template('grn_wizard.html', grns=grns, materials=materials, settings=settings, clients=clients, suppliers=suppliers_list, accounts=accounts, today_date=pk_today().strftime('%Y-%m-%d'), edit_grn=grn_obj, search='', sort='date', start_date=None, end_date=None)


@bp.route('/export_grn')
@login_required
def export_grn():
    if current_user.role not in ['admin', 'root']:
        flash('Only tenant admin or root can run import/export operations.', 'danger')
        return redirect(url_for('index'))
    search = request.args.get('search', '').strip()
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
    
    grns = query.order_by(GRN.date_posted.desc()).all()
    
    data = []
    for g in grns:
        total = calculate_grn_total(g)
        data.append({
            'Date': g.date_posted.strftime('%Y-%m-%d'),
            'GRN #': g.manual_bill_no or g.auto_bill_no,
            'Supplier': g.supplier,
            'Items Count': len([i for i in (g.items or []) if not bool(getattr(i, 'is_void', False))]),
            'Total Qty': sum((i.qty or 0) for i in (g.items or []) if not bool(getattr(i, 'is_void', False))),
            'Discount': g.discount,
            'Tax': g.tax_amount,
            'Expenses': (g.loading_cost or 0) + (g.freight_cost or 0) + (g.other_expense or 0),
            'Net Amount': total,
            'Note': g.note
        })
    
    import pandas as pd
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='GRN List')
    output.seek(0)
    
    return send_file(
        output,
        as_attachment=True,
        download_name=_download_filename('GRNEXPORT', 'xlsx'),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


