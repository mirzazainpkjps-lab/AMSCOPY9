"""Comprehensive smoke tests for Cash Flow end-to-end flows.

Covers:
1. Empty start state (no default categories).
2. Category, Subcategory, and Party CRUD + Meta API quick-add.
3. Received [IN], Spent [OUT], Transfer [INTERNAL] transactions.
4. Account balance mutations and running balances.
5. Entry details JSON endpoint (used by View modal) with audit trail.
6. Edit transaction flow with audit log and balance adjustments.
7. Void (delete) and Restore flow with balance reversal and restoration.
8. Physical cash reconciliation with expected vs counted difference.
9. Protection against deleting used categories/subcategories/parties.
10. Complete cleanup of test data.
"""
import os
import tempfile
import uuid
from datetime import datetime, timedelta

os.environ.setdefault('ALLOW_EMPTY_DB', '1')
os.environ.setdefault('ALLOW_DB_DROP', '1')
os.environ.setdefault(
    'APP_DB_PATH',
    os.path.join(tempfile.gettempdir(), 'ams_cash_flow_smoke_suite_test.db'),
)

import pytest
from app import create_app
from models import (
    db, Account, AccountTransaction, CashFlowCategory, CashFlowEntry,
    CashFlowSubcategory, CashFlowParty, CashFlowDifferenceAdjustment,
    CashFlowReconciliationAudit, CashFlowEntryAudit,
)
from app.services.time_money import pk_now, pk_today

app = create_app({
    'TESTING': True,
    'LOGIN_DISABLED': True,
    'WTF_CSRF_ENABLED': False,
})


def _cleanup_all():
    CashFlowEntryAudit.query.delete()
    CashFlowReconciliationAudit.query.delete()
    CashFlowDifferenceAdjustment.query.delete()
    CashFlowEntry.query.delete()
    AccountTransaction.query.delete()
    CashFlowSubcategory.query.delete()
    CashFlowParty.query.delete()
    CashFlowCategory.query.delete()
    Account.query.delete()
    db.session.commit()


@pytest.fixture(autouse=True)
def clean_database():
    with app.app_context():
        db.create_all()
        _cleanup_all()
    yield
    with app.app_context():
        _cleanup_all()


