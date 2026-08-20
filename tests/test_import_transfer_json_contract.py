"""Regression tests: /import_export/transfer/import must answer JSON for
fetch-driven clients (the hub page "Import Full XLSX" flow) instead of
redirecting to an HTML page, which swallowed the real result and made the UI
show the generic "Server did not send a table list" dialog.

Also guards that plain (non-fetch) clients keep the classic flash+redirect
behavior.
"""
import io
import os

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
            "FULL_RAW_IMPORT_ENABLED": "1",
            # Keep runtime artifacts out of the repo's instance dir.
            "IMPORT_UPLOADS_DIR": str(tmp_path / "uploads"),
            "IMPORT_REPORTS_DIR": str(tmp_path / "reports"),
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


def _xlsx_bytes(sheets):
    """Build an in-memory xlsx where sheets maps sheet name -> list of dict rows."""
    import pandas as pd

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


FETCH_HEADERS = {"X-Requested-With": "fetch", "Accept": "application/json"}


def test_import_export_center_renders_new_guidance(client):
    rv = client.get("/import_export/")
    assert rv.status_code == 200
    assert b"Safe, row-by-row restore" in rv.data
    assert b"Import Full XLSX" in rv.data
    assert b"Export Full XLSX" in rv.data


def test_bogus_xlsx_fetch_gets_json_error_not_redirect(client):
    """The exact reported failure: arbitrary xlsx + literal_all + fetch headers
    must come back as JSON with a real error, never an HTML redirect."""
    blob = _xlsx_bytes({"Data": [{"foo": "bar"}]})
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "random.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.is_json, "fetch client must receive JSON, got: %s" % rv.content_type
    body = rv.get_json()
    assert body.get("ok") is False
    assert "No importable sheets found" in (body.get("headline") or "")
    assert body.get("table_results") == []


def test_bogus_xlsx_plain_post_still_redirects(client):
    """Non-fetch clients keep the classic flash + redirect behavior."""
    blob = _xlsx_bytes({"Data": [{"foo": "bar"}]})
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "random.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        content_type="multipart/form-data",
    )
    assert rv.status_code == 302


