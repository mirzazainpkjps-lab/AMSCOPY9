"""Regression tests for the findings in AUDIT_2026-08.md.

Each test reproduces a defect that was live at commit 509be4f and asserts the
behaviour that now replaces it. They are deliberately written against the HTTP
layer, because every one of these defects was reachable by an ordinary
authenticated user submitting an ordinary form — the pre-existing suite missed
them by only ever exercising the service layer with well-formed input.
"""
from __future__ import annotations

import html
import re

import pytest

from tests.conftest import make_csrf_client


def _login(client):
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page).group(1)
    return client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "_csrf_token": token},
        follow_redirects=True,
    )


def _text(response):
    return html.unescape(re.sub(r"<[^>]+>", " ", response.get_data(as_text=True)))


@pytest.fixture()
def seeded(app):
    """Authenticated client with one material, one client and a 100-bag booking."""
    from models import Material

    client = make_csrf_client(app)
    _login(client)
    client.post(
        "/add_material",
        data={"material_name": "CEMENT", "material_unit": "Bags"},
        follow_redirects=True,
    )
    client.post(
        "/add_client",
        data={"name": "ACME", "code": "AC-1", "category": "General", "opening_balance": "0"},
        follow_redirects=True,
    )
    with app.app_context():
        material_id = Material.query.first().id
    client.post(
        "/add_booking",
        data={
            "client_code": "AC-1",
            "material_name[]": "CEMENT",
            "material_id[]": str(material_id),
            "qty[]": "100",
            "unit_rate[]": "1000",
            "amount": "100000",
            "paid_amount": "0",
            "date": "2026-03-01",
        },
        follow_redirects=True,
    )
    return client, material_id


def _dispatch(client, material_id, qty):
    return client.post(
        "/add_record",
        data={
            "date": "2026-03-02",
            "client": "ACME",
            "type": "OUT",
            "material_id": str(material_id),
            "material": "CEMENT",
            "qty": str(qty),
            "driver_name": "DRV",
        },
        follow_redirects=True,
    )


# --------------------------------------------------------------------------
# CRITICAL: negative quantity defeated the booking over-dispatch guard.
# --------------------------------------------------------------------------

def test_negative_dispatch_quantity_is_rejected(app, seeded):
    """A negative OUT entry used to be accepted and inflated `remaining`.

    Original exploit: booking of 100 bags, POST qty=-400 (accepted), which made
    the guard compute remaining = 100 - (-400) = 500, so a follow-up qty=450
    was also accepted — 450 bags dispatched against a 100-bag booking.
    """
    from models import Entry

    client, material_id = seeded

    assert "must be greater than zero" in _text(_dispatch(client, material_id, -400))
    with app.app_context():
        assert Entry.query.filter_by(type="OUT", is_void=False).count() == 0

    # The follow-up leg of the exploit must now also fail, on the real limit.
    assert "Cannot dispatch" in _text(_dispatch(client, material_id, 450))
    with app.app_context():
        assert Entry.query.filter_by(type="OUT", is_void=False).count() == 0


@pytest.mark.parametrize("bad_qty", [0, -1, "abc", ""])
def test_dispatch_rejects_non_positive_and_garbage_quantities(app, seeded, bad_qty):
    from models import Entry

    client, material_id = seeded
    _dispatch(client, material_id, bad_qty)
    with app.app_context():
        assert Entry.query.filter_by(type="OUT", is_void=False).count() == 0


def test_valid_dispatch_still_works(app, seeded):
    """Guard rails must not block legitimate traffic."""
    from models import Entry

    client, material_id = seeded
    _dispatch(client, material_id, 60)
    with app.app_context():
        entries = Entry.query.filter_by(type="OUT", is_void=False).all()
        assert [e.qty for e in entries] == [60.0]

    # Over-dispatching the remaining 40 is still refused.
    _dispatch(client, material_id, 60)
    with app.app_context():
        assert Entry.query.filter_by(type="OUT", is_void=False).count() == 1


