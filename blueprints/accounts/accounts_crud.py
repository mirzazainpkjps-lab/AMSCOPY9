"""accounts_crud — split from accounts.py."""
from ._common import *  # noqa


def _account_master_permission_ok():
    return (getattr(current_user, 'role', '') or '').strip().lower() in ('admin', 'root')


def _deny_account_master_mutation():
    if _account_master_permission_ok():
        return None
    from flask import abort
    abort(403)


@accounts_bp.route('/accounts')
@login_required
def manage_accounts():
    """Manage financial accounts."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    show_mode = (request.args.get('show') or 'active').strip().lower()
    q = Account.query
    if show_mode == 'archived':
        q = q.filter(Account.is_active == False)
    elif show_mode == 'all':
        pass
    else:
        show_mode = 'active'
        q = q.filter(func.coalesce(Account.is_active, True) == True)
    accounts = q.order_by(Account.name.asc(), Account.id.asc()).all()
    categories = _account_categories()
    
    # Group accounts by category and type for better organization
    account_summary = {}
    for account in accounts:
        category_name = (account.category or 'Unknown').upper()
        account_type_name = account.account_type or 'Unknown'
        key = f"{category_name} - {account_type_name}"
        if key not in account_summary:
            account_summary[key] = []
        account_summary[key].append(account)
    
    return render_template('accounts/manage_accounts.html', accounts=accounts, account_summary=account_summary,
                           categories=categories, show_mode=show_mode,
                           can_manage_master=_account_master_permission_ok())


@accounts_bp.route('/categories/add', methods=['POST'])
@login_required
def add_account_category():
    _deny_account_master_mutation()
    name = (request.form.get('name') or '').strip()
    note = (request.form.get('note') or '').strip()

    if not name:
        flash('Category name is required.', 'danger')
        return redirect(url_for('accounts.manage_accounts'))

    existing = AccountCategory.query.filter(
        func.lower(func.trim(AccountCategory.name)) == name.lower(),
        AccountCategory.is_active == True
    ).first()
    if existing:
        flash('This account category already exists.', 'warning')
        return redirect(url_for('accounts.manage_accounts'))

    db.session.add(AccountCategory(name=name, note=note or None))
    db.session.commit()
    audit_log(current_user, 'account.category.create', f'name={name}')
    flash('Account category created successfully.', 'success')
    return redirect(url_for('accounts.manage_accounts'))


@accounts_bp.route('/accounts/add', methods=['GET', 'POST'])
@login_required
def add_account():
    """Add a new account."""
    _deny_account_master_mutation()
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        category = (request.form.get('category') or '').strip().lower()
        source_category = (request.form.get('source_category') or '').strip()
        account_type = (request.form.get('account_type') or '').strip()
        note = request.form.get('note')
        initial_balance_raw = request.form.get('initial_balance', '0')

        if not name:
            flash('Account name is required.', 'danger')
            return redirect(url_for('accounts.add_account'))
        if category not in ('cash', 'bank'):
            flash('Please select Cash or Bank.', 'danger')
            return redirect(url_for('accounts.add_account'))
        if not account_type:
            flash('Please select an account type.', 'danger')
            return redirect(url_for('accounts.add_account'))
        if not source_category:
            flash('Please select an account category first.', 'danger')
            return redirect(url_for('accounts.add_account'))
        category_exists = AccountCategory.query.filter(
            func.lower(func.trim(AccountCategory.name)) == source_category.lower(),
            AccountCategory.is_active == True
        ).first()
        if not category_exists:
            flash('Please select a valid account category.', 'danger')
            return redirect(url_for('accounts.add_account'))

        try:
            from utils.money import from_minor, to_minor
            initial_balance_minor = to_minor(initial_balance_raw or 0, field='Initial balance')
            initial_balance = float(from_minor(initial_balance_minor))
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('accounts.add_account'))

        if category == 'bank':
            bank_name = (request.form.get('bank_name') or '').strip()
            account_holder_name = (request.form.get('account_holder_name') or '').strip()
            account_number = (request.form.get('account_number') or '').strip()
            branch_code = (request.form.get('branch_code') or '').strip()
            if not bank_name or not account_holder_name or not account_number:
                flash('Bank account name, holder and number are required for bank accounts.', 'danger')
                return redirect(url_for('accounts.add_account'))
        else:
            bank_name = None
            account_holder_name = None
            account_number = None
            branch_code = None

        account = Account(
            name=name,
            category=category,
            source_category=category_exists.name,
            account_type=account_type,
            type=account_type,
            balance=initial_balance,
            balance_minor=initial_balance_minor,
            opening_balance=initial_balance,
            opening_balance_minor=initial_balance_minor,
            opening_balance_date=pk_now(),
            bank_name=bank_name,
            account_holder_name=account_holder_name,
            account_number=account_number,
            branch_code=branch_code,
            note=note
        )
        try:
            from utils.accounting_audit import record_accounting_audit
            db.session.add(account)
            db.session.flush()
            record_accounting_audit(
                current_user, action='Create', entity_type='Account', entity_id=account.id,
                after={'name': name, 'category': category, 'source_category': category_exists.name,
                       'account_type': account_type, 'opening_balance': initial_balance},
                amount_after=initial_balance, account_after_id=account.id, reason=note,
            )
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            flash(f'Unable to add account due to database constraint: {exc.orig}', 'danger')
            return redirect(url_for('accounts.add_account'))
        except Exception as exc:
            db.session.rollback()
            logger.exception('Add account failed')
            flash(f'Unable to add account: {exc}', 'danger')
            return redirect(url_for('accounts.add_account'))

        flash('Account added successfully!', 'success')
        return redirect(url_for('accounts.manage_accounts'))

    return render_template('accounts/add_account.html', categories=_account_categories())


@accounts_bp.route('/ledger/<int:account_id>')
@login_required
def account_ledger(account_id):
    account = Account.query.get_or_404(account_id)
    page = request.args.get('page', 1, type=int)
    per_page = 50
    search = (request.args.get('q') or '').strip()
    type_f = (request.args.get('type') or '').strip()
    show_voided = request.args.get('show_voided') == '1'
    date_from, date_to_excl = _parse_date_range(default_days=90)

    base_filters = [
        or_(AccountTransaction.from_account_id == account.id,
            AccountTransaction.to_account_id == account.id)
    ]
    if not show_voided:
        base_filters.append(AccountTransaction.is_void == False)

    # Reproducible opening from the explicit account baseline + ledger, not from
    # a frontend/current-balance subtraction that can include later periods.
    from app.services.payments_crud import ledger_balance
    opening_cutoff = datetime.combine(date_from, datetime.min.time()) - timedelta(microseconds=1)
    opening_balance = ledger_balance(account.id, as_of=opening_cutoff)

    q = AccountTransaction.query.filter(*base_filters,
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        q = q.filter(or_(AccountTransaction.description.ilike(like), AccountTransaction.note.ilike(like)))
    if type_f:
        q = q.filter(AccountTransaction.transaction_type == type_f)

    period_in = q.with_entities(func.coalesce(func.sum(AccountTransaction.amount), 0)).filter(
        AccountTransaction.to_account_id == account.id, AccountTransaction.is_void == False
    ).scalar() or 0
    period_out = q.with_entities(func.coalesce(func.sum(AccountTransaction.amount), 0)).filter(
        AccountTransaction.from_account_id == account.id, AccountTransaction.is_void == False
    ).scalar() or 0

    # Running balances include *all* active movements, even when the displayed
    # rows are narrowed by search/type filters.
    all_period_rows = AccountTransaction.query.filter(
        or_(AccountTransaction.from_account_id == account.id, AccountTransaction.to_account_id == account.id),
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
    ).order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all()
    running = opening_balance
    running_by_id = {}
    for row in all_period_rows:
        if not row.is_void:
            if row.to_account_id == account.id:
                running += float(row.amount or 0)
            if row.from_account_id == account.id:
                running -= float(row.amount or 0)
        running_by_id[row.id] = running

    rows_asc = q.order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all()
    enriched = []
    for r in rows_asc:
        delta = 0.0
        if not r.is_void:
            if r.to_account_id == account.id:
                delta += float(r.amount or 0)
            if r.from_account_id == account.id:
                delta -= float(r.amount or 0)
        enriched.append({'tx': r, 'delta': delta, 'running': running_by_id.get(r.id)})

    enriched.reverse()  # display newest first

    # Manual pagination over enriched list
    total_rows = len(enriched)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = enriched[start:end]

    types = ['Receipt', 'Refund', 'Payment', 'Transfer', 'Supplier Payment', 'Driver Payment', 'Expense', 'Loss', 'Adjustment', 'Reconciliation Loss', 'Reconciliation Excess']

    return render_template('accounts/account_ledger.html', account=account, page_rows=page_rows,
                           opening_balance=opening_balance, period_in=period_in, period_out=period_out,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1),
                           search=search, type_f=type_f, types=types, show_voided=show_voided,
                           page=page, per_page=per_page, total_rows=total_rows,
                           has_prev=page > 1, has_next=end < total_rows)


@accounts_bp.route('/<int:account_id>/data')
@login_required
def account_data(account_id):
    """JSON data for the edit account modal."""
    a = Account.query.get_or_404(account_id)
    return jsonify({
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'source_category': a.source_category,
        'account_type': a.account_type or (getattr(a, 'type', None) or ''),
        'balance': float(a.balance or 0),
        'bank_name': a.bank_name or '',
        'account_holder_name': a.account_holder_name or '',
        'account_number': a.account_number or '',
        'branch_code': a.branch_code or '',
        'note': a.note or '',
        'is_active': bool(a.is_active),
    })


@accounts_bp.route('/<int:account_id>/edit', methods=['POST'])
@login_required
def edit_account(account_id):
    """Edit an account's metadata. Balance changes are recorded as Adjustment transactions."""
    _deny_account_master_mutation()
    a = Account.query.get_or_404(account_id)
    try:
        before = {
            'id': a.id, 'name': a.name, 'category': a.category,
            'source_category': a.source_category, 'account_type': a.account_type,
            'balance': _money_round(a.balance), 'is_active': bool(a.is_active),
            'bank_name': a.bank_name, 'account_number': a.account_number, 'note': a.note,
        }
        name = (request.form.get('name') or '').strip()
        category = (request.form.get('category') or '').strip().lower()
        source_category = (request.form.get('source_category') or '').strip()
        account_type = (request.form.get('account_type') or '').strip()
        note = (request.form.get('note') or '').strip()
        new_balance_raw = request.form.get('balance', '').strip()

        if not name:
            raise ValueError('Account name is required.')
        if category not in ('cash', 'bank'):
            raise ValueError('Please select Cash or Bank.')
        if not account_type:
            raise ValueError('Please select an account type.')
        if not source_category:
            raise ValueError('Please select an account category.')

        cat = AccountCategory.query.filter(
            func.lower(func.trim(AccountCategory.name)) == source_category.lower(),
            AccountCategory.is_active == True
        ).first()
        if not cat:
            raise ValueError('Selected account category not found.')

        a.name = name
        a.category = category
        a.source_category = cat.name
        a.account_type = account_type
        a.type = account_type
        a.note = note or None

        if category == 'bank':
            a.bank_name = (request.form.get('bank_name') or '').strip() or None
            a.account_holder_name = (request.form.get('account_holder_name') or '').strip() or None
            a.account_number = (request.form.get('account_number') or '').strip() or None
            a.branch_code = (request.form.get('branch_code') or '').strip() or None
        else:
            a.bank_name = None
            a.account_holder_name = None
            a.account_number = None
            a.branch_code = None

        if new_balance_raw != '':
            from utils.money import from_minor, to_minor
            from app.services.payments_crud import _assert_period_open
            old_minor = int(a.balance_minor) if getattr(a, 'balance_minor', None) is not None else to_minor(a.balance or 0)
            new_minor = to_minor(new_balance_raw, field='Balance')
            diff_minor = new_minor - old_minor
            if diff_minor:
                _assert_period_open(a.id, pk_now(), operation='manually adjusted')
                adj = AccountTransaction(
                    from_account_id=(a.id if diff_minor < 0 else None),
                    to_account_id=(a.id if diff_minor > 0 else None),
                    amount=float(from_minor(abs(diff_minor))), amount_minor=abs(diff_minor),
                    description='Balance adjustment (manual edit)',
                    note=(f'Adjusted from Rs. {float(from_minor(old_minor)):.2f} '
                          f'to Rs. {float(from_minor(new_minor)):.2f}'),
                    transaction_type='Adjustment', source_type='Account', source_id=a.id,
                    created_by=getattr(current_user, 'username', None), date_posted=pk_now()
                )
                db.session.add(adj)
                a.balance_minor = new_minor
                a.balance = float(from_minor(new_minor))

        a.updated_by = getattr(current_user, 'username', None)
        a.revision = int(getattr(a, 'revision', None) or 1) + 1
        from utils.accounting_audit import record_accounting_audit
        after = {
            'id': a.id, 'name': a.name, 'category': a.category,
            'source_category': a.source_category, 'account_type': a.account_type,
            'balance': _money_round(a.balance), 'is_active': bool(a.is_active),
            'bank_name': a.bank_name, 'account_number': a.account_number, 'note': a.note,
        }
        record_accounting_audit(
            current_user, action='Edit', entity_type='Account', entity_id=a.id,
            before=before, after=after, amount_before=before['balance'], amount_after=after['balance'],
            account_before_id=a.id, account_after_id=a.id, reason=note,
        )
        db.session.commit()
        flash('Account updated successfully.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Edit account failed')
        flash(f'Unable to update account: {exc}', 'danger')

    return redirect(url_for('accounts.manage_accounts'))


