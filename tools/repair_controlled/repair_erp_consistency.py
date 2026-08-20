"""Repair derived ERP ledger, pending-bill, and stock consistency.

Usage:
    python tools/repair_controlled/repair_erp_consistency.py --confirm
    python tools/repair_controlled/repair_erp_consistency.py --confirm --client-id 71
    python tools/repair_controlled/repair_erp_consistency.py --confirm --bill-no MB1234
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.repair_controlled.repair_guard import preflight
preflight(
    script_name=__file__,
    description="Rebuild derived ERP ledger, pending-bill, and stock from source transactions",
)

from main import app, db, rebuild_all_erp_consistency, repair_transaction_by_bill_no


def main():
    parser = argparse.ArgumentParser(description="Rebuild ERP derived ledgers from source transactions.")
    parser.add_argument("--client-id", type=int, default=None, help="Optional client id to repair only one client.")
    parser.add_argument("--bill-no", default=None, help="Repair only the source transaction owning this bill number.")
    args = parser.parse_args()

    with app.app_context():
        if args.bill_no:
            stats = repair_transaction_by_bill_no(args.bill_no)
        else:
            stats = rebuild_all_erp_consistency(client_id=args.client_id)
        db.session.commit()
        print(json.dumps(stats, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
