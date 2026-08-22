#!/usr/bin/env python3
"""One-shot repair: assign auto bill numbers to supplier payments.

Background
----------
Every supplier payment created through ``save_supplier_payment`` today gets a
real tracking number (``SB-SP-####``).  But payments migrated from the old
system (all 78 rows in the 2026-08-22 snapshot) have **no** ``auto_bill_no``
and **no** ``manual_bill_no``.  Consequences:

* the supplier ledger displayed an invented label ``PAY-<id>`` that resolves
  to nothing — clicking it in the ledger opened an unrelated client bill;
* the Accounts → Supplier Payments "Bill #" column was empty.

This script back-fills a unique ``SB-SP-####`` onto every payment missing one,
in chronological order.  It changes ONLY the reference number; no amounts,
dates, accounts or balances are touched.

Usage (on the server, with a fresh backup):
    python3 tools/inventory/backfill_supplier_payment_bill_nos.py          # dry run
    python3 tools/inventory/backfill_supplier_payment_bill_nos.py --apply
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
    from models import db, SupplierPayment
    from app.services.billing import AUTO_BILL_NAMESPACES, get_next_bill_no

    app = create_app({"TESTING": True})
    with app.app_context():
        missing = SupplierPayment.query.filter(
            db.or_(
                SupplierPayment.auto_bill_no.is_(None),
                SupplierPayment.auto_bill_no == "",
            )
        ).order_by(SupplierPayment.date_posted.asc(), SupplierPayment.id.asc()).all()

        print(f"Supplier payments without a bill number: {len(missing)}")
        if not missing:
            print("Nothing to back-fill.")
            return 0

        for p in missing[:10]:
            print(f"  would assign #{p.id} {p.date_posted} Rs.{p.amount} -> a new SB-SP number")
        if len(missing) > 10:
            print(f"  … and {len(missing) - 10} more")

        if apply:
            for p in missing:
                p.auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES["SUPPLIER_PAYMENT"])
            db.session.commit()
            print(f"APPLIED: {len(missing)} payments now carry SB-SP-#### tracking numbers.")
        else:
            db.session.rollback()
            print("DRY RUN: no changes written. Re-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
