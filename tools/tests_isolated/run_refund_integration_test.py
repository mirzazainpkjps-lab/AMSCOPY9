"""Refund integration test.

Usage:
    AMS_TEST_DB=/tmp/ams_test.db python tools/tests_isolated/run_refund_integration_test.py
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from tools.tests_isolated.test_isolation_guard import require_test_db
require_test_db()

from main import app, db, pk_now, _client_balance_as_of
from models import Client, Booking, Payment, AccountTransaction, Account

FAILED = False

with app.app_context():
    client = Client.query.filter_by(code='TEST-REFUND-1').first()
    if not client:
        client = Client(code='TEST-REFUND-1', name='Test Refund Client', opening_balance=0)
        db.session.add(client); db.session.flush()
    acc = Account.query.filter_by(name='Test Cash Account').first()
    if not acc:
        acc = Account(name='Test Cash Account', category='Cash', account_type='cash', balance=100000.0)
        db.session.add(acc); db.session.flush()

    # create booking that causes client credit
    bk = Booking(client_name=client.name, amount=0.0, paid_amount=11788.0, date_posted=pk_now())
    db.session.add(bk); db.session.commit()

    before_balance = _client_balance_as_of(client)
    if before_balance >= 0:
        print('FAIL: expected negative before_balance, got', before_balance)
        FAILED = True

    # post refund
    refund_amount = 11788.0
    payment = Payment(client_name=client.name, amount=-refund_amount, method='Refund', note='Integration test refund', date_posted=pk_now(), account_name=acc.name, payment_account_id=acc.id, auto_bill_no=None)
    db.session.add(payment); db.session.flush()
    tx = AccountTransaction(from_account_id=acc.id, to_account_id=None, amount=refund_amount, description=f'Client refund to {client.name}', note=f'[SRC:ClientRefund:{payment.id}]', transaction_type='Payment', date_posted=pk_now())
    db.session.add(tx); db.session.commit()

    # validations
    after_balance = _client_balance_as_of(client)
    if after_balance != 0:
        print('FAIL: expected after_balance == 0, got', after_balance)
        FAILED = True
    # verify refund payment exists and has no auto_bill_no
    p = db.session.get(Payment, payment.id)
    if not p or p.amount >= 0:
        print('FAIL: refund Payment missing or not negative:', p)
        FAILED = True
    if p.auto_bill_no is not None:
        print('FAIL: refund Payment has auto_bill_no set:', p.auto_bill_no)
        FAILED = True
    # verify account transaction created
    txq = AccountTransaction.query.filter(AccountTransaction.note.ilike(f'%ClientRefund%'), AccountTransaction.amount==refund_amount).all()
    if not any(f'[SRC:ClientRefund:{payment.id}]' in (t.note or '') for t in txq):
        print('FAIL: expected AccountTransaction with pay marker for payment id', payment.id)
        FAILED = True

    # verify no new negative SB-CP payment with this id
    sbcp = Payment.query.filter(Payment.auto_bill_no!=None, Payment.auto_bill_no.ilike('SB-CP-%'), Payment.amount < 0).all()
    # there may be historical SB-CP negatives, but our created payment must not be SB-CP
    if any(p.id == payment.id and p.auto_bill_no and p.auto_bill_no.upper().startswith('SB-CP') for p in [p]):
        print('FAIL: created negative payment has SB-CP auto bill', p.auto_bill_no)
        FAILED = True

if FAILED:
    print('\nINTEGRATION TEST: FAILED')
    sys.exit(2)
else:
    print('\nINTEGRATION TEST: PASSED')
    sys.exit(0)
