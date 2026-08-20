"""Inventory / GRN / stock movement integrity tests.

Covers the critical business scenarios for the stock ledger:

  * opening stock, single & multiple GRNs (each movement adds exactly once)
  * GRN edit applies only the delta (500->700 = +200, 700->400 = -300)
  * GRN delete/cancel reverses the original movement exactly
  * duplicate submission (same manual bill twice) creates one GRN / one movement
  * customer return increases stock once
  * booking (reservation) does NOT reduce physical stock
  * sale/dispatch reduces physical stock exactly once
  * zero/negative quantity is rejected
  * stored material.total always equals the entry movement ledger

Each test runs against an isolated temporary database using the app's real
routes (test client), so these are business-flow tests, not unit mocks.
"""
import os
from datetime import datetime, timedelta

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "inventory_flows.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    from app import create_app
    from models import db

    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
        }
    )
    with application.app_context():
        db.create_all()
        from app.services.schema import (
            _ensure_model_columns,
            _ensure_performance_indexes,
            _ensure_default_admin,
        )
        _ensure_model_columns()
        _ensure_performance_indexes()
        _ensure_default_admin()
        db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    """Test client authenticated as the bootstrap Admin user."""
    from models import User
    with app.app_context():
        user = User.query.first()
        assert user is not None
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _seed_material(app, name="OPC TEST CEMENT", opening=100.0, rate=1000.0):
    """Seed a material; opening stock is represented as an opening IN entry so
    the movement ledger (entry) and the stored balance (material.total) agree
    from the start — the same relationship the reconciliation tool verifies."""
    from models import db, Entry, Material
    with app.app_context():
        mat = Material(code=f"M-{name[:8]}", name=name, unit_price=rate, total=opening, is_active=True)
        db.session.add(mat)
        if opening:
            db.session.add(Entry(
                date="2025-01-01", time="00:00:00", type='IN',
                material=name, client="OPENING", qty=opening,
                nimbus_no="Opening", created_by="test",
            ))
        db.session.commit()
        return mat.id


def _stock(app, mat_id=None, name="OPC TEST CEMENT"):
    from models import Material
    with app.app_context():
        m = Material.query.get(mat_id) if mat_id else Material.query.filter_by(name=name).first()
        return float(m.total or 0)


def _ledger_net(app, name="OPC TEST CEMENT", include_cancel=False):
    """Movement-ledger stock for a material (IN - OUT, non-void)."""
    from models import Entry
    with app.app_context():
        q = Entry.query.filter(
            Entry.material == name,
            Entry.is_void == False,
        )
        if not include_cancel:
            q = q.filter(Entry.type.in_(['IN', 'OUT']))
        rows = q.all()
        net = 0.0
        for e in rows:
            t = (e.type or '').upper()
            if t == 'IN':
                net += float(e.qty or 0)
            elif t == 'OUT':
                net -= float(e.qty or 0)
        return net


def _entry_count(app, name="OPC TEST CEMENT", type_='IN'):
    from models import Entry
    from sqlalchemy import or_
    with app.app_context():
        return Entry.query.filter(
            Entry.material == name, Entry.type == type_, Entry.is_void == False,
            or_(Entry.nimbus_no.is_(None), Entry.nimbus_no != 'Opening'),
        ).count()


def _grn_form(supplier="TEST SUPPLIER", manual="", items=(("OPC TEST CEMENT", "50", "1000"),),
              note="t", date="2026-08-17"):
    form = {
        "action": "add",
        "supplier": supplier,
        "supplier_id": "",
        "manual_bill_no": manual,
        "note": note,
        "loading_cost": "0", "freight_cost": "0", "other_expense": "0",
        "adjustment_amount": "0", "discount": "0",
        "paid_amount": "0", "payment_type": "Cash",
        "date": date,
        "mat_name[]": [it[0] for it in items],
        "qty[]": [it[1] for it in items],
        "price[]": [it[2] for it in items],
    }
    return form


def _client(app, name="TEST CLIENT", code="TC1"):
    from models import db, Client
    with app.app_context():
        c = Client.query.filter_by(code=code).first()
        if not c:
            c = Client(code=code, name=name, is_active=True)
            db.session.add(c)
            db.session.commit()
        return c


def _cash_account(app):
    """Active cash account so paid cash sales can post into Accounts."""
    from models import Account, db
    with app.app_context():
        acc = Account.query.filter_by(name="TEST CASH").first()
        if not acc:
            acc = Account(name="TEST CASH", type="Cash", category="cash",
                          account_type="cash", balance=1000000, is_active=True)
            db.session.add(acc)
            db.session.commit()
        return acc.id


# ---------------------------------------------------------------------------
# 1-3. Opening stock + single/multiple GRN: each movement adds exactly once
# ---------------------------------------------------------------------------
def test_opening_stock_is_baseline(app):
    _seed_material(app, opening=100)
    assert _stock(app) == 100.0


