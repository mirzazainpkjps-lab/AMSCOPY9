"""01_audit_legacy.py — read-only audit of the legacy AMS ALLEXPORT workbook.

Runs BEFORE anything is transformed.  Prints:

  * purge profile (voided / cancelled rows per table)
  * cascade-orphan rows (active children of voided parents)
  * dangling foreign-key audit
  * ledger pre-checks (account balances, material totals, money sums)
  * duplicate natural keys
  * bill-counter / sequence safety

Nothing is written.  Exit code is 1 when a gate check fails so this can be
used in a migration pipeline.

Usage:
    python tools/migrate/01_audit_legacy.py
    python tools/migrate/01_audit_legacy.py --source path/to/ALLEXPORT.xlsx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _migrate_common as C  # noqa: E402

TOLERANCE = 0.01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(C.LEGACY_XLSX))
    args = ap.parse_args()
    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: source file not found: {source}")
        return 2

    xls = C.load_workbook(source)
    frames, report = C.compute_clean_frames(xls)
    problems: list[str] = []

    print("=" * 100)
    print(f"  AMS LEGACY DATA AUDIT  —  {source.name}")
    print("=" * 100)

    # ---- 1. Purge profile -------------------------------------------------
    print("\n1. PURGE PROFILE  (rows to exclude from migration)")
    print(f"   {'sheet':26} {'total':>7} {'void':>7} {'cancel':>7} "
          f"{'cascade':>8} {'missing':>8} {'kept':>7}")
    for name, r in report.items():
        if r["total"] == 0 and r["removed_void"] + r["removed_cascade"] == 0:
            continue
        print(f"   {name:26} {r['total']:>7} {r['removed_void']:>7} {r['removed_cancel']:>7} "
              f"{r['removed_cascade']:>8} {r['removed_missing_parent']:>8} {r['kept']:>7}")

    # ---- 2. Cascade detail ------------------------------------------------
    print("\n2. CASCADE ORPHANS  (active children of voided parents)")
    for (child, col, parent), (n_cas, n_mis) in cascade_counts_from_original(xls).items():
        extra = f"  (+{n_mis} dangling parent refs)" if n_mis else ""
        print(f"   {child:24}.{col:<20} -> voided {parent:16} rows purged: {n_cas}{extra}")
    total_purged = sum(r["total"] - r["kept"] for r in report.values())
    print(f"   total rows excluded across all sheets: {total_purged}")

    # ---- 3. Dangling FK audit on the *kept* data --------------------------
    print("\n3. DANGLING FOREIGN-KEY AUDIT  (kept rows only)")
    fk_ok = True
    cond_map = {(c, col, p): cond for c, col, p, cond in C.CASCADE_RULES}
    for child, col, parent in C.FK_PAIRS:
        if child not in frames or col not in frames[child].columns:
            continue
        cf = frames[child]
        pf = frames[parent]
        ck = pd_col(cf, col)
        if ck is None:
            continue
        missing = dangling_count(cf, col, pf, cond_map.get((child, col, parent)))
        flag = "OK " if missing == 0 else "FAIL"
        if missing:
            fk_ok = False
            problems.append(f"FK {child}.{col} -> {parent}: {missing} dangling")
        print(f"   [{flag}] {child:24}.{col:<22} -> {parent:18} dangling={missing}")

    # ---- 4. Ledger pre-checks --------------------------------------------
    print("\n4. LEDGER PRE-CHECKS (on kept/clean data)")
    bad_acc, acc_checked = check_account_ledger(frames)
    print(f"   account balance vs account_transaction net: "
          f"{acc_checked} accounts, {len(bad_acc)} mismatches")
    for b in bad_acc[:8]:
        print(f"     MISMATCH id={b['id']} {b['name']}: stored={b['stored']:,.2f} "
              f"ledger={b['ledger']:,.2f}")
        problems.append(f"account {b['name']} balance mismatch")
    bad_mat, mat_checked = check_material_ledger(frames)
    print(f"   material total vs entry IN/OUT net: {mat_checked} materials, "
          f"{len(bad_mat)} mismatches")
    for b in bad_mat[:8]:
        print(f"     MISMATCH {b['name']}: stored={b['stored']:,.2f} net={b['net']:,.2f}")
        problems.append(f"material {b['name']} total mismatch")

    # ---- 5. Money sums (informational) -----------------------------------
    print("\n5. MONEY SUMS (kept/clean data — expected post-migration values)")
    for t, cols in [
        ("direct_sale", ["amount", "paid_amount", "discount"]),
        ("payment", ["amount", "discount"]),
        ("pending_bill", ["amount"]),
        ("invoice", ["total_amount", "balance"]),
        ("account_transaction", ["amount"]),
        ("booking", ["amount", "paid_amount"]),
        ("waive_off", ["amount"]),
        ("material_return", ["amount"]),
        ("supplier_payment", ["amount"]),
        ("delivery_rent", ["amount"]),
        ("grn", ["paid_amount"]),
    ]:
        if t not in frames:
            continue
        for col in cols:
            v = C.money_sum(frames[t], col)
            print(f"   {t:20}.{col:<14} = {v:>18,.2f}")

    # ---- 6. Duplicate natural keys ---------------------------------------
    print("\n6. DUPLICATE NATURAL KEYS")
    for t, col in [("client", "code"), ("material", "name"), ("invoice", "invoice_no"),
                   ("delivery_person", "name"), ("supplier", "name")]:
        if t not in frames:
            continue
        df = frames[t]
        key = pd_col(df, col)
        norm = key.astype(str).str.strip().str.upper()
        dup = norm[norm.ne("")].value_counts()
        dup = dup[dup > 1]
        flag = "OK " if dup.empty else "FAIL"
        if not dup.empty:
            problems.append(f"duplicate {t}.{col}: {dict(dup.head(5))}")
        print(f"   [{flag}] {t}.{col}: {len(dup)} duplicate(s)")

    # ---- 7. Bill counter / sequence safety --------------------------------
    print("\n7. BILL COUNTER / SEQUENCE SAFETY")
    if "bill_counter" in frames:
        counters = dict(zip(frames["bill_counter"]["namespace"], frames["bill_counter"]["count"]))
        # largest SB-<ns>-<seq> in kept data per namespace
        max_seqs: dict[str, int] = {}
        for t, cols in [("pending_bill", ["bill_no"]), ("booking", ["auto_bill_no"]),
                        ("direct_sale", ["auto_bill_no"]), ("payment", ["auto_bill_no"]),
                        ("material_return", ["auto_bill_no"]), ("grn", ["auto_bill_no"])]:
            for col in cols:
                for ns, mx in C.sb_sequences(frames.get(t, pd.DataFrame()), col).items():
                    max_seqs[ns] = max(max_seqs.get(ns, 0), mx)
        seq_ok = True
        for ns in sorted(set(counters) | set(max_seqs)):
            ctr = counters.get(ns)
            mx = max_seqs.get(ns, 0)
            if ctr is None:
                ok = False
            else:
                ok = mx < ctr
            seq_ok &= ok
            flag = "OK " if ok else "FAIL"
            print(f"   [{flag}] ns={ns:4} counter={ctr} max_seq_in_data={mx}")
            if not ok:
                problems.append(f"bill_counter {ns} collides with data (counter={ctr} <= max={mx})")

    # ---- 8. Master records that are inactive (informational) --------------
    print("\n8. INACTIVE MASTER RECORDS (kept — required for FK integrity)")
    for t, col in [("client", "is_active"), ("material", "is_active"),
                   ("account", "is_active"), ("delivery_person", "is_active"),
                   ("supplier", "is_active")]:
        if t not in frames or col not in frames[t]:
            continue
        n = int((frames[t][col].fillna(1).astype(int) == 0).sum())
        print(f"   {t:18} {col}: {n} inactive row(s) preserved")

    print("\n" + "=" * 100)
    if problems:
        print(f"RESULT: FAIL — {len(problems)} gate issue(s)")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("RESULT: PASS — legacy data is internally consistent; ready to build clean export.")
    return 0


# --------------------------------------------------------------------------
# Small helpers (kept local so audit logic stays readable)
# --------------------------------------------------------------------------
def pd_col(df, col):
    return df[col] if col in df.columns else None


def _id_set(df, col="id"):
    if col not in df.columns:
        return set()
    s = pd_to_int(df[col])
    return set(s.dropna().astype(int).unique())


def pd_to_int(s):
    import pandas as pd
    return pd.to_numeric(s, errors="coerce")


def dangling_count(child_df, col, parent_df, cond=None):
    ck = pd_to_int(child_df[col])
    if ck.dropna().empty:
        return 0
    parents = _id_set(parent_df)
    mask = ~ck.dropna().astype(int).isin(parents)
    if cond is not None:
        try:
            cond_series = child_df.eval(cond)
            mask &= cond_series.loc[ck.dropna().index]
        except Exception:
            mask = pd.Series(False, index=mask.index)
    return int(mask.sum())


def cascade_counts_from_original(xls):
    """Per-rule (cascade, missing-parent) counts of child rows to purge.

    ``cascade`` = child row references a parent that was purged (voided /
    cancelled).  ``missing`` = child FK references a parent id that never
    existed in the legacy data at all (only used for booking_allocation).
    """
    orig = {s: C.read_sheet(xls, s) for s in xls.sheet_names if s != C.META_SHEET}
    purged = {}
    for name, df in orig.items():
        ids = pd_to_int(df["id"])
        drop_mask = df["_void"] == 1
        if name == "entry":
            drop_mask |= C._apply_cancel_entry_rule(df)
        purged[name] = set(ids[drop_mask & ids.notna()].astype(int))

    out = {}
    for child, col, parent, cond in C.CASCADE_RULES:
        if child not in orig or col not in orig[child].columns or parent not in orig:
            continue
        df = orig[child]
        parent_purged = set(purged.get(parent, set()))
        ck = pd_to_int(df[col])
        mask = ck.isin(parent_purged) & ck.notna()
        if cond:
            try:
                mask &= df.eval(cond)
            except Exception:
                mask &= pd.Series(False, index=df.index)
        n_cas = int(mask.sum())
        n_mis = 0
        if child == "booking_allocation" and col == "booking_item_id":
            bi_kept = set(pd_to_int(orig["booking_item"]["id"]).dropna().astype(int)) - purged.get("booking_item", set())
            bi_col = pd_to_int(df["booking_item_id"])
            n_mis = int((bi_col.notna() & ~bi_col.astype(int).isin(bi_kept)).sum())
        out[(child, col, parent)] = (n_cas, n_mis)
    return out


def check_account_ledger(frames):
    import pandas as pd
    accounts = frames.get("account", pd.DataFrame())
    txs = frames.get("account_transaction", pd.DataFrame())
    net = {}
    for _, r in txs.iterrows():
        amt = float(r.get("amount") or 0)
        to = r.get("to_account_id")
        frm = r.get("from_account_id")
        try:
            if pd.notna(to):
                net[int(to)] = net.get(int(to), 0.0) + amt
            if pd.notna(frm):
                net[int(frm)] = net.get(int(frm), 0.0) - amt
        except (ValueError, TypeError):
            continue
    bad = []
    for _, r in accounts.iterrows():
        aid = int(r["id"])
        stored = float(r.get("balance") or 0)
        ledger = net.get(aid, 0.0)
        if abs(stored - ledger) > 0.01:
            bad.append({"id": aid, "name": r.get("name"), "stored": stored, "ledger": ledger})
    return bad, len(accounts)


def check_material_ledger(frames):
    import pandas as pd
    mats = frames.get("material", pd.DataFrame())
    entries = frames.get("entry", pd.DataFrame())
    net = {}
    for _, r in entries.iterrows():
        ty = str(r.get("type") or "").upper()
        q = float(r.get("qty") or 0)
        if ty == "IN":
            net[r.get("material")] = net.get(r.get("material"), 0.0) + q
        elif ty == "OUT":
            net[r.get("material")] = net.get(r.get("material"), 0.0) - q
    bad = []
    for _, r in mats.iterrows():
        stored = float(r.get("total") or 0)
        ledger = net.get(r.get("name"), 0.0)
        if abs(stored - ledger) > 0.01:
            bad.append({"name": r.get("name"), "stored": stored, "net": ledger})
    return bad, len(mats)


if __name__ == "__main__":
    sys.exit(main())
