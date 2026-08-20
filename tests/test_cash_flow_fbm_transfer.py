import pytest
import os
import tempfile
from datetime import date

# Isolate this module-level app from the real production database.
os.environ.setdefault("ALLOW_EMPTY_DB", "1")
os.environ.setdefault("ALLOW_DB_DROP", "1")
os.environ.setdefault(
    "APP_DB_PATH",
    os.path.join(tempfile.gettempdir(), "ams_cash_flow_fbm_transfer_test.db"),
)

from app import create_app
from models import db
from app.services.time_money import pk_now

app = create_app({"TESTING": True})
from app.blueprints.reports.cash import cash_flow
from models import Account, AccountTransaction, DirectSale


def get_or_create_account(name, category='cash', account_type='company'):
    acc = Account.query.filter(Account.name == name).first()
    if not acc:
        acc = Account(name=name, category=category, account_type=account_type, type=account_type, balance=0.0)
        db.session.add(acc)
        db.session.flush()
    return acc


@pytest.fixture
def client_app():
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield


def test_fbm_drawer_transfer_is_included_and_not_double_counted(client_app, monkeypatch):
    fbm_account = Account.query.filter(Account.name == 'FBM DRAWER CASH').first()
    if fbm_account is None:
        fbm_account = get_or_create_account('FBM DRAWER CASH', category='cash', account_type='company')

    source_account = get_or_create_account('HDC CASH', category='cash', account_type='company')
    other_account = get_or_create_account('TEST CASH FLOW EXTERNAL', category='cash', account_type='company')

    tx_in = AccountTransaction(
        from_account_id=source_account.id,
        to_account_id=fbm_account.id,
        amount=60000.0,
        description='HDC CASH -> FBM DRAWER CASH',
        transaction_type='Transfer',
        date_posted=pk_now()
    )
    tx_out = AccountTransaction(
        from_account_id=fbm_account.id,
        to_account_id=source_account.id,
        amount=50000.0,
        description='FBM DRAWER CASH -> HDC CASH',
        transaction_type='Transfer',
        date_posted=pk_now()
    )
    tx_nonfbm = AccountTransaction(
        from_account_id=source_account.id,
        to_account_id=other_account.id,
        amount=70000.0,
        description='HDC CASH -> TEST CASH FLOW EXTERNAL',
        transaction_type='Transfer',
        date_posted=pk_now()
    )
    direct_sale = DirectSale(
        client_name='Cash Flow Sale Test',
        category='Cash',
        paid_amount=11111.0,
        date_posted=pk_now(),
        is_void=False,
        payment_method='Cash'
    )
    refund_tx = AccountTransaction(
        from_account_id=source_account.id,
        to_account_id=None,
        amount=12345.0,
        description='Refund Flow Test',
        transaction_type='Payment',
        date_posted=pk_now()
    )

    db.session.add_all([tx_in, tx_out, tx_nonfbm, direct_sale, refund_tx])
    db.session.flush()
    linked_sale_tx = AccountTransaction(
        from_account_id=None,
        to_account_id=source_account.id,
        amount=11111.0,
        description='Sale receipt mirror',
        note=f'[SRC:DirectSale:{direct_sale.id}]',
        transaction_type='Receipt',
        date_posted=pk_now(),
    )
    db.session.add(linked_sale_tx)
    db.session.commit()

    captured = {}

    def fake_render_template(template, **kwargs):
        captured.update(kwargs)
        return 'rendered'

    import app.blueprints.reports.cash as cash_mod
    monkeypatch.setattr(cash_mod, 'render_template', fake_render_template)

    today = date.today().strftime('%Y-%m-%d')
    app.config['LOGIN_DISABLED'] = True
    try:
        with app.test_request_context(f'/cash_flow?from_date={today}&to_date={today}'):
            from flask import session
            session['cash_flow_fresh_start_cutoff'] = {
                'date': today,
                'at': '2000-01-01 00:00:00',
            }
            response = cash_flow()
    finally:
        app.config['LOGIN_DISABLED'] = False

    assert response == 'rendered'

    rows = captured.get('rows', [])
    references = [row['reference'] for row in rows]
    descriptions = [row['description'] for row in rows]

    assert f'TX-{tx_in.id}' in references, 'Company-account transfer in is missing from cash flow.'
    assert f'TX-{tx_out.id}' in references, 'Company-account transfer out is missing from cash flow.'
    assert f'TX-{tx_nonfbm.id}' in references, 'Transfers between company cash/bank accounts must appear as Transfer rows.'
    assert any('Cash Flow Sale Test' in desc for desc in descriptions), 'Existing cash sale must still appear in cash flow.'
    assert any('Refund Flow Test' in desc for desc in descriptions), 'Existing refund payment must still appear in cash flow.'
    assert f'TX-{linked_sale_tx.id}' not in references, 'Direct-sale AccountTransaction mirror must not duplicate the sale row.'

    tx_in_rows = [r for r in rows if r['reference'] == f'TX-{tx_in.id}']
    tx_out_rows = [r for r in rows if r['reference'] == f'TX-{tx_out.id}']
    assert len(tx_in_rows) == 1
    assert len(tx_out_rows) == 1
    assert tx_in_rows[0]['type'] == 'transfer'
    assert tx_out_rows[0]['type'] == 'transfer'
    assert tx_in_rows[0]['cash_in'] == 0
    assert tx_in_rows[0]['cash_out'] == 0
    assert tx_in_rows[0]['transfer_amount'] == 60000.0
    assert tx_out_rows[0]['transfer_amount'] == 50000.0

    total_cash_in = captured.get('total_cash_in')
    total_cash_out = captured.get('total_cash_out')
    opening_balance = captured.get('opening_balance')
    closing_balance = captured.get('closing_balance')
    assert closing_balance == pytest.approx(opening_balance + total_cash_in - total_cash_out, rel=1e-9)
    assert sum(r['cash_in'] for r in tx_in_rows) == 0
    assert sum(r['cash_out'] for r in tx_out_rows) == 0
    assert any(r['cash_in'] == 11111.0 and 'Cash Flow Sale Test' in r['description'] for r in rows)
    assert any(r['cash_out'] == 12345.0 and 'Refund Flow Test' in r['description'] for r in rows)