def test_edit_entry_cannot_flip_quantity_negative(app, seeded):
    """/edit_entry was a second door to the same exploit — it parsed qty with a
    bare float() and no lower bound."""
    from models import Entry, db

    client, material_id = seeded
    _dispatch(client, material_id, 50)
    with app.app_context():
        entry_id = Entry.query.filter_by(type="OUT", is_void=False).first().id

    def edit(qty):
        return client.post(
            f"/edit_entry/{entry_id}",
            data={
                "date": "2026-03-02",
                "client": "ACME",
                "type": "OUT",
                "material_id": str(material_id),
                "material": "CEMENT",
                "qty": str(qty),
                "driver_name": "DRV",
            },
            follow_redirects=True,
        )

    for bad in (-400, 0, "abc"):
        edit(bad)
        with app.app_context():
            assert db.session.get(Entry, entry_id).qty == 50.0

    edit(60)
    with app.app_context():
        assert db.session.get(Entry, entry_id).qty == 60.0


# --------------------------------------------------------------------------
# HIGH: booking money/quantity invariants.
# --------------------------------------------------------------------------

def _book(client, material_id, qty, amount, paid="0"):
    return client.post(
        "/add_booking",
        data={
            "client_code": "AC-1",
            "material_name[]": "CEMENT",
            "material_id[]": str(material_id),
            "qty[]": str(qty),
            "unit_rate[]": "1000",
            "amount": str(amount),
            "paid_amount": str(paid),
            "date": "2026-03-01",
        },
        follow_redirects=True,
    )


def test_negative_booking_is_rejected(app, seeded):
    """`qty[]=-100, amount=-100000` used to persist Booking.amount = -100000.0."""
    from models import Booking

    client, material_id = seeded
    with app.app_context():
        before = Booking.query.count()
    _book(client, material_id, -100, -100000)
    with app.app_context():
        assert Booking.query.count() == before
        assert all((b.amount or 0) >= 0 for b in Booking.query.all())


def test_booking_overpayment_is_rejected(app, seeded):
    """A booking of 10,000 used to accept paid_amount=999,999."""
    from models import Booking

    client, material_id = seeded
    with app.app_context():
        before = Booking.query.count()
    _book(client, material_id, 100, 100000, paid="999999")
    with app.app_context():
        assert Booking.query.count() == before
        for booking in Booking.query.all():
            assert (booking.paid_amount or 0) <= (booking.amount or 0) + 1e-9


# --------------------------------------------------------------------------
# HIGH: authentication.
# --------------------------------------------------------------------------

def test_plaintext_passwords_are_migrated_and_no_longer_accepted(app):
    """Legacy rows stored the password in cleartext and login compared it
    directly. Boot migration must hash them, and the plaintext compare is gone.
    """
    from sqlalchemy import text

    from app.services.schema import _migrate_legacy_plaintext_passwords
    from models import User, db
    import utils.login_guard as login_guard

    with app.app_context():
        db.session.execute(
            text(
                "INSERT INTO user (username, password_hash, password_plain, role, status) "
                "VALUES ('legacy_plain', '', :pw, 'user', 'active')"
            ),
            {"pw": "secret123"},
        )
        db.session.execute(
            text(
                "INSERT INTO user (username, password_hash, role, status) "
                "VALUES ('legacy_inhash', 'plainpw', 'user', 'active')"
            )
        )
        db.session.commit()
        _migrate_legacy_plaintext_passwords()

        for name in ("legacy_plain", "legacy_inhash"):
            user = User.query.filter_by(username=name).one()
            assert user.password_plain is None, "cleartext must not be retained"
            assert (user.password_hash or "").count("$") >= 2, "password must be hashed"

    def attempt(username, password):
        login_guard.reset_all()
        client = app.test_client()
        page = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page).group(1)
        return client.post(
            "/login",
            data={"username": username, "password": password, "_csrf_token": token},
        ).status_code

    # The original passwords still work - via the hash, not a plaintext compare.
    assert attempt("legacy_plain", "secret123") == 302
    assert attempt("legacy_inhash", "plainpw") == 302
    assert attempt("legacy_inhash", "wrong") == 200
    login_guard.reset_all()


