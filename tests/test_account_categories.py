from __future__ import annotations

import pytest
from models import db, AccountCategory, Account

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=ADMIN, follow_redirects=True)


def csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def test_manage_categories_page_unauthenticated(client):
    # Should redirect to login
    resp = client.get("/accounts/categories")
    assert resp.status_code == 302
    assert "login" in resp.headers["Location"]


def test_manage_categories_crud_flow(client, app):
    with app.app_context():
        # Clean categories
        AccountCategory.query.delete()
        db.session.commit()

    # Login
    login_resp = _login(client)
    assert login_resp.status_code == 200
    
    tok = csrf_token(client)

    # 1. Create category via Page POST
    resp = client.post("/accounts/categories", data={
        "name": "Investment Group", 
        "note": "Capital investments",
        "_csrf_token": tok
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Account category created successfully." in resp.data

    with app.app_context():
        cat = AccountCategory.query.filter_by(name="Investment Group").first()
        assert cat is not None
        assert cat.note == "Capital investments"
        assert cat.is_active is True

    # 2. Edit category via Page POST
    resp = client.post(f"/accounts/categories/{cat.id}/edit", data={
        "name": "Investment Group Renamed", 
        "note": "Updated investments",
        "_csrf_token": tok
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Account category updated successfully." in resp.data

    with app.app_context():
        cat = AccountCategory.query.get(cat.id)
        assert cat.name == "Investment Group Renamed"
        assert cat.note == "Updated investments"

    # 3. Create another category via AJAX API POST (using the X-CSRF-Token header or _csrf_token in body)
    api_resp = client.post("/accounts/api/categories/add", json={
        "name": "Dynamic Group", 
        "note": "AJAX created"
    }, headers={"X-CSRF-Token": tok}, follow_redirects=True)
    assert api_resp.status_code == 200
    res_data = api_resp.get_json()
    assert res_data["ok"] is True
    assert res_data["category"]["name"] == "Dynamic Group"

    with app.app_context():
        dynamic_cat = AccountCategory.query.filter_by(name="Dynamic Group").first()
        assert dynamic_cat is not None
        assert dynamic_cat.note == "AJAX created"

    # 4. Try to delete category when it is used by an account (must be forbidden)
    with app.app_context():
        # Create an account using "Dynamic Group"
        account = Account(
            name="Test Account Under Dynamic Group",
            category="cash",
            source_category="Dynamic Group",
            account_type="company",
            balance=100.0,
            opening_balance=100.0,
            class_category="Assets",
            class_subcategory="Cash",
            class_account_type="Main Cash",
            channel="cash",
            account_status="active",
            is_active=True
        )
        db.session.add(account)
        db.session.commit()
        dynamic_cat_id = dynamic_cat.id

    # Try to delete dynamic group while it's in use
    del_resp = client.post(f"/accounts/categories/{dynamic_cat_id}/delete", data={"_csrf_token": tok}, follow_redirects=True)
    assert del_resp.status_code == 200
    assert b"This category is currently used by one or more accounts and cannot be deleted." in del_resp.data

    with app.app_context():
        dynamic_cat_check = AccountCategory.query.get(dynamic_cat_id)
        assert dynamic_cat_check is not None  # Not deleted!

    # 5. Delete category when it is NOT used by any account (must be allowed)
    with app.app_context():
        cat_id_to_delete = cat.id

    del_resp = client.post(f"/accounts/categories/{cat_id_to_delete}/delete", data={"_csrf_token": tok}, follow_redirects=True)
    assert del_resp.status_code == 200
    assert b"Account category deleted successfully." in del_resp.data

    with app.app_context():
        deleted_cat_check = AccountCategory.query.get(cat_id_to_delete)
        assert deleted_cat_check is None  # Deleted!
