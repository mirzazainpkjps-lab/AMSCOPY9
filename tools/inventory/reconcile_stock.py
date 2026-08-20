#!/usr/bin/env python3
"""reconcile_stock.py — stock movement-ledger vs stored-balance reconciliation.

Read-only.  For every material:

    Calculated Stock = Σ entry.qty(IN, non-void) − Σ entry.qty(OUT, non-void)
    Stored Stock     = material.total

Any material where the two differ is reported as a STOCK MISMATCH with the
offending movement rows (id, date, type, qty, bill, source) so the cause can
be investigated — never auto-repaired.

Also reports per-material totals for GRN receipts / sales / returns and
produces a JSON file (instance/inventory/reconcile_stock.json) for history.

Exit codes: 0 = reconciled, 1 = mismatches found, 2 = error.

Usage:
    python tools/inventory/reconcile_stock.py
    python tools/inventory/reconcile_stock.py --db /path/app.db --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "instance" / "ahmed_cement.db"
OUT_DIR = REPO_ROOT / "instance" / "inventory"
TOLERANCE = 0.01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--material", default="", help="restrict to one material name")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: database not found: {db}")
        return 2

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    material_filter = (args.material or '').strip()

    # --- authoritative movement ledger -------------------------------------
    where = ""
    params = ()
    if material_filter:
        where = "WHERE e.material = ?"
        params = (material_filter,)
    movements = [
        dict(r) for r in con.execute(
            f"""
            SELECT e.material,
                   SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN COALESCE(e.qty,0)
                            WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -COALESCE(e.qty,0)
                            ELSE 0 END) AS calculated
            FROM entry e
            WHERE COALESCE(e.is_void,0) = 0
              AND UPPER(COALESCE(e.type,'')) IN ('IN','OUT')
              {("AND e.material = ?" if material_filter else "")}
            GROUP BY e.material
            """
        ).fetchall()
    ]

    materials = [
        dict(r) for r in con.execute(
            "SELECT m.id, m.name, m.code, m.unit, m.total AS stored, m.is_active "
            "FROM material m " + where
        ).fetchall()
    ]

    calc = {str(r["material"]): float(r["calculated"] or 0) for r in movements}
    mismatches = []
    for m in materials:
        stored = float(m["stored"] or 0)
        expected = calc.get(str(m["name"]), 0.0)
        if abs(stored - expected) > TOLERANCE:
            mismatches.append({
                "material": m["name"],
                "code": m["code"],
                "stored": round(stored, 2),
                "calculated": round(expected, 2),
                "difference": round(stored - expected, 2),
            })

    # --- offending movement rows for mismatches -----------------------------
    mismatch_rows = {}
    if mismatches:
        names = [mm["material"] for mm in mismatches]
        qmarks = ",".join("?" for _ in names)
        rows = con.execute(
            f"""
            SELECT e.id, e.date, e.type, e.material, e.qty, e.bill_no, e.auto_bill_no,
                   e.nimbus_no, e.source_table, e.source_id, e.created_at, e.is_void,
                   e.transaction_category
            FROM entry e
            WHERE e.material IN ({qmarks})
            ORDER BY e.date, e.id
            """,
            names,
        ).fetchall()
        for r in rows:
            mismatch_rows.setdefault(str(r["material"]), []).append(dict(r))

    # --- movement composition per material (GRN in / sale out / returns) ----
    composition = {}
    comp_sql = """
        SELECT e.material,
               SUM(CASE WHEN e.type='IN'  AND e.auto_bill_no IS NOT NULL THEN COALESCE(e.qty,0) ELSE 0 END) AS grn_in,
               SUM(CASE WHEN e.type='IN'  AND COALESCE(e.nimbus_no,'')='Material Return' THEN COALESCE(e.qty,0) ELSE 0 END) AS returns_in,
               SUM(CASE WHEN e.type='IN'  AND e.auto_bill_no IS NULL AND COALESCE(e.nimbus_no,'')!='Material Return' THEN COALESCE(e.qty,0) ELSE 0 END) AS other_in,
               SUM(CASE WHEN e.type='OUT' AND COALESCE(e.nimbus_no,'')='Direct Sale' THEN COALESCE(e.qty,0) ELSE 0 END) AS sales_out,
               SUM(CASE WHEN e.type='OUT' AND COALESCE(e.nimbus_no,'') NOT IN ('Direct Sale','') THEN COALESCE(e.qty,0) ELSE 0 END) AS dispatch_out,
               SUM(CASE WHEN e.type='OUT' AND (COALESCE(e.nimbus_no,'')='' OR e.nimbus_no IS NULL) THEN COALESCE(e.qty,0) ELSE 0 END) AS other_out
        FROM entry e
        WHERE COALESCE(e.is_void,0)=0 AND UPPER(COALESCE(e.type,'')) IN ('IN','OUT')
        GROUP BY e.material
    """
    for r in con.execute(comp_sql).fetchall():
        composition[str(r["material"])] = {
            "grn_in": float(r["grn_in"] or 0),
            "returns_in": float(r["returns_in"] or 0),
            "other_in": float(r["other_in"] or 0),
            "sales_out": float(r["sales_out"] or 0),
            "dispatch_out": float(r["dispatch_out"] or 0),
            "other_out": float(r["other_out"] or 0),
        }
    con.close()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db),
        "materials_checked": len(materials),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "movement_rows_for_mismatches": mismatch_rows,
        "composition": composition,
        "overall": "PASS" if not mismatches else "FAIL",
    }
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "reconcile_stock.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
    except OSError:
        pass

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("=" * 90)
        print(f"  STOCK RECONCILIATION  —  {db}")
        print("=" * 90)
        print(f"  materials checked : {len(materials)}")
        print(f"  mismatches        : {len(mismatches)}")
        for m in materials:
            comp = composition.get(str(m["name"]), {})
            line = (f"    {m['name']:<30} stored={float(m['stored'] or 0):>14,.2f} "
                    f"calculated={calc.get(str(m['name']),0):>14,.2f}")
            if comp:
                line += (f"  [GRN+{comp['grn_in']:,.1f} RET+{comp['returns_in']:,.1f} "
                         f"OTHIN+{comp['other_in']:,.1f} | SALES-{comp['sales_out']:,.1f} "
                         f"DISP-{comp['dispatch_out']:,.1f} OTHOUT-{comp['other_out']:,.1f}]")
            print(line)
        if mismatches:
            print("\n  MISMATCH DETAIL (movement rows to investigate):")
            for mm in mismatches:
                print(f"\n  ✗ {mm['material']}: stored={mm['stored']} calculated={mm['calculated']} "
                      f"diff={mm['difference']}")
                for r in (mismatch_rows.get(mm["material"]) or [])[:20]:
                    print(f"      id={r['id']} {r['date']} {r['type']} qty={r['qty']} "
                          f"bill={r['bill_no'] or r['auto_bill_no'] or '-'} "
                          f"src={r['source_table'] or r['nimbus_no'] or '-'} void={r['is_void']}")
        print("=" * 90)
        print(f"  RESULT: {'PASS — stored stock equals movement ledger' if not mismatches else 'FAIL — see mismatches above'}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
