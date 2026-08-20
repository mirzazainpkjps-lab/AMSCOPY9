"""Refund flow quick verification.

Usage:
    AMS_TEST_DB=/tmp/ams_test.db python tools/tests_isolated/verify_refund_quick.py
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from tools.tests_isolated.test_isolation_guard import require_test_db
require_test_db()

from main import app, db, pk_now, _client_balance_as_of
from models import Client, Booking, Payment, AccountTransaction, Account

with app.app_context():
    # Prepare
    client = Client.query.filter_by(code='TEST-REFUND-1').first()
    if not client:
        client = Client(code='TEST-REFUND-1', name='Test Refund Client', opening_balance=0)
        db.session.add(client); db.session.flush()
    acc = Account.query.filter_by(name='Test Cash Account').first()
    if not acc:
        acc = Account(name='Test Cash Account', category='Cash', account_type='cash', balance=100000.0)
        db.session.add(acc); db.session.flush()

    # Create booking causing client credit
    bk = Booking(client_name=client.name, amount=0.0, paid_amount=11788.0, date_posted=pk_now())
    db.session.add(bk); db.session.commit()

    before_balance = _client_balance_as_of(client)
    payments_before = [(p.id, p.amount, p.auto_bill_no) for p in Payment.query.filter(Payment.client_name==client.name).all()]
    tx_before = [(t.id, t.amount, t.description, t.note) for t in AccountTransaction.query.filter(AccountTransaction.description.ilike(f'%{client.name}%')).all()]

    print('BEFORE: balance=', before_balance)
    print('BEFORE payments:', payments_before)
    print('BEFORE txs (matching desc):', tx_before)

    # Post refund
    refund_amount = 11788.0
    payment = Payment(client_name=client.name, amount=-refund_amount, method='Refund', note='Quick refund', date_posted=pk_now(), account_name=acc.name, payment_account_id=acc.id, auto_bill_no=None)
    db.session.add(payment); db.session.flush()
    tx = AccountTransaction(from_account_id=acc.id, to_account_id=None, amount=refund_amount, description=f'Client refund to {client.name}', note=f'[SRC:ClientRefund:{payment.id}]', transaction_type='Payment', date_posted=pk_now())
    db.session.add(tx); db.session.commit()

    after_balance = _client_balance_as_of(client)
    payments_after = [(p.id, p.amount, p.auto_bill_no) for p in Payment.query.filter(Payment.client_name==client.name).all()]
    tx_after = [(t.id, t.amount, t.description, t.note) for t in AccountTransaction.query.filter(AccountTransaction.description.ilike(f'%{client.name}%')).all()]
    neg_sbcp = [(p.id,p.amount,p.auto_bill_no) for p in Payment.query.filter(Payment.auto_bill_no!=None, Payment.auto_bill_no.ilike('SB-CP-%'), Payment.amount < 0).all()]

    print('AFTER: balance=', after_balance)
    print('AFTER payments:', payments_after)
    print('AFTER txs (matching desc):', tx_after)
    print('NEGATIVE SB-CP payments found:', neg_sbcp)
