"""Canonical Accounts payment CRUD and per-account reconciliation services.

All UI and legacy routes delegate financial mutations here.  The caller owns the
single database commit; this module updates the source row, linked ledger rows,
exact minor-unit balances, dependent bill state, and structured audit event in
that same transaction.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, or_

from models import (
    Account,
    AccountReconciliation,
    AccountTransaction,
    Client,
    MaterialReturn,
    Payment,
    Supplier,
    SupplierPayment,
    db,
)
from utils.accounting_audit import record_accounting_audit
from utils.money import decimal_money, from_minor, money_float, to_minor

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


def _money(value) -> float:
    """Compatibility helper used by templates/tests; arithmetic uses minor units."""
    return money_float(value)


def _actor(user):
    return (getattr(user, "username", None) or "").strip() or None if user else None


def _normalise_key(value):
    key = (value or "").strip()
    if not key:
        return None
    if len(key) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        raise ValueError("Invalid submission identifier.")
    return key


def _normalise_method(value, *, legacy_refund=False):
    raw = (value or ("Cash" if legacy_refund else "")).strip().lower()
    if legacy_refund and raw == "refund":
        raw = "cash"
    method = _ALLOWED_METHODS.get(raw)
    if not method:
        raise ValueError("Select a supported payment method.")
    return method


def _expected_account_category(method: str):
    return "cash" if method == "Cash" else "bank"


def _account_minor(account):
    value = getattr(account, "balance_minor", None)
    return int(value) if value is not None else to_minor(account.balance or 0)


def _set_account_minor(account, minor):
    account.balance_minor = int(minor)
    account.balance = float(from_minor(minor))


def _account_display_name(account):
    if not account:
        return ""
    return account.account_holder_name or account.name or ""


def _validate_account_for_method(account, method, *, allow_inactive=False):
    if account is None:
        raise ValueError("Please select a valid account.")
    if getattr(account, "is_active", True) is False and not allow_inactive:
        raise ValueError("The selected account is deactivated and cannot be used for a new transaction.")
    expected = _expected_account_category(method)
    actual = (account.category or "").strip().lower()
    if actual != expected:
        raise ValueError(f"The selected account must be a {expected} account for method '{method}'.")


def _period_end(rec):
    return rec.period_end_at or datetime.combine(rec.reconciliation_date, time.max)


def _latest_reconciliation(account_id):
    return AccountReconciliation.query.filter_by(account_id=account_id).order_by(
        AccountReconciliation.reconciliation_date.desc(),
        AccountReconciliation.id.desc(),
    ).first()


def _assert_period_open(account_id, posted_at, *, operation="change"):
    """Prevent a mutation from silently rewriting a finalised closing period."""
    if not account_id or not posted_at:
        return
    latest = _latest_reconciliation(account_id)
    if latest and posted_at <= _period_end(latest):
        raise ValueError(
            f"This transaction is in a reconciled period ending {_period_end(latest):%Y-%m-%d %H:%M}. "
            f"It cannot be {operation}; record a new reversal/adjustment in the open period instead."
        )


def active_clients():
    return Client.query.filter_by(is_active=True).order_by(Client.name.asc(), Client.id.asc()).all()


def active_suppliers():
    return Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc(), Supplier.id.asc()).all()


def active_cash_bank_accounts():
    return Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.category)).in_(("cash", "bank")),
    ).order_by(Account.name.asc(), Account.id.asc()).all()


def _resolve_client(client_code, client_name):
    code = (client_code or "").strip()
    name = (client_name or "").strip()
    if code:
        found = Client.query.filter(func.lower(func.trim(Client.code)) == code.lower()).first()
        if found:
            return found
    if name:
        return Client.query.filter(func.lower(func.trim(Client.name)) == name.lower()).first()
    return None


def _client_payment_source(payment):
    source_type = (getattr(payment, "source_type", None) or "").strip()
    source_id = getattr(payment, "source_id", None)
    marker = re.search(r"\[MATERIAL_RETURN:(\d+)\]", payment.note or "", re.IGNORECASE)
    if marker:
        return "MaterialReturn", int(marker.group(1))
    if source_type:
        return source_type, source_id
    linked = MaterialReturn.query.filter_by(payment_id=payment.id).first() if payment.id else None
    if linked:
        return "MaterialReturn", linked.id
    return None, None


def _supplier_payment_source(payment):
    source_type = (getattr(payment, "source_type", None) or "").strip()
    source_id = getattr(payment, "source_id", None)
    marker = re.search(r"\[AUTO_GRN_PAY:(\d+)\]", payment.note or "", re.IGNORECASE)
    if marker:
        return "GRN", int(marker.group(1))
    return (source_type or None), source_id


def _client_payment_kind(payment):
    source_type, _ = _client_payment_source(payment)
    if source_type == "MaterialReturn" or (payment.method or "").strip().lower() == "material return":
        return "Material Return"
    value = (getattr(payment, "payment_type", None) or "").strip().lower()
    if value in ("refund", "repayment") or float(payment.amount or 0) < 0 or (payment.method or "").strip().lower() == "refund":
        return "Refund"
    if value in ("waive-off", "waive off") or (float(payment.amount or 0) == 0 and float(payment.discount or 0) > 0):
        return "Waive-Off"
    return "Receipt"


def _client_payment_snapshot(payment):
    source_type, source_id = _client_payment_source(payment)
    return {
        "id": payment.id,
        "client_id": getattr(payment, "client_id", None),
        "client_name": payment.client_name or "",
        "amount": _money(payment.amount),
        "discount": _money(payment.discount),
        "discount_reason": payment.discount_reason or "",
        "payment_type": _client_payment_kind(payment),
        "method": payment.method or "",
        "payment_account_id": payment.payment_account_id,
        "account_name": payment.account_name or "",
        "manual_bill_no": payment.manual_bill_no or "",
        "auto_bill_no": payment.auto_bill_no or "",
        "date_posted": payment.date_posted.isoformat() if payment.date_posted else None,
        "note": payment.note or "",
        "photo_url": payment.photo_url or "",
        "photo_path": payment.photo_path or "",
        "is_void": bool(payment.is_void),
        "source_type": source_type,
        "source_id": source_id,
        "revision": getattr(payment, "revision", None) or 1,
    }


def _supplier_payment_snapshot(payment):
    supplier = db.session.get(Supplier, payment.supplier_id) if payment.supplier_id else None
    source_type, source_id = _supplier_payment_source(payment)
    return {
        "id": payment.id,
        "supplier_id": payment.supplier_id,
        "supplier_name": supplier.name if supplier else "",
        "amount": _money(payment.amount),
        "method": payment.method or "",
        "payment_account_id": payment.payment_account_id,
        "account_name": payment.account_name or "",
        "manual_bill_no": payment.manual_bill_no or "",
        "auto_bill_no": payment.auto_bill_no or "",
        "date_posted": payment.date_posted.isoformat() if payment.date_posted else None,
        "note": payment.note or "",
        "is_void": bool(payment.is_void),
        "source_type": source_type,
        "source_id": source_id,
        "revision": getattr(payment, "revision", None) or 1,
    }


def save_client_payment(
    *, payment_id=None, client_name="", client_code="", amount=0, discount=0,
    discount_reason="", method="Cash", payment_type=None, payment_account_id=None,
    bank_name="", account_name="", account_no="", manual_bill_no="", date_posted=None,
    note="", photo_path=None, photo_url=None, idempotency_key=None, expected_revision=None,
    actor=None,
):
    """Create/update a receipt, refund, or waive-off without changing its identity."""
    from app.services.accounting import _sync_payment_accounting
    from app.services.billing import AUTO_BILL_NAMESPACES, find_bill_conflict, get_next_bill_no, normalize_manual_bill
    from app.services.time_money import resolve_posted_datetime
    from app.services.void_rebuild import rebuild_pending_bills
    from app.services.waive import _sync_payment_waive_off

    key = _normalise_key(idempotency_key)
    if not payment_id and key:
        replay = Payment.query.filter_by(idempotency_key=key).first()
        if replay:
            replay._idempotent_replay = True
            return replay, False

    if payment_id:
        try:
            payment = db.session.get(Payment, int(payment_id))
        except (TypeError, ValueError):
            payment = None
        if payment is None:
            raise ValueError("Payment not found.")
        if payment.is_void:
            raise ValueError("This payment is deleted. Restore it before editing.")
        source_type, source_id = _client_payment_source(payment)
        if source_type == "MaterialReturn":
            raise ValueError(f"This payment is controlled by Material Return #{source_id}; edit it from Material Returns.")
        old = _client_payment_snapshot(payment)
        old_client = db.session.get(Client, payment.client_id) if getattr(payment, "client_id", None) else _resolve_client("", payment.client_name)
        revision = int(getattr(payment, "revision", None) or 1)
        if expected_revision not in (None, "") and int(expected_revision) != revision:
            raise ValueError("This payment changed in another session. Reload it before saving.")
        created = False
    else:
        payment = Payment()
        old = None
        old_client = None
        revision = 0
        created = True

    kind_raw = (payment_type or (_client_payment_kind(payment) if not created else "Receipt")).strip().lower()
    if kind_raw in ("receipt", "payment", "receive"):
        kind = "Receipt"
    elif kind_raw in ("refund", "repayment"):
        kind = "Refund"
    elif kind_raw in ("waive-off", "waive off", "discount"):
        kind = "Waive-Off"
    else:
        raise ValueError("Select Receipt, Refund, or Waive-Off as the payment type.")

    submitted_minor = abs(to_minor(amount, field="Amount"))
    discount_minor = to_minor(discount, field="Discount")
    if discount_minor < 0:
        raise ValueError("Discount cannot be negative.")
    if kind == "Refund":
        if submitted_minor <= 0:
            raise ValueError("Refund amount must be greater than zero.")
        amount_minor = -submitted_minor
        discount_minor = 0
        discount_reason = ""
    elif kind == "Waive-Off":
        amount_minor = 0
        if discount_minor <= 0:
            raise ValueError("Waive-Off amount must be greater than zero.")
    else:
        amount_minor = submitted_minor
        if amount_minor + discount_minor <= 0:
            raise ValueError("Amount and discount cannot both be zero.")
    if discount_minor > 0 and not (discount_reason or "").strip():
        raise ValueError("Discount reason is required when a discount is entered.")

    # Historical client remains valid when unchanged; suspended clients cannot
    # be selected for a new or changed relationship.
    client_obj = _resolve_client(client_code, client_name)
    if client_obj is None and old_client and (client_name or "").strip().lower() == (payment.client_name or "").strip().lower():
        client_obj = old_client
    if client_obj is None:
        raise ValueError("Client not found. Select a valid client from the search list.")
    same_client = bool(old_client and client_obj.id == old_client.id)
    if getattr(client_obj, "is_active", True) is False and not same_client:
        raise ValueError("The selected client is suspended and cannot be used for a new transaction.")

    legacy_refund_method = (method or "").strip().lower() == "refund"
    normal_method = _normalise_method(method, legacy_refund=legacy_refund_method)
    account = None
    if payment_account_id not in (None, ""):
        try:
            account = db.session.get(Account, int(payment_account_id))
        except (TypeError, ValueError):
            account = None
    account_required = amount_minor != 0
    if account_required and account is None:
        raise ValueError(f"Select a {_expected_account_category(normal_method)} account for this {kind.lower()}.")
    same_account = bool(old and account and old["payment_account_id"] == account.id)
    if account:
        _validate_account_for_method(account, normal_method, allow_inactive=same_account)
        if kind == "Refund":
            available_minor = _account_minor(account)
            if same_account and old and old["amount"] < 0:
                available_minor += abs(to_minor(old["amount"]))
            if available_minor < abs(amount_minor):
                raise ValueError("Insufficient balance in the selected account for this refund.")
        bank_name = account.bank_name or ""
        account_name = _account_display_name(account)
        account_no = account.account_number or ""
        payment_account_id = account.id
    else:
        payment_account_id = None
        bank_name = account_name = account_no = ""

    manual_bill_no = normalize_manual_bill(manual_bill_no) if (manual_bill_no or "").strip() else ""
    if manual_bill_no:
        conflict = find_bill_conflict(manual_bill_no)
        if conflict and not (payment.id and conflict[0] == "Payment" and conflict[1] == payment.id):
            raise ValueError(f"Manual bill '{manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.")

    posted = resolve_posted_datetime(date_posted, fallback_dt=(payment.date_posted if not created else None))
    if not created:
        accounting_changed = any((
            old["amount"] != float(from_minor(amount_minor)),
            old["discount"] != float(from_minor(discount_minor)),
            old["payment_account_id"] != payment_account_id,
            old["client_id"] not in (None, client_obj.id),
            old["payment_type"] != kind,
            old["date_posted"] != (posted.isoformat() if posted else None),
        ))
        if accounting_changed:
            _assert_period_open(old["payment_account_id"], payment.date_posted, operation="edited")
            if payment_account_id != old["payment_account_id"]:
                _assert_period_open(payment_account_id, posted, operation="posted")

    payment.client_id = client_obj.id
    payment.client_name = client_obj.name
    payment.amount_minor = amount_minor
    payment.amount = float(from_minor(amount_minor))
    payment.discount_minor = discount_minor
    payment.discount = float(from_minor(discount_minor))
    payment.discount_reason = (discount_reason or "").strip() if discount_minor > 0 else ""
    payment.payment_type = kind
    payment.method = normal_method
    payment.payment_account_id = payment_account_id
    payment.bank_name = bank_name or ""
    payment.account_name = account_name or ""
    payment.account_no = account_no or ""
    payment.manual_bill_no = manual_bill_no or ""
    payment.date_posted = posted
    payment.note = (note or "").strip()
    payment.photo_url = (photo_url or "").strip()
    if photo_path:
        payment.photo_path = photo_path
    payment.is_void = False
    payment.idempotency_key = key if created else payment.idempotency_key
    payment.updated_by = _actor(actor)
    payment.revision = revision + 1
    if created:
        payment.created_by = _actor(actor)
        payment.auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES["PAYMENT"])
        db.session.add(payment)
        db.session.flush()

    _sync_payment_waive_off(payment)
    _sync_payment_accounting(payment)
    for client_id in {old_client.id if old_client else None, client_obj.id}:
        if client_id:
            # Failure is intentionally fatal so the source row, ledger, bill
            # allocation and audit event cannot become partially inconsistent.
            rebuild_pending_bills(client_id=client_id)
    db.session.flush()

    after = _client_payment_snapshot(payment)
    record_accounting_audit(
        actor,
        action="Create" if created else "Edit",
        entity_type="Payment",
        entity_id=payment.id,
        before=old,
        after=after,
        amount_before=(old["amount"] if old else None),
        amount_after=after["amount"],
        account_before_id=(old["payment_account_id"] if old else None),
        account_after_id=payment.payment_account_id,
        party_before_id=(old["client_id"] if old else None),
        party_after_id=client_obj.id,
        reason=payment.note,
    )
    return payment, created


def delete_client_payment(payment, actor=None) -> bool:
    if payment is None:
        raise ValueError("Payment not found.")
    if payment.is_void:
        return False
    source_type, source_id = _client_payment_source(payment)
    if source_type == "MaterialReturn":
        raise ValueError(f"This payment is controlled by Material Return #{source_id}; delete the material return instead.")
    _assert_period_open(payment.payment_account_id, payment.date_posted, operation="deleted")

    from app.services.void_rebuild import _set_payment_void_state, rebuild_pending_bills

    before = _client_payment_snapshot(payment)
    if not _set_payment_void_state(payment, True):
        return False
    client = db.session.get(Client, payment.client_id) if getattr(payment, "client_id", None) else _resolve_client("", payment.client_name)
    if client:
        rebuild_pending_bills(client_id=client.id)
    payment.updated_by = _actor(actor)
    payment.revision = int(payment.revision or 1) + 1
    db.session.flush()
    record_accounting_audit(
        actor, action="Delete", entity_type="Payment", entity_id=payment.id,
        before=before, after=_client_payment_snapshot(payment),
        amount_before=before["amount"], amount_after=0,
        account_before_id=before["payment_account_id"], party_before_id=before["client_id"],
        reason=payment.note,
    )
    return True


def restore_client_payment(payment, actor=None) -> bool:
    if payment is None:
        raise ValueError("Payment not found.")
    if not payment.is_void:
        return False
    source_type, source_id = _client_payment_source(payment)
    if source_type == "MaterialReturn":
        raise ValueError(f"This payment is controlled by Material Return #{source_id}; restore the material return instead.")
    _assert_period_open(payment.payment_account_id, payment.date_posted, operation="restored")

    from app.services.void_rebuild import _set_payment_void_state, rebuild_pending_bills

    before = _client_payment_snapshot(payment)
    if not _set_payment_void_state(payment, False):
        return False
    client = db.session.get(Client, payment.client_id) if getattr(payment, "client_id", None) else _resolve_client("", payment.client_name)
    if client:
        rebuild_pending_bills(client_id=client.id)
    payment.updated_by = _actor(actor)
    payment.revision = int(payment.revision or 1) + 1
    db.session.flush()
    record_accounting_audit(
        actor, action="Restore", entity_type="Payment", entity_id=payment.id,
        before=before, after=_client_payment_snapshot(payment),
        amount_before=0, amount_after=payment.amount,
        account_after_id=payment.payment_account_id, party_after_id=payment.client_id,
        reason=payment.note,
    )
    return True


def save_supplier_payment(
    *, payment_id=None, supplier_id=None, amount=0, method="Cash",
    payment_account_id=None, bank_name="", account_name="", account_no="",
    manual_bill_no="", date_posted=None, note="", idempotency_key=None,
    expected_revision=None, actor=None,
):
    """Create/update a supplier payment through one validation/accounting path."""
    from app.services.accounting import _sync_supplier_payment_accounting
    from app.services.billing import AUTO_BILL_NAMESPACES, find_bill_conflict, get_next_bill_no, normalize_manual_bill
    from app.services.time_money import resolve_posted_datetime

    key = _normalise_key(idempotency_key)
    if not payment_id and key:
        replay = SupplierPayment.query.filter_by(idempotency_key=key).first()
        if replay:
            replay._idempotent_replay = True
            return replay, False

    amount_minor = to_minor(amount, field="Payment amount")
    if amount_minor <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    normal_method = _normalise_method(method)

    if payment_id:
        try:
            payment = db.session.get(SupplierPayment, int(payment_id))
        except (TypeError, ValueError):
            payment = None
        if payment is None:
            raise ValueError("Supplier payment not found.")
        if payment.is_void:
            raise ValueError("This supplier payment is deleted. Restore it before editing.")
        source_type, source_id = _supplier_payment_source(payment)
        if source_type:
            raise ValueError(f"This payment is controlled by {source_type} #{source_id}; edit it from that source module.")
        old = _supplier_payment_snapshot(payment)
        revision = int(payment.revision or 1)
        if expected_revision not in (None, "") and int(expected_revision) != revision:
            raise ValueError("This supplier payment changed in another session. Reload it before saving.")
        created = False
    else:
        payment = SupplierPayment()
        old = None
        revision = 0
        created = True

    try:
        supplier = db.session.get(Supplier, int(supplier_id)) if supplier_id not in (None, "") else None
    except (TypeError, ValueError):
        supplier = None
    if supplier is None and not created and payment.supplier_id:
        supplier = db.session.get(Supplier, payment.supplier_id)
    if supplier is None:
        raise ValueError("Supplier not found. Select a valid supplier.")
    same_supplier = bool(old and supplier.id == old["supplier_id"])
    if getattr(supplier, "is_active", True) is False and not same_supplier:
        raise ValueError("The selected supplier is suspended and cannot be used for a new transaction.")

    try:
        account = db.session.get(Account, int(payment_account_id)) if payment_account_id not in (None, "") else None
    except (TypeError, ValueError):
        account = None
    if account is None:
        raise ValueError(f"Select a {_expected_account_category(normal_method)} account for this supplier payment.")
    same_account = bool(old and old["payment_account_id"] == account.id)
    _validate_account_for_method(account, normal_method, allow_inactive=same_account)

    available_minor = _account_minor(account)
    if same_account:
        available_minor += to_minor(old["amount"])
    if available_minor + _EPS_MINOR < amount_minor:
        raise ValueError("Insufficient balance in the selected account.")

    manual_bill_no = normalize_manual_bill(manual_bill_no) if (manual_bill_no or "").strip() else ""
    if manual_bill_no:
        conflict = find_bill_conflict(manual_bill_no)
        if conflict and not (payment.id and conflict[0] == "SupplierPayment" and conflict[1] == payment.id):
            raise ValueError(f"Manual bill '{manual_bill_no}' already exists in {conflict[0]} #{conflict[1]}.")

    posted = resolve_posted_datetime(date_posted, fallback_dt=(payment.date_posted if not created else None))
    if not created:
        accounting_changed = any((
            old["amount"] != float(from_minor(amount_minor)),
            old["payment_account_id"] != account.id,
            old["supplier_id"] != supplier.id,
            old["date_posted"] != (posted.isoformat() if posted else None),
        ))
        if accounting_changed:
            _assert_period_open(old["payment_account_id"], payment.date_posted, operation="edited")
            if account.id != old["payment_account_id"]:
                _assert_period_open(account.id, posted, operation="posted")

    payment.supplier_id = supplier.id
    payment.amount_minor = amount_minor
    payment.amount = float(from_minor(amount_minor))
    payment.method = normal_method
    payment.payment_type = "Payment"
    payment.payment_account_id = account.id
    payment.bank_name = account.bank_name or ""
    payment.account_name = _account_display_name(account)
    payment.account_no = account.account_number or ""
    payment.manual_bill_no = manual_bill_no or ""
    payment.date_posted = posted
    payment.note = (note or "").strip()
    payment.is_void = False
    payment.idempotency_key = key if created else payment.idempotency_key
    payment.updated_by = _actor(actor)
    payment.revision = revision + 1
    if created:
        payment.created_by = _actor(actor)
        payment.auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES["SUPPLIER_PAYMENT"])
        db.session.add(payment)
        db.session.flush()

    _sync_supplier_payment_accounting(payment)
    db.session.flush()
    after = _supplier_payment_snapshot(payment)
    record_accounting_audit(
        actor, action="Create" if created else "Edit", entity_type="SupplierPayment",
        entity_id=payment.id, before=old, after=after,
        amount_before=(old["amount"] if old else None), amount_after=after["amount"],
        account_before_id=(old["payment_account_id"] if old else None), account_after_id=account.id,
        party_before_id=(old["supplier_id"] if old else None), party_after_id=supplier.id,
        reason=payment.note,
    )
    return payment, created


def delete_supplier_payment(payment, actor=None) -> bool:
    if payment is None:
        raise ValueError("Supplier payment not found.")
    if payment.is_void:
        return False
    source_type, source_id = _supplier_payment_source(payment)
    if source_type:
        raise ValueError(f"This payment is controlled by {source_type} #{source_id}; delete/reverse it there.")
    _assert_period_open(payment.payment_account_id, payment.date_posted, operation="deleted")
    from app.services.accounting import _sync_supplier_payment_accounting

    before = _supplier_payment_snapshot(payment)
    payment.is_void = True
    payment.updated_by = _actor(actor)
    payment.revision = int(payment.revision or 1) + 1
    _sync_supplier_payment_accounting(payment)
    db.session.flush()
    record_accounting_audit(
        actor, action="Delete", entity_type="SupplierPayment", entity_id=payment.id,
        before=before, after=_supplier_payment_snapshot(payment),
        amount_before=before["amount"], amount_after=0,
        account_before_id=before["payment_account_id"], party_before_id=before["supplier_id"],
        reason=payment.note,
    )
    return True


def restore_supplier_payment(payment, actor=None) -> bool:
    if payment is None:
        raise ValueError("Supplier payment not found.")
    if not payment.is_void:
        return False
    source_type, source_id = _supplier_payment_source(payment)
    if source_type:
        raise ValueError(f"This payment is controlled by {source_type} #{source_id}; restore it there.")
    _assert_period_open(payment.payment_account_id, payment.date_posted, operation="restored")
    account = db.session.get(Account, payment.payment_account_id) if payment.payment_account_id else None
    if account and _account_minor(account) < abs(to_minor(payment.amount or 0)):
        raise ValueError("Insufficient balance to restore this supplier payment.")
    from app.services.accounting import _sync_supplier_payment_accounting

    before = _supplier_payment_snapshot(payment)
    payment.is_void = False
    payment.updated_by = _actor(actor)
    payment.revision = int(payment.revision or 1) + 1
    _sync_supplier_payment_accounting(payment)
    db.session.flush()
    record_accounting_audit(
        actor, action="Restore", entity_type="SupplierPayment", entity_id=payment.id,
        before=before, after=_supplier_payment_snapshot(payment),
        amount_before=0, amount_after=payment.amount,
        account_after_id=payment.payment_account_id, party_after_id=payment.supplier_id,
        reason=payment.note,
    )
    return True


def _transaction_sums(account_id, *, after=None, through=None):
    q = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        or_(AccountTransaction.from_account_id == account_id, AccountTransaction.to_account_id == account_id),
    )
    if after is not None:
        q = q.filter(AccountTransaction.date_posted > after)
    if through is not None:
        q = q.filter(AccountTransaction.date_posted <= through)
    incoming = outgoing = 0
    for tx in q.order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all():
        minor = int(tx.amount_minor) if getattr(tx, "amount_minor", None) is not None else to_minor(tx.amount or 0)
        if tx.to_account_id == account_id:
            incoming += minor
        if tx.from_account_id == account_id:
            outgoing += minor
    return incoming, outgoing


def ledger_balance(account_id, as_of=None) -> float:
    """Reproducible calculated balance from opening baseline plus active ledger."""
    account = db.session.get(Account, int(account_id)) if account_id else None
    if account is None:
        raise ValueError("Account not found.")
    incoming_all, outgoing_all = _transaction_sums(account.id)
    if account.opening_balance_minor is not None:
        opening = int(account.opening_balance_minor)
    elif account.opening_balance is not None:
        opening = to_minor(account.opening_balance)
    else:
        # Safe inference for rows created before opening-balance metadata exists.
        opening = _account_minor(account) - incoming_all + outgoing_all
    if as_of is None:
        incoming, outgoing = incoming_all, outgoing_all
    else:
        incoming, outgoing = _transaction_sums(account.id, through=as_of)
    return float(from_minor(opening + incoming - outgoing))


def _request_metadata():
    try:
        from flask import has_request_context, request, session
        if has_request_context():
            ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or request.remote_addr
            return ip, session.get("login_sid") or session.get("_id")
    except Exception:
        pass
    return None, None


def reconcile_account(*, account_id, actual_balance, reconciliation_date=None, note="", actor=None):
    """Finalise one immutable account closing and post its transparent adjustment."""
    from app.services.accounting import _apply_account_tx_effect
    from app.services.time_money import pk_now, pk_today

    account = db.session.get(Account, int(account_id)) if account_id else None
    if account is None:
        raise ValueError("Account not found.")
    if getattr(account, "is_active", True) is False:
        raise ValueError("Archived accounts cannot be newly reconciled.")

    rec_date = reconciliation_date or pk_today()
    if isinstance(rec_date, str):
        try:
            rec_date = datetime.strptime(rec_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("Invalid reconciliation date.") from exc
    if rec_date > pk_today():
        raise ValueError("Reconciliation date cannot be in the future.")
    actual_minor = to_minor(actual_balance, field="Actual balance")

    latest_any = _latest_reconciliation(account.id)
    if latest_any and rec_date < latest_any.reconciliation_date:
        raise ValueError(
            f"A later reconciliation already exists on {latest_any.reconciliation_date:%Y-%m-%d}; "
            "historical reconciliations are immutable."
        )

    previous = AccountReconciliation.query.filter(
        AccountReconciliation.account_id == account.id,
        AccountReconciliation.reconciliation_date <= rec_date,
    ).order_by(AccountReconciliation.reconciliation_date.desc(), AccountReconciliation.id.desc()).first()

    now = pk_now()
    period_end = now if rec_date == pk_today() else datetime.combine(rec_date, time.max)
    if previous:
        opening_minor = (
            int(previous.final_reconciled_balance_minor)
            if previous.final_reconciled_balance_minor is not None
            else to_minor(previous.final_reconciled_balance or previous.actual_balance or 0)
        )
        start_at = _period_end(previous)
        incoming, outgoing = _transaction_sums(account.id, after=start_at, through=period_end)
        previous_minor = opening_minor
    else:
        incoming_all, outgoing_all = _transaction_sums(account.id)
        if account.opening_balance_minor is not None:
            opening_minor = int(account.opening_balance_minor)
        elif account.opening_balance is not None:
            opening_minor = to_minor(account.opening_balance)
        else:
            opening_minor = _account_minor(account) - incoming_all + outgoing_all
        start_at = account.opening_balance_date or account.created_at
        incoming, outgoing = _transaction_sums(account.id, through=period_end)
        previous_minor = opening_minor

    expected_minor = opening_minor + incoming - outgoing
    difference_minor = actual_minor - expected_minor
    difference_type = "Matched" if difference_minor == 0 else ("Loss" if difference_minor < 0 else "Excess")
    ip_address, session_id = _request_metadata()

    rec = AccountReconciliation(
        account_id=account.id,
        previous_reconciliation_id=(previous.id if previous else None),
        reconciliation_date=rec_date,
        period_start_at=start_at,
        period_end_at=period_end,
        previous_balance=float(from_minor(previous_minor)),
        opening_balance=float(from_minor(opening_minor)),
        transaction_in=float(from_minor(incoming)),
        transaction_out=float(from_minor(outgoing)),
        transaction_net=float(from_minor(incoming - outgoing)),
        expected_balance=float(from_minor(expected_minor)),
        actual_balance=float(from_minor(actual_minor)),
        difference=float(from_minor(difference_minor)),
        adjustment_amount=float(from_minor(difference_minor)),
        final_reconciled_balance=float(from_minor(actual_minor)),
        previous_balance_minor=previous_minor,
        opening_balance_minor=opening_minor,
        transaction_in_minor=incoming,
        transaction_out_minor=outgoing,
        transaction_net_minor=incoming - outgoing,
        expected_balance_minor=expected_minor,
        actual_balance_minor=actual_minor,
        difference_minor=difference_minor,
        final_reconciled_balance_minor=actual_minor,
        difference_type=difference_type,
        status="Reconciled",
        note=(note or "").strip() or None,
        created_by_id=getattr(actor, "id", None) if actor else None,
        created_by=_actor(actor),
        created_ip=ip_address,
        session_id=str(session_id)[:80] if session_id else None,
        created_at=now,
        updated_at=now,
    )
    db.session.add(rec)
    db.session.flush()

    if difference_minor:
        tx = AccountTransaction(
            from_account_id=(account.id if difference_minor < 0 else None),
            to_account_id=(account.id if difference_minor > 0 else None),
            amount=float(from_minor(abs(difference_minor))),
            amount_minor=abs(difference_minor),
            description=(
                f"Reconciliation shortage / loss ({account.name})"
                if difference_minor < 0 else f"Reconciliation profit / excess ({account.name})"
            ),
            note=(
                f"[RECON:{rec.id}] actual={float(from_minor(actual_minor)):.2f}, "
                f"expected={float(from_minor(expected_minor)):.2f}, "
                f"difference={float(from_minor(difference_minor)):.2f}"
                + (f" | {rec.note}" if rec.note else "")
            ),
            transaction_type=("Reconciliation Loss" if difference_minor < 0 else "Reconciliation Excess"),
            source_type="AccountReconciliation",
            source_id=rec.id,
            reconciliation_id=rec.id,
            created_by=_actor(actor),
            date_posted=period_end,
        )
        db.session.add(tx)
        db.session.flush()
        _apply_account_tx_effect(tx)
        rec.adjustment_transaction_id = tx.id

    db.session.flush()
    record_accounting_audit(
        actor, action="Reconcile", entity_type="AccountReconciliation", entity_id=rec.id,
        before={"account_id": account.id, "calculated_balance": float(from_minor(expected_minor))},
        after={
            "account_id": account.id,
            "previous_balance": rec.previous_balance,
            "opening_balance": rec.opening_balance,
            "transaction_in": rec.transaction_in,
            "transaction_out": rec.transaction_out,
            "expected_balance": rec.expected_balance,
            "actual_balance": rec.actual_balance,
            "difference": rec.difference,
            "difference_type": rec.difference_type,
            "final_reconciled_balance": rec.final_reconciled_balance,
            "adjustment_transaction_id": rec.adjustment_transaction_id,
        },
        amount_before=rec.expected_balance, amount_after=rec.final_reconciled_balance,
        account_before_id=account.id, account_after_id=account.id, reason=rec.note,
    )
    return rec
