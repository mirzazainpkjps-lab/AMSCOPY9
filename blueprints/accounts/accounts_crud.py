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
    """Add a new account (redesigned Create form)."""
    _deny_account_master_mutation()
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    from .classification import registry_json
    from .account_form import validate_account_form, cascade_options

    if request.method == 'POST':
        try:
            cleaned = validate_account_form(request.form, is_edit=False)
            name = cleaned["name"]

            # Opening position (PART 7): amount + direction + effective date.
            # Debit => positive opening (asset-like), Credit => negative opening
            # (liability-like).  Stored as the explicit opening baseline used by
            # ledger_balance(), so it is auditable and never a parallel balance.
            from utils.money import from_minor, to_minor
            opening_amount_raw = (request.form.get('opening_amount') or '0').strip()
            opening_minor = to_minor(opening_amount_raw or 0, field='Opening amount')
            position = (request.form.get('opening_position') or 'debit').strip().lower()
            if position == 'credit':
                opening_minor = -opening_minor
            effective_raw = (request.form.get('opening_effective_date') or '').strip()
            if effective_raw:
                try:
                    effective_dt = datetime.strptime(effective_raw, '%Y-%m-%d')
                except ValueError:
                    raise ValueError('Opening effective date must be a valid date (YYYY-MM-DD).')
            else:
                effective_dt = pk_now()
            opening_value = float(from_minor(opening_minor))

            account = Account(
                name=name,
                category=cleaned["category"],
                source_category=cleaned["source_category"],
                account_type=cleaned["account_type"],
                type=cleaned["type"],
                balance=opening_value,
                balance_minor=opening_minor,
                opening_balance=opening_value,
                opening_balance_minor=opening_minor,
                opening_balance_date=effective_dt,
                bank_name=cleaned["bank_name"],
                account_holder_name=cleaned["account_holder_name"],
                account_number=cleaned["account_number"],
                branch_code=cleaned["branch_code"],
                class_category=cleaned["class_category"],
                class_subcategory=cleaned["class_subcategory"],
                class_account_type=cleaned["class_account_type"],
                channel=cleaned["channel"],
                cash_location=cleaned["cash_location"],
                cash_responsible=cleaned["cash_responsible"],
                wallet_provider=cleaned["wallet_provider"],
                wallet_number=cleaned["wallet_number"],
                wallet_holder=cleaned["wallet_holder"],
                linked_entity_type=cleaned["linked_entity_type"],
                linked_client_id=cleaned["linked_client_id"],
                linked_supplier_id=cleaned["linked_supplier_id"],
                linked_party_name=cleaned["linked_party_name"],
                account_status=cleaned["account_status"],
                is_active=cleaned["is_active"],
                note=cleaned["note"],
            )
            from utils.accounting_audit import record_accounting_audit
            db.session.add(account)
            db.session.flush()
            record_accounting_audit(
                current_user, action='Create', entity_type='Account', entity_id=account.id,
                after={
                    'name': name, 'class_category': cleaned["class_category"],
                    'class_subcategory': cleaned["class_subcategory"],
                    'class_account_type': cleaned["class_account_type"],
                    'channel': cleaned["channel"], 'account_status': cleaned["account_status"],
                    'opening_balance': opening_value, 'opening_position': position,
                },
                amount_after=opening_value, account_after_id=account.id, reason=cleaned["note"],
            )
            db.session.commit()
            flash('Account added successfully!', 'success')
            return redirect(url_for('accounts.manage_accounts'))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return redirect(url_for('accounts.add_account'))
        except Exception as exc:
            db.session.rollback()
            logger.exception('Add account failed')
            flash(f'Unable to add account: {exc}', 'danger')
            return redirect(url_for('accounts.add_account'))

    return render_template(
        'accounts/add_account.html',
        categories=_account_categories(),
        registry=registry_json(),
        options=cascade_options(),
        clients=_active_clients(),
        suppliers=_active_suppliers(),
        today=pk_today().strftime('%Y-%m-%d'),
    )


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
    """JSON data for an account (kept for backwards compatibility / tooling)."""
    from app.services.payments_crud import ledger_balance
    a = Account.query.get_or_404(account_id)
    return jsonify({
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'source_category': a.source_category,
        'account_type': a.account_type or (getattr(a, 'type', None) or ''),
        'balance': float(a.balance or 0),
        'calculated_balance': ledger_balance(a.id),
        'bank_name': a.bank_name or '',
        'account_holder_name': a.account_holder_name or '',
        'account_number': a.account_number or '',
        'branch_code': a.branch_code or '',
        'note': a.note or '',
        'is_active': bool(a.is_active),
        'class_category': a.class_category or '',
        'class_subcategory': a.class_subcategory or '',
        'class_account_type': a.class_account_type or '',
        'channel': a.channel or '',
        'cash_location': a.cash_location or '',
        'cash_responsible': a.cash_responsible or '',
        'wallet_provider': a.wallet_provider or '',
        'wallet_number': a.wallet_number or '',
        'wallet_holder': a.wallet_holder or '',
        'linked_entity_type': a.linked_entity_type or 'none',
        'linked_client_id': a.linked_client_id,
        'linked_supplier_id': a.linked_supplier_id,
        'linked_party_name': a.linked_party_name or '',
        'account_status': a.account_status or ('active' if a.is_active else 'inactive'),
    })


