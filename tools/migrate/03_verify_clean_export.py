"""03_verify_clean_export.py — prove the cleaned workbook is leak-free.

Re-runs the full audit on the cleaned workbook produced by
02_build_clean_export.py and asserts the migration contract:

  * zero rows with ``is_void != 0`` anywhere
  * zero cancelled (``type='CANCEL'`` / ``transaction_category='Cancel'``) entries
  * zero dangling foreign keys
  * account balances still equal the non-void account_transaction ledger
  * material totals still equal the non-void entry IN/OUT net
  * no duplicate natural keys
  * per-table counts and money sums match the legacy "active" baseline
  * bill counters remain above every sequence number present in the data

Exit code 1 on any failure.  This is the green-light gate before importing.

Usage:
    python tools/migrate/03_verify_clean_export.py --clean instance/migration/ALLEXPORT-CLEAN-<ts>.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _migrate_common as C  # noqa: E402
from _migrate_common import money_sum  # noqa: E402

TOLERANCE = 0.01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", required=True, help="cleaned xlsx from step 02")
    ap.add_argument("--source", default=str(C.LEGACY_XLSX))
    args = ap.parse_args()

    clean_path = Path(args.clean)
    if not clean_path.exists():
        print(f"ERROR: clean file not found: {clean_path}")
        return 2

    problems: list[str] = []

    clean_xls = C.load_workbook(clean_path)
    frames, report = C.compute_clean_frames(clean_xls)

    print("=" * 100)
    print(f"  CLEAN EXPORT VERIFICATION  —  {clean_path.name}")
    print("=" * 100)

    # 1. Void leakage
    print("\n1. VOIDED-ROW LEAK CHECK (expect 0 everywhere)")
    leak = 0
    for name, df in frames.items():
        if "is_void" in df.columns:
            n = int((pd_num(df["is_void"]).fillna(0) != 0).sum())
            if n:
                leak += n
                problems.append(f"{name}: {n} voided row(s) leaked")
                print(f"   [FAIL] {name}: {n}")
    print(f"   voided rows found in clean file: {leak}")

    # 2. Cancelled entries
    print("\n2. CANCELLED ENTRY CHECK (expect 0)")
    e = frames.get("entry")
    n_cancel = 0
    if e is not None:
        if "type" in e:
            n_cancel += int(e["type"].fillna("").astype(str).str.strip().str.upper().eq("CANCEL").sum())
        if "transaction_category" in e:
            n_cancel += int(e["transaction_category"].fillna("").astype(str).str.strip().str.upper().eq("CANCEL").sum())
    if n_cancel:
        problems.append(f"entry: {n_cancel} cancelled row(s) leaked")
    print(f"   cancelled entry rows found: {n_cancel}")

    # 3. Dangling FKs
    print("\n3. DANGLING FOREIGN-KEY CHECK (expect 0 everywhere)")
    cond_map = {(c, col, p): cond for c, col, p, cond in C.CASCADE_RULES}
    for child, col, parent in C.FK_PAIRS:
        if child not in frames or col not in frames[child].columns or parent not in frames:
            continue
        cf = frames[child]
        missing = dangling_count(cf, col, frames[parent], cond_map.get((child, col, parent)))
        flag = "OK " if missing == 0 else "FAIL"
        if missing:
            problems.append(f"FK {child}.{col} -> {parent}: {missing} dangling in clean file")
        print(f"   [{flag}] {child:24}.{col:<22} -> {parent:18} dangling={missing}")

    # 4. Ledger identities
    print("\n4. LEDGER IDENTITY CHECK")
    bad_acc = account_ledger_mismatches(frames)
    print(f"   account balance vs transaction net: {len(bad_acc)} mismatch(es)")
    for b in bad_acc[:8]:
        problems.append(f"account {b['name']} balance mismatch in clean file")
    bad_mat = material_ledger_mismatches(frames)
    print(f"   material total vs entry net: {len(bad_mat)} mismatch(es)")
    for b in bad_mat[:8]:
        problems.append(f"material {b['name']} total mismatch in clean file")

    # 5. Counts and money sums vs legacy baseline
    print("\n5. COUNT & MONEY BASELINE vs legacy active data")
    source_xls = C.load_workbook(Path(args.source))
    src_frames, src_report = C.compute_clean_frames(source_xls)
    for name in sorted(set(frames) & set(src_frames)):
        a, b = len(frames[name]), len(src_frames[name])
        if a != b:
            problems.append(f"count mismatch {name}: clean={a} baseline={b}")
            print(f"   [FAIL] {name}: clean={a} baseline={b}")
    for t, cols in [("direct_sale", ["amount", "paid_amount", "discount"]),
                    ("payment", ["amount", "discount"]),
                    ("pending_bill", ["amount"]),
                    ("invoice", ["total_amount", "balance"]),
                    ("account_transaction", ["amount"]),
                    ("booking", ["amount", "paid_amount"]),
                    ("waive_off", ["amount"]),
                    ("material_return", ["amount"]),
                    ("supplier_payment", ["amount"]),
                    ("delivery_rent", ["amount"])]:
        for col in cols:
            v_clean = money_sum(frames.get(t, pd_empty()), col)
            v_base = money_sum(src_frames.get(t, pd_empty()), col)
            flag = "OK " if abs(v_clean - v_base) <= TOLERANCE else "FAIL"
            if flag != "OK ":
                problems.append(f"money mismatch {t}.{col}: clean={v_clean:,.2f} baseline={v_base:,.2f}")
            print(f"   [{flag}] {t:20}.{col:<14} clean={v_clean:>16,.2f} baseline={v_base:>16,.2f}")

    # 6. Duplicates
    print("\n6. DUPLICATE NATURAL KEYS")
    for t, col in [("client", "code"), ("material", "name"), ("invoice", "invoice_no"),
                   ("delivery_person", "name"), ("supplier", "name")]:
        if t not in frames or col not in frames[t]:
            continue
        df = frames[t]
        norm = df[col].astype(str).str.strip().str.upper()
        dup = norm[norm.ne("")].value_counts()
        dup = dup[dup > 1]
        flag = "OK " if dup.empty else "FAIL"
        if not dup.empty:
            problems.append(f"duplicate {t}.{col} in clean file: {dict(dup.head(5))}")
        print(f"   [{flag}] {t}.{col}: {len(dup)} duplicate(s)")

    # 7. Bill counters
    print("\n7. BILL COUNTER SAFETY")
    if "bill_counter" in frames:
        counters = dict(zip(frames["bill_counter"]["namespace"], frames["bill_counter"]["count"]))
        max_seqs: dict[str, int] = {}
        for t, cols in [("pending_bill", ["bill_no"]), ("booking", ["auto_bill_no"]),
                        ("direct_sale", ["auto_bill_no"]), ("payment", ["auto_bill_no"]),
                        ("material_return", ["auto_bill_no"]), ("grn", ["auto_bill_no"])]:
            for col in cols:
                for ns, mx in C.sb_sequences(frames.get(t, pd_empty()), col).items():
                    max_seqs[ns] = max(max_seqs.get(ns, 0), mx)
        for ns in sorted(set(counters) | set(max_seqs)):
            ctr = counters.get(ns)
            mx = max_seqs.get(ns, 0)
            ok = ctr is not None and mx < ctr
            flag = "OK " if ok else "FAIL"
            if not ok:
                problems.append(f"bill_counter {ns}: counter={ctr} <= max_seq={mx}")
            print(f"   [{flag}] ns={ns:4} counter={ctr} max_seq_in_data={mx}")

    print("\n" + "=" * 100)
    if problems:
        print(f"RESULT: FAIL — {len(problems)} issue(s)")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("RESULT: PASS — clean export is ready for import (Import & Export page).")
    return 0


# --------------------------------------------------------------------------
def pd_empty():
    import pandas as pd
    return pd.DataFrame()


def pd_num(s):
    import pandas as pd
    return pd.to_numeric(s, errors="coerce")


def _id_set(df, col="id"):
    s = pd_num(df[col])
    return set(s.dropna().astype(int).unique())


def dangling_count(child_df, col, parent_df, cond=None):
    ck = pd_num(child_df[col])
    if ck.dropna().empty:
        return 0
    parents = _id_set(parent_df)
    mask = ~ck.dropna().astype(int).isin(parents)
    if cond is not None:
        try:
            mask &= child_df.eval(cond).loc[ck.dropna().index]
        except Exception:
            # Conservative: if the condition cannot be evaluated, treat every
            # non-null FK as suspicious so the failure is visible.
            return int(len(ck.dropna()))
    return int(mask.sum())


def account_ledger_mismatches(frames):
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
        stored = float(r.get("balance") or 0)
        ledger = net.get(int(r["id"]), 0.0)
        if abs(stored - ledger) > 0.01:
            bad.append({"name": r.get("name"), "stored": stored, "ledger": ledger})
    return bad


def material_ledger_mismatches(frames):
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
            bad.append({"name": r.get("name"), "stored": stored, "ledger": ledger})
    return bad


if __name__ == "__main__":
    sys.exit(main())