@accounts_bp.route('/<int:account_id>/toggle', methods=['POST'])
@login_required
def toggle_account(account_id):
    """Soft-deactivate / reactivate an account (never corrupts history)."""
    _deny_account_master_mutation()
    a = Account.query.get_or_404(account_id)
    before = {'id': a.id, 'name': a.name, 'is_active': bool(a.is_active)}
    a.is_active = not bool(a.is_active)
    a.updated_by = getattr(current_user, 'username', None)
    a.revision = int(getattr(a, 'revision', None) or 1) + 1
    from utils.accounting_audit import record_accounting_audit
    record_accounting_audit(
        current_user, action='Activate' if a.is_active else 'Suspend',
        entity_type='Account', entity_id=a.id, before=before,
        after={'id': a.id, 'name': a.name, 'is_active': bool(a.is_active)},
        account_before_id=a.id, account_after_id=a.id,
    )
    db.session.commit()
    flash(f'Account {"reactivated" if a.is_active else "deactivated"}.', 'success')
    return redirect(url_for('accounts.manage_accounts'))


@accounts_bp.route('/<int:account_id>/delete', methods=['POST'])
@login_required
def delete_account(account_id):
    _deny_account_master_mutation()
    """Delete an account safely.

    Accounts that are referenced by any transaction/payment (voided or not) are
    archived (soft-deleted) instead of hard-deleted so historical accounting
    integrity is preserved.  Only unreferenced accounts are hard-deleted.
    """
    a = Account.query.get_or_404(account_id)
    before = {'id': a.id, 'name': a.name, 'balance': _money_round(a.balance), 'is_active': bool(a.is_active)}
    try:
        # Inspect every declared FK to account.id (payments, transactions, GRNs,
        # sales, rentals, cash-flow entries, reconciliations, etc.) rather than
        # maintaining an incomplete hand-written list.
        reference_count = 0
        for table in db.metadata.sorted_tables:
            for column in table.columns:
                if any(fk.target_fullname == 'account.id' for fk in column.foreign_keys):
                    reference_count += int(db.session.query(func.count()).select_from(table).filter(column == a.id).scalar() or 0)
        from utils.accounting_audit import record_accounting_audit
        if reference_count:
            a.is_active = False
            a.updated_by = getattr(current_user, 'username', None)
            a.revision = int(getattr(a, 'revision', None) or 1) + 1
            record_accounting_audit(
                current_user, action='Delete', entity_type='Account', entity_id=a.id,
                before=before, after={**before, 'is_active': False, 'archived': True,
                                      'historical_references': reference_count},
                amount_before=before['balance'], amount_after=before['balance'],
                account_before_id=a.id, account_after_id=a.id,
                reason='Archived because historical references exist',
            )
            db.session.commit()
            flash('Account has historical records and was safely archived. History and balances were preserved.', 'warning')
        else:
            record_accounting_audit(
                current_user, action='Delete', entity_type='Account', entity_id=a.id,
                before=before, after={'deleted': True}, amount_before=before['balance'], amount_after=0,
                account_before_id=a.id, reason='Unreferenced account hard-deleted',
            )
            db.session.delete(a)
            db.session.commit()
            flash('Unreferenced account deleted.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Delete account failed')
        flash(f'Unable to delete account safely: {exc}', 'danger')
    return redirect(url_for('accounts.manage_accounts'))




