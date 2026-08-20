"""04_run_post_import_audit.py — run the post-migration SQL audit.

Executes every statement in ``post_import_audit.sql`` against a target
database (default: instance/ahmed_cement.db) and prints a PASS/FAIL summary.

Semantics:
  * Queries 1-5, 7, 8 must return ZERO rows (leaks / orphans / mismatches).
  * Query 6 (client ledger) and 9 (inactive masters) are informational.
  * Query 10 (expected money totals) should match the values printed by
    03_verify_clean_export.py.

Exit code 1 when any required check fails.

Usage:
    python tools/migrate/04_run_post_import_audit.py
    python tools/migrate/04_run_post_import_audit.py --db /path/to/app.db
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_FILE = HERE / "post_import_audit.sql"
DEFAULT_DB = HERE.parents[1] / "instance" / "ahmed_cement.db"

REQUIRED_PREFIXES = ("VOID LEAK", "CANCEL LEAK", "FK ORPHAN", "LEDGER",
                     "SEQ CHECK", "DUP KEY")
INFO_PREFIXES = ("CLIENT LEDGER", "INACTIVE MASTER", "TOTAL")

EXPECTED_TOTALS = {
    "direct_sale.amount": "23719927.40",
    "direct_sale.paid_amount": "7291719.50",
    "payment.amount": "53923466.95",
    "pending_bill.amount": "16519225.37",
    "invoice.total_amount": "19467450.87",
    "invoice.balance": "18822430.57",
    "account_transaction.amount": "288365494.23",
    "booking.amount": "133672972.73",
    "booking.paid_amount": "88044330.99",
    "waive_off.amount": "350430.49",
    "material_return.amount": "1225363.30",
    "supplier_payment.amount": "28392809.99",
    "delivery_rent.amount": "1263529.00",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return 2
    sql = SQL_FILE.read_text(encoding="utf-8")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    failures: list[str] = []
    totals_ok = True

    # post_import_audit.sql uses "SELECT ... ;" statements separated by
    # blank lines; execute statement-by-statement.  Comment lines are removed
    # before the display name is extracted (comments may contain apostrophes).
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    print("=" * 100)
    print(f"  POST-IMPORT AUDIT  —  {db_path}")
    print("=" * 100)

    for stmt in statements:
        code_only = re.sub(r"(?m)^\s*--.*$", "", stmt).strip()
        name = code_only.split("'")[1] if "'" in code_only else "UNLABELLED"
        try:
            rows = [dict(r) for r in con.execute(stmt).fetchall()]
        except sqlite3.Error as exc:
            failures.append(f"{name}: SQL error: {exc}")
            print(f"  [ERROR] {name}: {exc}")
            continue

        violations = 0
        detail = []
        for r in rows:
            v = r.get("violations", 0)
            try:
                v = float(v or 0)
            except (TypeError, ValueError):
                v = 0
            if v != 0 and name.startswith(REQUIRED_PREFIXES):
                violations += 1
                detail.append(f"{r.get('tbl') or r.get('ref')}: {v}")
            elif name == "TOTAL":
                ref = r.get("ref")
                got = f"{v:,.2f}"
                want = EXPECTED_TOTALS.get(ref)
                ok = want is None or abs(v - float(want)) <= 0.005
                if not ok:
                    totals_ok = False
                    failures.append(f"TOTAL {ref}: got {got} expected {want}")

        flag = "OK "
        if name.startswith(REQUIRED_PREFIXES) and violations:
            flag = "FAIL"
            failures.append(f"{name}: {violations} violation(s): " + "; ".join(detail[:6]))
        elif name == "TOTAL" and not totals_ok:
            flag = "INFO"  # detail printed below

        if name.startswith(REQUIRED_PREFIXES):
            print(f"  [{flag}] {name:<22} rows_with_violations={violations}")
            for d in detail[:6]:
                print(f"          {d}")
        elif name in ("CLIENT LEDGER",):
            print(f"  [INFO] {name:<22} rows={len(rows)}")
        elif name == "INACTIVE MASTER":
            print(f"  [INFO] {name:<22} rows={len(rows)}")
            for r in rows[:10]:
                print(f"          {r.get('tbl')}: {r.get('violations')} inactive row(s) preserved")
        elif name == "TOTAL":
            print(f"  [INFO] {name:<22} expected money totals "
                  f"({'OK' if totals_ok else 'MISMATCH'})")
            if not totals_ok:
                for r in rows:
                    ref = r.get("ref")
                    got = f"{float(r.get('violations') or 0):,.2f}"
                    want = EXPECTED_TOTALS.get(ref)
                    mark = "OK " if want is None or abs(float(r.get('violations') or 0) - float(want)) <= 0.005 else "FAIL"
                    print(f"          [{mark}] {ref:<26} {got:>16}  expected {want}")

    con.close()
    print("=" * 100)
    if failures:
        print(f"RESULT: FAIL — {len(failures)} issue(s)")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("RESULT: PASS — migrated database is clean and balanced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
