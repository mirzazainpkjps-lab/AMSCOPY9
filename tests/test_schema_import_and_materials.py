"""Schema failures on import and on manual material writes.

Reproduces the two user-facing failures:
1. Full-raw import of a workbook whose child sheet appears *before* its parent
   (Excel sheet order ≠ FK order) must still insert under PRAGMA foreign_keys=ON.
2. After that import, POST /add_material must succeed.
3. The importer must not crash when there is no HTTP session (job/CLI path).
"""
from __future__ import annotations

import io

import pandas as pd

from models import Material, MaterialCategory, db


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _child_before_parent_workbook():
    """Workbook with ``material`` *before* ``material_category`` — the order
    that used to abort import with FOREIGN KEY constraint failed."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame([{
            "id": 11,
            "code": "FBMCEM-000011",
            "name": "OPC TEST CEMENT",
            "category_id": 7,
            "unit_price": 0,
            "total": 0,
            "unit": "Bags",
            "is_active": 1,
        }]).to_excel(writer, sheet_name="material", index=False)
        pd.DataFrame([{
            "id": 7,
            "name": "Cement",
            "is_active": 1,
        }]).to_excel(writer, sheet_name="material_category", index=False)
        pd.DataFrame([
            {"key": "export_kind", "value": "literal_all"},
        ]).to_excel(writer, sheet_name="__AMS_META__", index=False)
    return buf.getvalue()


def test_full_raw_import_child_sheet_before_parent_succeeds(app, client):
    login(client)
    from blueprints.import_export.engine import _run_full_raw_import_bytes
    from blueprints.import_export.scope import _default_scope_context

    with app.test_request_context("/import_export/full_raw_import", method="POST"):
        report, _name = _run_full_raw_import_bytes(
            _child_before_parent_workbook(),
            _default_scope_context(),
            "replace_tenant_data",
            "materials-child-first.xlsx",
        )

    assert report["failed"] == 0, report.get("table_results")
    assert report["inserted"] >= 2
    with app.app_context():
        cat = MaterialCategory.query.filter_by(id=7).first()
        mat = Material.query.filter_by(code="FBMCEM-000011").first()
        assert cat is not None and cat.name == "Cement"
        assert mat is not None and mat.category_id == 7


def test_add_material_after_import(app, client):
    login(client)
    from blueprints.import_export.engine import _run_full_raw_import_bytes
    from blueprints.import_export.scope import _default_scope_context

    with app.test_request_context("/"):
        _run_full_raw_import_bytes(
            _child_before_parent_workbook(),
            _default_scope_context(),
            "replace_tenant_data",
            "materials-child-first.xlsx",
        )

    resp = client.post("/add_material", data={
        "material_name": "Lucky Cement",
        "material_unit": "Bags",
        "category_id": "7",
    }, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "could not be saved" not in body.lower()
    assert "Brand Added" in body or "already exists" in body
    with app.app_context():
        assert Material.query.filter(
            db.func.lower(Material.name) == "lucky cement"
        ).first() is not None


def test_import_without_request_context_does_not_crash(app):
    from blueprints.import_export.engine import _run_full_raw_import_bytes
    from blueprints.import_export.scope import _default_scope_context

    with app.app_context():
        report, _name = _run_full_raw_import_bytes(
            _child_before_parent_workbook(),
            _default_scope_context(),
            "append",
            "materials-child-first.xlsx",
        )
    assert isinstance(report, dict)
    assert "status" in report
