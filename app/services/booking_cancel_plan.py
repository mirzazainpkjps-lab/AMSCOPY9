"""Shared booking-cancellation plan (ledger preview + cancel POST handler).

Remaining booked qty must follow the same booking-pool rule already used by
direct-sale validation and the client material ledger:

    remaining = booked - delivered + returned_booked

``returned_booked`` counts IN entries recorded as a *booked* material return
(``nimbus_no='Material Return'`` tagged ``transaction_category='Booked Return'``).
Such a return credits qty back into the client's booking pool, so that qty is
cancellable again. Without this term, a fully dispatched booking that later had
material returned back shows "No remaining booking items to cancel" even though
the material ledger reports a positive remaining qty.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, not_, or_

from models import Booking, BookingItem, Entry


def _fmt_date_short(dt_val):
    if not dt_val:
        return ''
    if isinstance(dt_val, str):
        return dt_val
    try:
        return dt_val.strftime('%Y-%m-%d')
    except Exception:
        return str(dt_val)


def build_client_booking_cancel_plan(client):
    """Compute remaining (cancellable) booking lines for a client.

    Returns ``(rows, cancel_total, cancel_total_qty)`` where each row is::

        {
            'item': BookingItem,          # live ORM row (POST handler mutates it)
            'item_id': int,
            'material': str,
            'bill_no': str,
            'booking_date': 'YYYY-MM-DD',
            'remaining_qty': float,       # qty left on this lot (LIFO listing)
            'rate': float,                # price_at_time
            'amount': float,              # remaining_qty * rate
        }

    Deliveries consume the oldest booking first (FIFO); leftover qty therefore
    sits on newer lots and is listed newest-first (LIFO) per material.
    """
    client_name_norm = (client.name or '').strip().lower()

    # Dispatches: OUT rows that draw on the booking pool (booking deliveries,
    # including Direct Sales flagged 'Booking Delivery'). Pure cash/credit
    # direct sales must not consume booked qty.
    delivered_entries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'OUT',
        Entry.is_void == False,  # noqa: E712
        not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))
    ).all()

    # Booked-material returns: IN rows that credit qty back into the booking
    # pool. Same tagging as _client_booked_material_returnable_qty_map().
    booked_return_entries = Entry.query.filter(
        (Entry.client_code == client.code) | (func.lower(func.trim(Entry.client)) == client_name_norm),
        Entry.type == 'IN',
        Entry.is_void == False,  # noqa: E712
        Entry.nimbus_no == 'Material Return',
        or_(
            Entry.transaction_category == 'Booked Return',
            Entry.client_category == 'Booked Return',
        )
    ).all()

    # Effective consumption per material = dispatched - booked returns.
    consumed_totals = {}
    for e in delivered_entries:
        key = e.booked_material or e.material
        consumed_totals[key] = consumed_totals.get(key, 0) + (e.qty or 0)
    for e in booked_return_entries:
        key = e.booked_material or e.material
        consumed_totals[key] = consumed_totals.get(key, 0) - (e.qty or 0)

    booking_items = BookingItem.query.join(Booking).filter(
        func.lower(func.trim(Booking.client_name)) == client_name_norm,
        Booking.is_void == False  # noqa: E712
    ).all()

    items_by_material = {}
    for item in booking_items:
        mat_name = item.material_name or ''
        items_by_material.setdefault(mat_name, []).append(item)

    rows = []
    cancel_total = 0
    cancel_total_qty = 0

    for mat_name, items in items_by_material.items():
        # Deliveries consume oldest booking first (FIFO). Remaining leftover
        # therefore sits on newer lots. Cancel UI still lists leftover newest-first.
        items.sort(
            key=lambda x: (
                x.booking.date_posted or datetime.min,
                x.booking.id or 0,
                x.id or 0
            )
        )
        remaining_delivered = float(consumed_totals.get(mat_name, 0) or 0)
        leftovers = []
        for item in items:
            booked_qty = float(item.qty or 0)
            consumed = min(booked_qty, remaining_delivered) if remaining_delivered > 0 else 0
            remaining_delivered = max(0, remaining_delivered - consumed)
            remaining_qty = booked_qty - consumed
            if remaining_qty > 0:
                leftovers.append((item, remaining_qty))
        leftovers.reverse()
        for item, remaining_qty in leftovers:
            rate = float(item.price_at_time or 0)
            amount = remaining_qty * rate
            booking = item.booking
            bill_ref = (booking.manual_bill_no or booking.auto_bill_no or f"BK-{booking.id}") if booking else ''
            cancel_total += amount
            cancel_total_qty += remaining_qty
            rows.append({
                'item': item,
                'item_id': item.id,
                'material': mat_name,
                'bill_no': bill_ref,
                'booking_date': _fmt_date_short(booking.date_posted if booking else None),
                'remaining_qty': remaining_qty,
                'rate': rate,
                'amount': amount,
            })

    return rows, cancel_total, cancel_total_qty