def test_login_is_rate_limited(app):
    """There was no throttling anywhere: /login accepted unlimited guesses."""
    import utils.login_guard as login_guard

    login_guard.reset_all()
    client = app.test_client()

    def attempt(password):
        page = client.get("/login").get_data(as_text=True)
        match = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page)
        token = match.group(1) if match else ""
        return client.post(
            "/login",
            data={"username": "Admin", "password": password, "_csrf_token": token},
        ).status_code

    codes = [attempt("wrong-password") for _ in range(12)]
    assert 429 in codes, "brute force was never throttled"

    # Lockout is not bypassable by then supplying the correct password.
    assert attempt("Admin@fbm12345") == 429

    login_guard.reset_all()
    assert attempt("Admin@fbm12345") == 302


# --------------------------------------------------------------------------
# MEDIUM: response headers.
# --------------------------------------------------------------------------

def test_framing_is_restricted_to_an_allowlist(app):
    """hooks.py sent `X-Frame-Options: ALLOWALL` and `frame-ancestors *`,
    which let any site frame the app and clickjack an authenticated user."""
    response = app.test_client().get("/login")

    assert response.headers.get("X-Frame-Options") != "ALLOWALL"
    csp = response.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors" in csp
    assert "frame-ancestors *" not in csp
    assert "'self'" in csp


def test_cors_does_not_reflect_arbitrary_origins(app):
    """`ALLOW_OPEN_CORS` used to emit `Access-Control-Allow-Origin: *`
    unconditionally."""
    response = app.test_client().get("/login", headers={"Origin": "https://evil.example"})
    assert response.headers.get("Access-Control-Allow-Origin") != "https://evil.example"


# --------------------------------------------------------------------------
# MEDIUM: deploy webhook / startup diagnostics.
# --------------------------------------------------------------------------

def test_webhook_refuses_to_run_without_a_configured_secret(monkeypatch):
    """main.py shipped a hardcoded token in a public repo, guarding an endpoint
    that runs `git reset --hard`.

    The deploy webhook was since reworked upstream to verify a GitHub
    HMAC-SHA256 signature instead of a query-string token. That is a stronger
    mechanism, but the property this test defends is unchanged: with no secret
    configured the endpoint must fail CLOSED, and a forged signature must never
    be accepted.
    """
    import hashlib
    import hmac

    import main

    body = b'{"ref": "refs/heads/main"}'

    def _signature(secret: bytes) -> str:
        return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    # No secret configured -> fail closed, even for a well-formed signature.
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "")
    with main.app.test_request_context(
        "/git-auto-pull",
        method="POST",
        data=body,
        headers={"X-Hub-Signature-256": _signature(b"anything")},
    ):
        assert main.verify_github_signature() is False

    # Secret configured -> only the genuine signature is accepted.
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "s3cret")

    with main.app.test_request_context(
        "/git-auto-pull", method="POST", data=body,
        headers={"X-Hub-Signature-256": _signature(b"s3cret")},
    ):
        assert main.verify_github_signature() is True

    for bad in ("sha256=deadbeef", _signature(b"wrong-secret"), "", "garbage"):
        with main.app.test_request_context(
            "/git-auto-pull", method="POST", data=body,
            headers={"X-Hub-Signature-256": bad},
        ):
            assert main.verify_github_signature() is False, bad

    # A missing signature header must also be refused.
    with main.app.test_request_context("/git-auto-pull", method="POST", data=body):
        assert main.verify_github_signature() is False


def test_no_hardcoded_webhook_token_in_source():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "main.py"
    assert "PakistanZindabad1947-2026" not in source.read_text(encoding="utf-8")


