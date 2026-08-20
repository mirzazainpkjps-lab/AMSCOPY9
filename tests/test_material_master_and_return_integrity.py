"""Material Master selection + multi-item return-type integrity.

These tests pin the two targeted guarantees:

1. Sales / bookings / dispatch never invent a Material from typed text.
   Identity is an existing Material Master record (id, or exact name/code).
2. A transaction-level Return Type applies to every selected item, atomically.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


def _flashes(resp):
    html = resp.get_data(as_text=True)
    found = re.findall(
        r'class="([^"]*alert[^"]*)"[^>]*>(.*?)</div>',
        html,
        flags=re.S | re.I,
    )
    out = []
    for cls, raw in found:
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = " ".join(txt.split())
        if txt:
            out.append((cls, txt))
    return out


def _danger(resp):
    return [
        txt for cls, txt in _flashes(resp)
        if "alert-danger" in cls and "alert-dismissible" in cls
    ]


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "material_integrity.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    from app import create_app
    from models import db
    from app.services.schema import _ensure_model_columns, _ensure_default_admin

    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "mi-test",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    with application.app_context():
        db.create_all()
        _ensure_model_columns()
        _ensure_default_admin()
        db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    c = app.test_client()
    rv = c.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "remember_me": "1"},
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303), f"login failed: {rv.status_code}"
    return c


@pytest.fixture()
def seed(app):
    from models import (
        db, Material, MaterialCategory, Client, Account, GRN, GRNItem, Entry,
        Settings,
    )

    with app.app_context():
        cat = MaterialCategory.query.filter_by(name="General").first()
        if not cat:
            cat = MaterialCategory(name="General")
            db.session.add(cat)
            db.session.flush()

        mats = {}
        for code, name, price in [
            ("MI-6MM", "6MM STEEL", 247),
            ("MI-8MM", "8MM STEEL", 250),
            ("MI-10MM", "10MM STEEL", 255),
        ]:
            mat = Material(
                code=code, name=name, unit_price=price, total=0,
                category_id=cat.id, is_active=True,
            )
            db.session.add(mat)
            mats[name] = mat

        cli = Client(code="MI-CL-1", name="Integrity Client", is_active=True, opening_balance=0)
        db.session.add(cli)
        acc = Account(
            name="MI CASH", category="cash", account_type="cash",
            balance=1_000_000.0, is_active=True,
        )
        db.session.add(acc)
        db.session.flush()

        g = GRN(supplier="MI Supplier", auto_bill_no="MI-GRN-1",
                date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(g)
        db.session.flush()
        for name in mats:
            db.session.add(GRNItem(
                grn_id=g.id, mat_name=name, qty=500, price_at_time=200, is_void=False,
            ))
            mats[name].total = 500
            db.session.add(Entry(
                date="2026-01-01", time="08:00:00", type="IN", material=name,
                client="MI Supplier", qty=500, bill_no="", auto_bill_no="MI-GRN-1",
                created_by="test", is_void=False,
            ))

        settings = Settings.query.first()
        if not settings:
            settings = Settings()
            db.session.add(settings)
        settings.allow_global_negative_stock = True
        db.session.commit()
        return {
            "materials": {name: m.id for name, m in mats.items()},
            "client_code": cli.code,
            "client_name": cli.name,
            "acc_id": acc.id,
        }


def _post(client, path, data):
    return client.post(path, data=data, follow_redirects=True)


def _material_count(app):
    from models import Material
    with app.app_context():
        return Material.query.count()


def _material_names(app):
    from models import Material
    with app.app_context():
        return sorted(m.name for m in Material.query.all())


def test_sale_does_not_create_material_from_typed_text(app, client, seed):
    before = _material_count(app)
    rv = _post(client, "/add_direct_sale", {
        "client_code": seed["client_code"],
        "category": "Cash",
        "driver_name": "MI Driver",
        "product_name[]": ["6mm"],
        "qty[]": ["2"],
        "unit_rate[]": ["247"],
        "paid_amount": "494",
        "payment_method": "Cash",
        "payment_account_id": str(seed["acc_id"]),
        "has_bill": "0",
        "create_invoice": "0",
    })
    assert _danger(rv), _flashes(rv)
    assert "Material not selected" in " ".join(_danger(rv))
    assert _material_count(app) == before
    assert "6mm" not in _material_names(app)


def test_sale_selects_existing_material_by_id_without_duplicating(app, client, seed):
    before = _material_count(app)
    mid = seed["materials"]["6MM STEEL"]
    rv = _post(client, "/add_direct_sale", {
        "client_code": seed["client_code"],
        "category": "Cash",
        "driver_name": "MI Driver",
        "product_name[]": ["6MM STEEL"],
        "material_id[]": [str(mid)],
        "qty[]": ["2"],
        "unit_rate[]": ["247"],
        "paid_amount": "494",
        "payment_method": "Cash",
        "payment_account_id": str(seed["acc_id"]),
        "has_bill": "0",
        "create_invoice": "0",
    })
    assert not _danger(rv), _flashes(rv)
    assert _material_count(app) == before
    from models import DirectSale
    with app.app_context():
        sale = DirectSale.query.filter_by(client_code=seed["client_code"], is_void=False).first()
        assert sale is not None
        assert [it.product_name for it in sale.items] == ["6MM STEEL"]


def test_sale_stale_material_id_is_rejected(app, client, seed):
    before = _material_count(app)
    mid = seed["materials"]["6MM STEEL"]
    rv = _post(client, "/add_direct_sale", {
        "client_code": seed["client_code"],
        "category": "Cash",
        "driver_name": "MI Driver",
        "product_name[]": ["6mm"],
        "material_id[]": [str(mid)],
        "qty[]": ["2"],
        "unit_rate[]": ["247"],
        "paid_amount": "494",
        "payment_method": "Cash",
        "payment_account_id": str(seed["acc_id"]),
        "has_bill": "0",
        "create_invoice": "0",
    })
    assert _danger(rv), _flashes(rv)
    assert "Material not selected" in " ".join(_danger(rv))
    assert _material_count(app) == before


def test_booking_does_not_create_material_from_typed_text(app, client, seed):
    before = _material_count(app)
    rv = _post(client, "/add_booking", {
        "client_code": seed["client_code"],
        "material_name[]": ["6mm"],
        "qty[]": ["10"],
        "unit_rate[]": ["247"],
        "amount": "2470",
        "paid_amount": "0",
    })
    assert _danger(rv), _flashes(rv)
    assert "Material not selected" in " ".join(_danger(rv))
    assert _material_count(app) == before
    from models import Booking
    with app.app_context():
        assert Booking.query.filter_by(is_void=False).count() == 0


def _book_and_deliver(client, seed, materials, booked_qty=20, deliver_qty=8):
    names = list(materials)
    rv = _post(client, "/add_booking", {
        "client_code": seed["client_code"],
        "material_name[]": names,
        "qty[]": [str(booked_qty)] * len(names),
        "unit_rate[]": ["247"] * len(names),
        "amount": str(247 * booked_qty * len(names)),
        "paid_amount": "1000",
        "payment_method": "Cash",
        "payment_account_id": str(seed["acc_id"]),
    })
    assert not _danger(rv), _flashes(rv)
    rv = _post(client, "/add_direct_sale", {
        "client_code": seed["client_code"],
        "category": "Booking Delivery",
        "driver_name": "MI Driver",
        "product_name[]": names,
        "qty[]": [str(deliver_qty)] * len(names),
        "unit_rate[]": ["247"] * len(names),
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
    })
    assert not _danger(rv), _flashes(rv)


def _credit_sale(client, seed, materials, qty=5):
    names = list(materials)
    rv = _post(client, "/add_direct_sale", {
        "client_code": seed["client_code"],
        "category": "Credit Customer",
        "driver_name": "MI Driver",
        "product_name[]": names,
        "qty[]": [str(qty)] * len(names),
        "unit_rate[]": ["247"] * len(names),
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "MI-CR-1",
    })
    assert not _danger(rv), _flashes(rv)


def _return_rows(app, client_name):
    from models import MaterialReturn, Entry
    from sqlalchemy import func
    with app.app_context():
        rows = MaterialReturn.query.filter(
            func.lower(func.trim(MaterialReturn.client_name)) == client_name.strip().lower(),
            MaterialReturn.is_void == False,
        ).order_by(MaterialReturn.id.asc()).all()
        out = []
        for r in rows:
            entries = Entry.query.filter(
                Entry.nimbus_no == "Material Return",
                Entry.bill_no.in_([x for x in [r.manual_bill_no, r.auto_bill_no, f"RTN-{r.id}"] if x]),
                Entry.is_void == False,
            ).all()
            out.append({
                "id": r.id,
                "return_type": r.return_type,
                "item_names": [it.material_name for it in r.items],
                "entry_txn": [e.transaction_category for e in entries],
                "entry_client": [e.client_category for e in entries],
            })
        return out


def test_multi_item_booked_return_applies_booked_to_all(app, client, seed):
    names = ["6MM STEEL", "8MM STEEL", "10MM STEEL"]
    _book_and_deliver(client, seed, names)
    before_stock = {}
    from models import Material
    with app.app_context():
        for n in names:
            before_stock[n] = float(Material.query.filter_by(name=n).first().total or 0)

    rv = _post(client, "/add_material_return", {
        "client_code": seed["client_code"],
        "return_type": "booked",
        "material_name[]": names,
        "qty[]": ["3", "2", "1"],
        "unit_rate[]": ["", "", ""],
        "rent_rate[]": ["200", "200", "200"],
    })
    assert not _danger(rv), _flashes(rv)
    rows = _return_rows(app, seed["client_name"])
    assert len(rows) == 1
    assert rows[0]["return_type"] == "booked"
    assert rows[0]["item_names"] == names
    assert rows[0]["entry_txn"] == ["Booked Return"] * 3
    assert rows[0]["entry_client"] == ["Booked Return"] * 3
    with app.app_context():
        assert float(Material.query.filter_by(name="6MM STEEL").first().total or 0) == before_stock["6MM STEEL"] + 3
        assert float(Material.query.filter_by(name="8MM STEEL").first().total or 0) == before_stock["8MM STEEL"] + 2
        assert float(Material.query.filter_by(name="10MM STEEL").first().total or 0) == before_stock["10MM STEEL"] + 1


def test_multi_item_normal_return_applies_normal_to_all(app, client, seed):
    names = ["6MM STEEL", "8MM STEEL"]
    _credit_sale(client, seed, names, qty=6)
    rv = _post(client, "/add_material_return", {
        "client_code": seed["client_code"],
        "return_type": "normal",
        "material_name[]": names,
        "qty[]": ["2", "3"],
        "unit_rate[]": ["247", "250"],
        "rent_rate[]": ["0", "0"],
    })
    assert not _danger(rv), _flashes(rv)
    rows = _return_rows(app, seed["client_name"])
    assert len(rows) == 1
    assert rows[0]["return_type"] == "normal"
    assert rows[0]["item_names"] == names
    assert rows[0]["entry_txn"] == ["Return"] * 2
    assert rows[0]["entry_client"] == ["Material Return"] * 2


def test_mixed_original_sources_follow_selected_booked_type(app, client, seed):
    """Booked type wins for every selected line even if originals differ."""
    _book_and_deliver(client, seed, ["6MM STEEL", "8MM STEEL"])
    _credit_sale(client, seed, ["10MM STEEL"], qty=5)
    # 10MM STEEL was never booked-delivered, so a booked return of all three must fail atomically.
    before = _return_rows(app, seed["client_name"])
    from models import Material, Entry
    with app.app_context():
        stock_before = {m.name: float(m.total or 0) for m in Material.query.all()}
        entry_count = Entry.query.filter(Entry.nimbus_no == "Material Return", Entry.is_void == False).count()

    rv = _post(client, "/add_material_return", {
        "client_code": seed["client_code"],
        "return_type": "booked",
        "material_name[]": ["6MM STEEL", "8MM STEEL", "10MM STEEL"],
        "qty[]": ["1", "1", "1"],
        "unit_rate[]": ["", "", ""],
        "rent_rate[]": ["200", "200", "200"],
    })
    assert _danger(rv), _flashes(rv)
    assert _return_rows(app, seed["client_name"]) == before
    with app.app_context():
        stock_after = {m.name: float(m.total or 0) for m in Material.query.all()}
        assert stock_after == stock_before
        assert Entry.query.filter(Entry.nimbus_no == "Material Return", Entry.is_void == False).count() == entry_count

    # Selecting only the booked-delivered items applies BOOKED to every line.
    rv = _post(client, "/add_material_return", {
        "client_code": seed["client_code"],
        "return_type": "booked",
        "material_name[]": ["6MM STEEL", "8MM STEEL"],
        "qty[]": ["1", "1"],
        "unit_rate[]": ["", ""],
        "rent_rate[]": ["200", "200"],
    })
    assert not _danger(rv), _flashes(rv)
    rows = _return_rows(app, seed["client_name"])
    assert len(rows) == 1
    assert rows[0]["return_type"] == "booked"
    assert rows[0]["entry_txn"] == ["Booked Return", "Booked Return"]


def test_partial_invalid_return_rolls_back_everything(app, client, seed):
    _book_and_deliver(client, seed, ["6MM STEEL", "8MM STEEL"], deliver_qty=2)
    from models import Material, Entry, MaterialReturn
    with app.app_context():
        stock_before = {m.name: float(m.total or 0) for m in Material.query.all()}

    rv = _post(client, "/add_material_return", {
        "client_code": seed["client_code"],
        "return_type": "booked",
        "material_name[]": ["6MM STEEL", "8MM STEEL"],
        "qty[]": ["1", "50"],  # 50 exceeds booked-delivered qty of 2
        "unit_rate[]": ["", ""],
        "rent_rate[]": ["200", "200"],
    })
    assert _danger(rv), _flashes(rv)
    with app.app_context():
        assert MaterialReturn.query.filter_by(is_void=False).count() == 0
        assert Entry.query.filter(Entry.nimbus_no == "Material Return", Entry.is_void == False).count() == 0
        stock_after = {m.name: float(m.total or 0) for m in Material.query.all()}
        assert stock_after == stock_before


def test_conflicting_per_item_return_types_are_rejected(app, client, seed):
    _book_and_deliver(client, seed, ["6MM STEEL", "8MM STEEL"])
    rv = _post(client, "/add_material_return", {
        "client_code": seed["client_code"],
        "return_type": ["booked", "normal"],
        "material_name[]": ["6MM STEEL", "8MM STEEL"],
        "qty[]": ["1", "1"],
        "unit_rate[]": ["", ""],
        "rent_rate[]": ["200", "200"],
    })
    assert _danger(rv), _flashes(rv)
    assert "same for every selected item" in " ".join(_danger(rv)).lower()
    from models import MaterialReturn
    with app.app_context():
        assert MaterialReturn.query.filter_by(is_void=False).count() == 0
