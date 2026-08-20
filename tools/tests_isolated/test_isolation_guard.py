"""
Test Isolation Guard
=====================
Ensures integration tests in this folder NEVER write to the production database.

Import this at the top of every test script in tools/tests_isolated/:

    from tools.tests_isolated.test_isolation_guard import require_test_db
    require_test_db()

What it does:
  - Checks for AMS_TEST_DB environment variable.
  - If not set, aborts with a clear error — the test will not run.
  - If set, patches the Flask app's SQLALCHEMY_DATABASE_URI and health snapshot
    path so all DB operations go to the test database.
  - Creates the test DB schema on first use.
  - Optionally wipes the test DB after the run (AMS_TEST_DB_CLEANUP=1).

How to run an isolated test:
    AMS_TEST_DB=/tmp/ams_test.db python tools/tests_isolated/verify_refund_quick.py --confirm
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def require_test_db(*, auto_create: bool = True) -> str:
    """
    Enforce test DB isolation. Returns the test DB path.
    Aborts if AMS_TEST_DB is not set.
    """
    test_db = os.environ.get("AMS_TEST_DB", "").strip()

    if not test_db:
        print(
            "\n[AMS TestGuard] STOPPED — Test isolation required.\n"
            "\n"
            "  These scripts in tools/tests_isolated/ write to a database.\n"
            "  They must NEVER run against the production database.\n"
            "\n"
            "  Set AMS_TEST_DB to a temporary path and re-run:\n"
            "\n"
            "    AMS_TEST_DB=/tmp/ams_test.db python " + " ".join(sys.argv) + "\n"
            "\n"
            "  Or use a one-liner:\n"
            "    AMS_TEST_DB=$(mktemp /tmp/ams_XXXXXX.db) python " + " ".join(sys.argv) + "\n",
            file=sys.stderr,
        )
        sys.exit(1)

    prod_db = Path("instance") / "ahmed_cement.db"
    test_path = Path(test_db).resolve()
    prod_path = prod_db.resolve()

    if test_path == prod_path:
        print(
            f"\n[AMS TestGuard] STOPPED — AMS_TEST_DB points to the production database!\n"
            f"  AMS_TEST_DB={test_db}\n"
            f"  Production DB={prod_db}\n"
            f"  They resolve to the same path. Aborting.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    os.environ["APP_DB_PATH"] = str(test_path)
    os.environ["DB_HEALTH_SNAPSHOT_PATH"] = str(test_path.with_suffix(".health.json"))
    os.environ["ALLOW_EMPTY_DB"] = "1"
    os.environ["ALLOW_DB_DROP"] = "1"

    print(f"[AMS TestGuard] Test DB isolation active: {test_path}")
    return str(test_path)


def use_temp_db() -> str:
    """
    Create a disposable temp DB for the duration of this process.
    Automatically sets AMS_TEST_DB and patches env vars.
    """
    tmp = tempfile.NamedTemporaryFile(prefix="ams_test_", suffix=".db", delete=False)
    tmp.close()
    os.environ["AMS_TEST_DB"] = tmp.name
    return require_test_db(auto_create=True)


def cleanup_test_db() -> None:
    """Remove the test DB file if AMS_TEST_DB_CLEANUP=1."""
    if os.environ.get("AMS_TEST_DB_CLEANUP", "0").strip() != "1":
        return
    test_db = os.environ.get("AMS_TEST_DB", "").strip()
    if test_db:
        try:
            Path(test_db).unlink(missing_ok=True)
            print(f"[AMS TestGuard] Cleaned up test DB: {test_db}")
        except Exception as exc:
            print(f"[AMS TestGuard] Could not clean up {test_db}: {exc}")