def test_single_grn_adds_stock_once(app, client):
    _seed_material(app, opening=100)
    client.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "500", "1000"),)), follow_redirects=False)
    assert _stock(app) == 600.0                     # 100 + 500
    assert _ledger_net(app) == 600.0                # ledger agrees
    assert _entry_count(app, type_='IN') == 1       # exactly one stock-in


def test_multiple_grns_are_cumulative(app, client):
    _seed_material(app, opening=100)
    c = client
    c.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "500", "1000"),)), follow_redirects=False)
    c.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "200", "1000"),)), follow_redirects=False)
    assert _stock(app) == 800.0                     # 100 + 500 + 200
    assert _entry_count(app, type_='IN') == 2       # two movements, both kept


# ---------------------------------------------------------------------------
# 4-5. GRN edit applies only the delta
# ---------------------------------------------------------------------------
def _make_grn(app, client, qty, manual="TEST-1"):
    from models import GRN
    client.post("/grn", data=_grn_form(manual=manual, items=(("OPC TEST CEMENT", qty, "1000"),)), follow_redirects=False)
    with app.app_context():
        g = GRN.query.filter_by(manual_bill_no=f"MB NO.{manual}").first()
        assert g is not None, f"GRN {manual} not created"
        return g.id


def test_grn_edit_increase_applies_delta(app, client):
    _seed_material(app, opening=100)
    gid = _make_grn(app, client, "500")                     # stock 600
    assert _stock(app) == 600.0
    # edit 500 -> 700
    form = {
        "grn_item_id[]": [str(_grn_item_id(app, gid))],
        "mat_name[]": ["OPC TEST CEMENT"], "qty[]": ["700"], "price[]": ["1000"],
        "supplier": "TEST SUPPLIER", "supplier_id": "",
        "manual_bill_no": "MB TEST-1", "note": "",
        "loading_cost": "0", "freight_cost": "0", "other_expense": "0",
        "adjustment_amount": "0", "discount": "0", "paid_amount": "0",
        "payment_type": "Cash", "date": "2026-08-17",
    }
    r = client.post(f"/edit_grn/{gid}", data=form, follow_redirects=False)
    assert r.status_code in (302, 200)
    assert _stock(app) == 800.0                     # delta +200, not +700
    assert _ledger_net(app) == 800.0


def test_grn_edit_reduction_applies_delta(app, client):
    _seed_material(app, opening=100)
    gid = _make_grn(app, client, "700")
    assert _stock(app) == 800.0
    form = {
        "grn_item_id[]": [str(_grn_item_id(app, gid))],
        "mat_name[]": ["OPC TEST CEMENT"], "qty[]": ["400"], "price[]": ["1000"],
        "supplier": "TEST SUPPLIER", "supplier_id": "",
        "manual_bill_no": "MB TEST-1", "note": "",
        "loading_cost": "0", "freight_cost": "0", "other_expense": "0",
        "adjustment_amount": "0", "discount": "0", "paid_amount": "0",
        "payment_type": "Cash", "date": "2026-08-17",
    }
    client.post(f"/edit_grn/{gid}", data=form, follow_redirects=False)
    assert _stock(app) == 500.0                     # delta -300
    assert _ledger_net(app) == 500.0


def _grn_item_id(app, grn_id):
    from models import GRNItem
    with app.app_context():
        return GRNItem.query.filter_by(grn_id=grn_id).first().id


# ---------------------------------------------------------------------------
# 10. GRN delete reverses exactly
# ---------------------------------------------------------------------------
def test_grn_delete_reverses_stock(app, client):
    _seed_material(app, opening=100)
    gid = _make_grn(app, client, "500")
    assert _stock(app) == 600.0
    r = client.post("/grn", data={"action": "delete", "id": str(gid)}, follow_redirects=False)
    assert r.status_code == 302
    assert _stock(app) == 100.0                     # reversed exactly
    assert _ledger_net(app) == 100.0


# ---------------------------------------------------------------------------
# 14/17. Duplicate submission creates one GRN / one movement
# ---------------------------------------------------------------------------
def test_duplicate_manual_bill_grn_is_rejected(app, client):
    _seed_material(app, opening=100)
    c = client
    r1 = c.post("/grn", data=_grn_form(manual="MB DUP-1", items=(("OPC TEST CEMENT", "500", "1000"),)), follow_redirects=False)
    assert _stock(app) == 600.0
    r2 = c.post("/grn", data=_grn_form(manual="MB DUP-1", items=(("OPC TEST CEMENT", "500", "1000"),)), follow_redirects=False)
    # second identical submission must not double the stock
    assert _stock(app) == 600.0
    assert _entry_count(app, type_='IN') == 1


