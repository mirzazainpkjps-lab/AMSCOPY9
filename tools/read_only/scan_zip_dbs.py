#!/usr/bin/env python
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DbCounts:
    path: Path
    size: int
    client: int | None = None
    booking: int | None = None
    direct_sale: int | None = None
    pending_bill: int | None = None
    entry: int | None = None
    payment: int | None = None
    billed_sales: int | None = None
    unbilled_sales: int | None = None
    note: str = ""


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,))
    return cur.fetchone() is not None


def _count_rows(cur: sqlite3.Cursor, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0] or 0)


def _count_not_void_if_possible(cur: sqlite3.Cursor, table: str) -> int:
    # Many tables in this project use `is_void` or similar.
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if "is_void" in cols:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE COALESCE(is_void, 0)=0")
        return int(cur.fetchone()[0] or 0)
    return _count_rows(cur, table)


def scan_db(path: Path) -> DbCounts:
    counts = DbCounts(path=path, size=path.stat().st_size)
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        for t in ["client", "booking", "direct_sale", "pending_bill", "entry", "payment"]:
            if not _table_exists(cur, t):
                continue
            if t in ("booking", "direct_sale", "pending_bill", "payment"):
                val = _count_not_void_if_possible(cur, t)
            else:
                val = _count_rows(cur, t)
            setattr(counts, t, val)

        # Compute billed/unbilled like main.py direct_sales_page().
        if _table_exists(cur, "direct_sale"):
            try:
                # billed: not void, not Open Khata, (manual_bill_no not empty OR invoice_id not null)
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM direct_sale
                    WHERE COALESCE(is_void, 0)=0
                      AND COALESCE(category, '') <> 'Open Khata'
                      AND (
                        LENGTH(TRIM(COALESCE(manual_bill_no, ''))) > 0
                        OR invoice_id IS NOT NULL
                      )
                    """
                )
                counts.billed_sales = int(cur.fetchone()[0] or 0)
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM direct_sale
                    WHERE COALESCE(is_void, 0)=0
                      AND COALESCE(category, '') <> 'Open Khata'
                      AND LENGTH(TRIM(COALESCE(manual_bill_no, ''))) = 0
                      AND invoice_id IS NULL
                    """
                )
                counts.unbilled_sales = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
        conn.close()
    except Exception as exc:
        counts.note = f"error: {exc}"
    return counts


def main() -> None:
    base = Path(__file__).resolve().parent
    extract_root = None
    local_dir = base / ".local"
    if local_dir.exists():
        candidates = [p for p in local_dir.iterdir() if p.is_dir() and p.name.startswith("zip_extract_")]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        extract_root = candidates[0] if candidates else None

    if not extract_root:
        raise SystemExit("No extracted zip dir found under .local/zip_extract_*")

    dbs = list(extract_root.rglob("*.db"))
    if not dbs:
        raise SystemExit(f"No .db files found under {extract_root}")

    results = [scan_db(p) for p in dbs]
    results.sort(key=lambda r: (r.client or -1, r.booking or -1, r.direct_sale or -1, r.size), reverse=True)

    print(f"Scanned {len(results)} DB files under: {extract_root}")
    print("Format: clients | billed | unbilled | direct_sales | pending_bills | entries | payments | size | path")
    for r in results[:60]:
        print(
            f"{str(r.client):>7} | {str(r.billed_sales):>6} | {str(r.unbilled_sales):>8} | {str(r.direct_sale):>11} | "
            f"{str(r.pending_bill):>12} | {str(r.entry):>7} | {str(r.payment):>8} | "
            f"{r.size:>8} | {r.path}"
        )

    targets = [
        r for r in results
        if (r.client in (205, 206))
        and (r.billed_sales in (656, 657, 655))
        and (r.unbilled_sales in (33, 34, 32))
    ]
    print("\nCandidates matching (clients=206, billed=656, unbilled=33):", len(targets))
    for r in targets[:20]:
        print(
            f"clients={r.client} billed={r.billed_sales} unbilled={r.unbilled_sales} direct_sale={r.direct_sale} "
            f"pending_bill={r.pending_bill} size={r.size} path={r.path}"
        )


if __name__ == "__main__":
    main()
