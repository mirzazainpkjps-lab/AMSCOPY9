"""Scan for active source bills with missing active derived effects."""

import json

from main import (
    app,
    db,
    DirectSale,
    Entry,
    PendingBill,
    _direct_sale_default_bill_ref,
    _not_void,
    normalize_sale_category,
)


def main():
    findings = []
    with app.app_context():
        sales = DirectSale.query.filter(_not_void(DirectSale)).all()
        for sale in sales:
            bill_ref = _direct_sale_default_bill_ref(sale)
            category = normalize_sale_category(sale.category)
            booking_qty = sum(
                float(it.qty or 0)
                for it in (sale.items or [])
                if float(it.qty or 0) > 0 and float(it.price_at_time or 0) <= 0
            )
            active_entries = Entry.query.filter(
                Entry.source_module == "sales",
                Entry.source_id == sale.id,
                _not_void(Entry),
                Entry.type == "OUT",
            ).count()
            pending_amount = max(
                0.0,
                float(sale.amount or 0)
                - float(getattr(sale, "discount", 0) or 0)
                - float(sale.paid_amount or 0),
            )
            active_pending = PendingBill.query.filter(
                PendingBill.source_module == "sales",
                PendingBill.source_id == sale.id,
                _not_void(PendingBill),
            ).count()
            issue = []
            if category in ["Booking Delivery", "Mixed Transaction"] and booking_qty > 0 and active_entries <= 0:
                issue.append("missing_material_entries")
            if pending_amount > 0 and active_pending <= 0:
                issue.append("missing_pending_bill")
            if issue:
                findings.append(
                    {
                        "sale_id": sale.id,
                        "bill_no": bill_ref,
                        "client_name": sale.client_name,
                        "category": category,
                        "amount": sale.amount,
                        "booking_qty": booking_qty,
                        "active_entries": active_entries,
                        "pending_amount": pending_amount,
                        "active_pending": active_pending,
                        "issue": issue,
                    }
                )
        print(json.dumps({"count": len(findings), "findings": findings}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
