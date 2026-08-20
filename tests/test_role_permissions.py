"""Role-permission audit tests.

Guards the module-permission system after the 2026-08 re-audit:
  * every module is covered by the permission map (no stale entries)
  * the two new flags (can_manage_accounts, can_view_cash_flow) gate their
    modules and default OFF
  * OR-semantics entries (delivery-person ledger) work
  * draft / modal / download / import-job endpoints are gated
  * the Settings user-role screen exposes every feature (matrix + modals)
"""
from __future__ import annotations

import os
import tempfile

import pytest
from werkzeug.security import generate_password_hash

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture(scope="module")
def app():
    db_file = tempfile.mktemp(suffix=".db")
    os.environ["APP_DB_PATH"] = db_file
    from app import create_app
    from models import db
    from app.services.schema import _ensure_model_columns, _ensure_default_admin

    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "perm-test",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    with application.app_context():
        db.create_all()
        _ensure_model_columns()
        _ensure_default_admin()
        db.session.commit()
    yield application
    try:
        os.remove(db_file)
    except OSError:
        pass


def _login(client, username, password):
    rv = client.post(
        "/login",
        data={"username": username, "password": password, "remember_me": "1"},
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303), f"login failed: {rv.status_code}"
    return rv


def _make_user(app, username, **perms):
    from models import db, User

    with app.app_context():
        existing = User.query.filter_by(username=username).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
        u = User(
            username=username,
            password_hash=generate_password_hash("Test@12345"),
            role="user",
            status="active",
        )
        for field, value in perms.items():
            setattr(u, field, value)
        db.session.add(u)
        db.session.commit()
        return u.id


def _client(app, username="permuser", password="Test@12345"):
    c = app.test_client()
    _login(c, username, password)
    return c


def _denied(client, path, method="GET", **data):
    """Confirm the permission gate bounces the request to the dashboard."""
    rv = client.open(path, method=method, data=data, follow_redirects=True)
    html = rv.get_data(as_text=True)
    return rv.status_code == 200 and "Permission denied" in html


def _ok(client, path, method="GET", **data):
    """Gate passed: page renders, or the (existing) record 404s — no denial."""
    rv = client.open(path, method=method, data=data, follow_redirects=True)
    html = rv.get_data(as_text=True)
    return rv.status_code in (200, 404) and "Permission denied" not in html


# ---------------------------------------------------------------- baseline

def test_map_has_no_stale_or_unknown_permissions():
    from app.services.constants import (
        ENDPOINT_PERMISSION_MAP,
        EDITABLE_USER_PERMISSION_FIELDS,
        USER_PERMISSION_DEFAULTS,
        PERMISSION_GROUPS,
    )

    # Every permission referenced by the map or groups exists with a default.
    refs = set()
    for spec in ENDPOINT_PERMISSION_MAP.values():
        perms = spec if isinstance(spec, (tuple, list, set)) else (spec,)
        refs.update(perms)
    for _title, fields in PERMISSION_GROUPS:
        for field, _label in fields:
            refs.add(field)
    missing = refs - set(USER_PERMISSION_DEFAULTS)
    assert not missing, f"permissions without defaults: {missing}"

    # Editable fields == map of all permission groups (UI stays complete).
    grouped = {field for _t, fields in PERMISSION_GROUPS for field, _l in fields}
    assert grouped == set(EDITABLE_USER_PERMISSION_FIELDS), (
        f"UI groups and editable fields diverged: "
        f"only-groups={grouped - set(EDITABLE_USER_PERMISSION_FIELDS)} "
        f"only-fields={set(EDITABLE_USER_PERMISSION_FIELDS) - grouped}"
    )

    # New flags default OFF.
    assert USER_PERMISSION_DEFAULTS["can_manage_accounts"] is False
    assert USER_PERMISSION_DEFAULTS["can_view_cash_flow"] is False


def test_new_columns_exist_on_user_model(app):
    from models import User

    assert hasattr(User, "can_manage_accounts")
    assert hasattr(User, "can_view_cash_flow")


def test_settings_page_shows_matrix_and_new_features(app):
    c = app.test_client()
    _login(c, "Admin", "Admin@fbm12345")
    rv = c.get("/settings", follow_redirects=True)
    html = rv.get_data(as_text=True)
    assert rv.status_code == 200
    assert "Feature Access Matrix" in html
    assert "Financial Accounts" in html
    assert "Cash Flow" in html
    for field in ("can_manage_accounts", "can_view_cash_flow"):
        assert f'name="{field}"' in html, f"checkbox {field} missing from settings UI"


# --------------------------------------------------------- cash flow module

def test_cash_flow_blocked_by_default_then_granted(app):
    _make_user(app, "cf-user")
    c = _client(app, "cf-user")
    assert _denied(c, "/cash_flow"), "cash_flow must be blocked without permission"
    assert _denied(c, "/cash_flow_differences")

    _make_user(app, "cf-user", can_view_cash_flow=True)
    c = _client(app, "cf-user")
    assert _ok(c, "/cash_flow"), "cash_flow must open after grant"
    assert _ok(c, "/cash_flow_differences")


def test_admin_always_sees_cash_flow(app):
    c = app.test_client()
    _login(c, "Admin", "Admin@fbm12345")
    assert _ok(c, "/cash_flow")
    assert _ok(c, "/accounts/")


