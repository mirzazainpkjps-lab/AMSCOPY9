"""Print detailed cash-flow source rows for a fixed set of dates.

This is a read-only diagnostic companion to ``cash_flow_audit.py``.  Run it
from the repository root, optionally overriding ``APP_DB_PATH`` in the normal
way used by the application.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, or_

from main import app
from models import AccountTransaction, DirectSale, Payment, SupplierPayment
from tools.read_only import cash_flow_audit as cash_audit


def print_day_detail(day: date) -> None:
    print("DATE", day)
    print("CASH_FLOW", cash_audit.cash_flow_day(day))

    payments = Payment.query.filter(
        Payment.is_void == False,
        func.date(Payment.date_posted) == day,
        or_(
            func.lower(func.trim(func.coalesce(Payment.method, ""))) == "cash",
            func.lower(func.trim(func.coalesce(Payment.method, ""))) == "cash sale",
        ),
    ).all()
    print("PAYMENTS", [(p.id, p.amount, p.method, p.date_posted) for p in payments])
    print("PAYMENT_SUM", sum(float(p.amount or 0) for p in payments))
    print(
        "PAYMENTS_GT0",
        [(p.id, p.amount, p.method, p.date_posted) for p in payments if float(p.amount or 0) > 0],
    )

    sales = DirectSale.query.filter(
        DirectSale.is_void == False,
        func.date(DirectSale.date_posted) == day,
        or_(
            func.lower(func.trim(func.coalesce(DirectSale.category, ""))) == "cash",
            func.lower(func.trim(func.coalesce(DirectSale.category, ""))) == "cash sale",
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ""))) == "cash",
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ""))) == "cash sale",
        ),
    ).all()
    print(
        "SALES",
        [(s.id, s.paid_amount, s.category, s.payment_method, s.date_posted) for s in sales],
    )
    print(
        "SALES_GT0",
        [(s.id, s.paid_amount, s.category, s.payment_method, s.date_posted) for s in sales if float(s.paid_amount or 0) > 0],
    )
    print("SALES_SUM", sum(float(s.paid_amount or 0) for s in sales))

    supplier_payments = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) == day,
    ).all()
    print("SUPPLIER", [(p.id, p.amount, p.method, p.date_posted) for p in supplier_payments])

    transactions = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        func.date(AccountTransaction.date_posted) == day,
        AccountTransaction.transaction_type.in_(["Expense", "Payment", "Transfer"]),
    ).all()
    print(
        "TXS",
        [
            (tx.id, tx.transaction_type, tx.amount, tx.from_account_id,
             tx.to_account_id, tx.description, tx.note)
            for tx in transactions
        ],
    )
    print("---")


def main() -> None:
    with app.app_context():
        for day in [
            date(2026, 5, 20),
            date(2026, 5, 21),
            date(2026, 5, 23),
            date(2026, 5, 24),
            date(2026, 5, 29),
            date(2026, 5, 30),
        ]:
            print_day_detail(day)


if __name__ == "__main__":
    main()
