"""Audit-preserving controls for the derived ``booking_allocation`` table."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from sqlalchemy.orm import load_only

from models import (
    Booking,
    BookingAllocation,
    BookingAllocationRepairArchive,
    BookingItem,
    DirectSale,
    DirectSaleItem,
    db,
)


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _snapshot(obj, fields):
    if obj is None:
        return None
    return json.dumps(
        {field: _json_value(getattr(obj, field, None)) for field in fields},
        sort_keys=True,
        separators=(",", ":"),
    )


_SALE_COLUMNS = (
    DirectSale.id, DirectSale.client_name, DirectSale.client_code, DirectSale.category,
    DirectSale.amount, DirectSale.paid_amount, DirectSale.discount, DirectSale.manual_bill_no,
    DirectSale.auto_bill_no, DirectSale.date_posted, DirectSale.is_void,
)
_SALE_ITEM_COLUMNS = (
    DirectSaleItem.id, DirectSaleItem.sale_id, DirectSaleItem.product_name,
    DirectSaleItem.qty, DirectSaleItem.price_at_time, DirectSaleItem.cost_rate_at_sale,
)
_BOOKING_COLUMNS = (
    Booking.id, Booking.client_name, Booking.amount, Booking.paid_amount, Booking.discount,
    Booking.manual_bill_no, Booking.auto_bill_no, Booking.date_posted, Booking.is_void,
)
_BOOKING_ITEM_COLUMNS = (
    BookingItem.id, BookingItem.booking_id, BookingItem.material_name,
    BookingItem.qty, BookingItem.price_at_time,
)


def _load_integrity_context(session):
    allocations = session.query(BookingAllocation).order_by(BookingAllocation.id).all()
    sale_ids = {row.sale_id for row in allocations}
    sale_item_ids = {row.sale_item_id for row in allocations}
    booking_item_ids = {row.booking_item_id for row in allocations}

    sales = {
        row.id: row
        for row in session.query(DirectSale)
        .options(load_only(*_SALE_COLUMNS))
        .filter(DirectSale.id.in_(sale_ids)).all()
    } if sale_ids else {}
    sale_items = {
        row.id: row
        for row in session.query(DirectSaleItem)
        .options(load_only(*_SALE_ITEM_COLUMNS))
        .filter(DirectSaleItem.id.in_(sale_item_ids)).all()
    } if sale_item_ids else {}
    booking_items = {
        row.id: row
        for row in session.query(BookingItem)
        .options(load_only(*_BOOKING_ITEM_COLUMNS))
        .filter(BookingItem.id.in_(booking_item_ids)).all()
    } if booking_item_ids else {}
    booking_ids = {row.booking_id for row in booking_items.values()}
    bookings = {
        row.id: row
        for row in session.query(Booking)
        .options(load_only(*_BOOKING_COLUMNS))
        .filter(Booking.id.in_(booking_ids)).all()
    } if booking_ids else {}
    return allocations, sales, sale_items, booking_items, bookings


def audit_booking_allocation_integrity(session=None):
    """Return a row-by-row diagnosis without modifying any business row."""
    session = session or db.session
    allocations, sales, sale_items, booking_items, bookings = _load_integrity_context(session)
    findings = []

    for allocation in allocations:
        sale = sales.get(allocation.sale_id)
        sale_item = sale_items.get(allocation.sale_item_id)
        booking_item = booking_items.get(allocation.booking_item_id)
        booking = bookings.get(booking_item.booking_id) if booking_item else None
        missing = []
        if sale is None:
            missing.append("sale_id")
        if sale_item is None:
            missing.append("sale_item_id")
        if booking_item is None:
            missing.append("booking_item_id")
        mismatched_sale_item = bool(sale_item and sale_item.sale_id != allocation.sale_id)
        missing_booking = bool(booking_item and booking is None)
        if not missing and not mismatched_sale_item and not missing_booking:
            continue

        active = not bool(allocation.is_void)
        blocked_reason = None
        if sale is None:
            blocked_reason = "The authoritative direct sale is missing; automatic removal could conceal a missing financial source."
        elif mismatched_sale_item:
            blocked_reason = "The sale line belongs to a different sale; identity must be resolved manually."
        elif missing_booking:
            blocked_reason = "The booking line exists but its authoritative booking is missing; identity must be resolved manually."
        elif sale_item is None and active:
            blocked_reason = "An active allocation has no sale line; automatic removal could conceal an active dispatch inconsistency."

        if "booking_item_id" in missing and active and sale_item is not None:
            classification = "active allocation to deleted booking line"
            business_meaning = (
                "The posted sale and sale line remain authoritative, but this derived reservation-consumption link "
                "points to a booking line that no longer exists. It can no longer be joined to a booking ledger."
            )
            criticality = "high business / no direct financial mutation"
        elif active:
            classification = "active allocation with missing authoritative parent"
            business_meaning = "An active derived booking-consumption link has lost a required source parent."
            criticality = "critical"
        elif "sale_item_id" in missing and "booking_item_id" in missing:
            classification = "void allocation with both child parents deleted"
            business_meaning = "A void historical allocation survived rewrites of both the sale line and booking line."
            criticality = "low financial / medium audit"
        elif "sale_item_id" in missing:
            classification = "void allocation to replaced sale line"
            business_meaning = "A sale edit replaced the old sale line after this derived allocation was voided."
            criticality = "low financial / medium audit"
        elif "booking_item_id" in missing:
            classification = "void allocation to deleted booking line"
            business_meaning = "A void derived allocation survived deletion of its booking line."
            criticality = "low financial / low business"
        else:
            classification = "allocation parent identity mismatch"
            business_meaning = "All direct FK values exist, but their parent ownership is inconsistent."
            criticality = "critical"

        repair_eligible = blocked_reason is None
        safe_repair = (
            "Archive the exact allocation identifiers, state, quantity, and every available parent snapshot; "
            "then remove only this derived dangling row in the same transaction. Do not change the retained "
            "sale, sale item, booking, inventory entry, invoice, pending bill, payment, or account record."
            if repair_eligible
            else "No automatic repair. Investigate and restore/identify the authoritative source before changing this row."
        )
        findings.append(
            {
                "table": "booking_allocation",
                "row_pk": allocation.id,
                "foreign_keys": {
                    "sale_id": {
                        "value": allocation.sale_id,
                        "references": "direct_sale.id",
                        "exists": sale is not None,
                    },
                    "sale_item_id": {
                        "value": allocation.sale_item_id,
                        "references": "direct_sale_item.id",
                        "exists": sale_item is not None,
                    },
                    "booking_item_id": {
                        "value": allocation.booking_item_id,
                        "references": "booking_item.id",
                        "exists": booking_item is not None,
                    },
                },
                "violating_fields": missing,
                "is_void": bool(allocation.is_void),
                "qty": float(allocation.qty or 0),
                "classification": classification,
                "business_meaning": business_meaning,
                "criticality": criticality,
                "safe_repair": safe_repair,
                "repair_eligible": repair_eligible,
                "blocked_reason": blocked_reason,
                "parent_context": {
                    "sale_bill": (
                        sale.manual_bill_no or sale.auto_bill_no or f"DirectSale-{sale.id}"
                    ) if sale else None,
                    "sale_client": sale.client_name if sale else None,
                    "sale_void": bool(sale.is_void) if sale else None,
                    "sale_item_material": sale_item.product_name if sale_item else None,
                    "sale_item_owner_sale_id": sale_item.sale_id if sale_item else None,
                    "booking_id": booking.id if booking else None,
                    "booking_bill": (
                        booking.manual_bill_no or booking.auto_bill_no or f"Booking-{booking.id}"
                    ) if booking else None,
                    "booking_item_material": booking_item.material_name if booking_item else None,
                },
            }
        )
    return findings


def _get_snapshot_parent(model, row_id, columns):
    if row_id is None:
        return None
    return (
        db.session.query(model)
        .options(load_only(*columns))
        .filter(model.id == row_id)
        .first()
    )


def _archive_row(allocation, *, violations, reason, run_id):
    sale = _get_snapshot_parent(DirectSale, allocation.sale_id, _SALE_COLUMNS)
    sale_item = _get_snapshot_parent(DirectSaleItem, allocation.sale_item_id, _SALE_ITEM_COLUMNS)
    booking_item = _get_snapshot_parent(BookingItem, allocation.booking_item_id, _BOOKING_ITEM_COLUMNS)
    booking = (
        _get_snapshot_parent(Booking, booking_item.booking_id, _BOOKING_COLUMNS)
        if booking_item else None
    )
    archive = BookingAllocationRepairArchive(
        original_allocation_id=allocation.id,
        sale_id=allocation.sale_id,
        sale_item_id=allocation.sale_item_id,
        booking_item_id=allocation.booking_item_id,
        qty=allocation.qty,
        was_void=bool(allocation.is_void),
        violations=violations,
        repair_reason=reason,
        repair_run_id=run_id,
        source_row_json=_snapshot(
            allocation,
            ("id", "sale_id", "sale_item_id", "booking_item_id", "qty", "is_void"),
        ),
        sale_snapshot_json=_snapshot(
            sale,
            (
                "id", "client_name", "client_code", "category", "amount", "paid_amount",
                "discount", "manual_bill_no", "auto_bill_no", "date_posted", "is_void",
            ),
        ),
        sale_item_snapshot_json=_snapshot(
            sale_item,
            ("id", "sale_id", "product_name", "qty", "price_at_time", "cost_rate_at_sale"),
        ),
        booking_item_snapshot_json=_snapshot(
            booking_item,
            ("id", "booking_id", "material_name", "qty", "price_at_time"),
        ),
        booking_snapshot_json=_snapshot(
            booking,
            (
                "id", "client_name", "amount", "paid_amount", "discount", "manual_bill_no",
                "auto_bill_no", "date_posted", "is_void",
            ),
        ),
    )
    db.session.add(archive)
    return archive


def archive_and_delete_booking_allocations(allocations, *, reason, run_id=None, violations="lifecycle_replacement"):
    """Archive and remove exact ORM rows; the caller owns commit/rollback."""
    run_id = run_id or f"lifecycle-{uuid.uuid4().hex}"
    rows = [row for row in allocations if row is not None]
    for allocation in rows:
        _archive_row(allocation, violations=violations, reason=reason, run_id=run_id)
    db.session.flush()
    for allocation in rows:
        db.session.delete(allocation)
    db.session.flush()
    return len(rows), run_id


def repair_dangling_booking_allocations(*, run_id=None):
    """Archive and remove only diagnosed, safely repairable dangling rows.

    The caller must commit. Any blocked finding aborts before the first write.
    """
    run_id = run_id or f"fk-repair-{uuid.uuid4().hex}"
    findings = audit_booking_allocation_integrity(db.session)
    blocked = [finding for finding in findings if not finding["repair_eligible"]]
    if blocked:
        ids = ", ".join(str(finding["row_pk"]) for finding in blocked[:20])
        raise ValueError(f"Blocked booking allocation findings require manual resolution: {ids}")

    repaired_ids = []
    for finding in findings:
        allocation = db.session.get(BookingAllocation, finding["row_pk"])
        if allocation is None:
            raise RuntimeError(f"Allocation {finding['row_pk']} changed during repair")
        # Recheck identity/state so a concurrent change cannot be archived under
        # stale diagnostics.
        expected = finding["foreign_keys"]
        if (
            allocation.sale_id != expected["sale_id"]["value"]
            or allocation.sale_item_id != expected["sale_item_id"]["value"]
            or allocation.booking_item_id != expected["booking_item_id"]["value"]
            or bool(allocation.is_void) != finding["is_void"]
        ):
            raise RuntimeError(f"Allocation {allocation.id} changed during repair")
        _archive_row(
            allocation,
            violations=",".join(finding["violating_fields"]),
            reason=finding["classification"],
            run_id=run_id,
        )
        db.session.flush()
        db.session.delete(allocation)
        repaired_ids.append(allocation.id)

    db.session.flush()
    remaining = audit_booking_allocation_integrity(db.session)
    if remaining:
        raise RuntimeError(f"Repair left {len(remaining)} booking allocation integrity findings")
    return {"run_id": run_id, "archived_and_removed": len(repaired_ids), "row_ids": repaired_ids}
