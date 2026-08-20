"""Regression coverage for the consolidated payables and shared ledgers."""
from __future__ import annotations

from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.services.financial_ledgers import (
    build_client_financial_ledger,
    build_current_payables,
    build_delivery_person_financial_ledger,
    build_supplier_financial_ledger,
)
from models import (
    Booking,
    Client,
    DeliveryPerson,
    DeliveryPersonPayment,
    DirectSale,
    GRN,
    GRNItem,
    Payment,
    SaleDeliveryPerson,
    Supplier,
    SupplierPayment,
    db,
)


@pytest.fixture()
def ledger_app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'ledger.db'}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "ledger-test",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    with app.app_context():
        yield app
        db.session.remove()


def test_client_payables_group_and_allocate_partial_payment(ledger_app):
    with ledger_app.app_context():
        client = Client(code="C-LEDGER", name="JAWAD SB AJNALA JPS", is_active=True)
        db.session.add(client)
        db.session.flush()
        db.session.add_all([
            Booking(client_name=client.name, amount=100000, paid_amount=30000,
                    manual_bill_no="11490", date_posted=datetime(2026, 8, 1), is_void=False),
            Booking(client_name=client.name, amount=50000, paid_amount=0,
                    manual_bill_no="11483", date_posted=datetime(2026, 8, 2), is_void=False),
        ])
        db.session.commit()

        report = build_current_payables()
        assert report["total"] == 1
        assert report["all_rows"][0]["outstanding"] == 120000
        assert len(build_client_financial_ledger(client)["obligations"]) == 2

        db.session.add(Payment(client_id=client.id, client_name=client.name, amount=20000,
                               date_posted=datetime(2026, 8, 3), is_void=False))
        db.session.commit()
        report = build_current_payables()
        assert report["all_rows"][0]["outstanding"] == 100000

        # Fully settle the remaining balance; current payables must be empty,
        # while the ledger still contains all historical source rows.
        db.session.add(Payment(client_id=client.id, client_name=client.name, amount=100000,
                               date_posted=datetime(2026, 8, 4), is_void=False))
        db.session.commit()
        assert build_current_payables()["total"] == 0
        assert len(build_client_financial_ledger(client)["rows"]) >= 4


def test_payable_filters_apply_to_consolidated_amount_and_total(ledger_app):
    with ledger_app.app_context():
        for idx, amount in enumerate((100000, 200000, 50000), start=1):
            client = Client(code=f"C-{idx}", name=f"Client {idx}", is_active=True)
            db.session.add(client)
            db.session.flush()
            db.session.add(DirectSale(client_name=client.name, client_code=client.code,
                                      amount=amount, paid_amount=0,
                                      date_posted=datetime(2026, 8, idx), is_void=False))
        db.session.commit()
        report = build_current_payables(amount_min=100000, page=1, per_page=10)
        assert report["total"] == 2
        assert report["total_outstanding"] == 300000
        report = build_current_payables(client_filter="client 1")
        assert report["total"] == 1 and report["total_outstanding"] == 100000


def test_supplier_ledger_reconciles_grn_and_payment(ledger_app):
    with ledger_app.app_context():
        supplier = Supplier(name="Supplier Ledger Test", opening_balance=1000, is_active=True)
        db.session.add(supplier)
        db.session.flush()
        grn = GRN(supplier_id=supplier.id, supplier=supplier.name,
                  date_posted=datetime(2026, 8, 1), is_void=False)
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name="Cement", qty=10, price_at_time=500, is_void=False))
        db.session.add(SupplierPayment(supplier_id=supplier.id, amount=3000,
                                       date_posted=datetime(2026, 8, 2), is_void=False))
        db.session.commit()
        ledger = build_supplier_financial_ledger(supplier)
        assert ledger["closing_balance"] == 3000  # 1000 opening + 5000 GRN - 3000 paid
        assert ledger["rows"][-1]["balance"] == 3000


def test_delivery_person_ledger_does_not_double_count_legacy_rent(ledger_app):
    with ledger_app.app_context():
        person = DeliveryPerson(name="Driver Ledger Test", is_active=True)
        client = Client(code="D-CLIENT", name="Driver Client", is_active=True)
        db.session.add_all([person, client])
        db.session.flush()
        sale = DirectSale(client_name=client.name, client_code=client.code, amount=0,
                          paid_amount=0, date_posted=datetime(2026, 8, 1), is_void=False)
        db.session.add(sale)
        db.session.flush()
        allocation = SaleDeliveryPerson(sale_id=sale.id, delivery_person_id=person.id,
                                        bags_delivered=10, rent_amount=500,
                                        created_at=datetime(2026, 8, 1), is_void=False)
        db.session.add(allocation)
        db.session.flush()
        # This legacy row is the same source sale and must not be added again.
        from models import DeliveryRent
        db.session.add(DeliveryRent(sale_id=sale.id, delivery_person_name=person.name,
                                    amount=500, date_posted=datetime(2026, 8, 1), is_void=False))
        db.session.add(DeliveryPersonPayment(delivery_person_id=person.id, sale_id=sale.id,
                                             allocation_id=allocation.id, amount_paid=200,
                                             date_posted=datetime(2026, 8, 2), is_void=False))
        db.session.commit()
        ledger = build_delivery_person_financial_ledger(person)
        assert ledger["closing_balance"] == 300
        assert sum(row["debit"] for row in ledger["rows"]) == 500
