"""Focused tests for the redesigned Account Create / Edit section.

Covers:
* Create for every major category (cash, bank, wallet, client receivable,
  supplier payable, own funds, loan, external/clearing).
* Server-side rejection of invalid classification combinations.
* Edit preloads existing values; classification changes clear stale channel
  detail data.
* Balance adjustment: decrease -> negative ledger entry; increase -> positive;
  desired == current -> no entry; reason required; idempotency on retry.
* Data integrity: existing account / transaction counts are unchanged by the
  classification schema bootstrap.
"""
from __future__ import annotations

import pytest

from models import db, Account, AccountTransaction, Client, Supplier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def create_account(client, **overrides):
    """POST the redesigned Create form. Returns the follow-redirect response."""
    base = {
        "name": "Test Account",
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": "0",
        "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
        "_csrf_token": csrf_token(client),
    }
    base.update(overrides)
    return client.post("/accounts/accounts/add", data=base, follow_redirects=True)


def edit_account(client, aid, **overrides):
    base = {
        "name": "Test Account",
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "channel": "cash",
        "account_status": "active",
        "_csrf_token": csrf_token(client),
    }
    base.update(overrides)
    return client.post(f"/accounts/{aid}/edit", data=base, follow_redirects=True)


def first_account_id(client):
    """Return the id of the most recently created account."""
    resp = client.get("/accounts/accounts")
    # Parse the edit link from the rendered table.
    body = resp.get_data(as_text=True)
    import re

    m = re.search(r"/accounts/(\d+)/edit", body)
    assert m, "no account found on manage page"
    return int(m.group(1))


def last_account(app):
    with app.app_context():
        return db.session.get(Account, db.session.query(db.func.max(Account.id)).scalar())


# ---------------------------------------------------------------------------
# Registry / validation unit tests
# ---------------------------------------------------------------------------
def test_registry_blocks_invalid_combination():
    from blueprints.accounts import classification as cls

    assert cls.is_valid_triple("Assets", "Cash", "Main Cash")
    # Expense category has no "Bank" subcategory.
    assert not cls.is_valid_triple("Expense", "Bank", "Operating Bank")
    assert not cls.is_valid_triple("Assets", "Bank", "Client Ledger")
    assert not cls.is_valid_triple("Liabilities", "Supplier Payables", "Main Cash")


def test_registry_forces_channel_and_entity():
    from blueprints.accounts import classification as cls

    # Assets -> Cash forces channel 'cash' and needs no entity.
    assert cls.allowed_channels("Assets", "Cash", "Main Cash") == ["cash"]
    assert cls.required_entity("Assets", "Cash", "Main Cash") == "none"
    # Client receivable forces ledger_only + client.
    assert cls.allowed_channels("Assets", "Client Receivables", "Client Ledger") == ["ledger_only"]
    assert cls.required_entity("Assets", "Client Receivables", "Client Ledger") == "client"
    # Supplier payable forces ledger_only + supplier.
    assert cls.required_entity("Liabilities", "Supplier Payables", "Supplier Ledger") == "supplier"
    # Owner capital allows cash/bank/ledger selection.
    assert set(cls.allowed_channels("Equity / Own Funds", "Owner Capital", "Owner Capital")) >= {
        "ledger_only",
        "cash",
    }


def test_validate_account_form_rejects_invalid_triple(app):
    from blueprints.accounts.account_form import validate_account_form

    class Form:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=""):
            return self._d.get(k, default)

    bad = Form(
        {
            "name": "X",
            "class_category": "Expenses",
            "class_subcategory": "Cash",
            "class_account_type": "Main Cash",
            "account_status": "active",
        }
    )
    with app.app_context():
        with pytest.raises(ValueError):
            validate_account_form(bad, is_edit=False)


