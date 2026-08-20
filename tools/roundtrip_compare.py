"""Compare the original ALLEXPORT xlsx with a re-export after import.

Proves "import file == export file" semantically (no data loss). The current
app schema has evolved past the export, so the re-export legitimately contains
*extra* empty columns/tables — those are ignored here. Every cell present in
the original must appear, unchanged, in the re-export.
"""
from __future__ import annotations

import sys
import math
from datetime import datetime, date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ORIG = ROOT / "Realdata" / "ALLEXPORT-14-08-2026_05-51PM.xlsx"
REEX = ROOT / "instance" / "_migration_check" / "ALLEXPORT_reexport.xlsx"


def canonical(v):
    """Reduce a cell to a comparable canonical form."""
    if v is None:
        return ("",)
    if isinstance(v, float) and math.isnan(v):
        return ("",)
    if isinstance(v, bool):
        return ("bool", 1 if v else 0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return ("num", float(v))
    if isinstance(v, datetime):
        return ("dt", v.replace(tzinfo=None))
    if isinstance(v, date):
        return ("d", v)
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none", "nat"):
        return ("",)
    # normalize datetime-like strings
    if "T" in s or (len(s) >= 10 and s[4] == "-" and s[7] == "-"):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return ("dt", dt.replace(tzinfo=None))
        except Exception:
            try:
                d = date.fromisoformat(s[:10])
                return ("d", d)
            except Exception:
                pass
    # normalize numeric strings
    try:
        f = float(s)
        return ("num", f)
    except Exception:
        pass
    # bool-like strings
    low = s.lower()
    if low in ("true", "yes", "on"):
        return ("bool", 1)
    if low in ("false", "no", "off"):
        return ("bool", 0)
    return ("str", s)


def main():
    orig = pd.ExcelFile(ORIG)
    reex = pd.ExcelFile(REEX)

    reex_sheets = set(reex.sheet_names)
    total_mismatch = 0
    compared_sheets = 0
    compared_cells = 0

    for sheet in orig.sheet_names:
        if sheet == "__AMS_META__":
            continue
        if sheet not in reex_sheets:
            print(f"[MISSING SHEET] {sheet}")
            total_mismatch += 1
            continue
        df_o = pd.read_excel(orig, sheet)
        df_r = pd.read_excel(reex, sheet)
        compared_sheets += 1

        # only compare columns that exist in the ORIGINAL file
        shared_cols = [c for c in df_o.columns if c in df_r.columns]
        missing_cols = [c for c in df_o.columns if c not in df_r.columns]
        if missing_cols:
            # A column missing from the re-export is only data loss if the
            # original actually held data in it.
            lost = 0
            for c in missing_cols:
                if df_o[c].dropna().astype(str).str.strip().replace("", pd.NA).dropna().size:
                    lost += 1
                    print(f"[{sheet}] DATA LOSS: column {c!r} has values in original but not in re-export")
            benign = len(missing_cols) - lost
            print(f"[{sheet}] {len(missing_cols)} column(s) in original missing from re-export "
                  f"({lost} with data, {benign} empty/benign)")
            total_mismatch += lost

        if len(df_o) != len(df_r):
            print(f"[{sheet}] ROW COUNT differs: orig={len(df_o)} reexport={len(df_r)}")
            total_mismatch += 1

        n = min(len(df_o), len(df_r))
        sheet_mismatch = 0
        for i in range(n):
            for c in shared_cols:
                vo = canonical(df_o.iloc[i][c])
                vr = canonical(df_r.iloc[i][c])
                compared_cells += 1
                if vo != vr:
                    sheet_mismatch += 1
                    if sheet_mismatch <= 5:
                        print(f"[{sheet}] row {i + 2} col {c!r}: orig={vo!r} reexport={vr!r}")
        if sheet_mismatch:
            print(f"[{sheet}] {sheet_mismatch} cell mismatches")
            total_mismatch += sheet_mismatch

    print("=" * 60)
    print(f"compared {compared_sheets} sheets, {compared_cells} cells")
    print(f"TOTAL MISMATCHES: {total_mismatch}")
    print("RESULT:", "PASS — import file == export file" if total_mismatch == 0
          else "FAIL — see mismatches above")


if __name__ == "__main__":
    main()
