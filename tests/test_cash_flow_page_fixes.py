"""HTTP-level Cash Flow fixes: date default, CRUD delete, physical cash."""
import os
import tempfile
import uuid
from datetime import datetime, timedelta

os.environ.setdefault('ALLOW_EMPTY_DB', '1')
os.environ.setdefault('ALLOW_DB_DROP', '1')
os.environ.setdefault(
    'APP_DB_PATH',
    os.path.join(tempfile.gettempdir(), 'ams_cash_flow_page_fixes_test.db'),
)

from app import create_app
from models import (
    db, Account, CashFlowCategory, CashFlowEntry, CashFlowSubcategory,
    CashFlowDifferenceAdjustment,
)
from app.services.time_money import pk_now, pk_today

app = create_app({
    'TESTING': True,
    'LOGIN_DISABLED': True,
    'WTF_CSRF_ENABLED': False,
})


def _account(name='PAGE CASH', balance=500000, category='cash'):
    acc = Account.query.filter_by(name=name).first()
    if acc:
        acc.balance = balance
        acc.category = category
        acc.account_type = 'company'
        acc.is_active = True
        return acc
    acc = Account(
        name=name, category=category, account_type='company',
        type='company', balance=balance, is_active=True,
    )
    db.session.add(acc)
    db.session.flush()
    return acc


def _client():
    client = app.test_client()
    today = pk_today().strftime('%Y-%m-%d')
    with client.session_transaction() as sess:
        sess['cash_flow_fresh_start_cutoff'] = {
            'date': today,
            'at': '2000-01-01 00:00:00',
        }
    return client


