import os
import sys
from datetime import date
sys.path.insert(0, os.getcwd())
from main import app
from models import AccountTransaction, Account
from sqlalchemy import func

with app.app_context():
    txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        func.date(AccountTransaction.date_posted) == date(2026,5,26),
        AccountTransaction.transaction_type.in_(['Expense','Payment','Transfer']),
    ).all()
    for tx in txs:
        acc = Account.query.get(tx.from_account_id) if tx.from_account_id else None
        print(tx.id, tx.transaction_type, tx.amount, tx.from_account_id, acc.category if acc else None, tx.to_account_id, tx.description, tx.note, tx.date_posted)
