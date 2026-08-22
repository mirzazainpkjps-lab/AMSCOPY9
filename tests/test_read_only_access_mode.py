"""Regression tests: per-user READ-ONLY vs READ & WRITE access mode.

Settings -> User Permissions now offers an Access Mode per user:
  * read_write (default) — behaves exactly as before;
  * read_only            — every mutating request (POST/PUT/PATCH/DELETE)
                           is blocked for non-admin users; all views keep
                           working; own-theme endpoint stays usable.
"""
import os

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "read_only_mode.db"
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
def admin_client(app):
    from models import User
    with app.app_context():
        user = User.query.first()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _login_as(app, username):
    from models import User
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        assert u is not None, f"user {username} missing"
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return c


PERM_FORM = {
    field: "on" for field in (
        'can_view_dashboard', 'can_manage_grn', 'can_view_stock', 'can_view_daily',
        'can_view_history', 'can_manage_bookings', 'can_manage_payments',
        'can_manage_sales', 'can_view_delivery_rent', 'can_view_client_ledger',
        'can_view_supplier_ledger', 'can_view_decision_ledger',
        'can_manage_pending_bills', 'can_view_reports', 'can_manage_notifications',
        'can_manage_suppliers',
    )
}


def _create_user(app, admin_client, username, access_mode=None):
    form = {"username": username, "password": "Pass@123", "role": "user"}
    form.update(PERM_FORM)
    if access_mode:
        form["access_mode"] = access_mode
    r = admin_client.post("/add_user", data=form, follow_redirects=False)
    assert r.status_code == 302
    from models import User
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        assert u is not None
    return u.id


def test_read_only_user_can_view_but_not_write(app, admin_client):
    uid = _create_user(app, admin_client, "viewer1", access_mode="read_only")
    assert uid

    c = _login_as(app, "viewer1")
    # Viewing the GRN page is allowed
    r = c.get("/grn")
    assert r.status_code == 200

    # Saving a GRN is blocked, with a clear message, and nothing is stored
    from models import GRN
    r = c.post("/grn", data={
        "action": "add", "supplier": "S", "supplier_id": "", "date": "2026-08-22",
        "payment_type": "Credit", "mat_name[]": ["CEMENT"], "qty[]": ["10"],
        "price[]": ["100"],
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "READ-ONLY" in body
    with app.app_context():
        assert GRN.query.count() == 0


def test_read_write_default_and_toggle(app, admin_client):
    # Default (no access_mode in form) must be read_write — old behaviour
    uid = _create_user(app, admin_client, "worker1")
    from models import User, db
    with app.app_context():
        u = db.session.get(User, uid)
        assert (u.access_mode or "read_write") == "read_write"

    c = _login_as(app, "worker1")
    r = c.post("/add_supplier", data={"name": "RW SUPPLIER"}, follow_redirects=False)
    assert r.status_code == 302
    from models import Supplier
    with app.app_context():
        assert Supplier.query.filter_by(name="RW SUPPLIER").count() == 1

    # Admin switches the user to read-only via Settings
    form = {"role": "user", "password": ""}
    form.update(PERM_FORM)
    form["access_mode"] = "read_only"
    r = admin_client.post(f"/edit_user_permissions/{uid}", data=form, follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        u = db.session.get(User, uid)
        assert u.access_mode == "read_only"

    # Now the same write is blocked
    c = _login_as(app, "worker1")
    r = c.post("/add_supplier", data={"name": "RW SUPPLIER 2"}, follow_redirects=True)
    assert "READ-ONLY" in r.get_data(as_text=True)
    with app.app_context():
        assert Supplier.query.filter_by(name="RW SUPPLIER 2").count() == 0

    # Back to read & write — works again
    form["access_mode"] = "read_write"
    admin_client.post(f"/edit_user_permissions/{uid}", data=form, follow_redirects=False)
    c = _login_as(app, "worker1")
    r = c.post("/add_supplier", data={"name": "RW SUPPLIER 3"}, follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        assert Supplier.query.filter_by(name="RW SUPPLIER 3").count() == 1


def test_admin_role_ignores_read_only_mode(app, admin_client):
    uid = _create_user(app, admin_client, "mini_admin")
    from models import User, db
    with app.app_context():
        u = db.session.get(User, uid)
        u.role = "admin"
        u.access_mode = "read_only"  # even if set, admins stay unaffected
        db.session.commit()
    c = _login_as(app, "mini_admin")
    r = c.post("/add_supplier", data={"name": "ADMIN RW SUPPLIER"}, follow_redirects=False)
    assert r.status_code == 302
    from models import Supplier
    with app.app_context():
        assert Supplier.query.filter_by(name="ADMIN RW SUPPLIER").count() == 1


def test_read_only_theme_preference_still_usable(app, admin_client):
    _create_user(app, admin_client, "viewer2", access_mode="read_only")
    c = _login_as(app, "viewer2")
    # Own-theme preference stays usable (exempt from the read-only block)
    r = c.post("/api/ui/theme", json={"theme": "light"})
    assert r.status_code == 200
    import json as _json
    assert _json.loads(r.get_data(as_text=True))["ok"] is True


def test_settings_page_shows_access_mode_ui(app, admin_client):
    _create_user(app, admin_client, "viewer3", access_mode="read_only")
    r = admin_client.get("/settings")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Read &amp; Write" in body
    assert "Read Only" in body
    assert "READ ONLY" in body  # badge on the users table