def test_webhook_secret_loads_from_private_file(tmp_path, monkeypatch):
    """PythonAnywhere free Web tabs have no Environment variables UI.

    The webhook secret must still be configurable via a private file so GitHub
    HMAC verification works without AMS_WEBHOOK_SECRET in the process env.
    """
    import main

    secret_file = tmp_path / ".ams_webhook_secret"
    secret_file.write_text("file-secret-value\n", encoding="utf-8")

    monkeypatch.delenv("AMS_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("AMS_WEBHOOK_SECRET_FILE", str(secret_file))
    assert main.load_webhook_secret() == "file-secret-value"

    monkeypatch.setenv("AMS_WEBHOOK_SECRET", "env-wins")
    assert main.load_webhook_secret() == "env-wins"

    missing = tmp_path / "does-not-exist"
    monkeypatch.delenv("AMS_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("AMS_WEBHOOK_SECRET_FILE", str(missing))
    assert main.load_webhook_secret() == ""


# --------------------------------------------------------------------------
# HIGH: concurrent payments were dropped by optimistic-locking conflicts.
# --------------------------------------------------------------------------

def test_concurrent_payments_are_all_persisted(app):
    """12 simultaneous payments against one account used to persist only 7-8.

    `Account` carries version_id_col='revision', so concurrent writers collide
    by design; the losers raised StaleDataError, were swallowed by a broad
    `except Exception`, and the payment was silently discarded with a message
    blaming the user's input. The unit of work is now retried.
    """
    import threading

    from models import Account, Payment

    def new_client():
        client = make_csrf_client(app)
        _login(client)
        return client

    setup = new_client()
    setup.post(
        "/add_client",
        data={"name": "CC", "code": "CC-1", "category": "General", "opening_balance": "0"},
        follow_redirects=True,
    )
    setup.post(
        "/accounts/accounts/add",
        data={
            "name": "CASH",
            "class_category": "Assets",
            "class_subcategory": "Cash",
            "class_account_type": "Main Cash",
            "account_status": "active",
            "opening_amount": "0",
            "opening_position": "debit",
            "opening_effective_date": "2026-01-01",
        },
        follow_redirects=True,
    )
    with app.app_context():
        account_id = Account.query.first().id

    def pay(_i):
        new_client().post(
            "/add_payment",
            data={
                "client_code": "CC-1",
                "amount": "100",
                "payment_type": "Receipt",
                "method": "Cash",
                "payment_account_id": str(account_id),
                "date": "2026-02-01",
            },
            follow_redirects=True,
        )

    threads = [threading.Thread(target=pay, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with app.app_context():
        assert Payment.query.count() == 12, "payments were dropped under contention"
        assert Account.query.get(account_id).balance == pytest.approx(1200.0)


# --------------------------------------------------------------------------
# MEDIUM: bootstrap failures were swallowed.
# --------------------------------------------------------------------------

def test_app_refuses_requests_when_bootstrap_failed(app):
    """A bootstrap exception was only stored in `AMS_BOOTSTRAP_ERROR`, which
    nothing read. The app then served opaque HTTP 500s - including on write
    paths - against a possibly half-created schema."""
    app.config["AMS_BOOTSTRAP_ERROR"] = "boom: simulated bootstrap failure"
    try:
        response = app.test_client().get("/login")
        assert response.status_code == 503
        body = response.get_data(as_text=True)
        assert "failed to initialise" in body
        # The traceback must not leak unless explicitly enabled.
        assert "boom: simulated bootstrap failure" not in body
    finally:
        app.config["AMS_BOOTSTRAP_ERROR"] = None


def test_startup_traceback_is_not_exposed_by_default():
    """wsgi.py served the raw startup traceback as the 500 body."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "wsgi.py").read_text(encoding="utf-8")
    assert "AMS_DEBUG_STARTUP" in source, "traceback exposure must be opt-in"


# --------------------------------------------------------------------------
# MEDIUM: CSRF enforcement was never exercised by the suite.
# --------------------------------------------------------------------------

def test_csrf_is_enforced_when_explicitly_enabled(app_factory):
    """CSRF is hand-rolled and short-circuits under TESTING unless
    AMS_CSRF_ALWAYS is set - which nothing in the suite ever set, so the gate
    itself had zero coverage."""
    app = app_factory()
    app.config["AMS_CSRF_ALWAYS"] = True

    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="_csrf_token"[^>]*value="([^"]+)"', page).group(1)

    # A POST with no token at all must be refused.
    missing = client.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"})
    assert missing.status_code in (400, 403), "CSRF gate did not reject a tokenless POST"

    # A POST with a forged token must be refused.
    forged = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "_csrf_token": "not-the-token"},
    )
    assert forged.status_code in (400, 403), "CSRF gate accepted a forged token"

    # The genuine token still works.
    ok = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "_csrf_token": token},
    )
    assert ok.status_code == 302


# --------------------------------------------------------------------------
# MEDIUM: binary-float drift reached stored money values.
# --------------------------------------------------------------------------

def test_float_money_is_quantized_before_storage(app):
    """A PendingBill was found holding 99999.69999999998.

    85 money columns are db.Float with no integer-minor mirror, so
    ``qty * unit_rate`` drifts and the drift is what gets persisted.
    """
    from models import Booking, PendingBill, db

    with app.app_context():
        # 4567 bags * 21.90 == 100017.29999999999 in binary floating point.
        drifted = 4567 * 21.90
        assert drifted != 100017.30, "precondition: this value must actually drift"

        bill = PendingBill(amount=drifted)
        booking = Booking(client_name="DRIFT", amount=drifted, paid_amount=0.1 + 0.2)
        db.session.add_all([bill, booking])
        db.session.commit()

        assert PendingBill.query.get(bill.id).amount == 100017.30
        stored = Booking.query.get(booking.id)
        assert stored.amount == 100017.30
        assert stored.paid_amount == 0.30, "0.1 + 0.2 must not persist as 0.30000000000000004"


# --------------------------------------------------------------------------
# HIGH: edit_payment shared add_payment's unretried-commit shape.
# --------------------------------------------------------------------------

def test_edit_payment_commit_is_retried_on_conflict():
    """``edit_payment`` must re-execute its whole unit of work on a conflict.

    ``add_payment`` lost 4 of 12 concurrent writes because a StaleDataError
    from Account.version_id_col was swallowed by a broad ``except``. The edit
    endpoint was written against the same template and had the same defect.
    """
    import inspect

    from app.blueprints.sales import payments as payments_mod

    src = inspect.getsource(payments_mod.edit_payment)

    assert "retry_on_conflict" in src, "edit_payment still commits without retry"
    assert "label='edit_payment'" in src or 'label="edit_payment"' in src

    # The retried closure must re-read the row: retry_on_conflict rolls the
    # session back between attempts, so an instance loaded before the closure
    # is detached and its attributes cannot be used as fallback defaults.
    body = src.split("def _save():", 1)
    assert len(body) == 2, "edit_payment has no retried _save() closure"
    assert "Payment.query.get_or_404(id)" in body[1], (
        "the retried closure reuses a stale instance loaded outside the retry"
    )

    # Non-idempotent side effects must stay outside the retried closure.
    assert "save_photo" not in body[1], (
        "photo upload runs inside the retry and would be repeated per attempt"
    )

    # A lock conflict must not be reported to the user as bad input.
    assert "_is_transient" in src


# --------------------------------------------------------------------------
# The pinned v4.4 SQL bundle does not exist; the schema state must say so.
# --------------------------------------------------------------------------

def test_schema_provenance_is_reported_truthfully(app):
    """AMS_SCHEMA_VERSION=v44 is pinned unconditionally by the factory.

    ``v44/SCHEMA_v4_4.sql`` has never existed in this repository, so every
    database is really built by ``db.create_all()`` and no CHECK constraint
    from the bundle is in force. The app must not claim otherwise.
    """
    from app.services.v44_schema import (
        describe_schema_state,
        schema_bundle_available,
    )

    state = app.config.get("AMS_SCHEMA_STATE")
    assert state is not None, "boot did not record the real schema provenance"

    # Configured version and actual provenance are separate facts.
    assert app.config["AMS_SCHEMA_VERSION"] == "v44"
    assert state["schema_bundle_present"] is schema_bundle_available()

    if not state["schema_bundle_present"]:
        assert state["effective_schema"] == "orm-create-all", (
            "the SQL bundle is absent, so the schema must be reported as "
            f"ORM-built, not {state['effective_schema']!r}"
        )
        assert state["is_v44_schema"] is False

    # The helper must be callable independently and never raise on a path
    # that does not exist.
    missing = describe_schema_state("/nonexistent/never/created.db")
    assert missing["database_exists"] is False
    assert missing["effective_schema"] == "absent"
