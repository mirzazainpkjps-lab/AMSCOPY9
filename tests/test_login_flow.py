"""Regression tests for the "HTTP 500 after login" failure.

Root cause: the startup bootstrap aborted before ``_bootstrap_database()``
(the ORM ``create_all`` + default admin step), so the database had no ``user``
table and POST /login died with ``no such table: user``.
"""
from __future__ import annotations

import sqlite3

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}

# Login is protected by the same session-bound CSRF gate as every other
# mutating route; the login template carries the token.
_CSRF = "login-flow-test-csrf"


def _login(client):
    with client.session_transaction() as sess:
        sess["_csrf_token"] = _CSRF
    data = dict(ADMIN)
    data["_csrf_token"] = _CSRF
    return client.post("/login", data=data, follow_redirects=False)


def _raw_login_post(raw_client):
    """Login via a raw (non-proxied) test client, seeding the token first."""
    with raw_client.session_transaction() as sess:
        sess["_csrf_token"] = _CSRF
    data = dict(ADMIN)
    data["_csrf_token"] = _CSRF
    return raw_client.post("/login", data=data)


def test_login_then_dashboard_on_fresh_database(client):
    assert client.get("/login").status_code == 200
    resp = _login(client)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:2000]
    assert resp.headers["Location"].endswith("/")
    home = client.get("/")
    assert home.status_code == 200


def test_bootstrap_recovers_from_empty_database_file(app_factory, tmp_path):
    """An empty SQLite file must not brick every later start-up."""
    db_file = tmp_path / "empty.db"
    sqlite3.connect(db_file).close()  # 0-byte / table-less database
    assert db_file.exists()

    app = app_factory(db_file)
    resp = _raw_login_post(app.test_client())
    assert resp.status_code == 302
    assert app.config.get("AMS_BOOTSTRAP_ERROR") is None


def test_second_start_on_existing_database_still_logs_in(app_factory, tmp_path):
    db_file = tmp_path / "reused.db"
    first = app_factory(db_file)
    assert _raw_login_post(first.test_client()).status_code == 302

    second = app_factory(db_file)
    assert _raw_login_post(second.test_client()).status_code == 302
    assert second.config.get("AMS_BOOTSTRAP_ERROR") is None


def test_bad_credentials_do_not_500(client):
    resp = client.post("/login", data={"username": "Admin", "password": "nope"})
    assert resp.status_code == 200
    assert b"Invalid Credentials" in resp.data


def test_malformed_session_cookie_redirects_instead_of_500(client):
    """A non-integer user id in the cookie used to raise ValueError -> 500."""
    with client.session_transaction() as sess:
        sess["_user_id"] = "Admin"  # legacy/tampered cookie format
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_unknown_user_id_in_cookie_redirects(client):
    with client.session_transaction() as sess:
        sess["_user_id"] = "999999"
    resp = client.get("/")
    assert resp.status_code == 302


def test_authenticated_pages_do_not_return_server_errors(app, client):
    """Smoke-crawl every argument-less GET route as the admin user."""
    assert _login(client).status_code == 302
    failures = []
    for rule in app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()):
            continue
        if rule.arguments or rule.rule.startswith("/static"):
            continue
        try:
            status = client.get(rule.rule).status_code
        except Exception as exc:  # pragma: no cover - reported below
            failures.append((rule.rule, repr(exc)))
            continue
        if status >= 500:
            failures.append((rule.rule, status))
    assert not failures, failures