def test_grn_duplicate_without_manual_bill_creates_two_grns_but_each_one_movement(app, client):
    """Auto-bill GRNs from distinct submissions each move stock once (no
    double-counting per submission)."""
    _seed_material(app, opening=100)
    c = client
    c.post("/grn", data=_grn_form(manual="", items=(("OPC TEST CEMENT", "50", "1000"),)), follow_redirects=False)
    c.post("/grn", data=_grn_form(manual="", items=(("OPC TEST CEMENT", "70", "1000"),)), follow_redirects=False)
    assert _stock(app) == 220.0
    assert _entry_count(app, type_='IN') == 2


# ---------------------------------------------------------------------------
# 6. Customer return increases stock once (exactly one IN movement)
# ---------------------------------------------------------------------------
def test_customer_return_increases_stock(app, client):
    from models import db, Client, MaterialReturn
    _seed_material(app, opening=100)
    _client(app)
    c = client
    # direct sale of 300 first (stock 100 -> -200 with negative allowed)
    sale_form = {
        "client_name": "TEST CLIENT", "category": "Cash", "sale_date": "2026-08-17",
        "driver_name": "TEST DRIVER", "payment_method": "Cash",
        "product_name[]": ["OPC TEST CEMENT"], "qty[]": ["300"], "unit_rate[]": ["1000"],
        "paid_amount": "300000", "payment_account_id": str(_cash_account(app)),
        "allow_negative_stock": "1",
    }
    r = c.post("/add_direct_sale", data=sale_form, follow_redirects=False)
    assert _stock(app) == -200.0
    # material return of 120 -> stock -80
    ret_form = {
        "client_name": "TEST CLIENT", "return_type": "normal", "date": "2026-08-17",
        "material_name[]": ["OPC TEST CEMENT"], "qty[]": ["120"],
        "unit_rate[]": ["1000"], "rent_rate[]": ["0"],
    }
    c.post("/add_material_return", data=ret_form, follow_redirects=False)
    assert _stock(app) == -80.0
    assert _ledger_net(app) == -80.0


# ---------------------------------------------------------------------------
# 11. Booking (reservation) must NOT reduce physical stock
# ---------------------------------------------------------------------------
def test_booking_does_not_deduct_physical_stock(app, client):
    from models import db, Booking
    _seed_material(app, opening=1000)
    _client(app)
    c = client
    booking_form = {
        "client_name": "TEST CLIENT", "amount": "200000", "paid_amount": "0",
        "material_name[]": ["OPC TEST CEMENT"], "qty[]": ["200"], "unit_rate[]": ["1000"],
        "date": "2026-08-17",
    }
    r = c.post("/add_booking", data=booking_form, follow_redirects=False)
    # booking is reservation-only: physical stock unchanged, no OUT entry
    assert _stock(app) == 1000.0
    assert _ledger_net(app) == 1000.0
    assert _entry_count(app, type_='OUT') == 0


# ---------------------------------------------------------------------------
# 4/13. Sale reduces physical stock exactly once
# ---------------------------------------------------------------------------
def test_sale_reduces_stock_exactly_once(app, client):
    _seed_material(app, opening=1000)
    _client(app)
    sale_form = {
        "client_name": "TEST CLIENT", "category": "Cash", "sale_date": "2026-08-17",
        "driver_name": "TEST DRIVER", "payment_method": "Cash",
        "product_name[]": ["OPC TEST CEMENT"], "qty[]": ["300"], "unit_rate[]": ["1000"],
        "paid_amount": "300000", "payment_account_id": str(_cash_account(app)),
        "allow_negative_stock": "1",
    }
    client.post("/add_direct_sale", data=sale_form, follow_redirects=False)
    assert _stock(app) == 700.0                     # 1000 - 300
    assert _ledger_net(app) == 700.0
    assert _entry_count(app, type_='OUT') == 1      # exactly one OUT movement


# ---------------------------------------------------------------------------
# 16. Zero / invalid quantity rejected
# ---------------------------------------------------------------------------
def test_zero_quantity_grn_rejected(app, client):
    _seed_material(app, opening=100)
    r = client.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "0", "1000"),)), follow_redirects=False)
    assert _stock(app) == 100.0
    assert _entry_count(app, type_='IN') == 0


def test_negative_quantity_grn_rejected(app, client):
    _seed_material(app, opening=100)
    r = client.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "-50", "1000"),)), follow_redirects=False)
    assert _stock(app) == 100.0
    assert _entry_count(app, type_='IN') == 0


# ---------------------------------------------------------------------------
# 7. Stored stock == movement ledger after a mixed sequence
# ---------------------------------------------------------------------------
def test_stored_balance_always_matches_ledger(app, client):
    _seed_material(app, opening=100)
    c = client
    c.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "500", "1000"),)), follow_redirects=False)
    c.post("/grn", data=_grn_form(items=(("OPC TEST CEMENT", "200", "1000"),)), follow_redirects=False)
    assert _stock(app) == _ledger_net(app) == 800.0
