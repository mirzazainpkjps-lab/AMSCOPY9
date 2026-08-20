"""Second round-trip: import the re-export -> export again -> must be identical.

Proves "export file == import file": the exported file re-imports losslessly and
the export format is stable (no drift). Only __AMS_META__ exported_at changes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_EMPTY_DB", "1")
os.environ.setdefault("ALLOW_DB_DROP", "1")
os.environ.setdefault("FULL_RAW_IMPORT_ENABLED", "1")

E1 = ROOT / "instance" / "_migration_check" / "ALLEXPORT_reexport.xlsx"
E2 = ROOT / "instance" / "_migration_check" / "ALLEXPORT_reexport2.xlsx"

import pandas as pd
from roundtrip_compare import canonical  # reuse the same canonicalizer


def main():
    from app import create_app
    from models import db

    app = create_app()

    with app.app_context():
        # clean DB again
        db.drop_all()
        db.create_all()

        from blueprints.import_export.engine import _run_full_raw_import_bytes
        from blueprints.import_export.scope import (
            _resolve_scope_context,
            _set_import_actor_context,
            _clear_import_actor_context,
        )
        scope_ctx = _resolve_scope_context(scope_raw=None, tenant_id_raw=None)
        file_bytes = E1.read_bytes()
        with app.test_request_context("/import_export/full_raw_import"):
            from flask import g
            g.user = None
            _set_import_actor_context(username="__migration__", tenant_id=None, role="admin")
            try:
                report, _ = _run_full_raw_import_bytes(
                    file_bytes=file_bytes, scope_ctx=scope_ctx,
                    mode="replace_tenant_data", source_file_name=E1.name,
                )
            finally:
                _clear_import_actor_context()
        print(f"re-import report: status={report.get('status')} inserted={report.get('inserted')} "
              f"failed={report.get('failed')} warnings={report.get('warnings')}")

        from blueprints.import_export.export_build import _build_full_raw_export_bytes
        E2.write_bytes(_build_full_raw_export_bytes(scope_ctx=scope_ctx))

    # ---- compare E1 vs E2 ----
    a = pd.ExcelFile(E1)
    b = pd.ExcelFile(E2)
    mismatches = 0
    for sheet in a.sheet_names:
        if sheet == "__AMS_META__":
            continue
        df_a = pd.read_excel(a, sheet)
        df_b = pd.read_excel(b, sheet)
        if list(df_a.columns) != list(df_b.columns):
            print(f"[{sheet}] COLUMN SET differs: {list(df_a.columns)} vs {list(df_b.columns)}")
            mismatches += 1
            continue
        if len(df_a) != len(df_b):
            print(f"[{sheet}] ROW COUNT differs: {len(df_a)} vs {len(df_b)}")
            mismatches += 1
        n = min(len(df_a), len(df_b))
        for i in range(n):
            for c in df_a.columns:
                if canonical(df_a.iloc[i][c]) != canonical(df_b.iloc[i][c]):
                    if mismatches < 10:
                        print(f"[{sheet}] row {i + 2} col {c!r}: {canonical(df_a.iloc[i][c])!r} != {canonical(df_b.iloc[i][c])!r}")
                    mismatches += 1

    print("=" * 60)
    print(f"TOTAL MISMATCHES (E1 vs E2): {mismatches}")
    print("RESULT:", "PASS — export file == import file (idempotent)" if mismatches == 0
          else "FAIL")


if __name__ == "__main__":
    main()
