"""Import files are disposable; pre-wipe backups must not write under instance/."""
import io
import os
from pathlib import Path

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "test.db"
    os.environ["APP_DB_PATH"] = str(db_file)
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
            "IMPORT_TMP_DIR": str(tmp_path / ".tmp"),
            "IMPORT_UPLOADS_DIR": str(tmp_path / ".tmp" / "import_uploads"),
            "IMPORT_REPORTS_DIR": str(tmp_path / ".tmp" / "import_reports"),
            "IMPORT_ARTIFACT_RETENTION_SECONDS": 7 * 24 * 3600,
        }
    )
    with application.app_context():
        db.create_all()
        if not User.query.filter_by(username="tester").first():
            db.session.add(
                User(
                    username="tester",
                    role="admin",
                    status="active",
                    password_hash=generate_password_hash("secret"),
                    can_import_export=True,
                )
            )
            db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def _xlsx_bytes(sheets):
    import pandas as pd

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            import pandas as pd
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


FETCH_HEADERS = {"X-Requested-With": "fetch", "Accept": "application/json"}


def test_successful_full_import_does_not_keep_report_file(client, app, tmp_path):
    blob = _xlsx_bytes({"client": [{"code": "FBMCL-OK1", "name": "Clean Import Client"}]})
    rv = client.post(
        "/import_export/transfer/import",
        data={"file": (io.BytesIO(blob), "ok.xlsx"), "sections": "literal_all", "mode": "append"},
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is True
    assert not body.get("report_name")
    reports = tmp_path / ".tmp" / "import_reports"
    leftover = list(reports.glob("full_raw_import_report_*")) if reports.exists() else []
    assert leftover == []


def test_partial_import_keeps_report_until_retention(client, app, tmp_path):
    blob = _xlsx_bytes(
        {
            "material_category": [
                {"name": "Keep Report Category", "is_active": True, "created_at": "2026-08-14T12:00:00"},
                {"name": "Broken", "is_active": True, "created_at": "not-a-date"},
            ]
        }
    )
    rv = client.post(
        "/import_export/transfer/import",
        data={"file": (io.BytesIO(blob), "partial.xlsx"), "sections": "literal_all", "mode": "append"},
        headers=FETCH_HEADERS,
        content_type="multipart/form-data",
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["ok"] is False
    assert body.get("report_name", "").endswith(".csv")
    report_path = tmp_path / ".tmp" / "import_reports" / body["report_name"]
    assert report_path.exists()


def test_successful_job_deletes_upload_after_commit(client, app, tmp_path):
    blob = _xlsx_bytes({"client": [{"code": "FBMCL-JOB1", "name": "Job Import Client"}]})
    data = {"file": (io.BytesIO(blob), "demo.xlsx")}
    rv = client.post("/import_export/upload", data=data, content_type="multipart/form-data")
    body = rv.get_json()
    upload_id = body["upload_id"]
    dest = tmp_path / ".tmp" / "import_uploads" / f"{upload_id}.xlsx"
    assert dest.exists()
    start = client.post(f"/import_export/uploads/{upload_id}/start")
    assert start.status_code == 200
    job = start.get_json()
    assert job["status"] == "completed", job
    assert not dest.exists()


def test_failed_job_keeps_upload(client, app, tmp_path):
    data = {"file": (io.BytesIO(b"PK\x03\x04fake-xlsx"), "bad.xlsx")}
    rv = client.post("/import_export/upload", data=data, content_type="multipart/form-data")
    upload_id = rv.get_json()["upload_id"]
    dest = tmp_path / ".tmp" / "import_uploads" / f"{upload_id}.xlsx"
    start = client.post(f"/import_export/uploads/{upload_id}/start")
    assert start.get_json()["status"] == "failed"
    assert dest.exists()


def test_pre_wipe_safety_backup_writes_nothing(app, tmp_path):
    from app.services.wipe import _create_pre_wipe_safety_backups, _create_pre_wipe_tenant_backup

    with app.app_context():
        info = _create_pre_wipe_safety_backups(["clients"])
        assert info.get("skipped") is True
        assert info.get("db_backup_path") is None
        assert info.get("backup_dir") is None
        name, path = _create_pre_wipe_tenant_backup(type("T", (), {"id": 1, "name": "x"})())
        assert name is None and path is None
    instance_hits = list(Path(app.instance_path).rglob("pre_wipe*"))
    assert instance_hits == []
    assert not (tmp_path / "pre_wipe_backups").exists()
