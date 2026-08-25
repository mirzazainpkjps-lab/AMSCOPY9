"""Regression tests for every defect in QA_FULL_AUDIT.md (PRED-001..PRED-014).

Each test drives the real HTTP layer and asserts the invariant the audit
described.  Where the audit required raw-DB ground truth, the test reads the
database directly through the ORM (same rows, no service helpers).
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta

import pytest

from app.services.time_money import pk_now, pk_today

from models import (
    db,
    Account,
    AccountReconciliation,
    AccountTransaction,
    Client,
    DeliveryPerson,
    DirectSale,
    DirectSaleItem,
    Entry,
    GRNAllocation,
    GRN,
    GRNItem,
    Material,
    PendingBill,
    Payment,
)
from conftest import make_csrf_client

_LEAK_SIGNATURES = (
    "[SQL:", "sqlite3", "IntegrityError", "sqlalche.me", "UNIQUE constraint failed",
    "FOREIGN KEY constraint failed", "(Background on this error", "Traceback",
)


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def body_text(response):
    return response.get_data(as_text=True)


def flash_text(response):
    msgs = re.findall(r"alert[^>]*>(.{0,400}?)</div>", body_text(response), re.S)
    return " | ".join(re.sub(r"<[^>]+>|\s+", " ", m).strip() for m in msgs)


def assert_no_leak(response):
    text = body_text(response)
    for sig in _LEAK_SIGNATURES:
        assert sig not in text, f"leaked internal detail {sig!r}: {text[-500:]}"


def grn_date():
    """GRNs are dated in the past so FIFO lots are consumable by sales today."""
    return (pk_today() - timedelta(days=5)).strftime("%Y-%m-%d")


def seed_material(client, app, name="CEMENT", grn_qty=1000, grn_rate=100):
    """Material + GRN stock (past date so FIFO lots are consumable today)."""
    r = client.post("/add_material", data={
        "material_name": name, "material_unit": "Bags",
    }, follow_redirects=True)
    assert r.status_code == 200
    r = client.post("/grn", data={
        "action": "add", "supplier": "SUP1",
        "mat_name[]": name, "qty[]": str(grn_qty), "price[]": str(grn_rate),
        "paid_amount": "0", "date": grn_date(),
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        mat = Material.query.filter_by(name=name).first()
        assert mat is not None and float(mat.total or 0) >= grn_qty
        return mat.id, GRN.query.first().id


def seed_client(client, app, name="C1", code="CL-1"):
    r = client.post("/add_client", data={
        "name": name, "code": code, "category": "General", "opening_balance": "0",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        return Client.query.filter_by(code=code).first().id


def seed_cash_account(client, app, name="CASH1", opening="100000"):
    r = client.post("/accounts/accounts/add", data={
        "name": name,
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": opening,
        "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        acc = Account.query.filter_by(name=name).first()
        assert acc is not None
        return acc.id


def sale_payload(manual_bill=None, **overrides):
    payload = {
        "client_name": "C1",
        "driver_name": "D",
        "category": "Credit Customer",
        "product_name[]": "CEMENT",
        "qty[]": "1",
        "unit_rate[]": "100",
        "payment_method": "Cash",
        "paid_amount": "0",
    }
    if manual_bill is not None:
        payload["manual_bill_no"] = manual_bill
    # convenience: plain qty / unit_rate keys map onto the list-form fields
    if "qty" in overrides:
        overrides["qty[]"] = overrides.pop("qty")
    if "unit_rate" in overrides:
        overrides["unit_rate[]"] = overrides.pop("unit_rate")
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# PRED-001 — concurrent sales must never share an auto bill number
# ---------------------------------------------------------------------------
def test_concurrent_sales_unique_auto_bill(app_factory, tmp_path):
    db_file = tmp_path / "race.db"
    apps = [app_factory(db_file, AUTO_RECONCILE_ENABLED="0") for _ in range(8)]
    clients = [make_csrf_client(a) for a in apps]
    for c in clients:
        login(c)
    seed_material(clients[0], apps[0])
    seed_client(clients[0], apps[0])
    with apps[0].app_context():
        db.session.add(DeliveryPerson(name="D"))
        db.session.commit()

    barrier = threading.Barrier(8)
    results = [None] * 8

    def worker(i):
        barrier.wait()
        results[i] = clients[i].post(
            "/add_direct_sale",
            data=sale_payload(manual_bill=f"MB NO.R-{i + 1}"),
            follow_redirects=False,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with apps[0].app_context():
        bills = [s.auto_bill_no for s in DirectSale.query.all()]
        assert len(bills) == 8, f"expected 8 sales, got {len(bills)}"
        assert len(set(bills)) == 8, f"duplicate auto bills: {bills}"
        # every bill must resolve through the bill viewer's lookup
        from app.services.billing import _lookup_bill
        for bill in bills:
            booking, payment, invoice, sale, grn, pending = _lookup_bill(bill)
            assert sale is not None, f"{bill} not resolvable"


def test_concurrent_first_sales_share_one_driver(app_factory, tmp_path):
    """8 simultaneous first-time sales auto-creating driver D: all succeed."""
    db_file = tmp_path / "race_driver.db"
    apps = [app_factory(db_file, AUTO_RECONCILE_ENABLED="0") for _ in range(6)]
    clients = [make_csrf_client(a) for a in apps]
    for c in clients:
        login(c)
    seed_material(clients[0], apps[0])
    seed_client(clients[0], apps[0])

    barrier = threading.Barrier(6)
    results = [None] * 6

    def worker(i):
        barrier.wait()
        results[i] = clients[i].post(
            "/add_direct_sale",
            data=sale_payload(manual_bill=f"MB NO.D-{i + 1}"),
            follow_redirects=True,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with apps[0].app_context():
        assert DirectSale.query.count() == 6
        assert DeliveryPerson.query.filter_by(name="D").count() == 1
    for r in results:
        assert_no_leak(r)


# ---------------------------------------------------------------------------
# PRED-002 — reconciliation must not double-count future-dated receipts
# ---------------------------------------------------------------------------
def test_future_dated_payment_is_rejected(client, app):
    login(client)
    seed_client(client, app)
    aid = seed_cash_account(client, app)

    future_date = (pk_today() + timedelta(days=10)).strftime("%Y-%m-%d")
    r = client.post("/add_payment", data={
        "client_code": "CL-1",
        "amount": "11",
        "payment_type": "Receipt",
        "method": "Cash",
        "payment_account_id": str(aid),
        "date": future_date,
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert Payment.query.count() == 0
        assert Account.query.get(aid).balance == 100000.0
    assert "cannot be in the future" in flash_text(r)


def test_reconcile_with_legacy_future_receipt_keeps_balance(client, app):
    """A future-dated receipt already inside the live balance must reconcile
    cleanly: expected == live ledger, difference 0, account unchanged."""
    login(client)
    seed_client(client, app)
    aid = seed_cash_account(client, app)

    # Legacy future-dated receipt injected at the DB level (pre-fix data).
    with app.app_context():
        from app.services.accounting import _sync_payment_accounting
        p = Payment(
            client_name="C1", amount=11.0, amount_minor=1100,
            method="Cash", payment_type="Receipt",
            payment_account_id=aid,
            date_posted=datetime.combine(pk_today() + timedelta(days=10), datetime.min.time()),
            is_void=False,
        )
        db.session.add(p)
        db.session.flush()
        _sync_payment_accounting(p)
        db.session.commit()
        assert Account.query.get(aid).balance == 100011.0

    r = client.post(f"/accounts/{aid}/reconcile", data={
        "actual_balance": "100011.00",
        "reconciliation_date": pk_today().strftime("%Y-%m-%d"),
        "note": "pred-002",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        acc = Account.query.get(aid)
        rec = AccountReconciliation.query.order_by(AccountReconciliation.id.desc()).first()
        assert rec.difference == 0.0, f"expected 0 difference, got {rec.difference}"
        assert acc.balance == 100011.0
        assert rec.final_reconciled_balance == acc.balance
    assert "Matched" in flash_text(r)


# ---------------------------------------------------------------------------
# PRED-003 / PRED-004 — Open-Khata receivables must be visible AND settleable
# ---------------------------------------------------------------------------
def _open_khata_sale(client, app, qty=25, rate=110):
    seed_material(client, app, grn_qty=1000)
    r = client.post("/add_direct_sale", data={
        "client_name": "",
        "manual_client_name": "Walk-in Customer 1",
        "category": "Open Khata",
        "driver_name": "D",
        "product_name[]": "CEMENT",
        "qty[]": str(qty),
        "unit_rate[]": str(rate),
        "payment_method": "Cash",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert DirectSale.query.count() == 1
        return DirectSale.query.first()


def test_open_khata_sale_visible_in_payables_api_page_and_csv(client, app):
    login(client)
    sale = _open_khata_sale(client, app)
    expected = sale.amount

    with app.app_context():
        # the system master row anchors the receivable
        master = Client.query.filter_by(code="OPEN-KHATA").first()
        assert master is not None

    r = client.get("/api/current_payables")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["total_outstanding"] == expected
    assert any(row["client_code"] == "OPEN-KHATA" for row in payload["rows"])

    page = client.get("/current_payables")
    assert page.status_code == 200
    assert f"{expected:,.2f}" in body_text(page)
    assert "OPEN KHATA" in body_text(page)

    csv = client.get("/export_current_payables")
    assert csv.status_code == 200
    assert f"{expected:.2f}" in csv.get_data(as_text=True)


def test_open_khata_settlement(client, app):
    login(client)
    sale = _open_khata_sale(client, app)
    aid = seed_cash_account(client, app)

    r = client.post("/add_payment", data={
        "client_code": "OPEN-KHATA",
        "client_name": "Walk-in Customer 1",
        "amount": "1000",
        "payment_type": "Receipt",
        "method": "Cash",
        "payment_account_id": str(aid),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Payment received" in flash_text(r) or "saved successfully" in flash_text(r)

    with app.app_context():
        assert Payment.query.count() == 1
        pending = PendingBill.query.filter(PendingBill.is_void == False).all()
        assert sum(float(p.amount or 0) for p in pending) == sale.amount - 1000.0
    r = client.get("/api/current_payables")
    assert r.get_json()["total_outstanding"] == sale.amount - 1000.0


# ---------------------------------------------------------------------------
# PRED-005 — CSRF must gate every mutating route
# ---------------------------------------------------------------------------
def test_csrf_required_for_sales_payment_grn_posts(app, client):
    login(client)
    seed_material(client, app)
    seed_client(client, app)
    aid = seed_cash_account(client, app)

    raw = app.test_client()
    with raw.session_transaction() as sess:
        sess["_csrf_token"] = "tok"
    r = raw.post("/login", data={
        "username": "Admin", "password": "Admin@fbm12345", "_csrf_token": "tok",
    })
    assert r.status_code in (302, 303)

    probes = [
        ("/add_payment", {
            "client_code": "CL-1", "amount": "1", "payment_type": "Receipt",
            "method": "Cash", "payment_account_id": str(aid),
        }),
        ("/add_direct_sale", sale_payload()),
        ("/grn", {
            "action": "add", "supplier": "SUP1",
            "mat_name[]": "CEMENT", "qty[]": "10", "price[]": "100",
            "paid_amount": "0",
        }),
    ]
    with app.app_context():
        before = (Payment.query.count(), DirectSale.query.count(), GRN.query.count())
    for path, payload in probes:
        resp = raw.post(path, data=payload, follow_redirects=False)
        assert resp.status_code == 400, f"{path}: expected CSRF 400, got {resp.status_code}"
        assert "Invalid or expired form token" in body_text(resp)
    with app.app_context():
        after = (Payment.query.count(), DirectSale.query.count(), GRN.query.count())
        assert before == after, "CSRF-less POST mutated state"


# ---------------------------------------------------------------------------
# PRED-006 — un-keyed replay must be rejected server-side
# ---------------------------------------------------------------------------
def test_backend_rejects_unkeyed_replay(client, app):
    login(client)
    seed_material(client, app, grn_qty=100)
    seed_client(client, app)
    payload = sale_payload(manual_bill=None)

    r1 = client.post("/add_direct_sale", data=dict(payload), follow_redirects=True)
    assert r1.status_code == 200
    r2 = client.post("/add_direct_sale", data=dict(payload), follow_redirects=True)
    assert r2.status_code == 200

    with app.app_context():
        assert DirectSale.query.count() == 1
        assert Material.query.filter_by(name="CEMENT").first().total == 99.0
    assert "already saved" in flash_text(r2)


# ---------------------------------------------------------------------------
# PRED-007 — an idempotency key reused with a different payload must be rejected
# ---------------------------------------------------------------------------
def test_idem_key_payload_binding(client, app):
    login(client)
    seed_material(client, app)
    seed_client(client, app)
    with app.app_context():
        db.session.add(DeliveryPerson(name="D"))
        db.session.commit()

    key = "KEY-ALPHA-001"
    r1 = client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.ALPHA", idempotency_key=key,
    ), follow_redirects=True)
    assert r1.status_code == 200

    # completely different payload, same key: must NOT create sale B
    r2 = client.post("/add_direct_sale", data={
        "client_name": "C1", "driver_name": "D", "category": "Credit Customer",
        "product_name[]": "CEMENT", "qty[]": "77", "unit_rate[]": "105",
        "payment_method": "Cash", "paid_amount": "0",
        "manual_bill_no": "MB NO.BETA", "idempotency_key": key,
    }, follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        assert DirectSale.query.count() == 1
        assert DirectSale.query.first().manual_bill_no == "MB NO.ALPHA"
    assert "already used for a different sale" in flash_text(r2)


# ---------------------------------------------------------------------------
# PRED-008 — the reconciled-period guard must hold on payment CREATE
# ---------------------------------------------------------------------------
def test_payment_create_in_reconciled_period_rejected(client, app):
    login(client)
    seed_client(client, app)
    aid = seed_cash_account(client, app)
    early = (pk_now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    r = client.post("/add_payment", data={
        "client_code": "CL-1", "amount": "100", "payment_type": "Receipt",
        "method": "Cash", "payment_account_id": str(aid), "date": early,
    }, follow_redirects=True)
    assert "Payment received" in flash_text(r)

    r = client.post(f"/accounts/{aid}/reconcile", data={
        "actual_balance": "100100.00",
        "reconciliation_date": pk_today().strftime("%Y-%m-%d"),
    }, follow_redirects=True)
    assert "reconciled" in flash_text(r).lower()

    # dated AFTER the early receipt but BEFORE the closing instant
    inside = (pk_now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    r = client.post("/add_payment", data={
        "client_code": "CL-1", "amount": "50", "payment_type": "Receipt",
        "method": "Cash", "payment_account_id": str(aid),
        "date": inside,
    }, follow_redirects=True)
    with app.app_context():
        assert Payment.query.count() == 1, "post-close receipt was accepted"
        assert Account.query.get(aid).balance == 100100.0
    assert "reconciled period" in flash_text(r)


# ---------------------------------------------------------------------------
# PRED-009 — domain wipe must work on GRN-linked sales and never leak SQL
# ---------------------------------------------------------------------------
def test_full_wipe_with_grn_linked_sales_succeeds(client, app):
    login(client)
    seed_material(client, app, grn_qty=500)
    seed_client(client, app)
    # credit sale consumes a GRN lot (FIFO) -> grn_item locked + GRNAllocation
    r = client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.W1", qty="200", unit_rate="110",
    ), follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert GRNAllocation.query.count() == 1
        assert any(i.is_locked for i in GRNItem.query.all())
        assert DirectSaleItem.query.first().grn_item_id is not None

    r = client.post("/delete_selected_data", data={
        "confirm_text": "DELETE ALL DATA",
        "hard_delete_override": "1",
        "delete_targets": ["direct_sales", "payments", "accounts"],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert_no_leak(r)
    with app.app_context():
        assert DirectSale.query.count() == 0
        assert DirectSaleItem.query.count() == 0
        assert GRN.query.count() == 0
        assert GRNItem.query.count() == 0
        assert PendingBill.query.count() == 0
        assert Entry.query.count() == 0
        assert AccountTransaction.query.count() == 0
        # material totals recomputed from the (empty) surviving ledger
        assert Material.query.filter_by(name="CEMENT").first().total == 0.0
    assert "Data Wiped" in body_text(r)


def test_wipe_error_does_not_leak_sql(client, app, monkeypatch):
    login(client)
    leak = (
        "(sqlite3.IntegrityError) FOREIGN KEY constraint failed "
        "[SQL: DELETE FROM grn_item] "
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )

    def boom(_targets):
        raise RuntimeError(leak)

    monkeypatch.setattr(
        "app.blueprints.misc._wipe_delete_selected_data._create_pre_wipe_safety_backups",
        boom,
    )
    r = client.post("/delete_selected_data", data={
        "confirm_text": "DELETE ALL DATA",
        "hard_delete_override": "1",
        "delete_targets": ["direct_sales"],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert_no_leak(r)
    assert "Pre-wipe backup failed" in flash_text(r)


# ---------------------------------------------------------------------------
# PRED-010 — user-visible errors must never expose SQL / internals
# ---------------------------------------------------------------------------
def test_sale_error_flash_does_not_leak_sql(client, app, monkeypatch):
    login(client)
    seed_material(client, app)
    seed_client(client, app)

    def boom(_name):
        from sqlalchemy.exc import IntegrityError
        raise IntegrityError(
            "INSERT INTO delivery_person (name, phone) VALUES (?, ?)",
            {"name": "D", "phone": None},
            Exception("UNIQUE constraint failed: delivery_person.name"),
        )

    monkeypatch.setattr(
        "app.blueprints.sales._direct_sales_add_direct_sale.get_or_create_delivery_person",
        boom,
    )
    r = client.post("/add_direct_sale", data=sale_payload(), follow_redirects=True)
    assert r.status_code == 200
    assert_no_leak(r)
    with app.app_context():
        assert DirectSale.query.count() == 0
    assert "could not be saved" in flash_text(r)


# ---------------------------------------------------------------------------
# PRED-011 — /export_unpaid_transactions must run its documented handler
# ---------------------------------------------------------------------------
def test_export_unpaid_transactions_runs_documented_handler(client, app):
    login(client)
    rules = [r for r in app.url_map.iter_rules() if r.rule == "/export_unpaid_transactions"]
    assert rules, "route missing"
    handlers = {app.view_functions[r.endpoint] for r in rules}
    assert len(handlers) == 1, f"shadowed handlers: {[r.endpoint for r in rules]}"
    handler = next(iter(handlers))
    assert handler.__module__ == "app.blueprints.misc.extra"

    r = client.get("/export_unpaid_transactions", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["Location"]
    assert "/import_export/export" in location
    assert "dataset=unpaid_transactions" in location


# ---------------------------------------------------------------------------
# PRED-012 — GRN header edits stay available while lots are locked
# ---------------------------------------------------------------------------
def _grn_with_locked_lot(client, app):
    login(client)
    seed_material(client, app, grn_qty=500)
    seed_client(client, app)
    r = client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.L1", qty="200", unit_rate="110",
    ), follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        grn = GRN.query.first()
        assert any(i.is_locked for i in grn.items)
        return grn.id, grn.items[0].id, Material.query.filter_by(name="CEMENT").first().total


def _grn_edit_form(grn_id, item_id, *, freight="500", qty="500", price="100"):
    return {
        "supplier": "SUP1",
        "manual_bill_no": "",
        "loading_cost": "0",
        "freight_cost": freight,
        "other_expense": "0",
        "adjustment_amount": "0",
        "discount": "0",
        "paid_amount": "0",
        "payment_type": "",
        "tax_percent": "0",
        "tax_amount": "0",
        "tax_type": "",
        "date": grn_date(),
        "grn_item_id[]": str(item_id),
        "mat_name[]": "CEMENT",
        "qty[]": qty,
        "price[]": price,
    }


def test_grn_nonstock_edit_allowed_when_lots_locked(client, app):
    grn_id, item_id, stock_before = _grn_with_locked_lot(client, app)
    r = client.post(f"/edit_grn/{grn_id}", data=_grn_edit_form(
        grn_id, item_id, freight="500",
    ), follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        grn = db.session.get(GRN, grn_id)
        assert grn.freight_cost == 500.0, "freight edit blocked by locked lots"
        assert Material.query.filter_by(name="CEMENT").first().total == stock_before
        assert any(i.is_locked for i in grn.items)


def test_grn_item_change_blocked_when_lots_locked(client, app):
    grn_id, item_id, stock_before = _grn_with_locked_lot(client, app)
    r = client.post(f"/edit_grn/{grn_id}", data=_grn_edit_form(
        grn_id, item_id, freight="999", qty="499",
    ), follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        grn = db.session.get(GRN, grn_id)
        assert grn.freight_cost == 0.0, "blocked edit must not save anything"
        assert grn.items[0].qty == 500.0
        assert Material.query.filter_by(name="CEMENT").first().total == stock_before
    assert "locked lots" in flash_text(r)


# ---------------------------------------------------------------------------
# PRED-013 — check_bill must see auto-billed sales
# ---------------------------------------------------------------------------
def test_check_bill_detects_auto_billed_sale(client, app):
    login(client)
    seed_material(client, app)
    seed_client(client, app)
    with app.app_context():
        db.session.add(DeliveryPerson(name="D"))
        db.session.commit()
    client.post("/add_direct_sale", data=sale_payload(manual_bill=None), follow_redirects=True)
    with app.app_context():
        sale = DirectSale.query.first()
        assert sale.auto_bill_no
        auto = sale.auto_bill_no

    r = client.get(f"/api/check_bill/{auto}")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload["exists"] is True
    assert payload["kind"] == "direct_sale"
    assert payload["id"] == sale.id


# ---------------------------------------------------------------------------
# Mutation blind spots M1–M3
# ---------------------------------------------------------------------------
def test_insufficient_stock_sale_rejected(client, app):
    login(client)
    seed_material(client, app, grn_qty=100)
    seed_client(client, app)
    r = client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.NEG", qty="200",
    ), follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert DirectSale.query.count() == 0
        assert Material.query.filter_by(name="CEMENT").first().total == 100.0
    assert "Insufficient stock" in flash_text(r)


def test_duplicate_manual_bill_rejected(client, app):
    login(client)
    seed_material(client, app)
    seed_client(client, app)
    with app.app_context():
        db.session.add(DeliveryPerson(name="D"))
        db.session.commit()
    client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.DUP", qty="1",
    ), follow_redirects=True)
    # different payload (qty 2) but the SAME manual bill: not a replay —
    # the duplicate-bill guard must reject it
    r = client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.DUP", qty="2",
    ), follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert DirectSale.query.count() == 1
    assert "already exists" in body_text(r)


def test_sales_appear_in_receivables_projection(client, app):
    login(client)
    seed_material(client, app)
    seed_client(client, app)
    with app.app_context():
        db.session.add(DeliveryPerson(name="D"))
        db.session.commit()
    client.post("/add_direct_sale", data=sale_payload(
        manual_bill="MB NO.PRJ", qty="10", unit_rate="250",
    ), follow_redirects=True)

    r = client.get("/api/current_payables")
    payload = r.get_json()
    rows = {row["client_code"]: row for row in payload["rows"]}
    assert "CL-1" in rows
    assert rows["CL-1"]["outstanding"] == 2500.0
    assert payload["total_outstanding"] == 2500.0
