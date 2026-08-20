"""Regression: editing a booking that already has deliveries must not 500.

The bookings edit form used to DELETE every BookingItem and recreate them.
SQLite FK enforcement then raised IntegrityError whenever a BookingAllocation
still pointed at one of those lines — exactly the SB-BK-1288 / discount-edit
failure.
"""
from __future__ import annotations

import os
import re

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
    return [txt for cls, txt in _flashes(resp) if "alert-danger" in cls]


@pytest.fixture()
def app(tmp_path):
    from app import create_app
    from models import db
    from app.services.schema import _ensure_model_columns, _ensure_default_admin

    db_file = tmp_path / "booking-edit.db"
    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "booking-edit-test",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    with application.app_context():
        db.create_all()
        _ensure_model_columns()
        _ensure_default_admin()
        db.session.commit()
        yield application
        db.session.remove()


@pytest.fixture()
def http(app):
    client = app.test_client()
    rv = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "remember_me": "1"},
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303), f"login failed: {rv.status_code}"
    return client


@pytest.fixture()
def booked(app):
    """Booking with four lines; LABOUR already delivered (allocation present)."""
    from datetime import datetime
    from models import (
        db, Client, Material, MaterialCategory, Account, Booking, BookingItem,
        DirectSale, DirectSaleItem, BookingAllocation,
    )

    with app.app_context():
        cat = MaterialCategory(name="General")
        db.session.add(cat)
        db.session.flush()
        mats = {}
        for name, price in [
            ("6MM STEEL", 247),
            ("WIRE", 380),
            ("RENT-STEEL", 2500),
            ("LABOUR", 1000),
        ]:
            m = Material(code=f"BK-{name[:6]}", name=name, unit_price=price,
                         total=1000, category_id=cat.id, is_active=True)
            db.session.add(m)
            mats[name] = m
        client = Client(code="BK-CL-EDIT", name="ALI SHAN SB RANGRA", is_active=True)
        acc = Account(name="BK CASH", category="cash", account_type="cash",
                      type="cash", balance=1_000_000, is_active=True)
        db.session.add_all([client, acc])
        booking = Booking(
            client_name=client.name,
            amount=67125.9,
            paid_amount=67000.0,
            discount=0.0,
            discount_reason="",
            manual_bill_no="MB NO.9817",
            auto_bill_no="SB-BK-1288",
            date_posted=datetime(2026, 6, 27, 16, 18),
            is_void=False,
        )
        booking.items.extend([
            BookingItem(material_name="6MM STEEL", qty=199.7, price_at_time=247),
            BookingItem(material_name="WIRE", qty=10, price_at_time=380),
            BookingItem(material_name="RENT-STEEL", qty=4, price_at_time=2500),
            BookingItem(material_name="LABOUR", qty=4, price_at_time=1000),
        ])
        sale = DirectSale(
            client_name=client.name,
            client_code=client.code,
            category="Booking Delivery",
            amount=0,
            paid_amount=0,
            manual_bill_no="SB-DS-LABOUR",
            driver_name="Driver",
            is_void=False,
        )
        sale_item = DirectSaleItem(product_name="LABOUR", qty=4, price_at_time=0)
        sale.items.append(sale_item)
        db.session.add_all([booking, sale])
        db.session.flush()
        labour = next(it for it in booking.items if it.material_name == "LABOUR")
        db.session.add(BookingAllocation(
            sale_id=sale.id,
            sale_item_id=sale_item.id,
            booking_item_id=labour.id,
            qty=4,
            is_void=False,
        ))
        db.session.commit()
        return {
            "booking_id": booking.id,
            "client_code": client.code,
            "client_id": client.id,
            "acc_id": acc.id,
            "item_ids": {it.material_name: it.id for it in booking.items},
            "alloc_id": BookingAllocation.query.filter_by(booking_item_id=labour.id).one().id,
        }


def _edit_payload(booked, **overrides):
    data = {
        "client_code": booked["client_code"],
        "material_name[]": ["6MM STEEL", "WIRE", "RENT-STEEL", "LABOUR"],
        "qty[]": ["199.7", "10", "4", "4"],
        "unit_rate[]": ["247", "380", "2500", "1000"],
        "booking_item_id[]": [
            str(booked["item_ids"]["6MM STEEL"]),
            str(booked["item_ids"]["WIRE"]),
            str(booked["item_ids"]["RENT-STEEL"]),
            str(booked["item_ids"]["LABOUR"]),
        ],
        "amount": "67125.9",
        "paid_amount": "67000",
        "discount": "200",
        "discount_reason": "DISCOUNT",
        "manual_bill_no": "MB NO.9817",
        "note": "",
        "date": "2026-06-27T16:18",
    }
    data.update(overrides)
    return data


