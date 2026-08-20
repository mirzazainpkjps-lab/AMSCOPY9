"""Cash Flow engine: configuration-driven received / spent / transfer."""
import os
import tempfile
import uuid
from datetime import date

os.environ.setdefault('ALLOW_EMPTY_DB', '1')
os.environ.setdefault('ALLOW_DB_DROP', '1')
os.environ.setdefault(
    'APP_DB_PATH',
    os.path.join(tempfile.gettempdir(), 'ams_cash_flow_engine_test.db'),
)

from app import create_app
from models import (
    db, Account, AccountTransaction, CashFlowCategory, CashFlowEntry,
    CashFlowSubcategory, DirectSale,
)
from app.services.cash_flow_svc import (
    apply_running_balance,
    categories_for_direction,
    collect_cash_flow_rows,
    disable_cf_category,
    filter_cash_flow_rows,
    save_cf_category,
    save_cf_subcategory,
    save_manual_cash_flow_entry,
    subcategories_for_category,
    summarize_cash_flow_rows,
    update_manual_cash_flow_entry,
    void_manual_cash_flow_entry,
)
from app.services.time_money import pk_now

app = create_app({'TESTING': True})


def _account(name, balance=100000, category='cash'):
    acc = Account.query.filter_by(name=name).first()
    if acc:
        acc.balance = balance
        acc.category = category
        acc.account_type = 'company'
        acc.is_active = True
        return acc
    acc = Account(name=name, category=category, account_type='company', type='company', balance=balance, is_active=True)
    db.session.add(acc)
    db.session.flush()
    return acc


