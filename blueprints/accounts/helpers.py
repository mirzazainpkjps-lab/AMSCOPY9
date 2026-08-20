"""helpers — split from accounts.py."""
from ._common import *  # noqa
from app.services.financial_ledgers import (
    build_current_payables,
    build_supplier_financial_ledger,
    build_supplier_payable_summaries,
)


def _grn_has_active_auto_payment(grn):
    """Return whether a GRN's paid amount is already represented by a payment row.

    New GRNs create a marked ``SupplierPayment`` so that supplier ledgers and
    account postings have one canonical payment source.  The fallback keeps
    legacy GRNs (created before that row existed) visible without counting a
    new GRN twice in dashboards and KPI pages.
    """
    if not grn or not getattr(grn, 'id', None):
        return False
    marker = f'[AUTO_GRN_PAY:{grn.id}]'
    return SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        SupplierPayment.note.ilike(f'%{marker}%')
    ).first() is not None


def _legacy_unrepresented_grn_paid_total(grns):
    return sum(
        float(getattr(grn, 'paid_amount', 0) or 0)
        for grn in (grns or [])
        if not _grn_has_active_auto_payment(grn)
    )


def _normalize_namespace(namespace):
    ns = (namespace or AUTO_BILL_NS_DEFAULT).strip().upper()
    if not ns:
        ns = AUTO_BILL_NS_DEFAULT
    if not re.fullmatch(r'[A-Z][A-Z0-9]{1,7}', ns):
        ns = AUTO_BILL_NS_DEFAULT
    return ns


def _extract_sb_parts(value):
    raw = (value or '').strip()
    if not raw:
        return (None, None)
    txt = raw.upper().replace(' ', '')
    m = re.match(r'^SB-([A-Z][A-Z0-9]{1,7})-(\d+)$', txt)
    if m:
        return (_normalize_namespace(m.group(1)), int(m.group(2)))
    # legacy "SB NO." formats
    m = re.search(r'SB\s*NO\.\s*([A-Z][A-Z0-9]{1,7})\s*[-#]?\s*(\d+)', raw.upper())
    if m:
        return (_normalize_namespace(m.group(1)), int(m.group(2)))
    return (None, None)


def _format_auto_bill(namespace, seq):
    ns = _normalize_namespace(namespace)
    return f"SB-{ns}-{int(seq)}"


def _get_or_create_bill_counter(namespace):
    ns = _normalize_namespace(namespace)
    counter = BillCounter.query.filter_by(namespace=ns).first()
    if counter:
        return counter
    counter = BillCounter(namespace=ns, count=1000)
    db.session.add(counter)
    db.session.flush()
    return counter


def _max_existing_seq_for_namespace(namespace):
    ns = _normalize_namespace(namespace)
    max_seq = 0
    rows = Payment.query.with_entities(Payment.auto_bill_no).filter(
        Payment.auto_bill_no.isnot(None),
        func.upper(func.trim(Payment.auto_bill_no)).like(f"SB-{ns}-%")
    ).all()
    for (val,) in rows:
        parsed_ns, seq = _extract_sb_parts(val or '')
        if parsed_ns == ns and seq is not None:
            try:
                max_seq = max(max_seq, int(seq))
            except Exception:
                continue
    return max_seq


def _sync_bill_counter_with_db(namespace):
    ns = _normalize_namespace(namespace)
    counter = _get_or_create_bill_counter(ns)
    max_seq = _max_existing_seq_for_namespace(ns)
    next_seq = (max_seq + 1) if max_seq else int(counter.count or 1000)
    if int(counter.count or 0) < next_seq:
        counter.count = next_seq
        db.session.flush()
    return int(counter.count or 1000)


@accounts_bp.before_request
def _accounts_permission_check():
    if not current_user.is_authenticated:
        return
    role_norm = (getattr(current_user, 'role', '') or '').strip().lower()
    # Module entry: admin/root, the dedicated Khata flag, or a payment
    # manager (legacy path).  Account MASTER mutations are checked again
    # inside each view via _deny_account_master_mutation() (admin/root only).
    if (
        role_norm not in ('admin', 'root')
        and not getattr(current_user, 'can_manage_accounts', False)
        and not getattr(current_user, 'can_manage_payments', False)
    ):
        from flask import abort
        abort(403)


def _resolve_client(client_input, active_only=False):
    value = (client_input or '').strip()
    if not value:
        return None
    q = Client.query
    if active_only:
        q = q.filter_by(is_active=True)
    client = q.filter(func.lower(func.trim(Client.code)) == value.lower()).first()
    if client:
        return client
    return q.filter(func.lower(func.trim(Client.name)) == value.lower()).first()


def _resolve_supplier(supplier_input, active_only=True):
    value = (supplier_input or '').strip()
    if not value:
        return None
    try:
        supplier_id = int(value)
    except Exception:
        supplier_id = None
    if supplier_id:
        supplier = Supplier.query.get(supplier_id)
        if supplier:
            return supplier
    q = Supplier.query
    if active_only:
        q = q.filter_by(is_active=True)
    return q.filter(func.lower(func.trim(Supplier.name)) == value.lower()).first()


def _account_option_label(account):
    cat = (account.category or '').strip().upper() or 'CASH'
    label = f"[{cat}] {account.name or ''}"
    if account.bank_name:
        label += f" — {account.bank_name}"
    label += f" (Bal: Rs {_money_round(account.balance):,.2f})"
    return label


def _active_clients():
    """Clients that may be selected for new transactions (suspended excluded)."""
    return Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()


def _active_suppliers():
    """Suppliers that may be selected for new transactions (suspended excluded)."""
    return Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()


