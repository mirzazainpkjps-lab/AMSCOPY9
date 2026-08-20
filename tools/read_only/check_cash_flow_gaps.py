import os
import sys
from datetime import date
sys.path.insert(0, os.getcwd())
from main import app
from models import Payment, DirectSale, SupplierPayment, AccountTransaction, CashFlowDifferenceAdjustment, Account
from sqlalchemy import func, or_

def count_dates(start, end):
    with app.app_context():
        print('Range', start, 'to', end)
        for model, name in [
            (Payment, 'Payment'),
            (DirectSale, 'DirectSale'),
            (SupplierPayment, 'SupplierPayment'),
            (AccountTransaction, 'AccountTransaction'),
            (CashFlowDifferenceAdjustment, 'CashFlowDifferenceAdjustment'),
        ]:
            c = model.query.filter(func.date(model.date_posted) >= start, func.date(model.date_posted) <= end).count() if hasattr(model, 'date_posted') else model.query.filter(model.adjustment_date >= start, model.adjustment_date <= end).count()
            print(name, c)

        txs = AccountTransaction.query.filter(func.date(AccountTransaction.date_posted) > start, func.date(AccountTransaction.date_posted) < end).all()
        print('Detailed tx dates:', sorted({t.date_posted.date() for t in txs}))
        for t in txs:
            print(t.id, t.transaction_type, t.amount, t.from_account_id, t.to_account_id, t.date_posted)

if __name__ == '__main__':
    count_dates(date(2026,5,24), date(2026,5,29))
