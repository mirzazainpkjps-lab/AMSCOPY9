"""Refund flow full verification.

Usage:
    AMS_TEST_DB=/tmp/ams_test.db python tools/tests_isolated/verify_refund_full.py
"""
import os,sys,time
from datetime import datetime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from tools.tests_isolated.test_isolation_guard import require_test_db
require_test_db()

from pprint import pprint
from main import app, db, pk_now, _client_balance_as_of
from sqlalchemy import or_
from models import Client, Entry, PendingBill, Booking, Payment, AccountTransaction, Account

now = datetime.now().strftime('%Y%m%d%H%M%S')
client_code = f'TEST-REFUND-FULL-{now}'

with app.app_context():
    print('RUN AT:', datetime.now().isoformat())
    # create client
    client = Client.query.filter_by(code=client_code).first()
    if client:
        print('Reusing existing test client', client_code)
    else:
        client = Client(code=client_code, name=f'Test Refund Full {now}', opening_balance=0)
        db.session.add(client); db.session.flush()
        print('Created client', client.id, client.code, client.name)

    # create account
    acc_name = f'Test Cash Account Full {now}'
    acc = Account.query.filter_by(name=acc_name).first()
    if not acc:
        acc = Account(name=acc_name, category='Cash', account_type='cash', balance=100000.0)
        db.session.add(acc); db.session.flush()
        print('Created account', acc.id, acc.name)

    # ensure no existing payments
    payments_existing = Payment.query.filter(Payment.client_name==client.name).all()
    if payments_existing:
        print('Existing payments found for client (will print):')
        for p in payments_existing:
            pprint({
                'id':p.id,'client_name':p.client_name,'amount':p.amount,'method':p.method,'auto_bill_no':p.auto_bill_no,'date_posted':str(p.date_posted),'note':p.note
            })
    else:
        print('No existing payments for client')

    # STEP 1: create booking cancellation effect: booking with paid_amount -> creates client credit
    cancel_amount = 11788.0
    bk = Booking(client_name=client.name, amount=0.0, paid_amount=cancel_amount, date_posted=pk_now())
    db.session.add(bk); db.session.commit()

    # Print client ledger rows BEFORE refund: Entry rows and Payment rows
    print('\nCLIENT LEDGER ROWS BEFORE REFUND:')
    entries = Entry.query.filter(or_(Entry.client_code==client.code, Entry.client==client.name)).order_by(Entry.created_at.asc()).all()
    print('Entry rows count:', len(entries))
    for e in entries:
        pprint({k:getattr(e,k) for k in ['id','date','time','type','material','client','client_code','qty','bill_no','nimbus_no','created_by','note','is_void']})

    payments_before = Payment.query.filter(Payment.client_name==client.name).order_by(Payment.date_posted.asc()).all()
    print('\nPayment rows BEFORE refund count:', len(payments_before))
    for p in payments_before:
        pprint({k:getattr(p,k) for k in ['id','client_name','amount','method','auto_bill_no','manual_bill_no','date_posted','note','payment_account_id']})

    # Pending amount BEFORE
    pending_before = _client_balance_as_of(client)
    print('\nPENDING BEFORE:', pending_before)

    # Account balance BEFORE
    print('\nACCOUNT BALANCE BEFORE:', {'id':acc.id,'name':acc.name,'balance':acc.balance})

    # STEP 2: Post refund (negative Payment + AccountTransaction)
    refund_amount = cancel_amount
    payment = Payment(client_name=client.name, amount=-refund_amount, method='Refund', note='Full verification refund', date_posted=pk_now(), account_name=acc.name, payment_account_id=acc.id, auto_bill_no=None)
    db.session.add(payment); db.session.flush()
    pay_marker = f'[SRC:ClientRefund:{payment.id}]'
    tx = AccountTransaction(from_account_id=acc.id, to_account_id=None, amount=refund_amount, description=f'Client refund to {client.name}', note=f'{payment.note} {pay_marker}', transaction_type='Payment', date_posted=pk_now())
    db.session.add(tx); db.session.commit()

    time.sleep(0.2)

    # Print exact refund transaction row
    print('\nREFUND PAYMENT ROW:')
    p = db.session.get(Payment, payment.id)
    pprint({k:getattr(p,k) for k in ['id','client_name','amount','method','auto_bill_no','date_posted','note','payment_account_id']})

    print('\nREFUND ACCOUNTTRANSACTION ROW:')
    tx_row = db.session.query(AccountTransaction).filter(AccountTransaction.note.ilike(f'%ClientRefund%'), AccountTransaction.amount==refund_amount).order_by(AccountTransaction.id.desc()).first()
    pprint({k:getattr(tx_row,k) for k in ['id','from_account_id','to_account_id','amount','description','note','transaction_type','date_posted']})

    # Rebuild pending bills to reflect new payment
    try:
        from main import rebuild_pending_bills
        rebuild_pending_bills(client_id=client.id)
    except Exception as e:
        print('rebuild_pending_bills failed:', e)

    # Print client ledger rows AFTER refund
    print('\nCLIENT LEDGER ROWS AFTER REFUND:')
    entries_after = Entry.query.filter(or_(Entry.client_code==client.code, Entry.client==client.name)).order_by(Entry.created_at.asc()).all()
    print('Entry rows count:', len(entries_after))
    for e in entries_after:
        pprint({k:getattr(e,k) for k in ['id','date','time','type','material','client','client_code','qty','bill_no','nimbus_no','created_by','note','is_void']})

    payments_after = Payment.query.filter(Payment.client_name==client.name).order_by(Payment.date_posted.asc()).all()
    print('\nPayment rows AFTER refund count:', len(payments_after))
    for p in payments_after:
        pprint({k:getattr(p,k) for k in ['id','client_name','amount','method','auto_bill_no','manual_bill_no','date_posted','note','payment_account_id']})

    # Pending amount AFTER
    pending_after = _client_balance_as_of(client)
    print('\nPENDING AFTER:', pending_after)

    # Account balance AFTER (account object may not auto-update balance since we didn't change it)
    acc_refreshed = db.session.get(Account, acc.id)
    print('\nACCOUNT BALANCE AFTER:', {'id':acc_refreshed.id,'name':acc_refreshed.name,'balance':acc_refreshed.balance})

    # Cash flow rows AFTER refund: list AccountTransaction rows for this account and payments with method='cash'
    print('\nCASH FLOW: AccountTransaction rows for this account (last 20):')
    txs = AccountTransaction.query.filter((AccountTransaction.from_account_id==acc.id)|(AccountTransaction.to_account_id==acc.id)).order_by(AccountTransaction.date_posted.desc()).limit(20).all()
    for t in txs:
        pprint({k:getattr(t,k) for k in ['id','from_account_id','to_account_id','amount','description','note','transaction_type','date_posted']})

    cash_payments = Payment.query.filter(Payment.method.ilike('cash')).order_by(Payment.date_posted.desc()).limit(20).all()
    print('\nCASH Payments (method cash) recent 20:')
    for cp in cash_payments:
        pprint({k:getattr(cp,k) for k in ['id','client_name','amount','method','auto_bill_no','date_posted','note']})

    # Exact ledger settlement row (the Payment negative we created)
    print('\nLEDGER SETTLEMENT ROW (the payment):')
    pprint({k:getattr(payment,k) for k in ['id','client_name','amount','method','auto_bill_no','date_posted','note','payment_account_id']})

    # Search DB: Payment.amount < 0 (print all)
    neg_payments = Payment.query.filter(Payment.amount < 0).order_by(Payment.date_posted.asc()).all()
    print('\nALL Payments with amount < 0 (count):', len(neg_payments))
    for np in neg_payments:
        pprint({
            'id':np.id,'client_name':np.client_name,'amount':np.amount,'auto_bill_no':np.auto_bill_no,'date_posted':str(np.date_posted),'note':np.note
        })

    # Part B: Audit corrupted negative SB-CP rows
    print('\nAUDIT: corrupted negative SB-CP payments')
    corrupted = Payment.query.filter(Payment.amount < 0, Payment.auto_bill_no!=None, Payment.auto_bill_no.ilike('SB-CP-%')).order_by(Payment.date_posted.asc()).all()
    print('Count corrupted SB-CP negatives:', len(corrupted))
    for cp in corrupted:
        linked_tx = AccountTransaction.query.filter(
            (AccountTransaction.note.ilike(f'%{cp.auto_bill_no}%')) | (AccountTransaction.note.ilike(f'%{cp.id}%')) | (AccountTransaction.description.ilike(f'%{cp.client_name}%'))
        ).all()
        pprint({
            'id':cp.id,'auto_bill_no':cp.auto_bill_no,'amount':cp.amount,'client':cp.client_name,'date_posted':str(cp.date_posted),'note':cp.note,'linked_tx_count':len(linked_tx)
        })
        for lt in linked_tx:
            pprint({'tx_id':lt.id,'amount':lt.amount,'description':lt.description,'note':lt.note,'date_posted':str(lt.date_posted)})

    print('\nDone.')
