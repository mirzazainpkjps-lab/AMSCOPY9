import os
import sys
from datetime import date

sys.path.insert(0, os.getcwd())

from main import app
from models import Payment, DirectSale, SupplierPayment, AccountTransaction, Account, CashFlowDifferenceAdjustment
from sqlalchemy import func, or_


def report_day(day):
    print('===', day, '===')
    pay = Payment.query.filter(
        Payment.is_void == False,
        func.date(Payment.date_posted) == day,
    ).all()
    print('Payments', len(pay), sum(float(p.amount or 0) for p in pay))
    for p in pay:
        if float(p.amount or 0) != 0:
            print(' P', p.id, p.amount, p.method, p.date_posted)
    sales = DirectSale.query.filter(
        DirectSale.is_void == False,
        func.date(DirectSale.date_posted) == day,
    ).all()
    print('Sales', len(sales), sum(float(s.paid_amount or 0) for s in sales))
    for s in sales:
        if float(s.paid_amount or 0) != 0:
            print(' S', s.id, s.paid_amount, s.category, s.payment_method, s.date_posted)
    sup = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) == day,
    ).all()
    print('Supplier', len(sup), sum(float(s.amount or 0) for s in sup))
    for s in sup:
        print(' SP', s.id, s.amount, s.method, s.date_posted)
    txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        func.date(AccountTransaction.date_posted) == day,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Transfer']),
    ).all()
    print('TXs', len(txs), sum(float(t.amount or 0) for t in txs))
    for t in txs:
        print(' TX', t.id, t.transaction_type, t.amount, t.from_account_id, t.to_account_id, t.description, t.note, t.date_posted)


def sum_before(day):
    prev_pay = float(Payment.query.filter(
        Payment.is_void == False,
        func.date(Payment.date_posted) < day,
    ).with_entities(func.sum(Payment.amount)).scalar() or 0)
    prev_sales = float(DirectSale.query.filter(
        DirectSale.is_void == False,
        DirectSale.paid_amount > 0,
        func.date(DirectSale.date_posted) < day,
    ).with_entities(func.sum(DirectSale.paid_amount)).scalar() or 0)
    prev_sup = float(SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) < day,
    ).with_entities(func.sum(SupplierPayment.amount)).scalar() or 0)
    prev_adj = float(CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.adjustment_date < day,
    ).with_entities(func.sum(CashFlowDifferenceAdjustment.amount)).scalar() or 0)
    print('Before', day, 'pay', prev_pay, 'sales', prev_sales, 'sup', prev_sup, 'adj', prev_adj, 'net', prev_pay + prev_sales - prev_sup - prev_adj)


def run():
    with app.app_context():
        for d in [date(2026,5,24), date(2026,5,29), date(2026,5,30)]:
            report_day(d)
        print('--- totals ---')
        sum_before(date(2026,5,24))
        sum_before(date(2026,5,29))
        sum_before(date(2026,5,30))


if __name__ == '__main__':
    run()