def test_edit_discount_on_delivered_booking_does_not_500(app, http, booked):
    rv = http.post(
        f"/edit_bill/Booking/{booked['booking_id']}",
        data=_edit_payload(booked),
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "Internal Server Error" not in rv.get_data(as_text=True)
    assert not _danger(rv), _flashes(rv)
    assert any("Booking updated" in txt for _cls, txt in _flashes(rv)), _flashes(rv)

    from models import Booking, BookingItem, BookingAllocation
    with app.app_context():
        booking = Booking.query.get(booked["booking_id"])
        assert abs(float(booking.discount or 0) - 200) < 0.001
        assert (booking.discount_reason or "") == "DISCOUNT"
        items = {it.material_name: it for it in BookingItem.query.filter_by(booking_id=booking.id)}
        assert set(items) == {"6MM STEEL", "WIRE", "RENT-STEEL", "LABOUR"}
        # IDs must be preserved so the delivery allocation stays valid.
        assert items["LABOUR"].id == booked["item_ids"]["LABOUR"]
        alloc = BookingAllocation.query.get(booked["alloc_id"])
        assert alloc is not None and alloc.is_void is False
        assert alloc.booking_item_id == booked["item_ids"]["LABOUR"]


def test_edit_without_payment_account_on_legacy_paid_booking(app, http, booked):
    """Historical bookings often have Paid Now but no receive-into account."""
    rv = http.post(
        f"/edit_bill/Booking/{booked['booking_id']}",
        data=_edit_payload(booked, discount="125.90", discount_reason="ROUND OFF"),
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert not _danger(rv), _flashes(rv)
    from models import Booking
    with app.app_context():
        booking = Booking.query.get(booked["booking_id"])
        assert abs(float(booking.discount or 0) - 125.90) < 0.001
        assert booking.receive_in_account_id is None


def test_cannot_reduce_delivered_qty(app, http, booked):
    rv = http.post(
        f"/edit_bill/Booking/{booked['booking_id']}",
        data=_edit_payload(booked, **{"qty[]": ["199.7", "10", "4", "1"]}),
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert _danger(rv)
    assert any("delivered" in txt.lower() for txt in _danger(rv))
    from models import BookingItem
    with app.app_context():
        labour = BookingItem.query.get(booked["item_ids"]["LABOUR"])
        assert abs(float(labour.qty or 0) - 4) < 0.001


def test_cannot_remove_delivered_item(app, http, booked):
    rv = http.post(
        f"/edit_bill/Booking/{booked['booking_id']}",
        data=_edit_payload(
            booked,
            **{
                "material_name[]": ["6MM STEEL", "WIRE", "RENT-STEEL"],
                "qty[]": ["199.7", "10", "4"],
                "unit_rate[]": ["247", "380", "2500"],
                "booking_item_id[]": [
                    str(booked["item_ids"]["6MM STEEL"]),
                    str(booked["item_ids"]["WIRE"]),
                    str(booked["item_ids"]["RENT-STEEL"]),
                ],
            },
        ),
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert _danger(rv)
    from models import BookingItem, BookingAllocation
    with app.app_context():
        assert BookingItem.query.get(booked["item_ids"]["LABOUR"]) is not None
        assert BookingAllocation.query.get(booked["alloc_id"]) is not None


def test_can_add_unallocated_item(app, http, booked):
    rv = http.post(
        f"/edit_bill/Booking/{booked['booking_id']}",
        data=_edit_payload(
            booked,
            **{
                "material_name[]": ["6MM STEEL", "WIRE", "RENT-STEEL", "LABOUR", "WIRE"],
                "qty[]": ["199.7", "10", "4", "4", "2"],
                "unit_rate[]": ["247", "380", "2500", "1000", "380"],
                "booking_item_id[]": [
                    str(booked["item_ids"]["6MM STEEL"]),
                    str(booked["item_ids"]["WIRE"]),
                    str(booked["item_ids"]["RENT-STEEL"]),
                    str(booked["item_ids"]["LABOUR"]),
                    "",
                ],
                "amount": "67885.9",
            },
        ),
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert not _danger(rv), _flashes(rv)
    from models import BookingItem
    with app.app_context():
        wires = BookingItem.query.filter_by(
            booking_id=booked["booking_id"], material_name="WIRE"
        ).all()
        assert len(wires) == 2
        assert BookingItem.query.get(booked["item_ids"]["LABOUR"]).id == booked["item_ids"]["LABOUR"]


def test_ledger_discount_edit(app, http, booked):
    rv = http.post(
        f"/edit_ledger_transaction/Booking/{booked['booking_id']}",
        data={
            "manual_bill_no": "MB NO.9817",
            "discount": "50",
            "discount_reason": "LEDGER",
            "date_posted": "2026-06-27T16:18",
            "note": "from ledger",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert not _danger(rv), _flashes(rv)
    from models import Booking
    with app.app_context():
        booking = Booking.query.get(booked["booking_id"])
        assert abs(float(booking.discount or 0) - 50) < 0.001
        assert booking.discount_reason == "LEDGER"
        assert booking.note == "from ledger"