def test_cash_flow_engine_rules():
    with app.app_context():
        db.drop_all()
        db.create_all()
        cash = _account('ENGINE CASH', 100000)
        bank = _account('ENGINE BANK', 20000, category='bank')
        db.session.commit()

        cat, _ = save_cf_category('Workshop', 'out')
        save_cf_subcategory(cat.id, 'Parts')
        db.session.commit()

        # Received-only category must not be usable on spent.
        save_cf_category('Incoming Only', 'in')
        db.session.commit()
        try:
            save_manual_cash_flow_entry(
                direction='out', amount=10, account_id=cash.id,
                category_name='Incoming Only', create_missing=False,
            )
            assert False, 'spent + received-only category should fail'
        except ValueError:
            db.session.rollback()

        # Wrong subcategory parent is rejected.
        other, _ = save_cf_category('Other Box', 'both')
        save_cf_subcategory(other.id, 'Alien Sub')
        db.session.commit()
        alien = CashFlowSubcategory.query.filter_by(name='Alien Sub').first()
        try:
            save_manual_cash_flow_entry(
                direction='out', amount=10, account_id=cash.id,
                category_id=cat.id, subcategory_id=alien.id, create_missing=False,
            )
            assert False, 'foreign subcategory should fail'
        except ValueError:
            db.session.rollback()

        # Spent updates account and appears as spent, not income.
        spend_key = 'engine-spend-' + uuid.uuid4().hex
        spent, created = save_manual_cash_flow_entry(
            direction='out', amount=25000, account_id=cash.id,
            category_name='Workshop', subcategory_name='Parts',
            party_name='Adnan', note='Diesel truck #12',
            reference='V-1', description='Workshop bill',
            idempotency_key=spend_key,
        )
        db.session.commit()
        assert created
        db.session.refresh(cash)
        assert cash.balance == 75000
        assert spent.direction == 'out'
        assert spent.category.name == 'Workshop'
        assert spent.subcategory.name == 'Parts'

        # Idempotent retry.
        again, created2 = save_manual_cash_flow_entry(
            direction='out', amount=25000, account_id=cash.id,
            category_name='Workshop', idempotency_key=spend_key,
        )
        assert created2 is False
        assert again.id == spent.id
        db.session.refresh(cash)
        assert cash.balance == 75000

        # Received.
        rec, _ = save_manual_cash_flow_entry(
            direction='in', amount=50000, account_id=cash.id,
            category_name='Office In', note='advance',
        )
        db.session.commit()
        db.session.refresh(cash)
        assert cash.balance == 125000

        # Transfer is not income/expense.
        xfer, _ = save_manual_cash_flow_entry(
            direction='transfer', amount=10000, account_id=cash.id,
            destination_account_id=bank.id, description='Bank deposit',
        )
        db.session.commit()
        db.session.refresh(cash)
        db.session.refresh(bank)
        assert cash.balance == 115000
        assert bank.balance == 30000
        assert xfer.direction == 'transfer'

        # Same-account transfer rejected.
        try:
            save_manual_cash_flow_entry(
                direction='transfer', amount=1, account_id=cash.id,
                destination_account_id=cash.id,
            )
            assert False
        except ValueError:
            db.session.rollback()

        # Zero amount rejected.
        try:
            save_manual_cash_flow_entry(
                direction='in', amount=0, account_id=cash.id, category_name='Office In',
            )
            assert False
        except ValueError:
            db.session.rollback()

        today = date.today().strftime('%Y-%m-%d')
        rows = collect_cash_flow_rows(today, today)
        spent_row = next(r for r in rows if r.get('entry_id') == spent.id)
        rec_row = next(r for r in rows if r.get('entry_id') == rec.id)
        xfer_row = next(r for r in rows if r.get('entry_id') == xfer.id)
        assert spent_row['cash_out'] == 25000
        assert spent_row['cash_in'] == 0
        assert rec_row['cash_in'] == 50000
        assert xfer_row['type'] == 'transfer'
        assert xfer_row['cash_in'] == 0
        assert xfer_row['cash_out'] == 0
        assert xfer_row['transfer_amount'] == 10000

        notes_hit = filter_cash_flow_rows(rows, {'notes': 'truck', 'status': 'all'})
        assert any(r.get('entry_id') == spent.id for r in notes_hit)

        # Combined filters.
        combo = filter_cash_flow_rows(rows, {
            'filter_type': 'spent', 'category': 'workshop', 'notes': 'truck', 'status': 'active',
        })
        assert len(combo) == 1
        assert combo[0]['entry_id'] == spent.id

        closing = apply_running_balance(list(rows), 0)
        summary = summarize_cash_flow_rows(rows)
        assert summary['total_cash_in'] == 50000
        assert summary['total_cash_out'] == 25000
        assert closing == 25000  # company-wide ignores transfers

        # Edit amount: reverse then apply.
        update_manual_cash_flow_entry(
            spent, direction='out', amount=30000, account_id=cash.id,
            category_id=cat.id, subcategory_name='Parts', note='Diesel truck #12',
            description='Workshop bill',
        )
        db.session.commit()
        db.session.refresh(cash)
        assert cash.balance == 110000  # 115000 +25000 -30000

        # Received -> Spent.
        update_manual_cash_flow_entry(
            rec, direction='out', amount=50000, account_id=cash.id,
            category_name='Workshop', description='flipped',
        )
        db.session.commit()
        db.session.refresh(cash)
        assert cash.balance == 10000  # 110000 -50000 -50000

        # Void reverses.
        void_manual_cash_flow_entry(spent, reason='correction')
        db.session.commit()
        db.session.refresh(cash)
        assert cash.balance == 40000
        assert spent.is_void is True
        assert CashFlowEntry.query.get(spent.id) is not None

        # Disable category keeps the row name via ID.
        disable_cf_category(cat.id)
        db.session.commit()
        assert CashFlowCategory.query.get(cat.id).is_active is False
        assert CashFlowEntry.query.get(xfer.id).id

        # Direction-aware category list has no hard-coded business names.
        src = open(os.path.join(os.path.dirname(__file__), '..', 'app', 'blueprints', 'reports', 'cash.py'), encoding='utf-8').read()
        svc = open(os.path.join(os.path.dirname(__file__), '..', 'app', 'services', 'cash_flow_svc.py'), encoding='utf-8').read()
        for banned in ('if category == "Fuel"', 'categories = ["Fuel"', 'Loan Received'):
            assert banned not in src
            assert banned not in svc
        spent_cats = {c.name for c in categories_for_direction('out')}
        assert 'Incoming Only' not in spent_cats
        assert 'Workshop' not in spent_cats
        assert [s.name for s in subcategories_for_category(cat.id)] == ['Parts']

        from app.services.cash_flow_svc import (
            delete_cf_category,
            delete_cf_subcategory,
            parse_physical_cash_amount,
            compute_physical_cash_difference,
        )

        unused, _ = save_cf_category('Disposable Box', 'both')
        unused_sub, _ = save_cf_subcategory(unused.id, 'Disposable Sub')
        db.session.commit()
        delete_cf_subcategory(unused_sub.id)
        db.session.commit()
        assert CashFlowSubcategory.query.get(unused_sub.id) is None
        delete_cf_category(unused.id)
        db.session.commit()
        assert CashFlowCategory.query.get(unused.id) is None

        try:
            delete_cf_category(cat.id)
            assert False, 'historically used category must not hard-delete'
        except ValueError as exc:
            assert 'cannot be deleted' in str(exc).lower() or 'historical' in str(exc).lower()
        db.session.rollback()
        assert CashFlowCategory.query.get(cat.id) is not None
        assert CashFlowEntry.query.get(spent.id) is not None
        assert CashFlowEntry.query.get(spent.id).category_id == cat.id

        assert parse_physical_cash_amount('0') == 0.0
        assert parse_physical_cash_amount('98,500') == 98500.0
        try:
            parse_physical_cash_amount('')
            assert False
        except ValueError:
            pass
        try:
            parse_physical_cash_amount('abc')
            assert False
        except ValueError:
            pass
        assert compute_physical_cash_difference(98500, 100000) == -1500
        assert compute_physical_cash_difference(102000, 100000) == 2000
        assert compute_physical_cash_difference(100000, 100000) == 0


