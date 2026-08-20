"""Booked material returns must become cancellable booking qty.

Regression for the ZAFAR CONT (FBMCL-00168) case: booking of 20.7 kg 6MM STEEL
fully dispatched (MB NO.8393), then 11.4 kg returned back into the booked
material (MB NO.8746, transaction_category='Booked Return'). The cancel-booking
preview/handler must offer 11.4 kg for cancellation instead of reporting
"No remaining booking items to cancel".
"""
import os
from datetime import datetime

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "booking-cancel-return.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    from app import create_app
    from models import db
    from app.services.schema import _ensure_model_columns

    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test",
        "LOGIN_DISABLED": True,
    })
    with application.app_context():
        db.create_all()
        _ensure_model_columns()
    yield application


def _seed_client_with_returned_booked_material():
    """20.7 booked @100 -> 20.7 dispatched -> 11.4 booked-returned."""
    from models import db, Client, Booking, BookingItem, Entry, PendingBill

    cli = Client(code="FBMCL-00168", name="ABDUL REHMAN SB JPS", is_active=True)
    db.session.add(cli)
    db.session.flush()

    bk = Booking(
        client_name=cli.name,
        amount=2070.0,
        paid_amount=1000.0,
        manual_bill_no="MB NO.6223",
        date_posted=datetime(2026, 2, 20, 15, 40),
        is_void=False,
    )
    db.session.add(bk)
    db.session.flush()
    db.session.add(BookingItem(booking_id=bk.id, material_name="6MM STEEL", qty=20.7, price_at_time=100.0))
    db.session.add(PendingBill(
        client_code=cli.code,
        client_name=cli.name,
        bill_no="MB NO.6223",
        amount=1070.0,
        reason="Booking",
        is_manual=True,
        created_at="2026-02-20 15:40",
        created_by="seed",
    ))

    # Dispatch of the full booked qty (booking delivery).
    db.session.add(Entry(
        date="2026-05-18", time="09:00:00", type="OUT", material="6MM STEEL",
        client=cli.name, client_code=cli.code, client_category="Booking Delivery",
        qty=20.7, bill_no="MB NO.8393", nimbus_no="Direct Sale", is_void=False,
    ))
    # Booked material return credits 11.4 back into the booking pool.
    db.session.add(Entry(
        date="2026-05-31", time="08:18:00", type="IN", material="6MM STEEL",
        client=cli.name, client_code=cli.code, client_category="Booked Return",
        qty=11.4, bill_no="MB NO.8746", nimbus_no="Material Return",
        transaction_category="Booked Return", is_void=False,
    ))
    db.session.commit()
    return cli, bk


def test_returned_booked_qty_appears_in_cancel_plan(app):
    from app.services.booking_cancel_plan import build_client_booking_cancel_plan
    from models import db, Client

    with app.app_context():
        cli, _bk = _seed_client_with_returned_booked_material()
        rows, total, total_qty = build_client_booking_cancel_plan(cli)

        assert len(rows) == 1
        assert rows[0]["material"] == "6MM STEEL"
        assert rows[0]["remaining_qty"] == pytest.approx(11.4)
        assert rows[0]["rate"] == pytest.approx(100.0)
        assert rows[0]["amount"] == pytest.approx(1140.0)
        assert total_qty == pytest.approx(11.4)
        assert total == pytest.approx(1140.0)


def test_ledger_preview_shows_cancel_value_for_returned_qty(app):
    with app.app_context():
        cli, _bk = _seed_client_with_returned_booked_material()
        client_id = cli.id

    http = app.test_client()
    login = http.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"}, follow_redirects=False)
    assert login.status_code == 302

    # financial_ledger view renders client_ledger.html with the cancel modal.
    response = http.get(f"/ledger/{client_id}")
    assert response.status_code == 200
    # Cancel modal: value 1,140.00 and remaining qty 11.40 must be offered.
    assert b"1,140.00" in response.data
    assert b"11.40" in response.data
    assert b"No remaining booking items to cancel." not in response.data


def test_cancel_booking_post_reduces_amount_and_pending(app):
    from models import db, Booking, BookingItem, Entry, PendingBill

    with app.app_context():
        cli, bk = _seed_client_with_returned_booked_material()
        client_id = cli.id
        booking_id = bk.id
        item_id = BookingItem.query.filter_by(booking_id=booking_id).one().id

    http = app.test_client()
    login = http.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"}, follow_redirects=False)
    assert login.status_code == 302

    response = http.post(f"/client_booking_cancel/{client_id}", data={}, follow_redirects=True)
    assert response.status_code == 200

    with app.app_context():
        from models import Client

        item = db.session.get(BookingItem, item_id)
        assert float(item.qty) == pytest.approx(9.3)  # 20.7 booked - 11.4 returned cancellation

        booking = db.session.get(Booking, booking_id)
        assert float(booking.amount) == pytest.approx(930.0)  # 9.3 * 100

        cancel_entries = Entry.query.filter(
            Entry.client_code == cli.code, Entry.type == "CANCEL", Entry.is_void == False
        ).all()
        assert len(cancel_entries) == 1
        assert float(cancel_entries[0].qty) == pytest.approx(11.4)
        assert cancel_entries[0].material == "6MM STEEL"

        # Booking due dropped below what was paid -> pending bill removed.
        assert PendingBill.query.filter_by(client_code=cli.code, bill_no="MB NO.6223").count() == 0

        # Nothing left to cancel afterwards.
        from app.services.booking_cancel_plan import build_client_booking_cancel_plan
        rows, _total, _qty = build_client_booking_cancel_plan(db.session.get(Client, client_id))
        assert rows == []


def test_normal_return_does_not_unlock_booking_cancel(app):
    """Only *booked* returns credit the booking pool; normal stock returns must not."""
    from models import db, Client, Booking, BookingItem, Entry
    from app.services.booking_cancel_plan import build_client_booking_cancel_plan

    with app.app_context():
        cli = Client(code="C-NORM", name="Normal Return Client", is_active=True)
        db.session.add(cli)
        db.session.flush()
        bk = Booking(client_name=cli.name, amount=500.0, paid_amount=0.0,
                     manual_bill_no="NB-1", date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(bk)
        db.session.flush()
        db.session.add(BookingItem(booking_id=bk.id, material_name="6MM STEEL", qty=10.0, price_at_time=50.0))
        db.session.add(Entry(
            date="2026-01-05", time="09:00:00", type="OUT", material="6MM STEEL",
            client=cli.name, client_code=cli.code, client_category="Booking Delivery",
            qty=10.0, bill_no="NB-DISP", nimbus_no="Direct Sale", is_void=False,
        ))
        # Normal (cash/credit) stock return — unrelated to the booking pool.
        db.session.add(Entry(
            date="2026-01-10", time="09:00:00", type="IN", material="6MM STEEL",
            client=cli.name, client_code=cli.code, client_category="Material Return",
            qty=4.0, bill_no="NB-RET", nimbus_no="Material Return",
            transaction_category="Return", is_void=False,
        ))
        db.session.commit()

        rows, total, total_qty = build_client_booking_cancel_plan(cli)
        assert rows == []
        assert total == 0
        assert total_qty == 0
