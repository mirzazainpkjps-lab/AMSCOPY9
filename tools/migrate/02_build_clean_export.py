"""02_build_clean_export.py — build the purge-cleaned, import-ready workbook.

Applies the migration purge contract to the legacy ALLEXPORT workbook and
writes a new xlsx that the app's own full-raw importer can load:

  * ``is_void == 1`` rows are dropped everywhere.
  * ``entry`` rows with ``type='CANCEL'`` or ``transaction_category='Cancel'``
    are dropped (cancelled entries), even when ``is_void == 0``.
  * Child rows referencing purged parents are dropped (cascade), and
    ``booking_allocation`` rows with dangling ``booking_item_id`` are dropped.
  * Every sheet name and column set is preserved, so the file remains a
    literal full export the Import & Export page accepts as-is.

Outputs:
  * instance/migration/ALLEXPORT-CLEAN-<timestamp>.xlsx
  * instance/migration/purge_report.json   (machine-readable removal log)

Usage:
    python tools/migrate/02_build_clean_export.py
    python tools/migrate/02_build_clean_export.py --source path/in.xlsx \
        --out path/out.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _migrate_common as C  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(C.LEGACY_XLSX))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: source file not found: {source}")
        return 2

    out_path = Path(args.out) if args.out else (
        C.MIGRATION_DIR / f"ALLEXPORT-CLEAN-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    xls = C.load_workbook(source)
    kept_frames, report = C.compute_clean_frames(xls)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sheet in xls.sheet_names:
            if sheet == C.META_SHEET:
                meta = pd.read_excel(xls, C.META_SHEET)
                meta.loc[meta["key"] == "exported_at", "value"] = (
                    datetime.now(timezone.utc).isoformat()
                )
                meta.to_excel(writer, sheet_name=C.META_SHEET, index=False)
            elif sheet in kept_frames:
                kept_frames[sheet].to_excel(writer, sheet_name=sheet, index=False)
            else:
                # Sheet present in source but not modelled (should not happen;
                # __AMS_META__ is the only legacy-only sheet).
                pd.read_excel(xls, sheet).to_excel(writer, sheet_name=sheet, index=False)

    total_in = sum(r["total"] for r in report.values())
    total_out = sum(r["kept"] for r in report.values())

    # Machine-readable removal log.
    purge_log = {
        "source": str(source),
        "output": str(out_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tables": report,
        "totals": {"rows_in_source": total_in, "rows_kept": total_out,
                   "rows_removed": total_in - total_out},
    }
    log_path = out_path.with_name("purge_report.json")
    log_path.write_text(json.dumps(purge_log, indent=2), encoding="utf-8")

    print("=" * 100)
    print("  CLEAN EXPORT BUILT")
    print("=" * 100)
    print(f"  source : {source}")
    print(f"  output : {out_path}")
    print(f"  log    : {log_path}")
    print(f"  rows in source : {total_in}")
    print(f"  rows kept      : {total_out}")
    print(f"  rows removed   : {total_in - total_out}")
    print("\n  per-sheet removal summary:")
    print(f"   {'sheet':26} {'total':>7} {'void':>7} {'cancel':>7} "
          f"{'cascade':>8} {'missing':>8} {'kept':>7}")
    for name, r in report.items():
        if r["total"] == 0:
            continue
        print(f"   {name:26} {r['total']:>7} {r['removed_void']:>7} {r['removed_cancel']:>7} "
              f"{r['removed_cascade']:>8} {r['removed_missing_parent']:>8} {r['kept']:>7}")
    print("\n  NEXT: python tools/migrate/03_verify_clean_export.py "
          f"--clean {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
