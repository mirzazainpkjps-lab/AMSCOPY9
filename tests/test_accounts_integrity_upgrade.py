"""Regression coverage for the production Accounts integrity upgrade."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

os.environ['APP_DB_PATH'] = os.path.join(tempfile.gettempdir(), 'ams_accounts_integrity_upgrade.db')
os.environ['ALLOW_EMPTY_DB'] = '1'
os.environ['ALLOW_DB_DROP'] = '1'

from app import create_app
from models import (
    Account, AccountingAuditLog, AccountReconciliation, AccountTransaction,
    Client, MaterialReturn, Payment, Supplier, SupplierPayment, User, db,
)
from app.services.payments_crud import (
    delete_client_payment, delete_supplier_payment, ledger_balance,
    reconcile_account, save_client_payment, save_supplier_payment,
)

app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False})


class AccountsIntegrityUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.cash = Account(name='Exact Cash', category='cash', source_category='Company',
                            account_type='company', balance=5000, is_active=True)
        self.bank = Account(name='Exact Bank', category='bank', source_category='Company',
                            account_type='company', balance=5000, is_active=True)
        self.inactive_bank = Account(name='Archived Bank', category='bank', source_category='Company',
                                     account_type='company', balance=1000, is_active=False)
        self.client = Client(code='C-EXACT', name='Exact Client', is_active=True)
        self.supplier = Supplier(name='Exact Supplier', is_active=True)
        db.session.add_all([self.cash, self.bank, self.inactive_bank, self.client, self.supplier])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_refund_create_edit_delete_is_exact_and_traceable(self):
        payment, created = save_client_payment(
            client_code=self.client.code, amount='49.99', payment_type='Refund',
            method='Bank', payment_account_id=self.bank.id, note='return excess', actor=None,
        )
        self.assertTrue(created)
        db.session.commit()
        self.assertEqual(payment.amount_minor, -4999)
        self.assertEqual(self.bank.balance_minor, 495001)
        self.assertAlmostEqual(self.bank.balance, 4950.01, places=2)
        refund = AccountTransaction.query.filter_by(source_type='Payment', source_id=payment.id,
                                                     transaction_type='Refund', is_void=False).one()
        self.assertEqual(refund.amount_minor, 4999)

        same_id = payment.id
        save_client_payment(
            payment_id=same_id, client_code=self.client.code, amount='0.01',
            payment_type='Refund', method='Bank', payment_account_id=self.bank.id,
            expected_revision=payment.revision, actor=None,
        )
        db.session.commit()
        self.assertEqual(Payment.query.count(), 1)
        self.assertEqual(payment.id, same_id)
        self.assertEqual(self.bank.balance_minor, 499999)

        self.assertTrue(delete_client_payment(payment, actor=None))
        db.session.commit()
        self.assertEqual(self.bank.balance_minor, 500000)
        self.assertTrue(payment.is_void)
        self.assertEqual(AccountTransaction.query.filter_by(source_type='Payment', source_id=same_id,
                                                             is_void=False).count(), 0)

    def test_idempotency_and_optimistic_revision(self):
        p1, created = save_client_payment(
            client_code=self.client.code, amount='11905692.80', method='Cash',
            payment_account_id=self.cash.id, idempotency_key='request:fixed-1', actor=None,
        )
        db.session.commit()
        p2, created_again = save_client_payment(
            client_code=self.client.code, amount='11905692.80', method='Cash',
            payment_account_id=self.cash.id, idempotency_key='request:fixed-1', actor=None,
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(Payment.query.count(), 1)
        self.assertEqual(p1.amount_minor, 1190569280)

        stale_revision = p1.revision - 1
        with self.assertRaisesRegex(ValueError, 'another session'):
            save_client_payment(
                payment_id=p1.id, client_code=self.client.code, amount='1.00', method='Cash',
                payment_account_id=self.cash.id, expected_revision=stale_revision, actor=None,
            )
        db.session.rollback()

    def test_inactive_historical_account_can_be_preserved_not_newly_selected(self):
        # Simulate a historical row whose account was archived later.
        self.inactive_bank.is_active = True
        db.session.commit()
        p, _ = save_client_payment(client_code=self.client.code, amount=10, method='Bank',
                                   payment_account_id=self.inactive_bank.id, actor=None)
        db.session.commit()
        self.inactive_bank.is_active = False
        db.session.commit()

        # Metadata/amount edit may preserve the exact historical relationship.
        save_client_payment(payment_id=p.id, client_code=self.client.code, amount=11,
                            method='Bank', payment_account_id=self.inactive_bank.id,
                            expected_revision=p.revision, note='corrected', actor=None)
        db.session.commit()
        self.assertEqual(p.payment_account_id, self.inactive_bank.id)

        with self.assertRaisesRegex(ValueError, 'deactivated'):
            save_client_payment(client_code=self.client.code, amount=1, method='Bank',
                                payment_account_id=self.inactive_bank.id, actor=None)
        db.session.rollback()

    def test_stable_client_identity_survives_master_rename(self):
        from app.services.finance_clients import _compute_client_financial_summary
        payment, _ = save_client_payment(client_code=self.client.code, amount='12446109.99',
                                         method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.commit()
        historical_name = payment.client_name
        self.client.name = 'Renamed Exact Client'
        db.session.commit()
        summary = _compute_client_financial_summary(self.client)
        self.assertEqual(payment.client_id, self.client.id)
        self.assertEqual(payment.client_name, historical_name)  # preserved historical snapshot
        self.assertAlmostEqual(summary['cash_received_total'], 12446109.99, places=2)

    def test_source_controlled_payments_cannot_be_mutated_independently(self):
        ret = MaterialReturn(client_name=self.client.name, amount=100, date_posted=datetime.now())
        db.session.add(ret)
        db.session.flush()
        p = Payment(client_id=self.client.id, client_name=self.client.name, amount=100,
                    method='Material Return', payment_type='Material Return',
                    source_type='MaterialReturn', source_id=ret.id,
                    note=f'[MATERIAL_RETURN:{ret.id}]')
        db.session.add(p)
        db.session.flush()
        ret.payment_id = p.id
        sp = SupplierPayment(supplier_id=self.supplier.id, amount=20, method='Cash',
                             source_type='GRN', source_id=77, note='[AUTO_GRN_PAY:77]')
        db.session.add(sp)
        db.session.commit()

        with self.assertRaisesRegex(ValueError, 'controlled by Material Return'):
            delete_client_payment(p, actor=None)
        with self.assertRaisesRegex(ValueError, 'controlled by GRN'):
            delete_supplier_payment(sp, actor=None)
        db.session.rollback()
        self.assertFalse(p.is_void)
        self.assertFalse(sp.is_void)

    def test_reconciliation_uses_opening_balance_and_complete_carry_chain(self):
        # An account with an opening 5,000 and no ledger rows must expect 5,000,
        # not zero (the previous implementation's production bug).
        self.assertAlmostEqual(ledger_balance(self.bank.id), 5000.0, places=2)
        yesterday = date.today() - timedelta(days=1)
        rec1 = reconcile_account(account_id=self.bank.id, actual_balance='4999.99',
                                 reconciliation_date=yesterday, note='statement close', actor=None)
        db.session.commit()
        self.assertEqual(rec1.difference_type, 'Loss')
        self.assertEqual(rec1.previous_balance_minor, 500000)
        self.assertEqual(rec1.expected_balance_minor, 500000)
        self.assertEqual(rec1.actual_balance_minor, 499999)
        self.assertEqual(rec1.final_reconciled_balance_minor, 499999)
        self.assertIsNotNone(rec1.adjustment_transaction_id)
        adj = db.session.get(AccountTransaction, rec1.adjustment_transaction_id)
        self.assertEqual(adj.reconciliation_id, rec1.id)
        self.assertIn(f'[RECON:{rec1.id}]', adj.note)
        self.assertEqual(adj.transaction_type, 'Reconciliation Loss')

        save_client_payment(client_code=self.client.code, amount='0.01', method='Bank',
                            payment_account_id=self.bank.id, date_posted=date.today().isoformat(), actor=None)
        db.session.commit()
        rec2 = reconcile_account(account_id=self.bank.id, actual_balance='5000.00',
                                 reconciliation_date=date.today(), note='next close', actor=None)
        db.session.commit()
        self.assertEqual(rec2.previous_reconciliation_id, rec1.id)
        self.assertEqual(rec2.opening_balance_minor, 499999)
        self.assertEqual(rec2.transaction_in_minor, 1)
        self.assertEqual(rec2.expected_balance_minor, 500000)
        self.assertEqual(rec2.difference_minor, 0)

        with self.assertRaisesRegex(ValueError, 'later reconciliation'):
            reconcile_account(account_id=self.bank.id, actual_balance=1,
                              reconciliation_date=yesterday - timedelta(days=1), actor=None)
        db.session.rollback()

    def test_structured_audit_is_atomic_and_contains_before_after(self):
        actor = User(username='auditor', role='admin', status='active')
        db.session.add(actor)
        db.session.commit()
        p, _ = save_supplier_payment(supplier_id=self.supplier.id, amount=100,
                                     method='Bank', payment_account_id=self.bank.id,
                                     note='invoice 42', actor=actor)
        db.session.commit()
        save_supplier_payment(payment_id=p.id, supplier_id=self.supplier.id, amount=120,
                              method='Bank', payment_account_id=self.bank.id,
                              expected_revision=p.revision, note='corrected invoice 42', actor=actor)
        db.session.commit()
        delete_supplier_payment(p, actor=actor)
        db.session.commit()

        rows = AccountingAuditLog.query.filter_by(entity_type='SupplierPayment', entity_id=p.id).order_by(
            AccountingAuditLog.created_at.asc(), AccountingAuditLog.id.asc()).all()
        self.assertEqual({r.action for r in rows}, {'Create', 'Edit', 'Delete'})
        edit = next(r for r in rows if r.action == 'Edit')
        self.assertIn('"amount":100.0', edit.before_json)
        self.assertIn('"amount":120.0', edit.after_json)
        self.assertEqual(edit.username, 'auditor')
        self.assertEqual(edit.amount_before_minor, 10000)
        self.assertEqual(edit.amount_after_minor, 12000)


class AccountsSecurityHttpTest(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['TESTING'] = True
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.user = User(username='cashier', role='user', status='active', can_manage_payments=True)
        self.cash = Account(name='Secure Cash', category='cash', source_category='Company',
                            account_type='company', balance=0, is_active=True)
        self.party = Client(code='C-SEC', name='Secure Client', is_active=True)
        db.session.add_all([self.user, self.cash, self.party])
        db.session.commit()
        self.http = self.app.test_client()
        with self.http.session_transaction() as sess:
            sess['_user_id'] = str(self.user.id)
            sess['_fresh'] = True
            sess['_csrf_token'] = 'known-csrf-token'

    def tearDown(self):
        self.app.config['TESTING'] = True
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_accounts_csrf_and_master_permission_enforcement(self):
        self.app.config['TESTING'] = False
        self.app.config['WTF_CSRF_ENABLED'] = True
        rejected = self.http.post('/accounts/payments/clients/save', data={
            'client_code': self.party.code, 'amount': '1.00', 'method': 'Cash',
            'payment_account_id': str(self.cash.id),
        })
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(Payment.query.count(), 0)

        accepted = self.http.post('/accounts/payments/clients/save', data={
            '_csrf_token': 'known-csrf-token', 'client_code': self.party.code,
            'client_name': self.party.name, 'amount': '1.00', 'payment_type': 'Receipt',
            'method': 'Cash', 'payment_account_id': str(self.cash.id),
        })
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(Payment.query.count(), 1)

        # Payment managers can transact, but account-master deletion is admin/root only.
        forbidden = self.http.post(f'/accounts/{self.cash.id}/delete', data={
            '_csrf_token': 'known-csrf-token',
        })
        self.assertEqual(forbidden.status_code, 403)
        self.assertIsNotNone(db.session.get(Account, self.cash.id))


if __name__ == '__main__':
    unittest.main()