# ---------------------------------------------------------------------------
# Create form tests
# ---------------------------------------------------------------------------
def test_create_cash_account(client, app):
    login(client)
    resp = create_account(client, name="FBM Cash in Hand", opening_amount="50000")
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.name == "FBM Cash in Hand"
    assert acc.class_category == "Assets"
    assert acc.class_subcategory == "Cash"
    assert acc.class_account_type == "Main Cash"
    assert acc.channel == "cash"
    assert acc.account_status == "active"
    # Legacy columns kept in sync for backward compatibility.
    assert acc.category == "cash"
    assert acc.source_category == "Company"
    assert acc.account_type == "company"
    # Opening position stored as the auditable baseline (debit = positive).
    assert acc.opening_balance == 50000.0
    assert acc.balance == 50000.0


def test_create_bank_account_validates_required_fields(client, app):
    login(client)
    # Missing bank number -> rejected.
    resp = create_account(
        client,
        name="FBM MCB",
        class_subcategory="Bank",
        class_account_type="Operating Bank",
        channel="bank",
        bank_name="MCB",
        account_holder_name="FBM",
    )
    assert b"required for a bank account" in resp.data
    # Complete bank details -> created.
    resp = create_account(
        client,
        name="FBM MCB",
        class_subcategory="Bank",
        class_account_type="Operating Bank",
        channel="bank",
        bank_name="MCB",
        account_holder_name="FBM",
        account_number="0123-4567890-01",
        branch_code="Main",
    )
    acc = last_account(app)
    assert acc.channel == "bank"
    assert acc.bank_name == "MCB"
    assert acc.account_number == "0123-4567890-01"
    assert acc.category == "bank"


def test_create_digital_wallet(client, app):
    login(client)
    resp = create_account(
        client,
        name="FBM JazzCash",
        class_subcategory="Digital Wallet",
        class_account_type="Mobile Wallet",
        channel="digital_wallet",
        wallet_provider="JazzCash",
        wallet_number="03001234567",
        wallet_holder="FBM",
    )
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.channel == "digital_wallet"
    assert acc.wallet_provider == "JazzCash"
    # Wallet behaves like transferable bank funds for legacy payment flows.
    assert acc.category == "bank"


def test_create_client_receivable_requires_client(client, app):
    login(client)
    # No linked client -> rejected.
    resp = create_account(
        client,
        name="Ahmed Receivable",
        class_subcategory="Client Receivables",
        class_account_type="Client Ledger",
    )
    assert b"valid linked client" in resp.data
    # Create a client, then succeed.
    with app.app_context():
        c = Client(code="C001", name="Ahmed", is_active=True)
        db.session.add(c)
        db.session.commit()
        cid = c.id
    resp = create_account(
        client,
        name="Ahmed Receivable",
        class_subcategory="Client Receivables",
        class_account_type="Client Ledger",
        linked_client_id=str(cid),
    )
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.channel == "ledger_only"
    assert acc.linked_client_id == cid
    assert acc.linked_entity_type == "client"


def test_create_supplier_payable_requires_supplier(client, app):
    login(client)
    with app.app_context():
        s = Supplier(name="ABC Suppliers", is_active=True)
        db.session.add(s)
        db.session.commit()
        sid = s.id
    resp = create_account(
        client,
        name="ABC Payable",
        class_category="Liabilities",
        class_subcategory="Supplier Payables",
        class_account_type="Supplier Ledger",
        linked_supplier_id=str(sid),
    )
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.class_category == "Liabilities"
    assert acc.channel == "ledger_only"
    assert acc.linked_supplier_id == sid
    assert acc.source_category == "External"


def test_create_own_funds_account(client, app):
    login(client)
    resp = create_account(
        client,
        name="Ahmed Capital",
        class_category="Equity / Own Funds",
        class_subcategory="Owner Capital",
        class_account_type="Owner Capital",
        channel="ledger_only",
        opening_amount="100000",
    )
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.class_category == "Equity / Own Funds"
    assert acc.source_category == "Own Funds"
    assert acc.opening_balance == 100000.0


def test_create_loan_account(client, app):
    login(client)
    resp = create_account(
        client,
        name="Personal Loan from Uncle",
        class_category="Liabilities",
        class_subcategory="Loans Payable",
        class_account_type="Personal Loan",
        linked_party_name="Uncle Tariq",
        opening_amount="200000",
        opening_position="credit",
    )
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.class_category == "Liabilities"
    # Credit opening stored as negative (liability direction).
    assert acc.opening_balance == -200000.0
    # Loan group preserved for the existing transfer/loan flow.
    assert acc.source_category == "Loan"


