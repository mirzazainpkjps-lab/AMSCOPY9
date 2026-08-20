"""Regression coverage for list pages that lazy-load per-row dialogs."""
from __future__ import annotations

import pytest

from app import create_app
from models import (
    Booking,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    PendingBill,
    db,
)


@pytest.fixture()
def app_with_sales_rows(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'lazy-modals.db'}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "lazy-modal-test",
            "SESSION_COOKIE_SECURE": False,
            "SESSION_COOKIE_SAMESITE": "Lax",
        }
    )
    with application.app_context():
        client = Client(code="LAZY-1", name="Lazy Modal Client", is_active=True)
        booking = Booking(
            client_name=client.name,
            amount=150,
            paid_amount=25,
            manual_bill_no="LAZY-BK-1",
            is_void=False,
        )
        booking.items.append(BookingItem(material_name="Cement", qty=3, price_at_time=50))
        sale = DirectSale(
            client_name=client.name,
            client_code=client.code,
            category="Credit Customer",
            amount=200,
            paid_amount=0,
            manual_bill_no="LAZY-DS-1",
            is_void=False,
        )
        sale.items.append(DirectSaleItem(product_name="Cement", qty=4, price_at_time=50))
        bill = PendingBill(
            client_code=client.code,
            client_name=client.name,
            bill_no="LAZY-PB-1",
            amount=200,
            is_void=False,
        )
        db.session.add_all([client, booking, sale, bill])
        db.session.commit()
        ids = {
            "client": client.id,
            "booking": booking.id,
            "sale": sale.id,
            "bill": bill.id,
        }
        yield application, ids
        db.session.remove()


def _login_admin(http):
    response = http.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_list_pages_exclude_per_row_modals_and_lazy_endpoints_render_one(app_with_sales_rows):
    application, ids = app_with_sales_rows
    http = application.test_client()
    _login_admin(http)

    list_expectations = [
        ("/direct_sales", b'data-edit-sale-url=', b'id="editSaleModal'),
        ("/bookings", b'data-edit-booking-url=', b'id="editBookingModal'),
        ("/pending_bills", b'data-pending-bill-modal-url=', b'id="viewBillModal'),
        ("/clients", b'data-client-modal-url=', b'id="editCModal'),
    ]
    for url, lazy_attribute, pre_rendered_modal in list_expectations:
        response = http.get(url)
        assert response.status_code == 200
        assert lazy_attribute in response.data
        assert pre_rendered_modal not in response.data

    endpoint_expectations = [
        (f"/direct_sales/{ids['sale']}/edit-modal", f'id="editSaleModal{ids["sale"]}"'.encode()),
        (f"/bookings/{ids['booking']}/edit-modal", f'id="editBookingModal{ids["booking"]}"'.encode()),
        (f"/pending_bills/{ids['bill']}/modals", f'id="viewBillModal{ids["bill"]}"'.encode()),
        (f"/clients/{ids['client']}/modals", f'id="editCModal{ids["client"]}"'.encode()),
    ]
    for url, expected_modal in endpoint_expectations:
        response = http.get(url)
        assert response.status_code == 200
        assert expected_modal in response.data
        assert b'name="csrf_token"' in response.data


def test_direct_sales_client_filter_accepts_combobox_code(app_with_sales_rows):
    """Selecting a client code must find sales stored under its client name."""
    application, _ids = app_with_sales_rows
    http = application.test_client()
    _login_admin(http)

    by_code = http.get("/direct_sales?client=LAZY-1")
    assert by_code.status_code == 200
    assert b"LAZY-DS-1" in by_code.data

    # Free-text fallback searches both client names and partial client codes.
    by_partial_code = http.get("/direct_sales?client=LAZY")
    assert by_partial_code.status_code == 200
    assert b"LAZY-DS-1" in by_partial_code.data


def test_lazy_modal_endpoints_require_authentication(app_with_sales_rows):
    application, ids = app_with_sales_rows
    http = application.test_client()

    for url in (
        f"/direct_sales/{ids['sale']}/edit-modal",
        f"/bookings/{ids['booking']}/edit-modal",
        f"/pending_bills/{ids['bill']}/modals",
        f"/clients/{ids['client']}/modals",
    ):
        response = http.get(url, follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
