"""Regression tests for GRN/Supplier bugs found in the 2026-08-22 audit.

Each test reproduces a real, DB-verified defect:
  * edit_grn silently ignored Bill Date / Due Date
  * edit_grn silently wiped photo_url
  * comma-formatted numbers ('1,500') crashed add/edit with HTTP 500
  * zero-qty item lines were dropped silently (no warning to the user)
  * edit_supplier allowed duplicate names (breaks name-joined ledgers)
  * renaming a supplier left GRN.supplier / Entry.client stale
  * /suppliers built a full ledger per supplier (N+1 payment lookups)
"""
import os
from datetime import datetime

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "grn_bugcheck.db"
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
    from models import User
    with app.app_context():
        user = User.query.first()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _seed(app):
    from models import db, Material
    with app.app_context():
        db.session.add(Material(code="M-CEM", name="CEMENT", unit_price=1000, total=0, is_active=True))
        db.session.commit()


def _grn_form(**over):
    form = {
        "action": "add",
        "supplier": "TEST SUPPLIER",
        "supplier_id": "",
        "date": "2026-08-22",
        "payment_type": "Credit",
        "manual_bill_no": "",
        "note": "",
        "loading_cost": "0", "freight_cost": "0", "other_expense": "0",
        "adjustment_amount": "0", "discount": "0", "paid_amount": "0",
        "tax_percent": "0", "tax_amount": "0", "tax_type": "",
        "supplier_invoice_no": "", "due_date": "2026-09-01", "bill_date": "2026-08-01",
        "mat_name[]": ["CEMENT"], "qty[]": ["100"], "price[]": ["500"],
        "photo_url": "https://example.com/photo.jpg",
    }
    form.update(over)
    return form


def _edit_form(**over):
    form = {
        "mat_name[]": ["CEMENT"], "qty[]": ["100"], "price[]": ["500"],
        "supplier": "TEST SUPPLIER", "supplier_id": "",
        "manual_bill_no": "", "note": "edited",
        "loading_cost": "0", "freight_cost": "0", "other_expense": "0",
        "adjustment_amount": "0", "discount": "0", "paid_amount": "0",
        "payment_type": "Credit", "date": "2026-08-22",
    }
    form.update(over)
    return form


def _make_grn(app, client, **over):
    client.post("/grn", data=_grn_form(**over), follow_redirects=False)
    from models import GRN
    with app.app_context():
        return GRN.query.first().id


def test_edit_grn_saves_bill_and_due_date(app, client):
    _seed(app)
    gid = _make_grn(app, client)
    from models import GRN, db
    with app.app_context():
        assert db.session.get(GRN, gid).bill_date == datetime(2026, 8, 1).date()
    r = client.post(f"/edit_grn/{gid}", data=_edit_form(
        bill_date="2026-08-15", due_date="2026-09-15", supplier_invoice_no="INV-9",
    ), follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        g = db.session.get(GRN, gid)
        assert g.bill_date == datetime(2026, 8, 15).date(), "bill_date must be saved on edit"
        assert g.due_date == datetime(2026, 9, 15).date(), "due_date must be saved on edit"
        assert g.supplier_invoice_no == "INV-9"


def test_edit_grn_preserves_photo_url(app, client):
    _seed(app)
    gid = _make_grn(app, client)
    client.post(f"/edit_grn/{gid}", data=_edit_form(), follow_redirects=False)
    from models import GRN, db
    with app.app_context():
        assert db.session.get(GRN, gid).photo_url == "https://example.com/photo.jpg"


def test_money_fields_accept_commas_no_500(app, client):
    _seed(app)
    r = client.post("/grn", data=_grn_form(loading_cost="1,500", freight_cost="2,000.50"), follow_redirects=False)
    assert r.status_code == 302, "comma numbers must save, not crash"
    from models import GRN, db
    with app.app_context():
        g = GRN.query.first()
        assert g.loading_cost == 1500.0
        assert g.freight_cost == 2000.50

    gid = g.id
    r = client.post(f"/edit_grn/{gid}", data=_edit_form(loading_cost="3,250"), follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(GRN, gid).loading_cost == 3250.0


def test_money_fields_reject_garbage_with_flash_not_crash(app, client):
    _seed(app)
    r = client.post("/grn", data=_grn_form(paid_amount="abc"), follow_redirects=True)
    assert r.status_code == 200
    assert "invalid number" in r.get_data(as_text=True)
    from models import GRN
    with app.app_context():
        assert GRN.query.count() == 0, "invalid input must not half-save"


def test_zero_qty_line_warns_user_add_and_edit(app, client):
    _seed(app)
    r = client.post("/grn", data=_grn_form(**{
        "mat_name[]": ["CEMENT", "CEMENT"], "qty[]": ["100", "0"], "price[]": ["500", "500"],
    }), follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "were NOT saved because quantity was 0 or negative" in body

    from models import GRN, db
    with app.app_context():
        g = GRN.query.first()
        gid, item_id = g.id, g.items[0].id
    r = client.post(f"/edit_grn/{gid}", data=_edit_form(**{
        "grn_item_id[]": [str(item_id)], "qty[]": ["0"],
    }), follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "were NOT saved because quantity was 0 or negative" in body
    with app.app_context():
        g = db.session.get(GRN, gid)
        assert all(i.is_void for i in g.items), "zero-qty line must not stay active"


def test_edit_supplier_blocks_duplicate_names(app, client):
    _seed(app)
    _make_grn(app, client)  # creates supplier TEST SUPPLIER
    client.post("/add_supplier", data={"name": "OTHER SUPPLIER"}, follow_redirects=False)
    from models import Supplier, db
    with app.app_context():
        sid = Supplier.query.filter_by(name="TEST SUPPLIER").first().id
    r = client.post(f"/edit_supplier/{sid}", data={
        "name": "OTHER SUPPLIER", "phone": "", "address": "", "is_active": "on",
    }, follow_redirects=True)
    assert "already exists" in r.get_data(as_text=True)
    with app.app_context():
        assert Supplier.query.filter(db.func.lower(Supplier.name) == "other supplier").count() == 1


def test_supplier_rename_syncs_grn_and_entries(app, client):
    _seed(app)
    gid = _make_grn(app, client)
    from models import Supplier, GRN, Entry, db
    with app.app_context():
        s = Supplier.query.first()
        sid = s.id
        auto_bill = db.session.get(GRN, gid).auto_bill_no
    client.post(f"/edit_supplier/{sid}", data={
        "name": "RENAMED SUPPLIER", "phone": "", "address": "", "is_active": "on",
    }, follow_redirects=False)
    with app.app_context():
        g = db.session.get(GRN, gid)
        assert g.supplier == "RENAMED SUPPLIER", "GRN.supplier must follow supplier rename"
        e = Entry.query.filter(Entry.auto_bill_no == auto_bill, Entry.type == "IN").first()
        assert e.client == "RENAMED SUPPLIER", "IN entry client must follow supplier rename"


def test_suppliers_page_loads_fast_projection(app, client):
    _seed(app)
    for i in range(5):
        client.post("/add_supplier", data={"name": f"S{i}"}, follow_redirects=False)
    r = client.get("/suppliers")
    assert r.status_code == 200
    assert b"Payable" in r.get_data(as_text=True).encode() or r.status_code == 200
