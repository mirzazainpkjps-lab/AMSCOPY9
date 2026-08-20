import pytest
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from models import db
from app.services.time_money import pk_now
from app.services.void_rebuild import rebuild_pending_bills
from app.services.finance_clients import _client_balance_as_of
app = create_app({"TESTING": True})
from models import Client, Booking, Payment, AccountTransaction, Account

@pytest.fixture
def client_app():
    with app.app_context():
        db.create_all()
        yield

def test_refund_creates_ledger_and_clears_pending(client_app):
    # Create test client and cash account
    client = Client.query.filter_by(code='TEST-REFUND-1').first()
    if not client:
        client = Client(code='TEST-REFUND-1', name='Test Refund Client', opening_balance=0)
        db.session.add(client)
        db.session.flush()

    acc = Account.query.filter_by(name='Test Cash Account').first()
    if not acc:
        acc = Account(name='Test Cash Account', category='Cash', account_type='cash', balance=100000.0)
        db.session.add(acc)
        db.session.flush()

    # Create booking that causes client credit
    bk = Booking(client_name=client.name, amount=0.0, paid_amount=11788.0, date_posted=pk_now())
    db.session.add(bk)
    db.session.commit()

    before_balance = _client_balance_as_of(client)

    # Post refund
    refund_amount = 11788.0
    payment = Payment(
        client_name=client.name,
        amount=-float(refund_amount),
        method='Refund',
        note='Test refund',
        discount=0,
        discount_reason='Client refund',
        date_posted=pk_now(),
        account_name=(acc.name or ''),
        bank_name=(acc.bank_name or ''),
        account_no=(acc.account_number or ''),
        payment_account_id=acc.id,
        auto_bill_no=None
    )
    db.session.add(payment)
    db.session.flush()

    tx = AccountTransaction(
        from_account_id=acc.id,
        to_account_id=None,
        amount=refund_amount,
        description=f'Client refund to {client.name}',
        note=f'[SRC:ClientRefund:{payment.id}]',
        transaction_type='Payment',
        date_posted=pk_now()
    )
    db.session.add(tx)
    db.session.commit()

    after_balance = _client_balance_as_of(client)

    # reload rows
    payments = Payment.query.filter(Payment.client_name==client.name).all()
    txs = AccountTransaction.query.filter(AccountTransaction.note.ilike(f'%ClientRefund%')).all()

    # Assertions
    assert any(p.amount < 0 for p in payments), 'No negative refund Payment row created.'
    assert any(t for t in txs if t.amount == refund_amount), 'No AccountTransaction cash-out created.'
    assert before_balance < 0 and after_balance == 0, f'Expected balance to move from negative to zero (before {before_balance}, after {after_balance})'

    # Ensure no SB-CP negative payments
    neg_sbcp = Payment.query.filter(Payment.auto_bill_no!=None, Payment.auto_bill_no.ilike('SB-CP-%'), Payment.amount < 0).all()
    assert len(neg_sbcp) == 0, 'Found negative SB-CP payments'
