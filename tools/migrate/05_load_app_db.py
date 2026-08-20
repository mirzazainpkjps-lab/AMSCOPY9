"""05_load_app_db.py — load the cleaned export into the application database.

This is the final load step of the migration pipeline.  It:

  1. takes a backup of the current DB (instance/migration/pre_import_*.db)
  2. runs the app's own full-raw importer (replace mode) on the cleaned xlsx
  3. backfills direct_sale.client_code (same as app startup)
  4. applies post_import_enrichment.sql (money minor units, payment.client_id,
     direct_sale.client_code, plaintext-password wipe)

Requires --confirm (repo convention for anything that writes to the DB).

Usage:
    python tools/migrate/05_load_app_db.py --confirm
    python tools/migrate/05_load_app_db.py --confirm \
        --clean instance/migration/ALLEXPORT-CLEAN-<timestamp>.xlsx
    python tools/migrate/05_load_app_db.py --confirm --db /path/to/app.db

Afterwards run the gate:
    python tools/migrate/04_run_post_import_audit.py --db instance/ahmed_cement.db
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "instance" / "ahmed_cement.db"
BACKUP_DIR = ROOT / "instance" / "migration"


def _latest_clean_xlsx() -> Path:
    candidates = sorted(BACKUP_DIR.glob("ALLEXPORT-CLEAN-*.xlsx"))
    if not candidates:
        raise SystemExit(
            "No clean export found. Run tools/migrate/02_build_clean_export.py first."
        )
    return candidates[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", required=True,
                    help="required: this script writes to the application database")
    ap.add_argument("--clean", default="", help="cleaned xlsx (default: latest in instance/migration)")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    db_path = Path(args.db).resolve()
    clean_path = Path(args.clean).resolve() if args.clean else _latest_clean_xlsx()
    if not clean_path.exists():
        print(f"ERROR: clean export not found: {clean_path}")
        return 2

    # ---- 1. backup ---------------------------------------------------------
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"pre_import_{db_path.stem}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    if db_path.exists():
        shutil.copy2(db_path, backup)
        print(f"backup -> {backup}")

    # ---- 2. import via the app's own engine --------------------------------
    os.environ["APP_DB_PATH"] = str(db_path)
    os.environ.setdefault("ALLOW_EMPTY_DB", "1")
    os.environ.setdefault("ALLOW_DB_DROP", "1")
    os.environ.setdefault("FULL_RAW_IMPORT_ENABLED", "1")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    from app import create_app
    from models import db

    app = create_app()
    with app.app_context():
        from blueprints.import_export.engine import _run_full_raw_import_bytes
        from blueprints.import_export.scope import (
            _resolve_scope_context,
            _set_import_actor_context,
            _clear_import_actor_context,
        )

        file_bytes = clean_path.read_bytes()
        scope_ctx = _resolve_scope_context(scope_raw=None, tenant_id_raw=None)
        with app.test_request_context("/import_export/full_raw_import"):
            from flask import g
            g.user = None
            _set_import_actor_context(username="__migration__", tenant_id=None, role="admin")
            try:
                report, _ = _run_full_raw_import_bytes(
                    file_bytes=file_bytes,
                    scope_ctx=scope_ctx,
                    mode="replace_tenant_data",
                    source_file_name=clean_path.name,
                )
            finally:
                _clear_import_actor_context()

        print(f"import report: inserted={report.get('inserted')} updated={report.get('updated')} "
              f"skipped={report.get('skipped')} failed={report.get('failed')} "
              f"warnings={report.get('warnings')} status={report.get('status')}")
        if report.get("failed"):
            print("   !! failed rows present:")
            for tr in report.get("table_results", []):
                if tr.get("failed"):
                    print(f"      [FAIL] {tr.get('name')}: {tr.get('error')}")
            return 1

        # ---- 3. client_code backfill (same as app startup) ------------------
        from sqlalchemy import func, or_
        from app.services.lookups import get_client_by_input
        for sale in db.session.query(__import__("models", fromlist=["DirectSale"]).DirectSale).filter(
            or_(
                __import__("models", fromlist=["DirectSale"]).DirectSale.client_code.is_(None),
                func.trim(__import__("models", fromlist=["DirectSale"]).DirectSale.client_code) == "",
            )
        ).all():
            cli = get_client_by_input(sale.client_name or "")
            if cli:
                sale.client_code = cli.code
        db.session.commit()

    # ---- 4. enrichment ------------------------------------------------------
    con = sqlite3.connect(db_path)
    con.executescript((HERE / "post_import_enrichment.sql").read_text(encoding="utf-8"))
    con.commit()
    con.close()
    print("enrichment applied (money minor units, client_id/client_code backfills, plaintext wipe)")

    print("\nNEXT: python tools/migrate/04_run_post_import_audit.py --db "
          f"{db_path}  (must print PASS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