def test_missing_file_fetch_gets_json_error(client):
    rv = client.post(
        "/import_export/transfer/import",
        data={"sections": "literal_all", "mode": "append"},
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 400
    assert rv.is_json
    assert "upload" in (rv.get_json().get("headline") or "").lower()


def test_valid_xlsx_fetch_gets_json_report_and_inserts(client, app):
    """A workbook with real table sheets must return the per-table JSON report
    and actually persist the rows."""
    from models import Client, MaterialCategory, db

    blob = _xlsx_bytes(
        {
            "client": [
                {"code": "FBMCL-T01", "name": "Test Import Client"},
            ],
            "material_category": [
                {"name": "Import Test Category"},
            ],
        }
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "backup.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    assert rv.is_json
    body = rv.get_json()
    assert body.get("ok") is True
    assert body.get("table_results"), "expected per-table breakdown in JSON"
    names = [r.get("name") for r in body["table_results"]]
    assert "client" in names and "material_category" in names

    with app.app_context():
        assert Client.query.filter_by(code="FBMCL-T01").first() is not None
        assert MaterialCategory.query.filter_by(name="Import Test Category").first() is not None


def test_detect_master_backup_switches_and_returns_json(client, app):
    """A master-style workbook uploaded to the literal_all card is auto-switched
    to all_business and still answers JSON for fetch clients."""
    from models import MaterialCategory, db

    blob = _xlsx_bytes(
        {
            "Clients": [{"code": "FBMCL-T02", "name": "Master Import Client"}],
            "Materials": [{"code": "MAT-T02", "name": "Master Import Material"}],
            "MaterialCategories": [{"name": "Master Import Category"}],
            "PendingBills": [{"client_code": "FBMCL-T02", "bill_no": "PB-1", "amount": 100}],
        }
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "master.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    assert rv.is_json
    body = rv.get_json()
    assert body.get("ok") is True
    assert "Master Backup" in (body.get("headline") or "")

    with app.app_context():
        assert MaterialCategory.query.filter_by(name="Master Import Category").first() is not None
        assert db.session.execute(
            db.select(db.text("count(*)")).select_from(db.text("client"))
        ).scalar() >= 1


def test_master_bad_row_does_not_rollback_good_rows(client, app):
    """Auto-detected master backups also isolate legacy processor failures."""
    from models import Client, Material

    blob = _xlsx_bytes(
        {
            "Clients": [{"code": "FBMCL-MP01", "name": "Master Partial Client"}],
            "MaterialCategories": [{"name": "Master Partial Category"}],
            "Materials": [
                {
                    "code": "MAT-MP-OK",
                    "name": "Master Valid Material",
                    "category_name": "Master Partial Category",
                    "unit_price": 125,
                },
                {
                    "code": "MAT-MP-BAD",
                    "name": "Master Broken Material",
                    "category_name": "Master Partial Category",
                    "unit_price": "not-a-number",
                },
            ],
            "PendingBills": [],
        }
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "master-partial.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is False
    assert body["failed"] == 1
    materials = next(row for row in body["table_results"] if row["name"] == "Materials")
    assert materials["status"] == "partial"
    assert materials["inserted"] == 1
    assert materials["failed"] == 1
    assert "Materials row 3" in materials["error"]

    with app.app_context():
        assert Client.query.filter_by(code="FBMCL-MP01").first() is not None
        assert Material.query.filter_by(code="MAT-MP-OK").first() is not None
        assert Material.query.filter_by(code="MAT-MP-BAD").first() is None


def test_literal_user_iso_datetime_is_typed_before_autoflush(client, app):
    """Regression for the reported User.created_at SQLite DateTime crash."""
    from datetime import datetime
    from models import User

    blob = _xlsx_bytes(
        {
            "user": [
                {
                    "username": "imported-manager",
                    "role": "admin",
                    "status": "active",
                    "can_import_export": True,
                    "created_at": "2026-08-14T10:11:12",
                },
                {
                    "username": "imported-clerk",
                    "role": "user",
                    "status": "active",
                    "created_at": "2026-08-13 09:08:07",
                },
            ],
            "client": [{"code": "FBMCL-DT01", "name": "Date Restore Client"}],
        }
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "literal-users.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True, body
    user_result = next(row for row in body["table_results"] if row["name"].startswith("user"))
    assert user_result["inserted"] == 2
    assert user_result["failed"] == 0

    with app.app_context():
        restored = User.query.filter_by(username="imported-manager").one()
        assert isinstance(restored.created_at, datetime)
        assert restored.created_at == datetime(2026, 8, 14, 10, 11, 12)


def test_literal_bad_row_is_reported_but_valid_rows_commit(client, app, tmp_path):
    """A malformed typed value must reject only its row and keep the Session usable."""
    from models import MaterialCategory

    blob = _xlsx_bytes(
        {
            "material_category": [
                {"name": "Valid Partial Category", "is_active": True, "created_at": "2026-08-14T12:00:00"},
                {"name": "Broken Date Category", "is_active": True, "created_at": "definitely-not-a-date"},
                {"name": "Second Valid Category", "is_active": True, "created_at": "2026-08-14 13:00:00"},
            ]
        }
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "partial.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is False
    assert body["status"] == "partial"
    assert body["inserted"] == 2
    assert body["failed"] == 1
    table = next(row for row in body["table_results"] if row["name"] == "material_category")
    assert table["status"] == "partial"
    assert table["inserted"] == 2
    assert table["failed"] == 1
    assert "Row 3" in table["error"]
    assert "invalid date/time" in table["error"]
    assert body.get("report_name", "").endswith(".csv")
    assert (tmp_path / "reports" / body["report_name"]).exists()
    downloaded = client.get(f"/import_export/full_raw_import_report/{body['report_name']}")
    assert downloaded.status_code == 200
    assert b"material_category" in downloaded.data
    assert b"invalid date/time" in downloaded.data

    with app.app_context():
        assert MaterialCategory.query.filter_by(name="Valid Partial Category").first() is not None
        assert MaterialCategory.query.filter_by(name="Second Valid Category").first() is not None
        assert MaterialCategory.query.filter_by(name="Broken Date Category").first() is None
        # A query after the rejected row proves no PendingRollbackError remains.
        assert MaterialCategory.query.count() >= 2


def test_literal_overwrite_replaces_supplied_table_only(client, app):
    from models import Client, db

    with app.app_context():
        db.session.add(Client(code="FBMCL-OLD", name="Old Client"))
        db.session.commit()

    blob = _xlsx_bytes(
        {"client": [{"code": "FBMCL-NEW", "name": "Restored Client"}]}
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(blob), "overwrite.xlsx"),
            "sections": "literal_all",
            "mode": "replace_tenant_data",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True

    with app.app_context():
        assert Client.query.filter_by(code="FBMCL-OLD").first() is None
        assert Client.query.filter_by(code="FBMCL-NEW").one().name == "Restored Client"


def test_exported_full_workbook_round_trips_without_type_failures(client):
    exported = client.post(
        "/import_export/transfer/export",
        data={"sections": "literal_all"},
    )
    assert exported.status_code == 200
    assert exported.data[:2] == b"PK"  # XLSX is a ZIP container.

    restored = client.post(
        "/import_export/transfer/import",
        data={
            "file": (io.BytesIO(exported.data), "round-trip.xlsx"),
            "sections": "literal_all",
            "mode": "append",
        },
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert restored.status_code == 200
    body = restored.get_json()
    assert body["ok"] is True, body
    assert body["failed"] == 0
    assert body["warnings"] == 0
    assert body["table_results"]
    assert all(row.get("status") in {"ok", "skipped"} for row in body["table_results"])