def _money_round(value):
    from decimal import Decimal, ROUND_HALF_UP
    try:
        d = Decimal(str(value if value is not None else 0))
    except Exception:
        d = Decimal('0')
    d = d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if d == Decimal('-0.00'):
        d = Decimal('0.00')
    return float(d)


def _client_due_summary():
    """Dashboard due-client KPI sourced from the consolidated ledger projection."""
    report = build_current_payables(status='outstanding', page=1, per_page=200)
    return [{
        'client_code': row.get('client_code', ''),
        'client_name': row.get('client_name', ''),
        'due_amount': float(row.get('outstanding') or 0),
    } for row in report.get('all_rows', [])]


def _supplier_payable_summary():
    """Dashboard supplier KPI sourced from the same Supplier Ledger projection."""
    summary = []
    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()
    balances = build_supplier_payable_summaries(suppliers)
    for supplier in suppliers:
        amount = float(balances.get(supplier.id) or 0)
        if amount <= 0:
            continue
        summary.append({
            'supplier_id': supplier.id,
            'supplier_name': supplier.name,
            'payable_amount': amount,
        })
    summary.sort(key=lambda x: (-x['payable_amount'], x['supplier_name'].lower()))
    return summary


def _company_accounts():
    company_accounts = Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.account_type)) == 'company'
    ).order_by(Account.name.asc()).all()
    if company_accounts:
        return company_accounts
    return Account.query.filter(func.coalesce(Account.is_active, True) == True).order_by(Account.name.asc()).all()


def _account_categories():
    return AccountCategory.query.filter_by(is_active=True).order_by(AccountCategory.name.asc()).all()


def _active_accounts():
    return Account.query.filter(func.coalesce(Account.is_active, True) == True)


def _is_account_active(account):
    return bool(account) and getattr(account, 'is_active', True) is not False


def _expected_account_category(method):
    m = (method or '').strip().lower()
    if m in ['cash', 'cash sale']:
        return 'cash'
    if m in ['bank', 'bank transfer', 'check', 'cheque', 'card', 'online']:
        return 'bank'
    return None


def _validate_account_matches_method(account, method, role_label):
    expected = _expected_account_category(method)
    if not expected:
        return
    acc_cat = (getattr(account, 'category', None) or '').strip().lower()
    if acc_cat != expected:
        raise ValueError(f"{role_label} account must be a {expected} account for method '{method}'.")


def _ensure_default_account_categories():
    defaults = ['Company', 'Own Funds', 'Clients', 'External', 'Loan']
    existing = {
        (row.name or '').strip().lower()
        for row in AccountCategory.query.filter_by(is_active=True).all()
    }
    created = False
    for name in defaults:
        if name.lower() in existing:
            continue
        db.session.add(AccountCategory(name=name))
        created = True
    if created:
        db.session.commit()


def _backfill_legacy_account_groups():
    changed = False
    for account in Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        or_(Account.source_category.is_(None), func.trim(Account.source_category) == '')
    ).all():
        account_type = (account.account_type or '').strip().lower()
        if 'client' in account_type:
            account.source_category = 'Clients'
        elif 'loan' in account_type:
            account.source_category = 'Loan'
        elif 'supplier' in account_type or 'external' in account_type:
            account.source_category = 'External'
        else:
            account.source_category = 'Company'
        changed = True
    if changed:
        db.session.commit()


def _apply_payment_to_pending_bills(client, paid_amount, discount_amount=0):
    """Apply receive amount + discount to open pending bills of a client."""
    if not client:
        return

    total_settlement = max(0.0, float(paid_amount or 0)) + max(0.0, float(discount_amount or 0))
    if total_settlement <= 0:
        return

    client_name_norm = (client.name or '').strip().lower()
    match_filters = [
        func.lower(func.trim(func.coalesce(PendingBill.client_name, ''))) == client_name_norm
    ]
    client_code_norm = (client.code or '').strip().lower()
    if client_code_norm:
        match_filters.append(
            func.lower(func.trim(func.coalesce(PendingBill.client_code, ''))) == client_code_norm
        )

    open_bills = PendingBill.query.filter(
        PendingBill.is_void == False,
        PendingBill.is_paid == False,
        PendingBill.amount > 0,
        or_(*match_filters)
    ).order_by(PendingBill.id.asc()).all()

    remaining = total_settlement
    for pb in open_bills:
        if remaining <= 0:
            break
        bill_amount = float(pb.amount or 0)
        if bill_amount <= 0:
            pb.is_paid = True
            continue
        settle = min(bill_amount, remaining)
        pb.amount = max(0.0, bill_amount - settle)
        pb.client_name = client.name
        pb.client_code = client.code
        pb.is_paid = pb.amount <= 0.00001
        remaining -= settle


def _parse_date_range(default_days=30):
    """Parse `from`/`to` from query string. Returns (date_from, date_to_exclusive)."""
    from_raw = (request.args.get('from') or '').strip()
    to_raw = (request.args.get('to') or '').strip()
    today = pk_today()
    try:
        date_from = datetime.strptime(from_raw, '%Y-%m-%d').date() if from_raw else (today - timedelta(days=default_days))
    except ValueError:
        date_from = today - timedelta(days=default_days)
    try:
        date_to = datetime.strptime(to_raw, '%Y-%m-%d').date() if to_raw else today
    except ValueError:
        date_to = today
    if date_to < date_from:
        date_to = date_from
    return date_from, date_to + timedelta(days=1)


def _reverse_balance_effect(tx):
    """Reverse the balance effect of an AccountTransaction (used for voiding)."""
    if tx.from_account_id:
        a = Account.query.get(tx.from_account_id)
        if a:
            a.balance = float(a.balance or 0) + float(tx.amount or 0)
    if tx.to_account_id:
        a = Account.query.get(tx.to_account_id)
        if a:
            a.balance = float(a.balance or 0) - float(tx.amount or 0)


