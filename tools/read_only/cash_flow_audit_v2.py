import os
import sys
from datetime import datetime, timedelta, date

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
    CashFlowReconciliationAudit,
    FbmCashDrawerEntry,
)
from sqlalchemy import func, or_


def cash_method_clauses():
    """Filter clauses for cash payments."""
    return [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]


def calculate_calculated_closing(day):
    """Compute the calculated closing balance for a day (without any reconciliation adjustment)."""
    from_date = day
    to_date = day
    
    # Cash in
    cash_in = 0.0
    
    payments = Payment.query.filter(
        Payment.is_void == False,
        or_(*cash_method_clauses()),
        func.date(Payment.date_posted) >= from_date,
        func.date(Payment.date_posted) <= to_date,
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
        func.date(DirectSale.date_posted) >= from_date,
        func.date(DirectSale.date_posted) <= to_date,
    ).all()
    cash_in += sum(float(s.paid_amount or 0) for s in sales)

    # Cash out
    cash_out = 0.0
    supplier_payments = SupplierPayment.query.filter(
        SupplierPayment.is_void == False,
        func.date(SupplierPayment.date_posted) >= from_date,
        func.date(SupplierPayment.date_posted) <= to_date,
    ).all()
    cash_out += sum(float(sp.amount or 0) for sp in supplier_payments)

    # FBM drawer transfers
    fbm_account = (
        Account.query.filter(func.lower(func.trim(Account.name)) == 'fbm drawer cash').first()
        or Account.query.filter(Account.name.ilike('%fbm drawer cash%')).first()
    )
    fbm_id = fbm_account.id if fbm_account else None

    txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Transfer']),
        func.date(AccountTransaction.date_posted) >= from_date,
        func.date(AccountTransaction.date_posted) <= to_date,
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

    opening = opening_balance_for_day(day)
    closing = opening + cash_in - cash_out
    
    return {'opening': opening, 'cash_in': cash_in, 'cash_out': cash_out, 'closing': closing}


def opening_balance_for_day(from_date):
    """
    Calculate opening balance for a date using NEW RECONCILIATION LOGIC.
    
    CRITICAL: Opening balance = Physical Cash Available from previous day (if reconciliation exists)
    
    LOGIC:
    1. If previous day has physical_cash_available reconciliation → use that
    2. Else if previous day has legacy adjustment → use calculated_closing - difference
    3. Else → use calculated_closing
    """
    prev_day = from_date - timedelta(days=1)
    
    # Check if there's a reconciliation for previous day
    prev_reconciliation = CashFlowDifferenceAdjustment.query.filter_by(
        adjustment_date=prev_day
    ).first()
    
    if prev_reconciliation:
        # Use the get_opening_for_next_day method which handles both workflows
        prev_calc_closing = calculate_calculated_closing(prev_day)['closing']
        return prev_reconciliation.get_opening_for_next_day(prev_calc_closing)
    
    # No reconciliation for previous day: compute calculated closing before this date
    cash_method_clauses_local = [
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash',
        func.lower(func.trim(func.coalesce(Payment.method, ''))) == 'cash sale',
    ]
    prev_pay_in = float(
        Payment.query.filter(
            Payment.is_void == False,
            or_(*cash_method_clauses_local),
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
    
    # Include all FBM and account transfers/expenses from before this date
    fbm_account = (
        Account.query.filter(func.lower(func.trim(Account.name)) == 'fbm drawer cash').first()
        or Account.query.filter(Account.name.ilike('%fbm drawer cash%')).first()
    )
    fbm_id = fbm_account.id if fbm_account else None
    
    prev_tx_out = 0.0
    txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Transfer']),
        func.date(AccountTransaction.date_posted) < from_date,
    ).all()
    for tx in txs:
        if tx.transaction_type == 'Transfer' and fbm_id is not None:
            if tx.to_account_id == fbm_id and tx.from_account_id != fbm_id:
                prev_pay_in += float(tx.amount or 0)
                continue
            if tx.from_account_id == fbm_id and tx.to_account_id != fbm_id:
                prev_sup_out += float(tx.amount or 0)
                continue
        if tx.transaction_type in ['Expense', 'Payment'] and tx.from_account_id is not None:
            acc = Account.query.get(tx.from_account_id)
            if acc and (acc.category or '').lower() == 'cash':
                prev_sup_out += float(tx.amount or 0)
    
    return prev_pay_in + prev_sale_in - prev_sup_out


def gather_dates():
    """Collect all dates with relevant transactions."""
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
    """Audit cash flow with new reconciliation logic."""
    with app.app_context():
        dates = gather_dates()
        if not dates:
            print('No relevant dates found.')
            return
        print('Earliest date:', dates[0], 'Latest date:', dates[-1], 'Total dates:', len(dates))

        mismatches = []
        for i, d in enumerate(dates):
            calc = calculate_calculated_closing(d)
            reconciliation = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=d).first()
            
            if reconciliation and reconciliation.physical_cash_available is not None:
                # NEW WORKFLOW RECONCILIATION
                physical = reconciliation.physical_cash_available
                difference = calc['closing'] - physical
                next_opening = physical
            elif reconciliation and reconciliation.amount is not None:
                # LEGACY WORKFLOW
                difference = reconciliation.amount
                next_opening = calc['closing'] - difference
            else:
                # NO RECONCILIATION
                difference = 0.0
                next_opening = calc['closing']
            
            # Check if opening of next day matches
            if i < len(dates) - 1:
                next_d = dates[i + 1]
                next_calc = calculate_calculated_closing(next_d)
                if abs(next_calc['opening'] - next_opening) > 0.01:
                    mismatches.append({
                        'date': d,
                        'next_date': next_d,
                        'expected_opening': next_opening,
                        'actual_opening': next_calc['opening'],
                        'reconciliation': reconciliation,
                    })

        print(f'\nMismatch count: {len(mismatches)}')
        for m in mismatches[:10]:
            rec = m['reconciliation']
            print(f"  {m['date']} → {m['next_date']}: expected_opening={m['expected_opening']:.2f}, actual={m['actual_opening']:.2f}, rec={'YES' if rec else 'NO'}")


if __name__ == '__main__':
    main()