@accounts_bp.route('/<int:account_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_account(account_id):
    """Edit an account (redesigned Edit form).

    Structurally identical to the Create form, plus one extra Current Balance &
    Adjustment section (PART 12).  Opening can be corrected in place (rewrites
    the historical baseline and shifts today's calculated balance).  A physical
    mismatch is posted as a single traceable Adjustment ledger entry — never a
    silent overwrite of current balance (PART 13/15).
    """
    _deny_account_master_mutation()
    from .classification import registry_json
    from .account_form import validate_account_form, cascade_options
    from app.services.payments_crud import ledger_balance, _assert_period_open

    a = Account.query.get_or_404(account_id)

    if request.method == 'POST':
        try:
            cleaned = validate_account_form(request.form, is_edit=True)
            note = cleaned["note"]

            before = {
                'id': a.id, 'name': a.name, 'class_category': a.class_category,
                'class_subcategory': a.class_subcategory, 'class_account_type': a.class_account_type,
                'channel': a.channel, 'account_status': a.account_status,
                'balance': _money_round(a.balance), 'is_active': bool(a.is_active),
                'opening_balance': _money_round(a.opening_balance),
                'opening_balance_date': (
                    a.opening_balance_date.strftime('%Y-%m-%d') if a.opening_balance_date else None
                ),
            }

            # Update classification + details.  validate_account_form already
            # returned only the channel-relevant detail fields and cleared the
            # incompatible ones (PART 11), so no stale bank/wallet data remains.
            a.name = cleaned["name"]
            a.class_category = cleaned["class_category"]
            a.class_subcategory = cleaned["class_subcategory"]
            a.class_account_type = cleaned["class_account_type"]
            a.channel = cleaned["channel"]
            a.category = cleaned["category"]
            a.source_category = cleaned["source_category"]
            a.account_type = cleaned["account_type"]
            a.type = cleaned["type"]
            a.cash_location = cleaned["cash_location"]
            a.cash_responsible = cleaned["cash_responsible"]
            a.bank_name = cleaned["bank_name"]
            a.account_holder_name = cleaned["account_holder_name"]
            a.account_number = cleaned["account_number"]
            a.branch_code = cleaned["branch_code"]
            a.wallet_provider = cleaned["wallet_provider"]
            a.wallet_number = cleaned["wallet_number"]
            a.wallet_holder = cleaned["wallet_holder"]
            a.linked_entity_type = cleaned["linked_entity_type"]
            a.linked_client_id = cleaned["linked_client_id"]
            a.linked_supplier_id = cleaned["linked_supplier_id"]
            a.linked_party_name = cleaned["linked_party_name"]
            a.account_status = cleaned["account_status"]
            a.is_active = cleaned["is_active"]
            a.note = note

            # ---- Opening baseline (editable historical start) ----
            # Applied first so today's calculated balance already includes the
            # corrected opening before any physical-cash adjustment is posted.
            opening_msg = _apply_opening_update(a, request.form)
            if opening_msg:
                flash(opening_msg, 'info')

            # ---- Balance adjustment (PART 12 / PART 13) ----
            desired_raw = (request.form.get('desired_balance') or '').strip()
            adjustment_msg = _apply_balance_adjustment(a, desired_raw, request.form)
            if adjustment_msg:
                flash(adjustment_msg, 'info')

            a.updated_by = getattr(current_user, 'username', None)
            a.revision = int(getattr(a, 'revision', None) or 1) + 1
            from utils.accounting_audit import record_accounting_audit
            after = {
                'id': a.id, 'name': a.name, 'class_category': a.class_category,
                'class_subcategory': a.class_subcategory, 'class_account_type': a.class_account_type,
                'channel': a.channel, 'account_status': a.account_status,
                'balance': _money_round(a.balance), 'is_active': bool(a.is_active),
                'opening_balance': _money_round(a.opening_balance),
                'opening_balance_date': (
                    a.opening_balance_date.strftime('%Y-%m-%d') if a.opening_balance_date else None
                ),
            }
            record_accounting_audit(
                current_user, action='Edit', entity_type='Account', entity_id=a.id,
                before=before, after=after, amount_before=before['balance'], amount_after=after['balance'],
                account_before_id=a.id, account_after_id=a.id, reason=note,
            )
            db.session.commit()
            flash('Account updated successfully.', 'success')
            return redirect(url_for('accounts.manage_accounts'))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Edit account failed')
            flash(f'Unable to update account: {exc}', 'danger')

        # On validation failure, fall through to re-render the edit page so the
        # user keeps their entered values and sees the flash message.

    # GET (or re-render after a POST error): preload every value so dependent
    # dropdowns initialise in the correct order (PART 10).
    calculated = ledger_balance(a.id)
    return render_template(
        'accounts/edit_account.html',
        account=a,
        categories=_account_categories(),
        registry=registry_json(),
        options=cascade_options(),
        clients=_active_clients(),
        suppliers=_active_suppliers(),
        calculated_balance=calculated,
        today=pk_today().strftime('%Y-%m-%d'),
    )


def _current_opening_minor(account):
    from utils.money import to_minor
    if getattr(account, 'opening_balance_minor', None) is not None:
        return int(account.opening_balance_minor)
    return to_minor(account.opening_balance or 0)


def _apply_opening_update(account, form):
    """Rewrite the auditable opening baseline without posting a ledger entry.

    Changing opening shifts the cached current balance by the same delta so
    ledger_balance() (opening + movements) stays in lockstep.  Missing
    ``opening_amount`` is treated as no change so older edit posts keep working.
    """
    from utils.money import from_minor, to_minor

    amount_raw = (form.get('opening_amount') if form is not None else None)
    amount_raw = (amount_raw or '').strip() if amount_raw is not None else ''
    date_raw = (form.get('opening_effective_date') if form is not None else None)
    date_raw = (date_raw or '').strip() if date_raw is not None else ''
    if amount_raw == '' and date_raw == '':
        return ''

    old_minor = _current_opening_minor(account)
    new_minor = old_minor
    if amount_raw != '':
        amount_minor = to_minor(amount_raw or 0, field='Opening amount')
        if amount_minor < 0:
            raise ValueError('Opening amount cannot be negative. Use Credit for money we owe.')
        position = ((form.get('opening_position') or 'debit') if form is not None else 'debit')
        position = (position or 'debit').strip().lower()
        if position not in ('debit', 'credit'):
            raise ValueError('Opening balance direction must be debit or credit.')
        if position == 'credit':
            amount_minor = -amount_minor
        new_minor = amount_minor

    date_changed = False
    if date_raw:
        try:
            effective_dt = datetime.strptime(date_raw, '%Y-%m-%d')
        except ValueError:
            raise ValueError('Opening effective date must be a valid date (YYYY-MM-DD).')
        old_date = account.opening_balance_date
        old_d = old_date.date() if isinstance(old_date, datetime) else old_date
        if old_d != effective_dt.date():
            account.opening_balance_date = effective_dt
            date_changed = True

    if new_minor == old_minor:
        return 'Opening effective date updated.' if date_changed else ''

    delta = new_minor - old_minor
    account.opening_balance_minor = new_minor
    account.opening_balance = float(from_minor(new_minor))

    current_minor = (
        int(account.balance_minor)
        if getattr(account, 'balance_minor', None) is not None
        else to_minor(account.balance or 0)
    )
    new_current = current_minor + delta
    account.balance_minor = new_current
    account.balance = float(from_minor(new_current))
    return (
        f'Opening balance updated from Rs. {float(from_minor(old_minor)):.2f} '
        f'to Rs. {float(from_minor(new_minor)):.2f}. '
        f'Current calculated balance shifted by the same amount.'
    )


def _apply_balance_adjustment(account, desired_raw, form):
    """Post a single traceable Adjustment ledger entry for the desired balance.

    Returns a short human message, or '' when no adjustment is needed.
    Raises ValueError for invalid input.  Implements PART 13/14/15:

    * Desired == current  -> no entry, no zero-value transaction (PART 15).
    * Difference < 0      -> money OUT (visible negative ledger entry).
    * Difference > 0      -> money IN (visible positive ledger entry).
    * Idempotency key     -> retried / double-clicked save cannot post twice.
    """
    from utils.money import from_minor, to_minor
    from app.services.payments_crud import _assert_period_open, ledger_balance

    if desired_raw == '':
        return ''

    desired_minor = to_minor(desired_raw, field='Desired balance')
    # Compare against the reproducible ledger (opening + movements), which
    # already includes any opening correction applied earlier in this save.
    current_minor = to_minor(ledger_balance(account.id), field='Current balance')
    diff_minor = desired_minor - current_minor
    if diff_minor == 0:
        return ''  # PART 15: no-change save

    # Adjustment reason is mandatory whenever an adjustment is made (PART 12).
    reason = (form.get('adjustment_reason') or '').strip()
    reason_other = (form.get('adjustment_reason_other') or '').strip()
    if (reason or '').lower() == 'other':
        reason = reason_other
    if not reason:
        raise ValueError('An adjustment reason is required when changing the balance.')

    adj_date_raw = (form.get('adjustment_date') or '').strip()
    if adj_date_raw:
        try:
            adj_dt = datetime.strptime(adj_date_raw, '%Y-%m-%d')
        except ValueError:
            raise ValueError('Adjustment date must be a valid date (YYYY-MM-DD).')
    else:
        adj_dt = pk_now()

    # Reject edits inside a finalised (reconciled) period (PART 14 safety).
    _assert_period_open(account.id, adj_dt, operation='adjusted')

    # Idempotency: a retried/double-clicked save with the same key skips the
    # duplicate post.  An adjustment marker keyed to the account+key means a
    # repeat submission finds the existing entry and does nothing.
    idem = (form.get('idempotency_key') or '').strip()
    if idem:
        from models import AccountTransaction
        replay = AccountTransaction.query.filter(
            AccountTransaction.idempotency_key == idem,
            AccountTransaction.is_void == False,
        ).first()
        if replay:
            return ''

    direction = 'decrease' if diff_minor < 0 else 'increase'
    amount_minor = abs(diff_minor)
    adj = AccountTransaction(
        from_account_id=(account.id if diff_minor < 0 else None),
        to_account_id=(account.id if diff_minor > 0 else None),
        amount=float(from_minor(amount_minor)), amount_minor=amount_minor,
        description='Balance adjustment (manual edit)',
        note=(f'Adjusted from Rs. {float(from_minor(current_minor)):.2f} '
              f'to Rs. {float(from_minor(desired_minor)):.2f} ({direction} of '
              f'Rs. {float(from_minor(amount_minor)):.2f})'),
        reason=reason[:300],
        transaction_type='Adjustment', source_type='Account', source_id=account.id,
        idempotency_key=(idem[:64] if idem else None),
        created_by=getattr(current_user, 'username', None), date_posted=adj_dt,
    )
    db.session.add(adj)
    db.session.flush()
    from app.services.accounting import _apply_account_tx_effect
    _apply_account_tx_effect(adj)
    account.balance_minor = desired_minor
    account.balance = float(from_minor(desired_minor))
    return (f'Balance {direction}d by Rs. {float(from_minor(amount_minor)):.2f} '
            f'via an Adjustment ledger entry.')


@accounts_bp.route('/<int:account_id>/toggle', methods=['POST'])
@login_required
def toggle_account(account_id):
    """Soft-deactivate / reactivate an account (never corrupts history)."""
    _deny_account_master_mutation()
    a = Account.query.get_or_404(account_id)
    before = {'id': a.id, 'name': a.name, 'is_active': bool(a.is_active)}
    a.is_active = not bool(a.is_active)
    # Keep the 3-state status in sync with the legacy boolean so both the
    # redesigned status pills and the legacy filters agree.
    a.account_status = "active" if a.is_active else "inactive"
    a.updated_by = getattr(current_user, 'username', None)
    a.revision = int(getattr(a, 'revision', None) or 1) + 1
    from utils.accounting_audit import record_accounting_audit
    record_accounting_audit(
        current_user, action='Activate' if a.is_active else 'Suspend',
        entity_type='Account', entity_id=a.id, before=before,
        after={'id': a.id, 'name': a.name, 'is_active': bool(a.is_active),
               'account_status': a.account_status},
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
            a.account_status = "archived"
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