@accounts_bp.route('/reconciliations')
@login_required
def reconciliations():
    """List of per-account reconciliation records (immutable audit history)."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    per_page = min(max(request.args.get('per_page', 50, type=int) or 50, 10), 100)
    account_id_f = request.args.get('account_id', type=int)
    status_f = (request.args.get('status') or '').strip()
    date_from, date_to_excl = _parse_date_range(default_days=365)
    from models import AccountReconciliation

    q = AccountReconciliation.query.filter(
        AccountReconciliation.reconciliation_date >= date_from,
        AccountReconciliation.reconciliation_date < date_to_excl,
    )
    if account_id_f:
        q = q.filter(AccountReconciliation.account_id == account_id_f)
    if status_f:
        q = q.filter(AccountReconciliation.difference_type == status_f)
    total_count = q.count()
    recs = q.order_by(
        AccountReconciliation.reconciliation_date.desc(),
        AccountReconciliation.id.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    return render_template(
        'accounts/reconciliations.html', recs=recs, total_count=total_count,
        accounts=Account.query.order_by(Account.name.asc()).all(),
        account_id_f=account_id_f, status_f=status_f,
        date_from=date_from, date_to=date_to_excl - timedelta(days=1), per_page=per_page,
    )


@accounts_bp.route('/<int:account_id>/reconcile', methods=['GET', 'POST'])
@login_required
def reconcile_account(account_id):
    """Reconcile one account: compare ledger (expected) vs physical (actual)."""
    from app.services.payments_crud import ledger_balance, reconcile_account as do_reconcile
    from models import AccountReconciliation

    account = Account.query.get_or_404(account_id)
    expected = ledger_balance(account.id)
    recent = AccountReconciliation.query.filter_by(account_id=account.id).order_by(
        AccountReconciliation.reconciliation_date.desc(), AccountReconciliation.id.desc()
    ).limit(5).all()

    if request.method == 'POST':
        try:
            actual = request.form.get('actual_balance', '').strip()
            if actual == '':
                raise ValueError('Actual balance is required.')
            note = request.form.get('note', '')
            date_raw = (request.form.get('reconciliation_date') or '').strip()
            if date_raw:
                try:
                    rec_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
                except ValueError:
                    raise ValueError('Invalid reconciliation date.')
            else:
                rec_date = pk_today()
            rec = do_reconcile(
                account_id=account.id,
                actual_balance=actual,
                reconciliation_date=rec_date,
                note=note,
                actor=current_user,
            )
            db.session.commit()
            flash(
                f'Account reconciled as {rec.difference_type}. '
                f'Expected Rs. {rec.expected_balance:,.2f}, Actual Rs. {rec.actual_balance:,.2f}, '
                f'Difference Rs. {rec.difference:,.2f}.',
                'success' if rec.difference_type == 'Matched' else 'warning'
            )
            return redirect(url_for('accounts.account_ledger', account_id=account.id))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Account reconciliation failed')
            flash(f'Unable to reconcile account: {exc}', 'danger')

    return render_template('accounts/reconcile_account.html', account=account,
                           expected=expected, recent=recent,
                           today=pk_today().strftime('%Y-%m-%d'))