def test_system_sale_and_transfer_rows_not_double_counted():
    with app.app_context():
        db.drop_all()
        db.create_all()
        cash = _account('ENGINE CASH B', 0)
        bank = _account('ENGINE BANK B', 0, category='bank')
        sale = DirectSale(
            client_name='Cash Flow Sale Test',
            category='Cash', paid_amount=11111.0, date_posted=pk_now(),
            is_void=False, payment_method='Cash',
        )
        tx_in = AccountTransaction(
            from_account_id=bank.id, to_account_id=cash.id, amount=60000,
            description='bank to cash', transaction_type='Transfer', date_posted=pk_now(),
        )
        refund = AccountTransaction(
            from_account_id=cash.id, to_account_id=None, amount=12345,
            description='Refund Flow Test', transaction_type='Payment', date_posted=pk_now(),
        )
        db.session.add_all([sale, tx_in, refund])
        db.session.flush()
        mirror = AccountTransaction(
            from_account_id=None, to_account_id=cash.id, amount=11111,
            description='Sale receipt mirror', note=f'[SRC:DirectSale:{sale.id}]',
            transaction_type='Receipt', date_posted=pk_now(),
        )
        db.session.add(mirror)
        db.session.commit()

        today = date.today().strftime('%Y-%m-%d')
        rows = collect_cash_flow_rows(today, today)
        refs = [r['reference'] for r in rows]
        assert f'TX-{tx_in.id}' in refs
        assert f'TX-{mirror.id}' not in refs
        assert any('Cash Flow Sale Test' in (r.get('description') or '') for r in rows)
        assert any('Refund Flow Test' in (r.get('description') or '') for r in rows)
        xfer = next(r for r in rows if r['reference'] == f'TX-{tx_in.id}')
        assert xfer['type'] == 'transfer'
        assert xfer['cash_in'] == 0
        assert xfer['cash_out'] == 0
        summary = summarize_cash_flow_rows(rows)
        assert summary['total_cash_in'] == 11111
        assert summary['total_cash_out'] == 12345
