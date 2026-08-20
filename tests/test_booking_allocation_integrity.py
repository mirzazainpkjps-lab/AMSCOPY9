"""Regression coverage for controlled booking-allocation FK remediation."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.services.allocation_integrity import (
    audit_booking_allocation_integrity,
    repair_dangling_booking_allocations,
)
from models import (
    Booking,
    BookingAllocation,
    BookingAllocationRepairArchive,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    Entry,
    db,
)


@pytest.fixture()
def allocation_app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'allocation-integrity.db'}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "allocation-integrity-test",
            "SESSION_COOKIE_SECURE": False,
        }
    )
    with application.app_context():
        yield application
        db.session.remove()


def _force_legacy_parent_delete(application, table, row_id):
    """Simulate a historical write made before FK enforcement was enabled."""
    db.session.remove()
    uri = application.config["SQLALCHEMY_DATABASE_URI"]
    path = Path(uri.removeprefix("sqlite:///"))
    connection = sqlite3.connect(path)
    try:
        connection.execute(f'DELETE FROM "{table}" WHERE id = ?', (row_id,))
        connection.commit()
    finally:
        connection.close()


def _linked_allocation(*, is_void=False):
    client = Client(code="ALLOC-1", name="Allocation Client", is_active=True)
    booking = Booking(
        client_name=client.name,
        amount=100,
        paid_amount=0,
        manual_bill_no="ALLOC-BK-1",
        is_void=False,
    )
    booking_item = BookingItem(material_name="Cement", qty=2, price_at_time=50)
    booking.items.append(booking_item)
    sale = DirectSale(
        client_name=client.name,
        client_code=client.code,
        category="Booking Delivery",
        amount=0,
        paid_amount=0,
        manual_bill_no="ALLOC-DS-1",
        driver_name="Driver",
        is_void=False,
    )
    sale_item = DirectSaleItem(product_name="Cement", qty=2, price_at_time=0)
    sale.items.append(sale_item)
    db.session.add_all([client, booking, sale])
    db.session.flush()
    allocation = BookingAllocation(
        sale_id=sale.id,
        sale_item_id=sale_item.id,
        booking_item_id=booking_item.id,
        qty=2,
        is_void=is_void,
    )
    db.session.add(allocation)
    db.session.commit()
    return client, booking, booking_item, sale, sale_item, allocation


def test_sqlite_connections_enforce_foreign_keys(allocation_app):
    _client, _booking, booking_item, _sale, _sale_item, _allocation = _linked_allocation()
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    with pytest.raises(IntegrityError):
        db.session.execute(
            text("DELETE FROM booking_item WHERE id = :id"),
            {"id": booking_item.id},
        )
        db.session.commit()
    db.session.rollback()


def test_controlled_repair_archives_exact_row_and_preserves_financial_parents(allocation_app):
    _client, booking, booking_item, sale, sale_item, allocation = _linked_allocation()
    ids = {
        "booking": booking.id,
        "booking_item": booking_item.id,
        "sale": sale.id,
        "sale_item": sale_item.id,
        "allocation": allocation.id,
    }
    _force_legacy_parent_delete(allocation_app, "booking_item", booking_item.id)

    findings = audit_booking_allocation_integrity()
    assert len(findings) == 1
    assert findings[0]["row_pk"] == ids["allocation"]
    assert findings[0]["violating_fields"] == ["booking_item_id"]
    assert findings[0]["repair_eligible"] is True

    result = repair_dangling_booking_allocations(run_id="test-repair")
    db.session.commit()

    assert result["row_ids"] == [ids["allocation"]]
    assert db.session.get(BookingAllocation, ids["allocation"]) is None
    archive = BookingAllocationRepairArchive.query.filter_by(repair_run_id="test-repair").one()
    assert archive.original_allocation_id == ids["allocation"]
    assert json.loads(archive.source_row_json) == {
        "booking_item_id": ids["booking_item"],
        "id": ids["allocation"],
        "is_void": False,
        "qty": 2.0,
        "sale_id": ids["sale"],
        "sale_item_id": ids["sale_item"],
    }
    assert db.session.get(Booking, ids["booking"]) is not None
    assert db.session.get(DirectSale, ids["sale"]) is not None
    assert db.session.get(DirectSaleItem, ids["sale_item"]) is not None
    assert db.session.execute(text("PRAGMA foreign_key_check")).fetchall() == []


def test_controlled_repair_blocks_active_missing_sale_item_without_writes(allocation_app):
    _client, _booking, _booking_item, _sale, sale_item, allocation = _linked_allocation()
    allocation_id = allocation.id
    _force_legacy_parent_delete(allocation_app, "direct_sale_item", sale_item.id)

    finding = audit_booking_allocation_integrity()[0]
    assert finding["repair_eligible"] is False
    assert "active allocation has no sale line" in finding["blocked_reason"]

    with pytest.raises(ValueError, match="manual resolution"):
        repair_dangling_booking_allocations(run_id="must-not-write")
    db.session.rollback()

    assert db.session.get(BookingAllocation, allocation_id) is not None
    assert BookingAllocationRepairArchive.query.count() == 0


def test_booking_cancellation_blocks_parent_delete_when_active_allocation_exists(allocation_app):
    application = allocation_app
    client, _booking, booking_item, _sale, _sale_item, allocation = _linked_allocation()
    client_id = client.id
    booking_item_id = booking_item.id
    allocation_id = allocation.id

    http = application.test_client()
    login = http.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert login.status_code == 302
    response = http.post(f"/client_booking_cancel/{client_id}", follow_redirects=True)

    assert response.status_code == 200
    assert b"Cancellation blocked" in response.data
    assert db.session.get(BookingItem, booking_item_id) is not None
    assert db.session.get(BookingAllocation, allocation_id) is not None
    assert Entry.query.filter_by(type="CANCEL").count() == 0
    assert BookingAllocationRepairArchive.query.count() == 0
