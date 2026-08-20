"""One-shot real-data migration: clean DB -> import ALLEXPORT xlsx -> verify -> export.

Uses the app's own full-raw import/export engine (the same code paths as the
Import & Export web page), so the result is exactly what a user would get by
using the UI.

Run from the repo root:
    ALLOW_EMPTY_DB=1 ALLOW_DB_DROP=1 FULL_RAW_IMPORT_ENABLED=1 \
        python tools/realdata_migration.py
"""
from __future__ import annotations

import os
import sys
import shutil
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_EMPTY_DB", "1")
os.environ.setdefault("ALLOW_DB_DROP", "1")
os.environ.setdefault("FULL_RAW_IMPORT_ENABLED", "1")

DB = ROOT / "instance" / "ahmed_cement.db"
XLSX = ROOT / "Realdata" / "ALLEXPORT-14-08-2026_05-51PM.xlsx"
OUT_DIR = ROOT / "instance" / "_migration_check"
CLEAN = "--clean" in sys.argv
DO_EXPORT = "--no-export" not in sys.argv


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Phase 0: snapshot pre-state row counts for the report ----
    if CLEAN and DB.exists():
        print("== Backing up current DB (already backed up separately) ==")
        shutil.copy2(DB, OUT_DIR / "pre_migration_ahmed_cement.db")

    from app import create_app
    from models import db

    app = create_app()

    with app.app_context():
        # ---- Phase 1: complete clean ----
        if CLEAN:
            print("== Phase 1: complete clean (drop all tables, recreate fresh schema) ==")
            db.drop_all()
            db.create_all()
            # Non-destructive consistency/bootstrap backfills (same as startup)
            from app.services.schema import (
                _ensure_model_columns,
                _ensure_account_type_compat,
                _ensure_material_categories,
                _ensure_discount_columns,
                _ensure_bill_counter_namespace_defaults,
                _ensure_waive_off_table,
                _ensure_delivery_person_payments_table,
                _ensure_user_permission_defaults,
            )
            # NOTE: deliberately do NOT seed a default admin here. Importing into a
            # truly empty `user` table preserves every user row (ids, hashes,
            # created_at) exactly as they appear in the export.
            _ensure_model_columns()
            _ensure_account_type_compat()
            _ensure_material_categories()
            _ensure_discount_columns()
            _ensure_bill_counter_namespace_defaults()
            _ensure_waive_off_table()
            _ensure_delivery_person_payments_table()
            _ensure_user_permission_defaults()
            db.session.commit()
            print("   clean DB ready.")

        # ---- Phase 2: import ----
        print("== Phase 2: full raw import (replace mode) ==")
        from blueprints.import_export.engine import _run_full_raw_import_bytes
        from blueprints.import_export.scope import (
            _resolve_scope_context,
            _set_import_actor_context,
            _clear_import_actor_context,
        )

        file_bytes = XLSX.read_bytes()
        scope_ctx = _resolve_scope_context(scope_raw=None, tenant_id_raw=None)

        with app.test_request_context("/import_export/full_raw_import"):
            from flask import g
            g.user = None
            # Use a non-matching actor so the importer does not "protect" any
            # user's credentials and every user row is restored verbatim.
            _set_import_actor_context(username="__migration__", tenant_id=None, role="admin")
            try:
                report, report_name = _run_full_raw_import_bytes(
                    file_bytes=file_bytes,
                    scope_ctx=scope_ctx,
                    mode="replace_tenant_data",
                    source_file_name=XLSX.name,
                )
            finally:
                _clear_import_actor_context()

        print(f"   report status: {report.get('status')}")
        print(f"   inserted={report.get('inserted')} updated={report.get('updated')} "
              f"skipped={report.get('skipped')} failed={report.get('failed')} "
              f"warnings={report.get('warnings')} tables={report.get('tables')}")
        if report.get("failed"):
            print("   !! failed rows present — see table_results below")
            for tr in report.get("table_results", []):
                if tr.get("failed"):
                    print(f"      [FAIL] {tr.get('name')}: {tr.get('error')}")

        # ---- Phase 3: consistency backfills (same as app startup) ----
        print("== Phase 3: consistency backfills ==")
        from models import DirectSale
        from sqlalchemy import func, or_
        from app.services.lookups import get_client_by_input
        for sale in DirectSale.query.filter(
            or_(DirectSale.client_code.is_(None), func.trim(DirectSale.client_code) == "")
        ).all():
            cli = get_client_by_input(sale.client_name or "")
            if cli:
                sale.client_code = cli.code
        db.session.commit()
        print("   client_code backfill done.")

        # ---- Phase 4: verify counts vs sheets ----
        print("== Phase 4: verify row counts ==")
        import pandas as pd
        xls = pd.ExcelFile(XLSX)
        problems = []
        for table in db.metadata.sorted_tables:
            sheet = table.name[:31]
            if sheet not in xls.sheet_names:
                # table not present in file (newer feature) — expect empty or seed
                continue
            df = pd.read_excel(xls, sheet)
            sheet_rows = len(df)
            db_rows = db.session.execute(
                __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(table)
            ).scalar() or 0
            mark = "OK" if sheet_rows == db_rows else "MISMATCH"
            if sheet_rows != db_rows:
                problems.append((table.name, sheet_rows, db_rows))
            print(f"   {mark:9} {table.name:32} file={sheet_rows:6} db={db_rows:6}")
        if problems:
            print("   !! COUNT MISMATCHES:")
            for name, sr, dr in problems:
                print(f"      {name}: file={sr} db={dr}")
        else:
            print("   all shared tables match row-for-row.")

        # ---- Phase 5: export ----
        if DO_EXPORT:
            print("== Phase 5: full raw export ==")
            from blueprints.import_export.export_build import _build_full_raw_export_bytes
            out_bytes = _build_full_raw_export_bytes(scope_ctx=scope_ctx)
            out_path = OUT_DIR / "ALLEXPORT_reexport.xlsx"
            out_path.write_bytes(out_bytes)
            print(f"   wrote {out_path} ({len(out_bytes)} bytes)")

    print("DONE")


if __name__ == "__main__":
    main()
