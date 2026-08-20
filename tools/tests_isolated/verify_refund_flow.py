"""Refund flow verification.

Usage:
    AMS_TEST_DB=/tmp/ams_test.db python tools/tests_isolated/verify_refund_flow.py
"""
import json
from decimal import Decimal
from pprint import pprint
from datetime import datetime

import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from tools.tests_isolated.test_isolation_guard import require_test_db
require_test_db()

from main import app, db, pk_now, rebuild_pending_bills, _client_balance_as_of
from models import Client, Booking, Payment, AccountTransaction, Account


def ensure_clean_client(code='TEST-REFUND-1'):
    c = Client.query.filter_by(code=code).first()
    if c:
        return c
    c = Client(code=code, name='Test Refund Client', opening_balance=0)
    db.session.add(c)
    db.session.flush()
    return c


def print_rows(title, rows):
    print('\n' + '='*20 + ' ' + title + ' ' + '='*20)
    for r in rows:
        pprint({k: (getattr(r,k) if not isinstance(getattr(r,k), datetime) else getattr(r,k).isoformat()) for k in r.__dict__ if not k.startswith('_')})


with app.app_context():
    # Setup: create client, account
    client = ensure_clean_client()

    # create or get cash account
    acc = Account.query.filter_by(name='Test Cash Account').first()
    if not acc:
        acc = Account(name='Test Cash Account', category='Cash', account_type='cash', balance=100000.0)
        db.session.add(acc)
        db.session.flush()

    # STEP: create booking that results in client credit (-11788)
    # Create booking with amount=0 and paid_amount=11788 to simulate advance/credit from cancellations
    bk = Booking(client_name=client.name, amount=0.0, paid_amount=11788.0, date_posted=pk_now())
    db.session.add(bk)
    db.session.flush()

    db.session.commit()

    before_balance = _client_balance_as_of(client)

    print('\nINITIAL STATE:')
    print('Client balance (before refund):', before_balance)

    payments_before = Payment.query.filter(Payment.client_name==client.name).all()
    print_rows('Payments BEFORE', payments_before)
    tx_before = AccountTransaction.query.filter(AccountTransaction.note.ilike('%ClientRefund%') | AccountTransaction.description.ilike(f'%{client.name}%')).all()
    print_rows('AccountTransactions BEFORE (refund-related)', tx_before)

    # Now perform refund using the same logic as add_transaction(): create negative Payment and AccountTransaction
    refund_amount = 11788.0

    payment = Payment(
        client_name=client.name,
        amount=-float(refund_amount),
        method='Refund',
        note='Automated test refund',
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

    pay_marker = f"[SRC:ClientRefund:{payment.id}]"

    tx = AccountTransaction(
        from_account_id=acc.id,
        to_account_id=None,
        amount=refund_amount,
        description=f'Client refund to {client.name}',
        note=" ".join([x for x in [payment.note, pay_marker] if x]).strip(),
        transaction_type='Payment',
        date_posted=pk_now()
    )
    db.session.add(tx)
    db.session.commit()

    print('\nAFTER POSTING REFUND:')
    payments_after = Payment.query.filter(Payment.client_name==client.name).all()
    print_rows('Payments AFTER', payments_after)

    tx_after = AccountTransaction.query.filter(AccountTransaction.note.ilike('%ClientRefund%') | AccountTransaction.description.ilike(f'%{client.name}%')).all()
    print_rows('AccountTransactions AFTER (refund-related)', tx_after)

    after_balance = _client_balance_as_of(client)
    print('\nClient balance (after refund):', after_balance)

    # Check pending bills for this client
    pbs = db.session.query(Booking).filter(Booking.client_name==client.name).all()
    print_rows('Bookings for client', pbs)

    # Cash flow rows: payments with cash method, and account transactions
    cash_payments = Payment.query.filter(Payment.method.ilike('cash')).all()
    print_rows('Cash Payments (method cash)', cash_payments)
    cash_out_txs = AccountTransaction.query.filter(AccountTransaction.from_account_id==acc.id).all()
    print_rows('AccountTransactions from test cash account', cash_out_txs)

    # Check for any negative SB-CP Payments
    neg_sbcp = Payment.query.filter(Payment.auto_bill_no!=None, Payment.auto_bill_no.ilike('SB-CP-%'), Payment.amount < 0).all()
    print_rows('Negative SB-CP Payments', neg_sbcp)

    # Final assertions printed
    print('\nVERIFICATION:')
    print('Number of payment rows for client:', len(payments_after))
    print('Presence of negative Payment for client:', any((p.amount or 0) < 0 for p in payments_after))
    print('Any negative SB-CP payments found:', len(neg_sbcp) > 0)

    print('\nDone.')
