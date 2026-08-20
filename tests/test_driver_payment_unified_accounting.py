"""Cross-section consistency tests for unified driver/delivery-person payments.

Covers the acceptance scenarios: one authoritative transaction per payment,
identical effects from both entry points, accurate reversal, safe editing, and
duplicate-submission protection.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import create_app
from app.services.driver_payments import (
    delete_driver_payment,
    driver_outstanding,
    reconcile_driver_payments,
    restore_driver_payment,
    save_driver_payment,
    settle_driver_fifo,
)
from app.services.financial_ledgers import build_delivery_person_financial_ledger
from app.services.payments_crud import ledger_balance, save_supplier_payment
from models import (
    Account,
    AccountTransaction,
    AccountingAuditLog,
    Client,
    DeliveryPerson,
    DeliveryPersonPayment,
    DirectSale,
    SaleDeliveryPerson,
    Supplier,
    db,
)


@pytest.fixture()
def driver_app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'driver.db'}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "driver-test",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    with app.app_context():
        yield app
        db.session.remove()


def _seed(*, cash_balance=100000.0, rent=0.0, opening=0.0):
    """Cash account + driver, optionally with one delivery rent allocation."""
    cash = Account(name="FBM CASH IN HAND", category="cash", source_category="Company",
                   account_type="company", balance=cash_balance, opening_balance=cash_balance,
                   is_active=True)
    mcb = Account(name="FBM MCB", category="bank", source_category="Company",
                  account_type="company", balance=50000, opening_balance=50000, is_active=True)
    driver = DeliveryPerson(name="Ali", opening_balance=opening,
                            opening_balance_date=datetime(2026, 8, 1), is_active=True)
    db.session.add_all([cash, mcb, driver])
    db.session.flush()

    alloc = None
    if rent:
        client = Client(code="C-1", name="Test Client", is_active=True)
        db.session.add(client)
        db.session.flush()
        sale = DirectSale(client_name=client.name, client_code=client.code, manual_bill_no="9001",
                          date_posted=datetime(2026, 8, 2), is_void=False)
        db.session.add(sale)
        db.session.flush()
        alloc = SaleDeliveryPerson(sale_id=sale.id, delivery_person_id=driver.id,
                                   rent_amount=rent, created_at=datetime(2026, 8, 2), is_void=False)
        db.session.add(alloc)
        db.session.flush()
    db.session.commit()
    return cash, mcb, driver, alloc


def _driver_payment_txs(payment_id, *, active_only=True):
    q = AccountTransaction.query.filter(
        AccountTransaction.source_type == "DeliveryPersonPayment",
        AccountTransaction.source_id == payment_id,
        AccountTransaction.transaction_type == "Driver Payment",
    )
    if active_only:
        q = q.filter(AccountTransaction.is_void == False)  # noqa: E712
    return q.all()


# --------------------------------------------------------------------------- #
# §27 cross-section consistency
# --------------------------------------------------------------------------- #
def test_cross_section_consistency_scenario(driver_app):
    """Cash 100,000 / driver owed 10,000 opening; earns 5,000; paid 3,000."""
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=5000, opening=10000)
        assert driver_outstanding(driver.id) == pytest.approx(15000.0)

        payment, created = save_driver_payment(
            delivery_person_id=driver.id, amount_paid=3000, method="Cash",
            payment_account_id=cash.id, date_posted="2026-08-03", note="Delivery service payment",
        )
        db.session.commit()
        assert created

        # Exactly ONE authoritative financial transaction.
        txs = _driver_payment_txs(payment.id)
        assert len(txs) == 1
        assert txs[0].from_account_id == cash.id
        assert txs[0].to_account_id is None
        assert txs[0].amount == pytest.approx(3000.0)

        # Account balance: stored and calculated agree.
        assert cash.balance == pytest.approx(97000.0)
        assert ledger_balance(cash.id) == pytest.approx(97000.0)

        # Driver outstanding reduced by the same 3,000.
        assert driver_outstanding(driver.id) == pytest.approx(12000.0)

        # The same payment is visible from the driver ledger…
        ledger = build_delivery_person_financial_ledger(driver)
        rows = [r for r in ledger["rows"] if r["source_type"] == "DeliveryPersonPayment"]
        assert len(rows) == 1
        assert rows[0]["credit"] == pytest.approx(3000.0)
        assert rows[0]["account"] == "FBM CASH IN HAND"
        assert ledger["closing_balance"] == pytest.approx(12000.0)

        # …and from the audit trail.
        assert AccountingAuditLog.query.filter_by(
            entity_type="DeliveryPersonPayment", entity_id=payment.id, action="Create"
        ).count() == 1

        assert reconcile_driver_payments()["ok"]


# --------------------------------------------------------------------------- #
# §28 / §29 both entry points produce identical effects
# --------------------------------------------------------------------------- #
def test_payment_from_accounts_and_from_driver_section_are_identical(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=20000)

        # "Accounts section" path — FIFO settlement service.
        rows = settle_driver_fifo(delivery_person_id=driver.id, amount_paid=4000,
                                  method="Cash", payment_account_id=cash.id,
                                  note="From Accounts")
        db.session.commit()
        accounts_state = (cash.balance, driver_outstanding(driver.id))
        assert accounts_state[0] == pytest.approx(96000.0)
        assert accounts_state[1] == pytest.approx(16000.0)
        accounts_tx = [t for r in rows for t in _driver_payment_txs(r.id)]
        assert len(accounts_tx) == 1

        # "Driver section" path — direct save on the same core.
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=4000,
                                         method="Cash", payment_account_id=cash.id,
                                         note="From Driver section")
        db.session.commit()
        assert cash.balance == pytest.approx(92000.0)
        assert driver_outstanding(driver.id) == pytest.approx(12000.0)

        driver_tx = _driver_payment_txs(payment.id)
        assert len(driver_tx) == 1

        # Only the entry point differed: the accounting shape is the same.
        a, b = accounts_tx[0], driver_tx[0]
        assert (a.transaction_type, a.from_account_id, a.to_account_id, a.amount) == \
               (b.transaction_type, b.from_account_id, b.to_account_id, b.amount)
        assert ledger_balance(cash.id) == pytest.approx(92000.0)
        assert reconcile_driver_payments()["ok"]


def test_explicit_account_is_respected_not_defaulted_to_cash(driver_app):
    with driver_app.app_context():
        cash, mcb, driver, _alloc = _seed(cash_balance=100000, rent=10000)

        save_driver_payment(delivery_person_id=driver.id, amount_paid=5000, method="Bank",
                            payment_account_id=mcb.id)
        db.session.commit()

        assert mcb.balance == pytest.approx(45000.0)
        assert cash.balance == pytest.approx(100000.0)


def test_method_and_account_category_must_agree(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=10000)
        with pytest.raises(ValueError):
            save_driver_payment(delivery_person_id=driver.id, amount_paid=1000,
                                method="Bank", payment_account_id=cash.id)
        db.session.rollback()


def test_payment_requires_an_explicit_account(driver_app):
    with driver_app.app_context():
        _cash, _mcb, driver, _alloc = _seed(rent=10000)
        with pytest.raises(ValueError):
            save_driver_payment(delivery_person_id=driver.id, amount_paid=1000,
                                method="Cash", payment_account_id=None)
        db.session.rollback()


# --------------------------------------------------------------------------- #
# §12 insufficient balance / overpayment
# --------------------------------------------------------------------------- #
def test_insufficient_account_balance_is_rejected(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=3000, rent=10000)
        with pytest.raises(ValueError, match="Insufficient balance"):
            save_driver_payment(delivery_person_id=driver.id, amount_paid=5000,
                                method="Cash", payment_account_id=cash.id)
        db.session.rollback()
        assert cash.balance == pytest.approx(3000.0)
        assert AccountTransaction.query.count() == 0


def test_payment_cannot_exceed_driver_outstanding(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=2000)
        with pytest.raises(ValueError, match="exceeds"):
            save_driver_payment(delivery_person_id=driver.id, amount_paid=5000,
                                method="Cash", payment_account_id=cash.id)
        db.session.rollback()
        assert cash.balance == pytest.approx(100000.0)


# --------------------------------------------------------------------------- #
# §30 cancellation / reversal
# --------------------------------------------------------------------------- #
def test_reversal_restores_account_and_driver_balance_exactly_once(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=10000)
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=5000,
                                         method="Cash", payment_account_id=cash.id)
        db.session.commit()
        assert cash.balance == pytest.approx(95000.0)

        assert delete_driver_payment(payment) is True
        db.session.commit()

        assert cash.balance == pytest.approx(100000.0)
        assert ledger_balance(cash.id) == pytest.approx(100000.0)
        assert driver_outstanding(driver.id) == pytest.approx(10000.0)
        assert _driver_payment_txs(payment.id) == []

        # Original record remains auditable, not deleted.
        assert db.session.get(DeliveryPersonPayment, payment.id) is not None
        assert payment.is_void is True
        assert _driver_payment_txs(payment.id, active_only=False)

        # A second reversal must not double-refund.
        assert delete_driver_payment(payment) is False
        db.session.commit()
        assert cash.balance == pytest.approx(100000.0)

        # Restore re-applies the effect exactly once.
        assert restore_driver_payment(payment) is True
        db.session.commit()
        assert cash.balance == pytest.approx(95000.0)
        assert len(_driver_payment_txs(payment.id)) == 1
        assert reconcile_driver_payments()["ok"]


# --------------------------------------------------------------------------- #
# §14 editing may not double-count
# --------------------------------------------------------------------------- #
def test_edit_applies_only_the_delta(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=20000)
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=5000,
                                         method="Cash", payment_account_id=cash.id)
        db.session.commit()
        assert cash.balance == pytest.approx(95000.0)

        save_driver_payment(payment_id=payment.id, delivery_person_id=driver.id,
                            amount_paid=8000, method="Cash", payment_account_id=cash.id)
        db.session.commit()

        # Final effect is -8,000 in total, never -5,000 AND -8,000.
        assert cash.balance == pytest.approx(92000.0)
        assert ledger_balance(cash.id) == pytest.approx(92000.0)
        assert len(_driver_payment_txs(payment.id)) == 1
        assert driver_outstanding(driver.id) == pytest.approx(12000.0)
        assert reconcile_driver_payments()["ok"]


def test_edit_moving_account_moves_the_money(driver_app):
    with driver_app.app_context():
        cash, mcb, driver, _alloc = _seed(cash_balance=100000, rent=20000)
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=5000,
                                         method="Cash", payment_account_id=cash.id)
        db.session.commit()

        save_driver_payment(payment_id=payment.id, delivery_person_id=driver.id,
                            amount_paid=5000, method="Bank", payment_account_id=mcb.id)
        db.session.commit()

        assert cash.balance == pytest.approx(100000.0)
        assert mcb.balance == pytest.approx(45000.0)
        assert len(_driver_payment_txs(payment.id)) == 1


def test_stale_revision_is_rejected(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=20000)
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=5000,
                                         method="Cash", payment_account_id=cash.id)
        db.session.commit()
        with pytest.raises(ValueError, match="another session"):
            save_driver_payment(payment_id=payment.id, delivery_person_id=driver.id,
                                amount_paid=6000, method="Cash", payment_account_id=cash.id,
                                expected_revision=99)
        db.session.rollback()


# --------------------------------------------------------------------------- #
# §31 duplicate submission
# --------------------------------------------------------------------------- #
def test_duplicate_submission_creates_one_payment(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=20000)

        first, created_first = save_driver_payment(
            delivery_person_id=driver.id, amount_paid=3000, method="Cash",
            payment_account_id=cash.id, idempotency_key="submit-abc",
        )
        db.session.commit()
        second, created_second = save_driver_payment(
            delivery_person_id=driver.id, amount_paid=3000, method="Cash",
            payment_account_id=cash.id, idempotency_key="submit-abc",
        )
        db.session.commit()

        assert created_first is True and created_second is False
        assert first.id == second.id
        assert DeliveryPersonPayment.query.filter_by(is_void=False).count() == 1
        assert len(_driver_payment_txs(first.id)) == 1
        assert cash.balance == pytest.approx(97000.0)
        assert driver_outstanding(driver.id) == pytest.approx(17000.0)


def test_duplicate_fifo_submission_creates_one_settlement(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=20000)
        settle_driver_fifo(delivery_person_id=driver.id, amount_paid=4000, method="Cash",
                           payment_account_id=cash.id, idempotency_key="fifo-1")
        db.session.commit()
        settle_driver_fifo(delivery_person_id=driver.id, amount_paid=4000, method="Cash",
                           payment_account_id=cash.id, idempotency_key="fifo-1")
        db.session.commit()

        assert DeliveryPersonPayment.query.filter_by(is_void=False).count() == 1
        assert cash.balance == pytest.approx(96000.0)


# --------------------------------------------------------------------------- #
# waive-off: driver payable falls, no cash moves
# --------------------------------------------------------------------------- #
def test_waive_off_does_not_touch_any_account(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=10000)
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=0,
                                         waive_off_amount=2500, method="Cash")
        db.session.commit()

        assert cash.balance == pytest.approx(100000.0)
        assert driver_outstanding(driver.id) == pytest.approx(7500.0)
        assert _driver_payment_txs(payment.id) == []
        assert AccountTransaction.query.filter(
            AccountTransaction.transaction_type == "Loss",
            AccountTransaction.source_type == "DeliveryPersonPayment",
            AccountTransaction.is_void == False,  # noqa: E712
        ).count() == 1
        assert reconcile_driver_payments()["ok"]


# --------------------------------------------------------------------------- #
# §26 reconciliation of legacy data
# --------------------------------------------------------------------------- #
def test_reconciliation_flags_legacy_unlinked_payment(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, alloc = _seed(cash_balance=100000, rent=10000)
        # Simulate a pre-upgrade row: driver ledger moved, cash never did.
        legacy = DeliveryPersonPayment(delivery_person_id=driver.id, sale_id=alloc.sale_id,
                                       allocation_id=alloc.id, amount_paid=2000,
                                       waive_off_amount=0, date_posted=datetime(2026, 8, 1),
                                       is_void=False)
        db.session.add(legacy)
        db.session.commit()

        report = reconcile_driver_payments()
        assert report["ok"] is False
        assert report["legacy_unlinked_payments"] == 1
        kinds = {i["kind"] for i in report["issues"]}
        assert "driver_payment_missing_ledger_row" in kinds
        # Reporting only: the legacy row and balances are left untouched.
        assert cash.balance == pytest.approx(100000.0)
        assert db.session.get(DeliveryPersonPayment, legacy.id) is not None


# --------------------------------------------------------------------------- #
# §32 no regression for other payment types
# --------------------------------------------------------------------------- #
def test_supplier_payment_still_works_alongside_driver_payment(driver_app):
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=10000)
        supplier = Supplier(name="Steel Co", is_active=True)
        db.session.add(supplier)
        db.session.commit()

        save_supplier_payment(supplier_id=supplier.id, amount=7000, method="Cash",
                              payment_account_id=cash.id)
        db.session.commit()
        assert cash.balance == pytest.approx(93000.0)

        save_driver_payment(delivery_person_id=driver.id, amount_paid=3000, method="Cash",
                            payment_account_id=cash.id)
        db.session.commit()

        assert cash.balance == pytest.approx(90000.0)
        assert ledger_balance(cash.id) == pytest.approx(90000.0)
        assert AccountTransaction.query.filter_by(
            transaction_type="Supplier Payment", is_void=False).count() == 1
        assert AccountTransaction.query.filter_by(
            transaction_type="Driver Payment", is_void=False).count() == 1


def test_wipe_removes_driver_payment_with_its_ledger_row(driver_app):
    """Deleting driver payments must not leave an orphan money-out entry."""
    with driver_app.app_context():
        cash, _mcb, driver, _alloc = _seed(cash_balance=100000, rent=10000)
        payment, _ = save_driver_payment(delivery_person_id=driver.id, amount_paid=4000,
                                         method="Cash", payment_account_id=cash.id)
        db.session.commit()
        assert cash.balance == pytest.approx(96000.0)

        from app.services.accounting import _void_account_tx
        linked = AccountTransaction.query.filter(
            AccountTransaction.source_type == "DeliveryPersonPayment",
            AccountTransaction.source_id == payment.id,
        ).all()
        assert linked
        for tx in linked:
            _void_account_tx(tx)
            db.session.delete(tx)
        db.session.delete(payment)
        db.session.commit()

        # Balance restored, nothing orphaned.
        assert cash.balance == pytest.approx(100000.0)
        assert ledger_balance(cash.id) == pytest.approx(100000.0)
        assert AccountTransaction.query.filter_by(
            source_type="DeliveryPersonPayment").count() == 0
