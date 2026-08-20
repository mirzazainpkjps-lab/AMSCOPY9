"""Tests for the modular factory, services, and import job APIs."""
import os
import io
import uuid
import tempfile

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "test.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    os.environ["ALLOW_EMPTY_DB"] = "1"
    from app import create_app
    from models import db, User
    from werkzeug.security import generate_password_hash

    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "LOGIN_DISABLED": True,
            # Keep upload test artifacts out of the repo's instance dir.
            "IMPORT_UPLOADS_DIR": str(tmp_path / "uploads"),
        }
    )
    with application.app_context():
        db.create_all()
        if not User.query.filter_by(username="tester").first():
            u = User(
                username="tester",
                role="admin",
                status="active",
                password_hash=generate_password_hash("secret"),
                can_import_export=True,
            )
            db.session.add(u)
            db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    return client.post(
        "/login",
        data={"username": "tester", "password": "secret", "remember_me": "0"},
        follow_redirects=False,
    )


def test_login_page_renders(client):
    rv = client.get("/login")
    assert rv.status_code == 200


def test_factory_registers_core_routes(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/login" in rules
    assert "/" in rules
    assert "/import_export/upload" in rules


def test_progress_math():
    from app.services.import_jobs import calculate_progress_pct, calculate_eta, generate_upload_id

    assert calculate_progress_pct(0, 100) == 0
    assert calculate_progress_pct(50, 100) == 50
    assert calculate_progress_pct(200, 100) == 99.0
    eta = calculate_eta(10, 50, 100)
    assert eta["estimated_remaining_seconds"] == 10
    uid = generate_upload_id()
    uuid.UUID(uid)


def test_import_upload_and_job(client, app):
    login(client)
    data = {
        "file": (io.BytesIO(b"PK\x03\x04fake-xlsx"), "demo.xlsx"),
    }
    rv = client.post("/import_export/upload", data=data, content_type="multipart/form-data")
    assert rv.status_code == 200, rv.data
    body = rv.get_json()
    assert body["success"] is True
    upload_id = body["upload_id"]

    listed = client.get("/import_export/uploads").get_json()
    assert listed["total"] >= 1

    start = client.post(f"/import_export/uploads/{upload_id}/start")
    assert start.status_code == 200, start.data
    job = start.get_json()
    assert job["job_id"]
    prog = client.get(f"/import_export/jobs/{job['job_id']}/progress").get_json()
    assert prog["status"] in ("completed", "failed", "processing")
    hist = client.get(f"/import_export/jobs/{job['job_id']}/history").get_json()
    assert "events" in hist


def test_cash_reconciliation_helpers(app):
    from models import db
    from cash_flow_reconciliation_helpers import create_reconciliation
    from datetime import date

    with app.app_context():
        rec = create_reconciliation(date(2026, 8, 1), 70000, 50000, reason="test", created_by="tester")
        assert rec.difference == -20000
        assert rec.physical_cash_available == 50000
        assert rec.get_opening_for_next_day(70000) == 50000


def test_service_modules_exist():
    from app.services import billing, void_rebuild, accounting, wipe, import_jobs, cash_flow_svc
    assert hasattr(billing, "get_next_bill_no")
    assert hasattr(void_rebuild, "rebuild_pending_bills")
    assert hasattr(accounting, "_sync_payment_accounting")
    assert hasattr(wipe, "execute_domain_wipe")


def test_explicit_api_points_at_domain_module():
    from app.services import api, void_rebuild
    assert api.rebuild_pending_bills is void_rebuild.rebuild_pending_bills


def test_login_redirects_home(client):
    login(client)
    rv = client.get("/", follow_redirects=False)
    # authenticated users may get 200 or redirect depending on perms
    assert rv.status_code in (200, 302)
