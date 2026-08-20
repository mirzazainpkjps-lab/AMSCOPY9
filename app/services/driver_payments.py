"""Canonical driver / delivery-person payment service.

Every entry point (Driver ▸ Ledger ▸ Pay Driver, Accounts ▸ Driver Service
Payment, Delivery Rents ▸ Pay) delegates here so that one business event always
produces exactly one authoritative financial transaction:

    DeliveryPersonPayment   (source document: who / why / which rent allocation)
            │
            └── AccountTransaction  transaction_type='Driver Payment'
                                    source_type='DeliveryPersonPayment'
                                    from_account_id = selected cash/bank account

The driver ledger and the account ledger are *projections* of that pair; neither
keeps an independent balance field that could diverge.

The caller owns the single ``db.session.commit()``.  This module mutates the
source row, the linked ledger row, the exact minor-unit account balance and the
structured audit event inside one transaction, so a failure rolls all of it back.
"""
from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import func

from models import (
    Account,
    AccountTransaction,
    DeliveryPerson,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    SaleDeliveryPerson,
    db,
)
from utils.accounting_audit import record_accounting_audit
from utils.money import from_minor, money_float, to_minor

_EPS_MINOR = 0

_ALLOWED_METHODS = {
    "cash": "Cash",
    "bank": "Bank",
    "bank transfer": "Bank",
    "check": "Check",
    "cheque": "Check",
    "card": "Card",
    "online": "Online",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _actor(user):
    return (getattr(user, "username", None) or "").strip() or None if user else None


def _normalise_key(value):
    key = (value or "").strip()
    if not key:
        return None
    if len(key) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        raise ValueError("Invalid submission identifier.")
    return key


def _normalise_method(value):
    raw = (value or "Cash").strip().lower()
    method = _ALLOWED_METHODS.get(raw)
    if not method:
        raise ValueError("Select a supported payment method.")
    return method


def _expected_account_category(method: str):
    return "cash" if method == "Cash" else "bank"


def _account_minor(account):
    value = getattr(account, "balance_minor", None)
    return int(value) if value is not None else to_minor(account.balance or 0)


def _validate_account_for_method(account, method, *, allow_inactive=False):
    if account is None:
        raise ValueError("Please select a valid payment account.")
    if getattr(account, "is_active", True) is False and not allow_inactive:
        raise ValueError("The selected account is deactivated and cannot be used for a new transaction.")
    expected = _expected_account_category(method)
    actual = (account.category or "").strip().lower()
    if actual != expected:
        raise ValueError(f"The selected account must be a {expected} account for method '{method}'.")


def driver_payment_snapshot(payment):
    person = db.session.get(DeliveryPerson, payment.delivery_person_id) if payment.delivery_person_id else None
    return {
        "id": payment.id,
        "delivery_person_id": payment.delivery_person_id,
        "delivery_person_name": getattr(person, "name", "") or "",
        "sale_id": payment.sale_id,
        "allocation_id": payment.allocation_id,
        "amount_paid": money_float(payment.amount_paid),
        "waive_off_amount": money_float(payment.waive_off_amount),
        "method": payment.method or "",
        "payment_account_id": payment.payment_account_id,
        "reference": payment.reference or "",
        "date_posted": payment.date_posted.isoformat() if payment.date_posted else None,
        "note": payment.note or "",
        "is_void": bool(payment.is_void),
        "revision": getattr(payment, "revision", None) or 1,
    }


# --------------------------------------------------------------------------- #
# outstanding / allocation
# --------------------------------------------------------------------------- #
def driver_outstanding_minor(person_id, *, exclude_payment_id=None):
    """Calculated driver payable in minor units — never a stored field.

    ``opening_balance + Σ rent (debit) − Σ settlements (credit)``, matching
    ``financial_ledgers._delivery_person_rows`` exactly so the number shown in
    the ledger and the number validated here can never disagree.
    """
    person = db.session.get(DeliveryPerson, int(person_id))
    if person is None:
        raise ValueError("Delivery person not found.")

    total = to_minor(person.opening_balance or 0)

    allocations = SaleDeliveryPerson.query.filter_by(
        delivery_person_id=person.id, is_void=False
    ).join(DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id).filter(
        DirectSale.is_void == False  # noqa: E712
    ).all()
    active_sale_ids = set()
    for alloc in allocations:
        active_sale_ids.add(alloc.sale_id)
        total += to_minor(alloc.rent_amount or 0)

    name_key = " ".join(str(person.name or "").strip().casefold().split())
    for rent in DeliveryRent.query.filter(
        DeliveryRent.is_void == False,  # noqa: E712
        func.lower(func.trim(DeliveryRent.delivery_person_name)) == name_key,
    ).all():
        if rent.sale_id and rent.sale_id in active_sale_ids:
            continue
        total += to_minor(rent.amount or 0)

    q = DeliveryPersonPayment.query.filter_by(delivery_person_id=person.id, is_void=False)
    if exclude_payment_id:
        q = q.filter(DeliveryPersonPayment.id != int(exclude_payment_id))
    for payment in q.all():
        allocation = payment.allocation
        if allocation is not None and bool(getattr(allocation, "is_void", False)):
            continue
        total -= to_minor(payment.amount_paid or 0)
        total -= to_minor(payment.waive_off_amount or 0)
    return total


def driver_outstanding(person_id, *, exclude_payment_id=None) -> float:
    return float(from_minor(driver_outstanding_minor(person_id, exclude_payment_id=exclude_payment_id)))


def _allocation_due_minor(allocation, *, exclude_payment_id=None):
    if allocation is None:
        return None
    q = DeliveryPersonPayment.query.filter_by(allocation_id=allocation.id, is_void=False)
    if exclude_payment_id:
        q = q.filter(DeliveryPersonPayment.id != int(exclude_payment_id))
    settled = 0
    for row in q.all():
        settled += to_minor(row.amount_paid or 0) + to_minor(row.waive_off_amount or 0)
    return to_minor(allocation.rent_amount or 0) - settled


def _pick_allocation(person_id):
    """Oldest open rent allocation, so a driver-section payment stays FIFO."""
    allocations = SaleDeliveryPerson.query.filter_by(
        delivery_person_id=person_id, is_void=False
    ).join(DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id).filter(
        DirectSale.is_void == False,  # noqa: E712
        SaleDeliveryPerson.rent_amount > 0,
    ).order_by(SaleDeliveryPerson.created_at.asc(), SaleDeliveryPerson.id.asc()).all()
    for alloc in allocations:
        if (_allocation_due_minor(alloc) or 0) > 0:
            return alloc
    return None


# --------------------------------------------------------------------------- #
# core mutations
# --------------------------------------------------------------------------- #
def save_driver_payment(
    *, payment_id=None, delivery_person_id=None, amount_paid=0, waive_off_amount=0,
    method="Cash", payment_account_id=None, allocation_id=None, sale_id=None,
    reference="", date_posted=None, note="", idempotency_key=None,
    expected_revision=None, allow_overpay=False, actor=None,
):
    """Create or edit one driver payment and its single financial transaction."""
    from app.services.accounting import _sync_delivery_person_payment_accounting
    from app.services.payments_crud import _assert_period_open
    from app.services.time_money import resolve_posted_datetime

    key = _normalise_key(idempotency_key)
    if not payment_id and key:
        replay = DeliveryPersonPayment.query.filter_by(idempotency_key=key).first()
        if replay:
            # Duplicate submission (double-click / refresh / retry / second tab):
            # return the original event instead of creating a second one.
            replay._idempotent_replay = True
            return replay, False

    paid_minor = to_minor(amount_paid, field="Payment amount")
    waive_minor = to_minor(waive_off_amount, field="Waive-off amount")
    if paid_minor < 0 or waive_minor < 0:
        raise ValueError("Amounts cannot be negative.")
    if paid_minor + waive_minor <= 0:
        raise ValueError("Enter a payment or waive-off amount.")

    if payment_id:
        try:
            payment = db.session.get(DeliveryPersonPayment, int(payment_id))
        except (TypeError, ValueError):
            payment = None
        if payment is None:
            raise ValueError("Driver payment not found.")
        if payment.is_void:
            raise ValueError("This driver payment is reversed. Restore it before editing.")
        old = driver_payment_snapshot(payment)
        revision = int(payment.revision or 1)
        if expected_revision not in (None, "") and int(expected_revision) != revision:
            raise ValueError("This driver payment changed in another session. Reload it before saving.")
        created = False
    else:
        payment = DeliveryPersonPayment()
        old = None
        revision = 0
        created = True

    person_id = delivery_person_id if delivery_person_id not in (None, "") else (old or {}).get("delivery_person_id")
    try:
        person = db.session.get(DeliveryPerson, int(person_id)) if person_id not in (None, "") else None
    except (TypeError, ValueError):
        person = None
    if person is None:
        raise ValueError("Delivery person not found. Select a valid driver.")
    same_person = bool(old and person.id == old["delivery_person_id"])
    if getattr(person, "is_active", True) is False and not same_person:
        raise ValueError("The selected delivery person is inactive and cannot be used for a new transaction.")

    normal_method = _normalise_method(method)

    # An explicit account is mandatory whenever real money leaves the business.
    # A waive-off-only settlement moves no cash and therefore needs none.
    account = None
    if payment_account_id not in (None, ""):
        try:
            account = db.session.get(Account, int(payment_account_id))
        except (TypeError, ValueError):
            account = None
    if paid_minor > 0:
        if account is None:
            raise ValueError(
                f"Select a {_expected_account_category(normal_method)} account to pay this driver from."
            )
        same_account = bool(old and old["payment_account_id"] == account.id)
        _validate_account_for_method(account, normal_method, allow_inactive=same_account)
        available_minor = _account_minor(account)
        if same_account and old:
            available_minor += to_minor(old["amount_paid"])
        if available_minor + _EPS_MINOR < paid_minor:
            raise ValueError("Insufficient balance in the selected account.")
    else:
        account = None

    # Settlement may not exceed what the driver is actually owed.
    if not allow_overpay:
        outstanding_minor = driver_outstanding_minor(
            person.id, exclude_payment_id=(payment.id if not created else None)
        )
        if paid_minor + waive_minor > outstanding_minor + _EPS_MINOR:
            raise ValueError("Settlement exceeds the currently outstanding delivery-person balance.")

    allocation = None
    if allocation_id not in (None, ""):
        try:
            allocation = db.session.get(SaleDeliveryPerson, int(allocation_id))
        except (TypeError, ValueError):
            allocation = None
        if allocation is None or allocation.is_void:
            raise ValueError("The selected delivery rent allocation is not available.")
        if allocation.delivery_person_id != person.id:
            raise ValueError("The selected rent allocation belongs to a different delivery person.")
        due_minor = _allocation_due_minor(allocation, exclude_payment_id=(payment.id if not created else None))
        if not allow_overpay and due_minor is not None and paid_minor + waive_minor > due_minor + _EPS_MINOR:
            raise ValueError("Settlement exceeds the remaining rent amount for this delivery.")
    elif created:
        allocation = _pick_allocation(person.id)

    posted = resolve_posted_datetime(date_posted, fallback_dt=(payment.date_posted if not created else None))

    if not created:
        accounting_changed = any((
            old["amount_paid"] != float(from_minor(paid_minor)),
            old["waive_off_amount"] != float(from_minor(waive_minor)),
            old["payment_account_id"] != (account.id if account else None),
            old["delivery_person_id"] != person.id,
            old["date_posted"] != (posted.isoformat() if posted else None),
        ))
        if accounting_changed:
            _assert_period_open(old["payment_account_id"], payment.date_posted, operation="edited")
            if account and account.id != old["payment_account_id"]:
                _assert_period_open(account.id, posted, operation="posted")

    payment.delivery_person_id = person.id
    payment.allocation_id = allocation.id if allocation is not None else (payment.allocation_id if not created else None)
    payment.sale_id = (
        allocation.sale_id if allocation is not None
        else (int(sale_id) if sale_id not in (None, "") else (payment.sale_id if not created else None))
    )
    payment.amount_paid_minor = paid_minor
    payment.amount_paid = float(from_minor(paid_minor))
    payment.waive_off_minor = waive_minor
    payment.waive_off_amount = float(from_minor(waive_minor))
    payment.method = normal_method
    payment.payment_account_id = account.id if account else None
    payment.reference = (reference or "").strip()[:50]
    payment.date_posted = posted
    payment.note = (note or "").strip()
    payment.is_void = False
    payment.idempotency_key = key if created else payment.idempotency_key
    payment.updated_by = _actor(actor)
    payment.revision = revision + 1
    if created:
        payment.created_by = _actor(actor)
        db.session.add(payment)
        db.session.flush()
    if not payment.reference:
        payment.reference = f"DPP-{payment.id}"

    _sync_delivery_person_payment_accounting(payment)
    db.session.flush()

    after = driver_payment_snapshot(payment)
    record_accounting_audit(
        actor, action="Create" if created else "Edit", entity_type="DeliveryPersonPayment",
        entity_id=payment.id, before=old, after=after,
        amount_before=(old["amount_paid"] if old else None), amount_after=after["amount_paid"],
        account_before_id=(old["payment_account_id"] if old else None),
        account_after_id=payment.payment_account_id,
        party_before_id=(old["delivery_person_id"] if old else None), party_after_id=person.id,
        reason=payment.note,
    )
    return payment, created


def delete_driver_payment(payment, actor=None) -> bool:
    """Controlled reversal: the historical row stays, the effect is undone once."""
    from app.services.accounting import _sync_delivery_person_payment_accounting
    from app.services.payments_crud import _assert_period_open

    if payment is None:
        raise ValueError("Driver payment not found.")
    if payment.is_void:
        return False
    _assert_period_open(payment.payment_account_id, payment.date_posted, operation="reversed")

    before = driver_payment_snapshot(payment)
    payment.is_void = True
    payment.updated_by = _actor(actor)
    payment.revision = int(payment.revision or 1) + 1
    _sync_delivery_person_payment_accounting(payment)
    db.session.flush()
    record_accounting_audit(
        actor, action="Delete", entity_type="DeliveryPersonPayment", entity_id=payment.id,
        before=before, after=driver_payment_snapshot(payment),
        amount_before=before["amount_paid"], amount_after=0,
        account_before_id=before["payment_account_id"],
        party_before_id=before["delivery_person_id"], reason=payment.note,
    )
    return True


def restore_driver_payment(payment, actor=None) -> bool:
    from app.services.accounting import _sync_delivery_person_payment_accounting
    from app.services.payments_crud import _assert_period_open

    if payment is None:
        raise ValueError("Driver payment not found.")
    if not payment.is_void:
        return False
    _assert_period_open(payment.payment_account_id, payment.date_posted, operation="restored")

    paid_minor = to_minor(payment.amount_paid or 0)
    if paid_minor > 0:
        account = db.session.get(Account, payment.payment_account_id) if payment.payment_account_id else None
        if account is None:
            raise ValueError("The original payment account is missing; restore is not possible.")
        if _account_minor(account) < paid_minor:
            raise ValueError("Insufficient balance to restore this driver payment.")

    allocation = payment.allocation
    if allocation is not None and not allocation.is_void:
        due_minor = _allocation_due_minor(allocation, exclude_payment_id=payment.id)
        if due_minor is not None and paid_minor + to_minor(payment.waive_off_amount or 0) > due_minor + _EPS_MINOR:
            raise ValueError("This settlement cannot be restored because the allocation is already settled.")

    before = driver_payment_snapshot(payment)
    payment.is_void = False
    payment.updated_by = _actor(actor)
    payment.revision = int(payment.revision or 1) + 1
    _sync_delivery_person_payment_accounting(payment)
    db.session.flush()
    record_accounting_audit(
        actor, action="Restore", entity_type="DeliveryPersonPayment", entity_id=payment.id,
        before=before, after=driver_payment_snapshot(payment),
        amount_before=0, amount_after=payment.amount_paid,
        account_after_id=payment.payment_account_id,
        party_after_id=payment.delivery_person_id, reason=payment.note,
    )
    return True


def settle_driver_fifo(
    *, delivery_person_id, amount_paid=0, waive_off_amount=0, method="Cash",
    payment_account_id=None, reference="", date_posted=None, note="",
    idempotency_key=None, actor=None,
):
    """Allocate one settlement across open rent items, FIFO.

    Each allocated slice is still a fully-linked payment with its own account
    transaction, so no slice can move a driver balance without moving cash.
    """
    key = _normalise_key(idempotency_key)
    if key:
        replay = DeliveryPersonPayment.query.filter(
            DeliveryPersonPayment.idempotency_key.like(f"{key}%")
        ).first()
        if replay:
            replay._idempotent_replay = True
            return [replay]

    paid_minor = to_minor(amount_paid, field="Payment amount")
    waive_minor = to_minor(waive_off_amount, field="Waive-off amount")
    if paid_minor < 0 or waive_minor < 0:
        raise ValueError("Amounts cannot be negative.")
    if paid_minor + waive_minor <= 0:
        raise ValueError("Enter a payment or waive-off amount.")

    person = db.session.get(DeliveryPerson, int(delivery_person_id))
    if person is None:
        raise ValueError("Delivery person not found.")

    outstanding_minor = driver_outstanding_minor(person.id)
    if paid_minor + waive_minor > outstanding_minor + _EPS_MINOR:
        raise ValueError("Settlement exceeds the currently outstanding delivery-person balance.")

    allocations = SaleDeliveryPerson.query.filter_by(
        delivery_person_id=person.id, is_void=False
    ).join(DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id).filter(
        DirectSale.is_void == False,  # noqa: E712
        SaleDeliveryPerson.rent_amount > 0,
    ).order_by(SaleDeliveryPerson.created_at.asc(), SaleDeliveryPerson.id.asc()).all()

    remaining_paid, remaining_waive = paid_minor, waive_minor
    created_rows = []
    seq = 0

    def _emit(alloc, sale_id, slice_paid, slice_waive):
        nonlocal seq
        row, _ = save_driver_payment(
            delivery_person_id=person.id,
            amount_paid=from_minor(slice_paid),
            waive_off_amount=from_minor(slice_waive),
            method=method,
            payment_account_id=payment_account_id if slice_paid > 0 else None,
            allocation_id=(alloc.id if alloc is not None else None),
            sale_id=sale_id,
            reference=reference,
            date_posted=date_posted,
            note=note,
            idempotency_key=(f"{key}:{seq}" if key else None),
            allow_overpay=True,
            actor=actor,
        )
        seq += 1
        created_rows.append(row)

    for alloc in allocations:
        if remaining_paid + remaining_waive <= 0:
            break
        due_minor = _allocation_due_minor(alloc)
        if not due_minor or due_minor <= 0:
            continue
        slice_total = min(due_minor, remaining_paid + remaining_waive)
        slice_paid = min(remaining_paid, slice_total)
        slice_waive = slice_total - slice_paid
        _emit(alloc, alloc.sale_id, slice_paid, slice_waive)
        remaining_paid -= slice_paid
        remaining_waive -= slice_waive

    if remaining_paid + remaining_waive > 0:
        # Legacy DeliveryRent rows have no allocation; settle them by sale.
        active_sale_ids = {a.sale_id for a in allocations}
        name_key = " ".join(str(person.name or "").strip().casefold().split())
        legacy = DeliveryRent.query.filter(
            DeliveryRent.is_void == False,  # noqa: E712
            func.lower(func.trim(DeliveryRent.delivery_person_name)) == name_key,
        ).order_by(DeliveryRent.date_posted.asc(), DeliveryRent.id.asc()).all()
        for rent in legacy:
            if remaining_paid + remaining_waive <= 0:
                break
            if rent.sale_id in active_sale_ids:
                continue
            settled = 0
            for row in DeliveryPersonPayment.query.filter_by(
                delivery_person_id=person.id, allocation_id=None, sale_id=rent.sale_id, is_void=False
            ).all():
                settled += to_minor(row.amount_paid or 0) + to_minor(row.waive_off_amount or 0)
            due_minor = to_minor(rent.amount or 0) - settled
            if due_minor <= 0:
                continue
            slice_total = min(due_minor, remaining_paid + remaining_waive)
            slice_paid = min(remaining_paid, slice_total)
            slice_waive = slice_total - slice_paid
            _emit(None, rent.sale_id, slice_paid, slice_waive)
            remaining_paid -= slice_paid
            remaining_waive -= slice_waive

    if remaining_paid + remaining_waive > 0:
        # Opening-balance / unallocated payable: record one unallocated slice
        # rather than silently dropping the remainder.
        _emit(None, None, remaining_paid, remaining_waive)

    return created_rows


# --------------------------------------------------------------------------- #
# reconciliation
# --------------------------------------------------------------------------- #
def reconcile_driver_payments(tolerance=Decimal("0.01")) -> dict:
    """Compare every driver payment with its authoritative account transaction.

    Read-only: mismatches and legacy unlinked rows are reported so the cause can
    be understood before any balance is touched.
    """
    tolerance_minor = to_minor(tolerance)
    issues = []
    linked_ids = set()

    for payment in DeliveryPersonPayment.query.filter(DeliveryPersonPayment.is_void == False).all():  # noqa: E712
        paid_minor = to_minor(payment.amount_paid or 0)
        txs = AccountTransaction.query.filter(
            AccountTransaction.source_type == "DeliveryPersonPayment",
            AccountTransaction.source_id == payment.id,
            AccountTransaction.transaction_type == "Driver Payment",
            AccountTransaction.is_void == False,  # noqa: E712
        ).all()
        linked_ids.update(t.id for t in txs)
        if paid_minor <= 0:
            if txs:
                issues.append({"kind": "driver_payment_unexpected_ledger_row",
                               "payment_id": payment.id, "tx_ids": [t.id for t in txs]})
            continue
        if not txs:
            issues.append({
                "kind": "driver_payment_missing_ledger_row",
                "payment_id": payment.id,
                "amount": money_float(payment.amount_paid),
                "legacy": payment.payment_account_id is None,
            })
            continue
        if len(txs) > 1:
            issues.append({"kind": "driver_payment_duplicate_ledger_rows",
                           "payment_id": payment.id, "tx_ids": [t.id for t in txs]})
            continue
        tx = txs[0]
        tx_minor = int(tx.amount_minor) if tx.amount_minor is not None else to_minor(tx.amount or 0)
        if abs(tx_minor - paid_minor) > tolerance_minor:
            issues.append({"kind": "driver_payment_amount_mismatch", "payment_id": payment.id,
                           "payment_amount": money_float(payment.amount_paid),
                           "ledger_amount": money_float(tx.amount)})
        if tx.from_account_id != payment.payment_account_id:
            issues.append({"kind": "driver_payment_account_mismatch", "payment_id": payment.id,
                           "payment_account_id": payment.payment_account_id,
                           "ledger_account_id": tx.from_account_id})

    for tx in AccountTransaction.query.filter(
        AccountTransaction.transaction_type == "Driver Payment",
        AccountTransaction.is_void == False,  # noqa: E712
    ).all():
        if tx.id in linked_ids:
            continue
        source = db.session.get(DeliveryPersonPayment, tx.source_id) if tx.source_id else None
        if source is None or source.is_void:
            issues.append({"kind": "orphan_driver_account_transaction", "tx_id": tx.id,
                           "source_id": tx.source_id})

    legacy_unlinked = DeliveryPersonPayment.query.filter(
        DeliveryPersonPayment.is_void == False,  # noqa: E712
        DeliveryPersonPayment.payment_account_id.is_(None),
        DeliveryPersonPayment.amount_paid > 0,
    ).count()

    return {
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "legacy_unlinked_payments": legacy_unlinked,
    }


__all__ = [
    "save_driver_payment",
    "delete_driver_payment",
    "restore_driver_payment",
    "settle_driver_fifo",
    "driver_outstanding",
    "driver_outstanding_minor",
    "driver_payment_snapshot",
    "reconcile_driver_payments",
]
