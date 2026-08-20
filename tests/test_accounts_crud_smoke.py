"""Accounts section smoke tests: client & supplier payment CRUD, suspended-entity
filtering, and account reconciliation.

These tests exercise the shared business logic (app.services.payments_crud) and
the HTTP routes together so the full dependency chain (Payment/SupplierPayment ->
AccountTransaction -> Account.balance -> reconciliation) is verified.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime

os.environ['APP_DB_PATH'] = os.path.join(tempfile.gettempdir(), 'ams_accounts_crud_test.db')
os.environ['ALLOW_EMPTY_DB'] = '1'
os.environ['ALLOW_DB_DROP'] = '1'

from app import create_app
from models import db

app = create_app({"TESTING": True})

from models import (
    Account,
    AccountReconciliation,
    AccountTransaction,
    Client,
    Payment,
    Supplier,
    SupplierPayment,
    User,
)
from app.services.payments_crud import (
    delete_client_payment,
    delete_supplier_payment,
    ledger_balance,
    reconcile_account,
    save_client_payment,
    save_supplier_payment,
)


class AccountsCrudTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        self.cash = Account(name='Petty Cash', category='cash', source_category='Company',
                            account_type='company', balance=0, is_active=True)
        self.bank1 = Account(name='Bank One', category='bank', source_category='Company',
                             account_type='company', balance=5000, is_active=True)
        self.bank2 = Account(name='Bank Two', category='bank', source_category='Company',
                             account_type='company', balance=3000, is_active=True)
        self.suspended_account = Account(name='Old Vault', category='cash', source_category='Company',
                                         account_type='company', balance=0, is_active=False)
        db.session.add_all([self.cash, self.bank1, self.bank2, self.suspended_account])
        db.session.flush()

        self.client = Client(code='C-1001', name='Active Client', is_active=True)
        self.client_suspended = Client(code='C-1002', name='Suspended Client', is_active=False)
        self.supplier = Supplier(name='Active Supplier', is_active=True)
        self.supplier_suspended = Supplier(name='Suspended Supplier', is_active=False)
        db.session.add_all([self.client, self.client_suspended, self.supplier, self.supplier_suspended])
        db.session.commit()

    # ------------------------------ client payments ------------------------------ #
    def test_client_payment_create_edit_delete_recalculates_balance(self):
        p, created = save_client_payment(client_name=self.client.name, amount=1000,
                                         method='Cash', payment_account_id=self.cash.id, actor=None)
        self.assertTrue(created)
        db.session.commit()
        pid = p.id
        self.assertAlmostEqual(self.cash.balance, 1000.0, places=2)

        # Edit must keep the same transaction identity and recalc the balance.
        p2, created2 = save_client_payment(payment_id=pid, client_name=self.client.name,
                                           amount=1500, method='Cash',
                                           payment_account_id=self.cash.id, actor=None)
        self.assertFalse(created2)
        self.assertEqual(p2.id, pid)
        db.session.commit()
        self.assertAlmostEqual(self.cash.balance, 1500.0, places=2)
        receipt_rows = AccountTransaction.query.filter(
            AccountTransaction.transaction_type == 'Receipt',
            AccountTransaction.note.ilike(f'%[SRC:Payment:{pid}]%'),
            AccountTransaction.is_void == False,
        ).all()
        self.assertEqual(len(receipt_rows), 1)
        self.assertAlmostEqual(receipt_rows[0].amount, 1500.0, places=2)

        # Delete must reverse the effect.
        payment = db.session.get(Payment, pid)
        self.assertTrue(delete_client_payment(payment, actor=None))
        db.session.commit()
        self.assertAlmostEqual(self.cash.balance, 0.0, places=2)
        self.assertTrue(payment.is_void)
        active_receipts = AccountTransaction.query.filter(
            AccountTransaction.transaction_type == 'Receipt',
            AccountTransaction.note.ilike(f'%[SRC:Payment:{pid}]%'),
            AccountTransaction.is_void == False,
        ).count()
        self.assertEqual(active_receipts, 0)

    def test_client_payment_rejects_suspended_client(self):
        with self.assertRaises(ValueError):
            save_client_payment(client_name=self.client_suspended.name, amount=100,
                                method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.rollback()

    def test_client_payment_rejects_suspended_account(self):
        with self.assertRaises(ValueError):
            save_client_payment(client_name=self.client.name, amount=100,
                                method='Cash', payment_account_id=self.suspended_account.id, actor=None)
        db.session.rollback()

    def test_client_payment_rejects_mismatched_account_method(self):
        with self.assertRaises(ValueError):
            save_client_payment(client_name=self.client.name, amount=100,
                                method='Bank', payment_account_id=self.cash.id, actor=None)
        db.session.rollback()

    # ------------------------------ supplier payments ------------------------------ #
    def test_supplier_payment_create_edit_account_delete_recalculates(self):
        p, created = save_supplier_payment(supplier_id=self.supplier.id, amount=800,
                                           method='Bank', payment_account_id=self.bank1.id, actor=None)
        self.assertTrue(created)
        db.session.commit()
        pid = p.id
        self.assertAlmostEqual(self.bank1.balance, 4200.0, places=2)

        # Edit amount (same account) keeps identity, updates account balance.
        p2, created2 = save_supplier_payment(payment_id=pid, supplier_id=self.supplier.id,
                                             amount=1000, method='Bank',
                                             payment_account_id=self.bank1.id, actor=None)
        self.assertFalse(created2)
        self.assertEqual(p2.id, pid)
        db.session.commit()
        self.assertAlmostEqual(self.bank1.balance, 4000.0, places=2)

        # Edit account: reverses old account, applies to new account.
        save_supplier_payment(payment_id=pid, supplier_id=self.supplier.id, amount=1000,
                              method='Bank', payment_account_id=self.bank2.id, actor=None)
        db.session.commit()
        self.assertAlmostEqual(self.bank1.balance, 5000.0, places=2)
        self.assertAlmostEqual(self.bank2.balance, 2000.0, places=2)
        active_tx = AccountTransaction.query.filter(
            AccountTransaction.transaction_type == 'Supplier Payment',
            AccountTransaction.note.ilike(f'%[SRC:SupplierPayment:{pid}]%'),
            AccountTransaction.is_void == False,
        ).count()
        self.assertEqual(active_tx, 1)

        # Delete reverses the accounting effect.
        payment = db.session.get(SupplierPayment, pid)
        self.assertTrue(delete_supplier_payment(payment, actor=None))
        db.session.commit()
        self.assertAlmostEqual(self.bank2.balance, 3000.0, places=2)
        self.assertTrue(payment.is_void)

    def test_supplier_payment_rejects_suspended_supplier(self):
        with self.assertRaises(ValueError):
            save_supplier_payment(supplier_id=self.supplier_suspended.id, amount=100,
                                  method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.rollback()

    def test_supplier_payment_rejects_insufficient_balance(self):
        with self.assertRaises(ValueError):
            save_supplier_payment(supplier_id=self.supplier.id, amount=999999,
                                  method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.rollback()

    # ------------------------------ reconciliation ------------------------------ #
    def test_reconciliation_matched_shortage_excess(self):
        # Seed two receipts so the ledger has a known expected balance.
        save_client_payment(client_name=self.client.name, amount=1000,
                            method='Cash', payment_account_id=self.cash.id, actor=None)
        save_client_payment(client_name=self.client.name, amount=500,
                            method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.commit()
        self.assertAlmostEqual(ledger_balance(self.cash.id), 1500.0, places=2)

        # Matched: actual == expected -> no adjustment.
        rec = reconcile_account(account_id=self.cash.id, actual_balance=1500, note='counted', actor=None)
        db.session.commit()
        self.assertEqual(rec.difference_type, 'Matched')
        self.assertAlmostEqual(self.cash.balance, 1500.0, places=2)
        self.assertAlmostEqual(ledger_balance(self.cash.id), 1500.0, places=2)

        # Shortage: actual lower -> Loss (balance reduced).
        rec = reconcile_account(account_id=self.cash.id, actual_balance=1400, note='short', actor=None)
        db.session.commit()
        self.assertEqual(rec.difference_type, 'Loss')
        self.assertAlmostEqual(rec.difference, -100.0, places=2)
        self.assertAlmostEqual(self.cash.balance, 1400.0, places=2)
        # The adjustment is transparent in the ledger and balances the books.
        self.assertAlmostEqual(ledger_balance(self.cash.id), 1400.0, places=2)

        # Excess: actual higher -> Excess (balance increased).
        rec = reconcile_account(account_id=self.cash.id, actual_balance=1600, note='excess', actor=None)
        db.session.commit()
        self.assertEqual(rec.difference_type, 'Excess')
        self.assertAlmostEqual(rec.difference, 200.0, places=2)
        self.assertAlmostEqual(self.cash.balance, 1600.0, places=2)
        self.assertAlmostEqual(ledger_balance(self.cash.id), 1600.0, places=2)

        # History preserved.
        self.assertGreaterEqual(AccountReconciliation.query.filter_by(account_id=self.cash.id).count(), 3)

    def test_reconciliation_carries_forward_as_next_opening(self):
        save_client_payment(client_name=self.client.name, amount=900,
                            method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.commit()
        rec = reconcile_account(account_id=self.cash.id, actual_balance=800, note='short 100', actor=None)
        db.session.commit()
        # Account balance now equals the reconciled actual value, so a later
        # transaction starts from the corrected opening balance.
        self.assertAlmostEqual(self.cash.balance, 800.0, places=2)
        save_client_payment(client_name=self.client.name, amount=200,
                            method='Cash', payment_account_id=self.cash.id, actor=None)
        db.session.commit()
        self.assertAlmostEqual(self.cash.balance, 1000.0, places=2)


class AccountsHttpSmokeTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
        self.client.post('/login', data={'username': 'Admin', 'password': 'Admin@fbm12345'})

        self.cash = Account(name='HTTP Cash', category='cash', source_category='Company',
                            account_type='company', balance=0, is_active=True)
        self.bank = Account(name='HTTP Bank', category='bank', source_category='Company',
                            account_type='company', balance=1000, is_active=True)
        self.supplier = Supplier(name='HTTP Supplier', is_active=True)
        db.session.add_all([self.cash, self.bank, self.supplier])
        db.session.commit()
        self.client_obj = Client.query.filter_by(is_active=True).first()
        if not self.client_obj:
            self.client_obj = Client(code='C-HTTP', name='HTTP Client', is_active=True)
            db.session.add(self.client_obj)
            db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_client_payment_pages_and_save_flow(self):
        rv = self.client.get('/accounts/payments/clients')
        self.assertEqual(rv.status_code, 200)
        self.assertIn(b'New Payment', rv.data)

        # Create
        rv = self.client.post('/accounts/payments/clients/save', data={
            'client_code': self.client_obj.code,
            'client_name': self.client_obj.name,
            'amount': '777',
            'method': 'Cash',
            'payment_account_id': str(self.cash.id),
            'date': date.today().strftime('%Y-%m-%d'),
        }, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        pay = Payment.query.filter_by(amount=777).first()
        self.assertIsNotNone(pay)
        self.assertAlmostEqual(self.cash.balance, 777.0, places=2)

        # Edit through the same route
        rv = self.client.post('/accounts/payments/clients/save', data={
            'payment_id': str(pay.id),
            'client_code': self.client_obj.code,
            'client_name': self.client_obj.name,
            'amount': '888',
            'method': 'Cash',
            'payment_account_id': str(self.cash.id),
            'date': date.today().strftime('%Y-%m-%d'),
        }, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(Payment.query.count(), 1)  # no duplicate
        self.assertAlmostEqual(self.cash.balance, 888.0, places=2)

        # Delete
        rv = self.client.post(f'/accounts/payments/clients/void/{pay.id}', follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        self.assertAlmostEqual(self.cash.balance, 0.0, places=2)

    def test_supplier_payment_pages_and_save_flow(self):
        rv = self.client.get('/accounts/payments/suppliers')
        self.assertEqual(rv.status_code, 200)
        self.assertIn(b'New Payment', rv.data)

        rv = self.client.post('/accounts/payments/suppliers/save', data={
            'supplier_id': str(self.supplier.id),
            'amount': '600',
            'method': 'Bank',
            'payment_account_id': str(self.bank.id),
            'date': date.today().strftime('%Y-%m-%d'),
        }, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        sp = SupplierPayment.query.filter_by(amount=600).first()
        self.assertIsNotNone(sp)
        self.assertAlmostEqual(self.bank.balance, 400.0, places=2)

        # Edit through the same route
        rv = self.client.post('/accounts/payments/suppliers/save', data={
            'payment_id': str(sp.id),
            'supplier_id': str(self.supplier.id),
            'amount': '700',
            'method': 'Bank',
            'payment_account_id': str(self.bank.id),
            'date': date.today().strftime('%Y-%m-%d'),
        }, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(SupplierPayment.query.count(), 1)
        self.assertAlmostEqual(self.bank.balance, 300.0, places=2)

        # Delete
        rv = self.client.post(f'/accounts/payments/suppliers/{sp.id}/delete', follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        self.assertAlmostEqual(self.bank.balance, 1000.0, places=2)

    def test_reconcile_pages_render_and_post(self):
        rv = self.client.get(f'/accounts/{self.cash.id}/reconcile')
        self.assertEqual(rv.status_code, 200)
        rv = self.client.get('/accounts/reconciliations')
        self.assertEqual(rv.status_code, 200)

        rv = self.client.post(f'/accounts/{self.cash.id}/reconcile', data={
            'actual_balance': '123.45',
            'reconciliation_date': date.today().strftime('%Y-%m-%d'),
            'note': 'http smoke',
        }, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        rec = AccountReconciliation.query.filter_by(account_id=self.cash.id).first()
        self.assertIsNotNone(rec)
        self.assertAlmostEqual(self.cash.balance, 123.45, places=2)


if __name__ == '__main__':
    unittest.main()