# ------------------------------------------------------- accounts module

def test_accounts_module_gated_by_new_flag(app):
    # can_manage_payments defaults True, so a locked-down test user must deny
    # both entry paths (new flag + legacy payment-manager path) to be blocked.
    _make_user(app, "acc-user", can_manage_accounts=False, can_manage_payments=False)
    c = _client(app, "acc-user")
    assert _denied(c, "/accounts/"), "accounts dashboard must be blocked"
    assert _denied(c, "/accounts/accounts")
    assert _denied(c, "/accounts/transactions/new", method="POST")
    assert _denied(c, "/accounts/reconciliations")

    _make_user(app, "acc-user", can_manage_accounts=True, can_manage_payments=False)
    c = _client(app, "acc-user")
    assert _ok(c, "/accounts/"), "accounts dashboard must open after grant"
    assert _ok(c, "/accounts/accounts")


def test_payment_manager_keeps_accounts_entry(app):
    """Legacy behaviour: payment managers can transact inside Accounts."""
    _make_user(app, "pay-mgr", can_manage_accounts=False, can_manage_payments=True)
    c = _client(app, "pay-mgr")
    assert _ok(c, "/accounts/"), "payment managers keep Accounts module entry"


def test_other_module_still_blocked_for_accounts_user(app):
    _make_user(app, "acc-only", can_manage_accounts=True, can_manage_payments=False)
    c = _client(app, "acc-only")
    assert _ok(c, "/accounts/")
    assert _denied(c, "/cash_flow"), "cash flow stays blocked without its own flag"


# ------------------------------------------------- import / export module

def test_import_export_jobs_gated(app):
    _make_user(app, "imp-user")
    c = _client(app, "imp-user")
    assert _denied(c, "/import_export/history")

    _make_user(app, "imp-user", can_import_export=True)
    c = _client(app, "imp-user")
    assert _ok(c, "/import_export/history")


# --------------------------------------------------- supplier / driver PDFs

def test_supplier_ledger_download_gated(app):
    # Note: can_view_supplier_ledger defaults True, so the restricted user
    # must deny it explicitly to simulate a locked-down account.
    _make_user(app, "sup-user", can_view_supplier_ledger=False)
    c = _client(app, "sup-user")
    assert _denied(c, "/download_supplier_ledger/1")
    assert _denied(c, "/download_supplier_payment/1")

    _make_user(app, "sup-user", can_view_supplier_ledger=True)
    c = _client(app, "sup-user")
    # 404 (missing record) is fine — the gate itself must not redirect.
    rv = c.get("/download_supplier_ledger/1")
    assert "Permission denied" not in rv.get_data(as_text=True)


def test_delivery_ledger_or_semantics(app):
    # Client-ledger-only user may view the driver ledger (OR entry).
    _make_user(app, "drv-client-ledger", can_view_client_ledger=True,
               can_view_delivery_rent=False)
    c = _client(app, "drv-client-ledger")
    assert _ok(c, "/delivery_ledger/1")

    # User with neither is blocked.
    _make_user(app, "drv-none", can_view_client_ledger=False,
               can_view_delivery_rent=False)
    c = _client(app, "drv-none")
    assert _denied(c, "/delivery_ledger/1")


# ------------------------------------------------------ drafts & modals

def test_sale_draft_and_modals_gated(app):
    # sales/bookings/pending-bill permissions default True, so the restricted
    # user must deny each explicitly to simulate a locked-down account.
    _make_user(app, "draft-user", can_manage_sales=False, can_manage_bookings=False,
               can_manage_pending_bills=False, can_manage_clients=False)
    c = _client(app, "draft-user")
    assert _denied(c, "/direct_sales/hold", method="POST")
    assert _denied(c, "/bookings/1/edit-modal")
    assert _denied(c, "/direct_sales/1/edit-modal")
    assert _denied(c, "/pending_bills/1/modals")
    assert _denied(c, "/clients/1/modals")

    _make_user(app, "draft-user", can_manage_sales=True, can_manage_bookings=True,
               can_manage_pending_bills=True, can_manage_clients=True)
    c = _client(app, "draft-user")
    # Records do not exist -> 404, but the permission gate must be passed.
    for path, method in (
        ("/direct_sales/hold", "POST"),
        ("/bookings/1/edit-modal", "GET"),
        ("/direct_sales/1/edit-modal", "GET"),
        ("/pending_bills/1/modals", "GET"),
        ("/clients/1/modals", "GET"),
    ):
        rv = c.open(path, method=method, follow_redirects=True)
        assert "Permission denied" not in rv.get_data(as_text=True), path


# ------------------------------------------------ settings edit round-trip

def test_edit_user_permissions_saves_new_flags(app):
    uid = _make_user(app, "roundtrip", can_manage_accounts=False)
    admin = app.test_client()
    _login(admin, "Admin", "Admin@fbm12345")
    rv = admin.post(
        f"/edit_user_permissions/{uid}",
        data={"role": "user", "can_view_dashboard": "on", "can_manage_accounts": "on"},
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303)

    from models import db, User

    with app.app_context():
        u = db.session.get(User, uid)
        assert u.can_manage_accounts is True
        # Unticked fields are saved as False, not left NULL.
        assert u.can_view_cash_flow is False
