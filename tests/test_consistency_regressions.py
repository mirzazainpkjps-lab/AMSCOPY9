"""Regression tests for cross-module consistency fixes found in the app audit."""
from __future__ import annotations

from datetime import date

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from models import (
    Account,
    Client,
    DirectSale,
    Invoice,
    Supplier,
    SupplierPayment,
    GRN,
    db,
    User,
)


@pytest.fixture()
def isolated_app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'audit.db'}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "audit-test",
            "SESSION_COOKIE_SECURE": False,
            "SESSION_COOKIE_SAMESITE": "Lax",
        }
    )
    with application.app_context():
        yield application
        db.session.remove()


def add_user(role="user", **permissions):
    user = User(
        username=f"audit-{role}",
        role=role,
        status="active",
        password_hash=generate_password_hash("secret"),
        **permissions,
    )
    db.session.add(user)
    db.session.commit()
    return user


def login(client, username, password="secret"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_fresh_testing_database_has_documented_admin_login(isolated_app):
    with isolated_app.app_context():
        admin = User.query.filter_by(username="Admin").one()
        assert admin.role == "admin"
        assert admin.password_hash

    response = login(isolated_app.test_client(), "Admin", "Admin@fbm12345")
    assert response.status_code == 302


def test_blueprint_endpoint_aliases_still_enforce_permissions(isolated_app):
    with isolated_app.app_context():
        limited = add_user(
            can_manage_sales=False,
            can_view_history=True,
            can_manage_payments=True,
        )
        limited_username = limited.username

    client = isolated_app.test_client()
    login(client, limited_username)
    response = client.get("/direct_sales", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_root_has_same_accounts_access_as_other_superusers(isolated_app):
    with isolated_app.app_context():
        root = add_user(role="root", can_manage_payments=False)
        root_username = root.username

    client = isolated_app.test_client()
    login(client, root_username)
    response = client.get("/accounts/", follow_redirects=False)

    assert response.status_code == 200


def test_hard_delete_sale_removes_its_orphan_invoice(isolated_app):
    from app.services.void_rebuild import hard_delete_transaction

    with isolated_app.app_context():
        invoice = Invoice(
            client_name="Invoice Client",
            invoice_no="MB NO.AUDIT-DELETE-1",
            total_amount=100,
            balance=100,
            date=date.today(),
            is_void=False,
        )
        db.session.add(invoice)
        db.session.flush()
        sale = DirectSale(
            client_name="Invoice Client",
            category="Credit Customer",
            amount=100,
            invoice_id=invoice.id,
            is_void=False,
        )
        db.session.add(sale)
        db.session.commit()
        sale_id = sale.id
        invoice_id = invoice.id

        assert hard_delete_transaction("DirectSale", sale_id) is True
        db.session.commit()

        assert db.session.get(DirectSale, sale_id) is None
        assert db.session.get(Invoice, invoice_id) is None


def test_grn_auto_payment_is_not_counted_as_legacy_second_payment(isolated_app):
    from blueprints.accounts.helpers import _legacy_unrepresented_grn_paid_total

    with isolated_app.app_context():
        supplier = Supplier(name="Audit Supplier", is_active=True)
        db.session.add(supplier)
        db.session.flush()
        grn = GRN(
            supplier_id=supplier.id,
            supplier=supplier.name,
            paid_amount=500,
            payment_type="Cash",
            date_posted=None,
            is_void=False,
        )
        db.session.add(grn)
        db.session.flush()
        db.session.add(
            SupplierPayment(
                supplier_id=supplier.id,
                amount=500,
                is_void=False,
                note=f"[AUTO_GRN_PAY:{grn.id}] Auto-payment for GRN",
            )
        )
        db.session.commit()

        assert _legacy_unrepresented_grn_paid_total([grn]) == 0


def test_edit_client_keeps_direct_sale_code_in_sync(isolated_app):
    with isolated_app.app_context():
        client_obj = Client(code="OLD-CODE", name="Old Client", is_active=True)
        db.session.add(client_obj)
        db.session.flush()
        sale = DirectSale(
            client_name=client_obj.name,
            client_code=client_obj.code,
            category="Credit Customer",
            amount=100,
            is_void=False,
        )
        db.session.add(sale)
        db.session.commit()
        client_id = client_obj.id
        sale_id = sale.id

    web_client = isolated_app.test_client()
    login(web_client, "Admin", "Admin@fbm12345")
    response = web_client.post(
        f"/edit_client/{client_id}",
        data={
            "name": "New Client",
            "code": "NEW-CODE",
            "category": "General",
            "opening_balance": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    with isolated_app.app_context():
        updated = db.session.get(DirectSale, sale_id)
        assert updated.client_name == "New Client"
        assert updated.client_code == "NEW-CODE"
