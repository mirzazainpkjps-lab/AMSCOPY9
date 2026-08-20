import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.getcwd())

from main import app
from models import (
    db,
    Account,
    AccountTransaction,
    Payment,
    DirectSale,
    SupplierPayment,
    CashFlowDifferenceAdjustment,
    FbmCashDrawerEntry,
)
from sqlalchemy import func, or_


def cash_method_clauses():
    return [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]


def opening_balance(from_date):
    prev_pay_in = float(
        Payment.query.filter(
            Payment.is_void == False,
            or_(*cash_method_clauses()),
            func.date(Payment.date_posted) < from_date,
        )
        .with_entities(func.sum(Payment.amount))
        .scalar()
        or 0
    )
    prev_sale_in = float(
        DirectSale.query.filter(
            DirectSale.is_void == False,
            or_(
                func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash',
                func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash sale',
                func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash',
                func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash sale',
            ),
            DirectSale.paid_amount > 0,
            func.date(DirectSale.date_posted) < from_date,
        )
        .with_entities(func.sum(DirectSale.paid_amount))
        .scalar()
        or 0
    )
    prev_sup_out = float(
        SupplierPayment.query.filter(
            SupplierPayment.is_void == False,
            func.date(SupplierPayment.date_posted) < from_date,
        )
        .with_entities(func.sum(SupplierPayment.amount))
        .scalar()
        or 0
    )
    adj_total = float(
        CashFlowDifferenceAdjustment.query.with_entities(
            func.coalesce(func.sum(CashFlowDifferenceAdjustment.amount), 0)
        )
        .filter(CashFlowDifferenceAdjustment.adjustment_date < from_date)
        .scalar()
        or 0
    )
    return prev_pay_in + prev_sale_in - prev_sup_out - adj_total


def adjustment_amount(adjustment_date):
    adj = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=adjustment_date).first()
    return float(adj.amount or 0) if adj else 0.0


def cash_flow_day(day):
    cash_in = 0.0
    cash_out = 0.0

    payments = Payment.query.filter(
        Payment.is_void == False,
        or_(*cash_method_clauses()),
        func.date(Payment.date_posted) >= day,
        func.date(Payment.date_posted) <= day,
    ).all()
    cash_in += sum(float(p.amount or 0) for p in payments)

    sales = DirectSale.query.filter(
        DirectSale.is_void == False,
        or_(
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.category, ''))) == 'cash sale',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash',
            func.lower(func.trim(func.coalesce(DirectSale.payment_method, ''))) == 'cash sale',
        ),
        DirectSale.paid_amount > 0,
        func.date(DirectSale.date_posted) >= day,
        func.date(DirectSale.date_posted) <= day,
    ).all()
    cash_in += sum(float(s.paid_amount or 0) for s in sales)

    supplier_payments = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) >= day,
        func.date(SupplierPayment.date_posted) <= day,
    ).all()
    cash_out += sum(float(sp.amount or 0) for sp in supplier_payments)

    fbm_account = (
        Account.query.filter(func.lower(func.trim(Account.name)) == 'fbm drawer cash').first()
        or Account.query.filter(Account.name.ilike('%fbm drawer cash%')).first()
    )
    fbm_id = fbm_account.id if fbm_account else None

    txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Transfer']),
        func.date(AccountTransaction.date_posted) >= day,
        func.date(AccountTransaction.date_posted) <= day,
    ).all()
    for tx in txs:
        if tx.transaction_type == 'Transfer' and fbm_id is not None:
            if tx.to_account_id == fbm_id and tx.from_account_id != fbm_id:
                cash_in += float(tx.amount or 0)
                continue
            if tx.from_account_id == fbm_id and tx.to_account_id != fbm_id:
                cash_out += float(tx.amount or 0)
                continue
        if tx.transaction_type in ['Expense', 'Payment'] and tx.from_account_id is not None:
            acc = Account.query.get(tx.from_account_id)
            if acc and (acc.category or '').lower() == 'cash':
                cash_out += float(tx.amount or 0)

    opening = opening_balance(day)
    closing = opening + cash_in - cash_out
    adj = adjustment_amount(day)
    return {'opening': opening, 'cash_in': cash_in, 'cash_out': cash_out, 'closing': closing, 'adj': adj, 'adjusted_closing': closing - adj}


def gather_dates():
    dates = set()
    mapping = [
        (Payment, Payment.date_posted),
        (DirectSale, DirectSale.date_posted),
        (SupplierPayment, SupplierPayment.date_posted),
        (AccountTransaction, AccountTransaction.date_posted),
        (CashFlowDifferenceAdjustment, CashFlowDifferenceAdjustment.adjustment_date),
        (FbmCashDrawerEntry, FbmCashDrawerEntry.date_posted),
    ]
    for model, col in mapping:
        for row in model.query.with_entities(col).filter(col != None).all():
            dt = row[0]
            if dt is None:
                continue
            dates.add(dt.date() if hasattr(dt, 'date') else dt)
    return sorted(dates)


def main():
    with app.app_context():
        dates = gather_dates()
        if not dates:
            print('No relevant dates found.')
            return
        print('Earliest date:', dates[0], 'Latest date:', dates[-1], 'Total dates:', len(dates))

        day_map = {d: cash_flow_day(d) for d in dates}
        mismatches = []
        prev = None
        for d in dates:
            if prev is not None:
                expected_open = day_map[prev]['adjusted_closing']
                actual_open = day_map[d]['opening']
                if abs(actual_open - expected_open) > 0.01:
                    mismatches.append((d, actual_open, expected_open, day_map[prev], day_map[d]))
            prev = d

        print('Mismatch count:', len(mismatches))
        for d, actual, expected, prev_day, cur_day in mismatches[:50]:
            print(f'MISMATCH {d}: opening={actual:.2f}, expected_prev_adj_closing={expected:.2f}, prev_closing={prev_day["closing"]:.2f}, prev_adj={prev_day["adjusted_closing"]:.2f}, prev_adj_amount={prev_day["adj"]:.2f}, in={cur_day["cash_in"]:.2f}, out={cur_day["cash_out"]:.2f}')

        fbm_cnt = FbmCashDrawerEntry.query.filter(FbmCashDrawerEntry.is_void == False).count()
        fbm_out_cnt = FbmCashDrawerEntry.query.filter(FbmCashDrawerEntry.is_void == False, FbmCashDrawerEntry.entry_type == 'out').count()
        print('FbmCashDrawerEntry total:', fbm_cnt, 'cash-out entries:', fbm_out_cnt)

        tx_count = AccountTransaction.query.filter(AccountTransaction.is_void == False).count()
        print('AccountTransaction count (non-void):', tx_count)

        linked_payments = AccountTransaction.query.filter(AccountTransaction.note.like('%[SRC:Payment:%')).count()
        print('Linked Payment tx count:', linked_payments)
        linked_supplier = AccountTransaction.query.filter(AccountTransaction.note.like('%[SRC:SupplierPayment:%')).count()
        print('Linked SupplierPayment tx count:', linked_supplier)


if __name__ == '__main__':
    main()