def test_cash_flow_page_date_crud_and_physical_cash():
    with app.app_context():
        db.drop_all()
        db.create_all()
        cash = _account('PAGE CASH', 500000)
        bank = _account('PAGE BANK', 80000, category='bank')
        db.session.commit()
        cash_id = cash.id
        bank_id = bank.id

    client = _client()
    today = pk_today().strftime('%Y-%m-%d')
    now_prefix = pk_now().strftime('%Y-%m-%dT')

    # Test A — open page: date defaults to today (PKT).
    page = client.get('/cash_flow')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'name="movement_date"' in html
    assert f'value="{now_prefix}' in html
    assert 'Physical Cash Available is required' not in html
    assert 'No money movements match these filters.' in html

    # Test B — Received without physical cash.
    rec_key = 'page-rec-' + uuid.uuid4().hex
    rec_resp = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'in',
        'amount': '10000',
        'cash_account_id': str(cash_id),
        'category_name': 'Office In',
        'subcategory_name': 'Advance',
        'description': 'Received test',
        'movement_note': 'no physical cash',
        'reference': 'R-1',
        'movement_date': f'{today}T09:15',
        'idempotency_key': rec_key,
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    rec_html = rec_resp.get_data(as_text=True)
    assert rec_resp.status_code == 200
    assert 'Physical Cash Available is required' not in rec_html
    assert 'Received Rs. 10,000 recorded.' in rec_html

    # Test C — Spent without physical cash.
    spent_resp = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'out',
        'amount': '5000',
        'cash_account_id': str(cash_id),
        'category_name': 'Workshop',
        'subcategory_name': 'Parts',
        'description': 'Spent test',
        'movement_note': 'diesel',
        'reference': 'S-1',
        'movement_date': f'{today}T10:00',
        'idempotency_key': 'page-spent-' + uuid.uuid4().hex,
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    spent_html = spent_resp.get_data(as_text=True)
    assert spent_resp.status_code == 200
    assert 'Physical Cash Available is required' not in spent_html
    assert 'Spent Rs. 5,000 recorded.' in spent_html

    # Test D — Transfer is not income/expense and does not need physical cash.
    xfer_resp = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'transfer',
        'amount': '2000',
        'cash_account_id': str(cash_id),
        'to_account_id': str(bank_id),
        'description': 'Bank deposit',
        'movement_date': f'{today}T11:00',
        'idempotency_key': 'page-xfer-' + uuid.uuid4().hex,
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    xfer_html = xfer_resp.get_data(as_text=True)
    assert xfer_resp.status_code == 200
    assert 'Physical Cash Available is required' not in xfer_html
    assert 'Transfer Rs. 2,000 recorded.' in xfer_html

    with app.app_context():
        rec = CashFlowEntry.query.filter_by(reference='R-1').one()
        spent = CashFlowEntry.query.filter_by(reference='S-1').one()
        xfer = CashFlowEntry.query.filter_by(description='Bank deposit').one()
        assert rec.direction == 'in'
        assert spent.direction == 'out'
        assert xfer.direction == 'transfer'
        assert rec.date_posted.strftime('%Y-%m-%d %H:%M') == f'{today} 09:15'
        db.session.refresh(Account.query.get(cash_id))
        cash_bal = Account.query.get(cash_id).balance
        bank_bal = Account.query.get(bank_id).balance
        assert cash_bal == 500000 + 10000 - 5000 - 2000
        assert bank_bal == 80000 + 2000

    # Test E — backdated transaction keeps the selected date.
    back_day = (pk_today() - timedelta(days=4)).strftime('%Y-%m-%d')
    back_resp = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'in',
        'amount': '1500',
        'cash_account_id': str(cash_id),
        'category_name': 'Office In',
        'description': 'Backdated receipt',
        'reference': 'BACK-1',
        'movement_date': f'{back_day}T08:40',
        'idempotency_key': 'page-back-' + uuid.uuid4().hex,
        'from_date': back_day,
        'to_date': back_day,
    }, follow_redirects=True)
    assert back_resp.status_code == 200
    assert b'Physical Cash Available is required' not in back_resp.data
    with app.app_context():
        back = CashFlowEntry.query.filter_by(reference='BACK-1').one()
        assert back.date_posted.strftime('%Y-%m-%d %H:%M') == f'{back_day} 08:40'

    hist = client.get(f'/cash_flow?from_date={back_day}&to_date={back_day}')
    assert hist.status_code == 200
    assert b'BACK-1' in hist.data

    # Missing date on a Received POST falls back to application now, not an error.
    miss_resp = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'in',
        'amount': '250',
        'cash_account_id': str(cash_id),
        'category_name': 'Office In',
        'description': 'Missing date fallback',
        'reference': 'NOW-1',
        'movement_date': '',
        'idempotency_key': 'page-now-' + uuid.uuid4().hex,
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert miss_resp.status_code == 200
    assert b'Physical Cash Available is required' not in miss_resp.data
    with app.app_context():
        now_entry = CashFlowEntry.query.filter_by(reference='NOW-1').one()
        assert now_entry.date_posted.date() == pk_today()

    # Test F — reconciliation empty is required; 0 is accepted; formula is physical - system.
    empty_rec = client.post('/cash_flow', data={
        'action': 'save_reconciliation',
        'adjustment_date': today,
        'physical_cash_available': '',
        'from_date': today,
        'to_date': today,
        'filter_type': 'all',
    }, follow_redirects=True)
    assert empty_rec.status_code == 200
    assert b'Physical Cash Available is required' in empty_rec.data

    invalid_rec = client.post('/cash_flow', data={
        'action': 'save_reconciliation',
        'adjustment_date': today,
        'physical_cash_available': 'not-a-number',
        'from_date': today,
        'to_date': today,
        'filter_type': 'all',
    }, follow_redirects=True)
    assert b'must be a valid number' in invalid_rec.data

    zero_rec = client.post('/cash_flow', data={
        'action': 'save_reconciliation',
        'adjustment_date': today,
        'physical_cash_available': '0',
        'reconciliation_reason': 'empty drawer',
        'from_date': today,
        'to_date': today,
        'filter_type': 'all',
    }, follow_redirects=True)
    assert zero_rec.status_code == 200
    assert b'Physical Cash Available is required' not in zero_rec.data
    assert b'Reconciliation saved' in zero_rec.data
    with app.app_context():
        rec0 = CashFlowDifferenceAdjustment.query.filter_by(
            adjustment_date=pk_today()
        ).one()
        assert rec0.physical_cash_available == 0.0
        # Difference = 0 - system closing. Closing is opening(0)+in-out, transfers ignored.
        assert rec0.difference == 0.0 - rec0.calculated_closing

    counted = client.post('/cash_flow', data={
        'action': 'save_reconciliation',
        'adjustment_date': today,
        'physical_cash_available': '98500',
        'reconciliation_reason': 'counted',
        'from_date': today,
        'to_date': today,
        'filter_type': 'all',
    }, follow_redirects=True)
    assert b'Reconciliation saved' in counted.data
    with app.app_context():
        rec_n = CashFlowDifferenceAdjustment.query.filter_by(
            adjustment_date=pk_today()
        ).one()
        assert rec_n.physical_cash_available == 98500.0
        assert rec_n.difference == 98500.0 - rec_n.calculated_closing
        cash_after = Account.query.get(cash_id).balance
        # Reconciliation must not change account balances.
        assert cash_after == 500000 + 10000 - 5000 - 2000 + 1500 + 250

    # Category / subcategory CRUD + safe delete.
    add_cat = client.post('/cash_flow', data={
        'action': 'add_category',
        'new_category_name': 'Test Disposable',
        'new_category_direction': 'in',
        'new_category_notes': 'temp',
    }, follow_redirects=True)
    assert add_cat.status_code == 200
    assert b'Category saved.' in add_cat.data

    with app.app_context():
        disposable = CashFlowCategory.query.filter_by(name='Test Disposable').one()
        disposable_id = disposable.id
        parent = CashFlowCategory.query.filter_by(name='Office In').first()
        parent_id = parent.id

    add_sub = client.post('/cash_flow', data={
        'action': 'add_subcategory',
        'new_sub_category_id': str(disposable_id),
        'new_subcategory_name': 'Temp Sub',
    }, follow_redirects=True)
    assert b'Sub-category saved.' in add_sub.data

    with app.app_context():
        temp_sub = CashFlowSubcategory.query.filter_by(name='Temp Sub').one()
        temp_sub_id = temp_sub.id
    del_sub = client.post('/cash_flow', data={
        'action': 'delete_subcategory',
        'subcategory_id': str(temp_sub_id),
    }, follow_redirects=True)
    assert b'Unused sub-category deleted.' in del_sub.data

    del_cat = client.post('/cash_flow', data={
        'action': 'delete_category',
        'category_id': str(disposable_id),
    }, follow_redirects=True)
    assert b'Unused category deleted.' in del_cat.data
    with app.app_context():
        assert CashFlowCategory.query.get(disposable_id) is None

    blocked = client.post('/cash_flow', data={
        'action': 'delete_category',
        'category_id': str(parent_id),
    }, follow_redirects=True)
    assert b'cannot be deleted' in blocked.data
    with app.app_context():
        assert CashFlowCategory.query.get(parent_id) is not None
        assert CashFlowEntry.query.filter_by(reference='R-1').one().category_id == parent_id

    # Filter returning zero rows must not 500.
    empty = client.get('/cash_flow?from_date=%s&to_date=%s&notes=zzznomatch' % (today, today))
    assert empty.status_code == 200
    assert b'No money movements match these filters.' in empty.data

    quick_cat = client.post('/cash_flow/meta/categories', json={
        'name': 'Quick Fuel',
        'direction': 'out',
    })
    assert quick_cat.status_code == 200
    cat_body = quick_cat.get_json()
    assert cat_body['ok'] is True
    assert cat_body['category']['name'] == 'Quick Fuel'
    assert cat_body['category']['direction'] == 'out'

    quick_party = client.post('/cash_flow/meta/parties', json={
        'name': 'Adnan Driver',
        'party_type': 'person',
    })
    assert quick_party.status_code == 200
    party_body = quick_party.get_json()
    assert party_body['ok'] is True
    assert party_body['party']['name'] == 'Adnan Driver'

    # Unknown / empty action must not demand physical cash.
    stray = client.post('/cash_flow', data={
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert stray.status_code == 200
    assert b'Physical Cash Available is required' not in stray.data