def test_create_external_clearing_account(client, app):
    login(client)
    resp = create_account(
        client,
        name="HDC Settlement",
        class_category="External / Clearing",
        class_subcategory="External Settlement",
        class_account_type="External Settlement",
        linked_party_name="HDC",
    )
    assert resp.status_code == 200
    acc = last_account(app)
    assert acc.class_category == "External / Clearing"
    assert acc.channel == "ledger_only"


def test_invalid_combination_blocked(client, app):
    login(client)
    # Expense + Cash + Main Cash is not a valid triple.
    resp = create_account(
        client,
        name="Bad",
        class_category="Expenses",
        class_subcategory="Cash",
        class_account_type="Main Cash",
    )
    assert b"combination is not valid" in resp.data
    # No account should have been created with that name.
    with app.app_context():
        assert Account.query.filter_by(name="Bad").count() == 0


def test_invalid_subcategory_for_category_blocked(client):
    login(client)
    resp = create_account(
        client,
        name="Bad2",
        class_category="Assets",
        class_subcategory="Supplier Payables",  # belongs to Liabilities, not Assets
        class_account_type="Supplier Ledger",
    )
    assert b"combination is not valid" in resp.data


# ---------------------------------------------------------------------------
# Edit form tests
# ---------------------------------------------------------------------------
def test_edit_page_preloads_values(client, app):
    login(client)
    create_account(client, name="Preload Test", opening_amount="50000")
    aid = first_account_id(client)
    resp = client.get(f"/accounts/{aid}/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Preload Test" in body
    # The preset must include the saved classification so dependent dropdowns
    # initialise in the correct order.
    assert '"Main Cash"' in body
    assert "ACCOUNT_PRESET" in body
    assert 'name="opening_amount"' in body
    assert 'id="opening_amount"' in body


def test_edit_changes_classification_and_clears_stale_details(client, app):
    login(client)
    # Start as a bank account with bank details.
    create_account(
        client,
        name="Bank To Cash",
        class_subcategory="Bank",
        class_account_type="Operating Bank",
        channel="bank",
        bank_name="MCB",
        account_holder_name="FBM",
        account_number="ACC-1",
        opening_amount="50000",
    )
    aid = first_account_id(client)
    # Edit it into a Cash account (channel forced to cash). The bank-only
    # details must not survive the change.
    resp = edit_account(
        client,
        aid,
        name="Bank To Cash",
        cash_location="Main drawer",
        # desired_balance omitted => no adjustment
    )
    assert resp.status_code == 200
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.channel == "cash"
        assert acc.bank_name is None
        assert acc.account_number is None
        assert acc.cash_location == "Main drawer"


# ---------------------------------------------------------------------------
# Balance adjustment tests (PART 13/14/15)
# ---------------------------------------------------------------------------
def _seed_account_with_balance(app, name, opening):
    with app.app_context():
        acc = Account(
            name=name,
            category="cash",
            source_category="Company",
            account_type="company",
            type="company",
            balance=opening,
            balance_minor=int(opening * 100),
            opening_balance=opening,
            opening_balance_minor=int(opening * 100),
            opening_balance_date=None,
            class_category="Assets",
            class_subcategory="Cash",
            class_account_type="Main Cash",
            channel="cash",
            account_status="active",
            is_active=True,
        )
        db.session.add(acc)
        db.session.commit()
        return acc.id


def test_adjustment_decrease_creates_negative_ledger_entry(client, app):
    login(client)
    aid = _seed_account_with_balance(app, "Adj Down", 50000.0)
    # ledger_balance must equal the seeded opening before the edit.
    with app.app_context():
        from app.services.payments_crud import ledger_balance

        assert ledger_balance(aid) == 50000.0
    resp = edit_account(
        client,
        aid,
        name="Adj Down",
        desired_balance="45000",
        adjustment_reason="Physical cash verification",
        adjustment_date="2026-08-24",
        idempotency_key="key-down-1",
    )
    assert resp.status_code == 200
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.balance == 45000.0
        # Exactly one Adjustment entry, money OUT (from_account_id set).
        adjs = (
            AccountTransaction.query.filter_by(
                transaction_type="Adjustment", is_void=False
            )
            .filter(
                (AccountTransaction.from_account_id == aid)
                | (AccountTransaction.to_account_id == aid)
            )
            .all()
        )
        assert len(adjs) == 1
        tx = adjs[0]
        assert tx.from_account_id == aid and tx.to_account_id is None
        assert tx.amount == 5000.0
        assert tx.reason == "Physical cash verification"
        # ledger_balance reflects the new balance.
        from app.services.payments_crud import ledger_balance

        assert ledger_balance(aid) == 45000.0


def test_adjustment_increase_creates_positive_ledger_entry(client, app):
    login(client)
    aid = _seed_account_with_balance(app, "Adj Up", 45000.0)
    edit_account(
        client,
        aid,
        name="Adj Up",
        desired_balance="60000",
        adjustment_reason="Bank reconciliation",
        adjustment_date="2026-08-24",
        idempotency_key="key-up-1",
    )
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.balance == 60000.0
        txs = (
            AccountTransaction.query.filter_by(transaction_type="Adjustment", is_void=False)
            .filter(
                (AccountTransaction.from_account_id == aid)
                | (AccountTransaction.to_account_id == aid)
            )
            .all()
        )
        assert len(txs) == 1
        tx = txs[0]
        assert tx.to_account_id == aid and tx.from_account_id is None
        assert tx.amount == 15000.0


def test_no_adjustment_when_desired_equals_current(client, app):
    login(client)
    aid = _seed_account_with_balance(app, "No Change", 50000.0)
    with app.app_context():
        before_count = AccountTransaction.query.filter_by(is_void=False).count()
    resp = edit_account(
        client,
        aid,
        name="No Change",
        desired_balance="50000",  # == current
        adjustment_reason="Physical cash verification",
        adjustment_date="2026-08-24",
        idempotency_key="key-same-1",
    )
    assert resp.status_code == 200
    with app.app_context():
        after_count = AccountTransaction.query.filter_by(is_void=False).count()
        assert after_count == before_count  # PART 15: no zero-value entry
        acc = db.session.get(Account, aid)
        assert acc.balance == 50000.0


def test_adjustment_reason_required(client, app):
    login(client)
    aid = _seed_account_with_balance(app, "Reason Req", 50000.0)
    resp = edit_account(
        client,
        aid,
        name="Reason Req",
        desired_balance="45000",
        # adjustment_reason omitted
        adjustment_date="2026-08-24",
        idempotency_key="key-noreason-1",
    )
    assert b"adjustment reason is required" in resp.data
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.balance == 50000.0  # unchanged


def test_edit_opening_balance_shifts_current_without_adjustment(client, app):
    """Correcting the historical opening must rewrite the baseline, not post a txn."""
    login(client)
    aid = _seed_account_with_balance(app, "Open Fix", 50000.0)
    with app.app_context():
        before_count = AccountTransaction.query.filter_by(is_void=False).count()
    resp = edit_account(
        client,
        aid,
        name="Open Fix",
        opening_amount="75000",
        opening_position="debit",
        opening_effective_date="2026-01-15",
        # desired follows the new current so no physical-cash adjustment is posted
        desired_balance="75000",
    )
    assert resp.status_code == 200
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.opening_balance == 75000.0
        assert acc.opening_balance_minor == 7500000
        assert acc.opening_balance_date.strftime("%Y-%m-%d") == "2026-01-15"
        assert acc.balance == 75000.0
        from app.services.payments_crud import ledger_balance

        assert ledger_balance(aid) == 75000.0
        after_count = AccountTransaction.query.filter_by(is_void=False).count()
        assert after_count == before_count


def test_edit_opening_then_physical_adjustment(client, app):
    """Opening correction plus today's physical mismatch posts exactly one Adjustment."""
    login(client)
    aid = _seed_account_with_balance(app, "Open Plus Adj", 50000.0)
    resp = edit_account(
        client,
        aid,
        name="Open Plus Adj",
        opening_amount="60000",
        opening_position="debit",
        opening_effective_date="2026-01-01",
        # After opening 50k→60k, current is 60k. Physical count is 58k.
        desired_balance="58000",
        adjustment_reason="Physical cash verification",
        adjustment_date="2026-08-24",
        idempotency_key="key-open-adj-1",
    )
    assert resp.status_code == 200
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.opening_balance == 60000.0
        assert acc.balance == 58000.0
        adjs = (
            AccountTransaction.query.filter_by(transaction_type="Adjustment", is_void=False)
            .filter(
                (AccountTransaction.from_account_id == aid)
                | (AccountTransaction.to_account_id == aid)
            )
            .all()
        )
        assert len(adjs) == 1
        assert adjs[0].from_account_id == aid
        assert adjs[0].amount == 2000.0
        from app.services.payments_crud import ledger_balance

        assert ledger_balance(aid) == 58000.0


def test_edit_opening_credit_direction(client, app):
    login(client)
    aid = _seed_account_with_balance(app, "Open Credit", 10000.0)
    edit_account(
        client,
        aid,
        name="Open Credit",
        opening_amount="25000",
        opening_position="credit",
        opening_effective_date="2026-02-01",
        desired_balance="-25000",
    )
    with app.app_context():
        acc = db.session.get(Account, aid)
        assert acc.opening_balance == -25000.0
        assert acc.balance == -25000.0
        from app.services.payments_crud import ledger_balance

        assert ledger_balance(aid) == -25000.0


def test_adjustment_idempotent_on_retry(client, app):
    login(client)
    aid = _seed_account_with_balance(app, "Idem", 50000.0)
    edit_account(
        client,
        aid,
        name="Idem",
        desired_balance="45000",
        adjustment_reason="Physical cash verification",
        adjustment_date="2026-08-24",
        idempotency_key="key-retry-1",
    )
    # Simulate a double-click / retry with the SAME idempotency key but a
    # different desired balance: the second post must NOT create another entry
    # or move the balance away from the first adjustment's result.
    edit_account(
        client,
        aid,
        name="Idem",
        desired_balance="40000",
        adjustment_reason="Physical cash verification",
        adjustment_date="2026-08-24",
        idempotency_key="key-retry-1",
    )
    with app.app_context():
        adjs = (
            AccountTransaction.query.filter_by(
                transaction_type="Adjustment", is_void=False, idempotency_key="key-retry-1"
            ).all()
        )
        assert len(adjs) == 1  # no duplicate despite retry
        acc = db.session.get(Account, aid)
        assert acc.balance == 45000.0  # first adjustment's result preserved


# ---------------------------------------------------------------------------
# Data integrity (PART 18/23)
# ---------------------------------------------------------------------------
def test_bootstrap_preserves_existing_accounts(app_factory, tmp_path):
    """A second bootstrap against the same DB must not duplicate/drop accounts."""
    db_file = tmp_path / "integ.db"
    app = app_factory(db_file=db_file)
    with app.app_context():
        # Seed a legacy-style account (no new classification columns set).
        acc = Account(
            name="Legacy Cash",
            category="cash",
            source_category="Company",
            account_type="company",
            type="company",
            balance=1000.0,
            balance_minor=100000,
            opening_balance=1000.0,
            opening_balance_minor=100000,
            is_active=True,
            revision=1,
        )
        db.session.add(acc)
        db.session.commit()
        seeded_id = acc.id
        before_count = Account.query.count()

    # Re-create the app (re-runs bootstrap against the SAME file).
    app2 = app_factory(db_file=db_file)
    with app2.app_context():
        after_count = Account.query.count()
        assert after_count == before_count  # no duplication
        acc = db.session.get(Account, seeded_id)
        assert acc is not None
        assert acc.name == "Legacy Cash"
        assert acc.balance == 1000.0  # balance untouched
        # Legacy account was backfilled into a valid classification.
        assert acc.class_category == "Assets"
        assert acc.channel in ("cash", "bank", "digital_wallet", "ledger_only", "other")
