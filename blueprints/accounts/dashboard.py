"""dashboard — split from accounts.py."""
from ._common import *  # noqa

from uuid import uuid4

@accounts_bp.route('/')
@login_required
def dashboard():
    """Accounts dashboard with KPI cards and financial overview."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    today = pk_today()
    
    # Calculate KPIs
    # Total ledger payments from clients today (Payment table).
    client_payments_today = db.session.query(func.sum(Payment.amount)).filter(
        Payment.date_posted >= today,
        Payment.date_posted < today + timedelta(days=1),
        Payment.is_void == False,
        Payment.payment_account_id.isnot(None),
        Payment.amount > 0
    ).scalar() or 0
    
    # SupplierPayment is the canonical payment row for new GRNs.  Add only
    # legacy GRN payments that do not have the marked auto-payment row, or
    # the dashboard counts every GRN payment twice.
    supplier_payments_core_today = db.session.query(func.sum(SupplierPayment.amount)).filter(
        SupplierPayment.date_posted >= today,
        SupplierPayment.date_posted < today + timedelta(days=1),
        SupplierPayment.is_void == False
    ).scalar() or 0
    legacy_grns_today = GRN.query.filter(
        GRN.date_posted >= today,
        GRN.date_posted < today + timedelta(days=1),
        GRN.is_void == False,
        GRN.paid_amount > 0
    ).all()
    grn_supplier_paid_today = _legacy_unrepresented_grn_paid_total(legacy_grns_today)

    supplier_payments_today = float(supplier_payments_core_today or 0) + float(grn_supplier_paid_today or 0)
    
    # Total expenditures today
    expenditures_today = db.session.query(func.sum(FbmCashDrawerEntry.amount)).filter(
        FbmCashDrawerEntry.date_posted >= today,
        FbmCashDrawerEntry.date_posted < today + timedelta(days=1),
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False
    ).scalar() or 0
    
    # Total receipts today (paid amounts only): bookings + direct sales.
    booking_paid_today = db.session.query(func.sum(Booking.paid_amount)).filter(
        Booking.date_posted >= today,
        Booking.date_posted < today + timedelta(days=1),
        Booking.is_void == False,
        Booking.paid_amount > 0
    ).scalar() or 0

    sales_paid_today = db.session.query(func.sum(DirectSale.paid_amount)).filter(
        DirectSale.date_posted >= today,
        DirectSale.date_posted < today + timedelta(days=1),
        DirectSale.is_void == False
    ).scalar() or 0

    tx_receipts_today = db.session.query(func.sum(AccountTransaction.amount)).filter(
        AccountTransaction.date_posted >= today,
        AccountTransaction.date_posted < today + timedelta(days=1),
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type == 'Receipt',
        AccountTransaction.to_account_id.isnot(None),
        # Booking/direct-sale/payment source rows are already represented by
        # their source document totals above.  Only standalone receipts belong
        # in this additional bucket.
        or_(
            AccountTransaction.note.is_(None),
            ~AccountTransaction.note.ilike('%[SRC:%')
        )
    ).scalar() or 0

    receipts_today = float(booking_paid_today or 0) + float(sales_paid_today or 0) + float(client_payments_today or 0) + float(tx_receipts_today or 0)
    
    # Account balances + due clients + company liquidity KPI
    accounts = _active_accounts().order_by(Account.name.asc()).all()
    due_clients = _client_due_summary()
    supplier_payables = _supplier_payable_summary()
    company_accounts = _company_accounts()
    account_categories = _account_categories()
    total_company_money = sum(float(a.balance or 0) for a in company_accounts)
    total_cash_money = sum(float(a.balance or 0) for a in company_accounts if (a.category or '').lower() == 'cash')
    receive_source_accounts = [{
        'id': a.id,
        'name': a.name,
        'source_category': (a.source_category or '').strip()
    } for a in accounts if (a.source_category or '').strip()]
    
    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    # Drivers are payables like suppliers: the Accounts section can pay them
    # directly, and the resulting transaction appears in the driver ledger.
    from app.services.driver_payments import driver_outstanding
    delivery_persons = DeliveryPerson.query.filter_by(is_active=True).order_by(DeliveryPerson.name.asc()).all()
    driver_outstanding_map = {}
    for person in delivery_persons:
        try:
            driver_outstanding_map[person.id] = driver_outstanding(person.id)
        except Exception:
            driver_outstanding_map[person.id] = 0.0
    return render_template('accounts/dashboard.html',
                          client_payments_today=client_payments_today,
                          supplier_payments_today=supplier_payments_today,
                          expenditures_today=expenditures_today,
                          receipts_today=receipts_today,
                         accounts=accounts,
                         due_clients=due_clients,
                         supplier_payables=supplier_payables,
                         company_accounts=company_accounts,
                          account_categories=account_categories,
                          receive_source_accounts=receive_source_accounts,
                          total_company_money=total_company_money,
                          total_cash_money=total_cash_money,
                          suppliers=suppliers,
                          clients=clients,
                          delivery_persons=delivery_persons,
                          driver_outstanding=driver_outstanding_map,
                          submission_token=uuid4().hex,
                          default_tx_datetime=pk_now().strftime('%Y-%m-%dT%H:%M'))