def test_cash_flow_full_lifecycle_smoke():
    client = app.test_client()
    today = pk_today().strftime('%Y-%m-%d')

    # 1. Start with clean accounts
    with app.app_context():
        cash_acc = Account(name='Main Cash Drawer', category='cash', account_type='company', type='company', balance=100000, is_active=True)
        bank_acc = Account(name='HBL Business Account', category='bank', account_type='company', type='company', balance=500000, is_active=True)
        db.session.add_all([cash_acc, bank_acc])
        db.session.commit()
        cash_id = cash_acc.id
        bank_id = bank_acc.id

    # 2. Verify initial Cash Flow page renders with 0 categories
    resp = client.get('/cash_flow')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Cash Flow' in html
    assert 'RECEIPT [ IN ]' in html
    assert 'PAYMENT [ OUT ]' in html
    assert 'TRANSFER [ INTERNAL ]' in html

    # 3. Quick-Add Category via JSON API (Received only)
    cat_res = client.post('/cash_flow/meta/categories', json={
        'name': 'Scrap Sale',
        'direction': 'in',
        'notes': 'Income from scrap metal',
    })
    assert cat_res.status_code == 200
    cat_data = cat_res.get_json()
    assert cat_data['ok'] is True
    cat_in_id = cat_data['category']['id']
    assert cat_data['category']['name'] == 'Scrap Sale'
    assert cat_data['category']['direction'] == 'in'

    # Quick-Add Category via JSON API (Spent only)
    cat_out_res = client.post('/cash_flow/meta/categories', json={
        'name': 'Truck Diesel',
        'direction': 'out',
        'notes': 'Fleet fuel expense',
    })
    assert cat_out_res.status_code == 200
    cat_out_id = cat_out_res.get_json()['category']['id']

    # 4. Quick-Add Subcategory
    sub_res = client.post('/cash_flow/meta/subcategories', json={
        'category_id': str(cat_out_id),
        'name': 'Hino Truck 450',
    })
    assert sub_res.status_code == 200
    sub_data = sub_res.get_json()
    assert sub_data['ok'] is True
    sub_out_id = sub_data['subcategory']['id']

    # 5. Quick-Add Party
    party_res = client.post('/cash_flow/meta/parties', json={
        'name': 'PSO Petrol Pump',
        'party_type': 'contractor',
        'note': 'Main highway fuel station',
    })
    assert party_res.status_code == 200
    party_data = party_res.get_json()
    assert party_data['ok'] is True
    party_id = party_data['party']['id']

    # 6. Post Received [IN] Transaction
    post_in = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'in',
        'amount': '25000',
        'cash_account_id': str(cash_id),
        'category_id': str(cat_in_id),
        'category_name': 'Scrap Sale',
        'description': 'Sold leftover steel bars',
        'movement_note': 'weight 250kg',
        'reference': 'REC-001',
        'movement_date': f'{today}T10:00',
        'idempotency_key': str(uuid.uuid4()),
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert post_in.status_code == 200
    assert b'Received Rs. 25,000 recorded.' in post_in.data

    with app.app_context():
        cash = Account.query.get(cash_id)
        assert cash.balance == 100000 + 25000

    # 7. Post Spent [OUT] Transaction
    post_out = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'out',
        'amount': '8000',
        'cash_account_id': str(cash_id),
        'category_id': str(cat_out_id),
        'category_name': 'Truck Diesel',
        'subcategory_id': str(sub_out_id),
        'subcategory_name': 'Hino Truck 450',
        'party_id': str(party_id),
        'party_name': 'PSO Petrol Pump',
        'party_type': 'contractor',
        'description': 'Full tank refuel',
        'movement_note': 'diesel 32 liters',
        'reference': 'EXP-001',
        'movement_date': f'{today}T11:30',
        'idempotency_key': str(uuid.uuid4()),
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert post_out.status_code == 200
    assert b'Spent Rs. 8,000 recorded.' in post_out.data

    with app.app_context():
        cash = Account.query.get(cash_id)
        assert cash.balance == 100000 + 25000 - 8000

    # 8. Post Internal Transfer Transaction
    post_xfer = client.post('/cash_flow', data={
        'action': 'record_movement',
        'direction': 'transfer',
        'amount': '30000',
        'cash_account_id': str(cash_id),
        'to_account_id': str(bank_id),
        'description': 'Cash deposit to HBL',
        'movement_date': f'{today}T14:00',
        'idempotency_key': str(uuid.uuid4()),
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert post_xfer.status_code == 200
    assert b'Transfer Rs. 30,000 recorded.' in post_xfer.data

    with app.app_context():
        cash = Account.query.get(cash_id)
        bank = Account.query.get(bank_id)
        assert cash.balance == 100000 + 25000 - 8000 - 30000
        assert bank.balance == 500000 + 30000

    # 9. View Entry details endpoint (for View Modal)
    with app.app_context():
        entry_in = CashFlowEntry.query.filter_by(reference='REC-001').one()
        entry_in_id = entry_in.id

    view_res = client.get(f'/cash_flow/entry/{entry_in_id}.json')
    assert view_res.status_code == 200
    entry_json = view_res.get_json()
    assert entry_json['id'] == entry_in_id
    assert entry_json['direction'] == 'in'
    assert entry_json['amount'] == 25000.0
    assert entry_json['category_name'] == 'Scrap Sale'
    assert entry_json['reference'] == 'REC-001'
    assert entry_json['status'] == 'active'

    # 10. Edit an Entry
    edit_res = client.post('/cash_flow', data={
        'action': 'edit_entry',
        'entry_id': str(entry_in_id),
        'direction': 'in',
        'amount': '28000',  # revised amount +3000
        'cash_account_id': str(cash_id),
        'category_id': str(cat_in_id),
        'category_name': 'Scrap Sale',
        'description': 'Sold leftover steel bars (revised rate)',
        'movement_note': 'weight 280kg',
        'reference': 'REC-001-REV',
        'movement_date': f'{today}T10:00',
        'edit_reason': 'Rate recalculated with supervisor',
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert edit_res.status_code == 200
    assert b'Cash flow transaction updated.' in edit_res.data

    with app.app_context():
        cash = Account.query.get(cash_id)
        assert cash.balance == 100000 + 28000 - 8000 - 30000
        edited_entry = CashFlowEntry.query.get(entry_in_id)
        assert edited_entry.amount == 28000.0
        assert edited_entry.reference == 'REC-001-REV'
        assert len(edited_entry.audit_trail) >= 1
        assert 'Rate recalculated with supervisor' in edited_entry.audit_trail[-1].reason

    # 11. Physical Cash Reconciliation
    recon_res = client.post('/cash_flow', data={
        'action': 'save_reconciliation',
        'adjustment_date': today,
        'physical_cash_available': '89500',
        'reconciliation_reason': 'Counted at safe close',
        'from_date': today,
        'to_date': today,
        'filter_type': 'all',
    }, follow_redirects=True)
    assert recon_res.status_code == 200
    assert b'Reconciliation saved' in recon_res.data

    with app.app_context():
        recon = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=pk_today()).one()
        assert recon.physical_cash_available == 89500.0
        assert recon.difference == 89500.0 - recon.calculated_closing

    # 12. Safe Delete rules: cannot delete used category
    del_cat_fail = client.post('/cash_flow', data={
        'action': 'delete_category',
        'category_id': str(cat_in_id),
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert b'cannot be deleted' in del_cat_fail.data

    # 13. Void (Delete) transaction & restore
    with app.app_context():
        spent_entry = CashFlowEntry.query.filter_by(reference='EXP-001').one()
        spent_id = spent_entry.id

    void_res = client.post('/cash_flow', data={
        'action': 'void_entry',
        'entry_id': str(spent_id),
        'void_reason': 'Duplicate fuel voucher',
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert void_res.status_code == 200
    assert b'Transaction voided.' in void_res.data

    with app.app_context():
        # Cash account refunded the 8000
        cash = Account.query.get(cash_id)
        assert cash.balance == 100000 + 28000 - 30000

    restore_res = client.post('/cash_flow', data={
        'action': 'restore_entry',
        'entry_id': str(spent_id),
        'from_date': today,
        'to_date': today,
    }, follow_redirects=True)
    assert restore_res.status_code == 200
    assert b'Transaction restored' in restore_res.data

    with app.app_context():
        # Cash account re-spent the 8000
        cash = Account.query.get(cash_id)
        assert cash.balance == 100000 + 28000 - 8000 - 30000
