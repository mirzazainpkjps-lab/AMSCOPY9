#!/usr/bin/env python3
"""One-shot repair: sync stale GRN.supplier / IN-entry client strings.

Background
----------
Renaming a supplier used to leave ``GRN.supplier`` (and the IN ``Entry.client``
rows created by that GRN) carrying the OLD name.  Balances stayed correct when
``supplier_id`` was set (the ledger joins by id first), but:

* the GRN list / search showed the old name,
* legacy name-matched ledger joins could cross-link two suppliers,
* exports showed the old name.

As of 2026-08-22 ``edit_supplier`` keeps these strings in sync.  This script
repairs rows that went stale BEFORE that fix (verified example in the live
snapshot: GRNs #1-3 still say "Faizan Facto" while the supplier is
"Faizan Fecto").

Usage (run on the server, app stopped or quiet, with a fresh backup):
    python3 tools/inventory/fix_stale_grn_supplier_names.py            # dry run
    python3 tools/inventory/fix_stale_grn_supplier_names.py --apply
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

os.environ.setdefault("ALLOW_EMPTY_DB", "1")


def main() -> int:
    apply = "--apply" in sys.argv
    from app import create_app
    from models import db, GRN, Entry, Supplier

    app = create_app({"TESTING": True})
    with app.app_context():
        suppliers = {s.id: (s.name or "").strip() for s in Supplier.query.all()}
        stale_grns = []
        for g in GRN.query.all():
            canonical = suppliers.get(g.supplier_id)
            if canonical and (g.supplier or "").strip() != canonical:
                stale_grns.append((g, canonical))

        print(f"Suppliers: {len(suppliers)} | GRNs with stale supplier string: {len(stale_grns)}")
        entry_fixes = 0
        for g, canonical in stale_grns:
            print(f"  GRN #{g.id} {g.auto_bill_no or ''}: {g.supplier!r} -> {canonical!r}")
            if g.auto_bill_no:
                n = Entry.query.filter(
                    Entry.auto_bill_no == g.auto_bill_no, Entry.type == "IN"
                ).update({"client": canonical}, synchronize_session=False)
                entry_fixes += n
            g.supplier = canonical

        if not stale_grns:
            print("Nothing to fix.")
            return 0

        if apply:
            db.session.commit()
            print(f"APPLIED: {len(stale_grns)} GRNs updated, {entry_fixes} IN entries re-labelled.")
        else:
            db.session.rollback()
            print(f"DRY RUN: would update {len(stale_grns)} GRNs and {entry_fixes} IN entries.")
            print("Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
