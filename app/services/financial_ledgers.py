"""Authoritative, non-destructive financial ledger projections.

The application has historically had several screens which each calculated a
balance from a slightly different subset of the transaction tables.  This
module is the shared read-side accounting projection used by current payables
and the three party ledgers.

Nothing in this module mutates source rows.  Bills, sales, payments, GRNs and
settlements remain the source of truth; the objects returned here are ordinary
serialisable dictionaries intended for reports and templates.

Conventions
-----------

* A positive client balance means the client owes the business.
* A positive supplier balance means the business owes the supplier.
* A positive delivery-person balance means the business owes the driver.
* Client movements use ``debit - credit``.
* Supplier and delivery-person movements use the same columns (debit/credit)
  and are reported with the domain convention ``credit - debit`` because a GRN
  or delivery rent is a credit to the counter-party payable account.

Date filtering on the consolidated current-payables report deliberately uses
``last_transaction_date``.  The amount is the complete current balance, so a
filter must not silently remove individual contributing bills and display a
misleading partial balance.  Detail ledgers filter movements and add a
carry-forward row when a start date is selected.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
import re

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, selectinload

from models import (
    AccountTransaction,
    Booking,
    BookingItem,
    Client,
    DeliveryPerson,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    Entry,
    GRN,
    PendingBill,
    Payment,
    SaleDeliveryPerson,
    Supplier,
    SupplierPayment,
    WaiveOff,
)
from app.services.grn_svc import calculate_grn_total
from app.services.time_money import pk_now

CENT = Decimal("0.01")
EPS = Decimal("0.005")


def _decimal(value) -> Decimal:
    """Convert a legacy float/string safely to a two-decimal Decimal."""
    if value is None or value == "":
        return Decimal("0.00")
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def _float(value) -> float:
    return float(_decimal(value))


def _norm(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _parse_dt(value, time_value=None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raw = str(value or "").strip()
    if time_value:
        raw = f"{raw} {str(time_value).strip()}".strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except (TypeError, ValueError):
            continue
    return datetime.min


def _date_arg(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _display_dt(value) -> str:
    dt = _parse_dt(value)
    return "" if dt == datetime.min else dt.strftime("%Y-%m-%d %H:%M")


def _bill_ref(obj, *names, fallback="") -> str:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return fallback


def _source_key(source_type, source_id) -> str:
    return f"{source_type}:{source_id}" if source_id is not None else ""


def _row(
    *, date_value, row_type, reference="", description="", debit=0, credit=0,
    source_type=None, source_id=None, party_id=None, is_bill=False,
    note="", account="", related_account=None, source=None,
):
    debit_d = _decimal(debit)
    credit_d = _decimal(credit)
    return {
        "date": _parse_dt(date_value),
        "date_display": _display_dt(date_value),
        "type": row_type or "Transaction",
        "reference": reference or "",
        "ref": reference or "",
        "bill_no": reference or "",
        "description": description or row_type or "Transaction",
        "debit": _float(debit_d),
        "credit": _float(credit_d),
        "balance": 0.0,
        "source_type": source_type,
        "source_id": source_id,
        "party_id": party_id,
        "is_bill": bool(is_bill),
        "note": note or "",
        "account": account or "",
        "related_account": related_account or "",
        "source": source,
    }


def _sort_rows(rows, *, supplier=False):
    priorities = {
        "OPENING": 0,
        "Opening": 0,
        "Carry Forward": 0,
        "Booking": 10,
        "Direct Sale": 10,
        "Pending Bill": 10,
        "GRN": 10,
        "Delivery Rent": 10,
        "Payment": 20,
        "Receipt": 20,
        "Refund": 20,
        "Material Return": 20,
        "Waive-Off": 21,
        "Adjustment": 22,
        "Booking Cancel": 30,
    }
    rows.sort(key=lambda r: (
        r.get("date") if r.get("date") != datetime.min else datetime.min,
        priorities.get(r.get("type"), 15),
        int(r.get("source_id") or 0),
        r.get("reference") or "",
    ))
    return rows


def _apply_running_balance(rows, *, convention="client"):
    balance = Decimal("0.00")
    for row in rows:
        debit = _decimal(row.get("debit"))
        credit = _decimal(row.get("credit"))
        if convention == "client":
            balance += debit - credit
        else:
            balance += credit - debit
        if abs(balance) < EPS:
            balance = Decimal("0.00")
        row["debit"] = _float(debit)
        row["credit"] = _float(credit)
        row["balance"] = _float(balance)
    return _float(balance)


def _client_maps():
    """Return stable identity maps without changing legacy names."""
    clients = Client.query.order_by(Client.name.asc(), Client.id.asc()).all()
    by_id = {c.id: c for c in clients}
    by_code = {}
    by_name = {}
    for client in clients:
        if _norm(client.code):
            current = by_code.get(_norm(client.code))
            if current is None or (bool(getattr(client, 'is_active', False)) and not bool(getattr(current, 'is_active', False))):
                by_code[_norm(client.code)] = client
        if _norm(client.name):
            current = by_name.get(_norm(client.name))
            if current is None or (bool(getattr(client, 'is_active', False)) and not bool(getattr(current, 'is_active', False))):
                by_name[_norm(client.name)] = client
    return clients, by_id, by_code, by_name


def _resolve_client(value, *, by_id, by_code, by_name):
    """Resolve a source row to a Client using id, code, then historical name."""
    client_id = getattr(value, "client_id", None)
    if client_id and client_id in by_id:
        return by_id[client_id]
    code = getattr(value, "client_code", None)
    if code and _norm(code) in by_code:
        return by_code[_norm(code)]
    name = getattr(value, "client_name", None)
    if name is None:
        name = getattr(value, "client", None)
    if name and _norm(name) in by_name:
        return by_name[_norm(name)]
    return None


def _resolve_pending_client(value, *, by_code, by_name):
    code = getattr(value, "client_code", None)
    if code and _norm(code) in by_code:
        return by_code[_norm(code)]
    name = getattr(value, "client_name", None)
    return by_name.get(_norm(name)) if name else None


def _cancel_rate_lookup():
    """Bulk-load the legacy cancellation rate resolver's source rows.

    The old display helper queried the newest matching booking and then its
    newest matching item for every cancellation.  Besides being an N+1 query
    pattern, the helper is called more than once while one ledger is built.
    This index preserves that exact newest-booking/newest-item precedence.
    """
    latest_booking = {}
    booking_rows = Booking.query.with_entities(
        Booking.id,
        Booking.client_name,
        Booking.manual_bill_no,
        Booking.auto_bill_no,
    ).order_by(Booking.id.desc()).all()
    for booking_id, client_name, manual_bill_no, auto_bill_no in booking_rows:
        client_key = _norm(client_name)
        for bill_ref in (manual_bill_no, auto_bill_no):
            bill_key = str(bill_ref or "").strip()
            if client_key and bill_key:
                latest_booking.setdefault((client_key, bill_key), booking_id)

    relevant_ids = set(latest_booking.values())
    rates = {}
    if relevant_ids:
        item_rows = BookingItem.query.with_entities(
            BookingItem.id,
            BookingItem.booking_id,
            BookingItem.material_name,
            BookingItem.price_at_time,
        ).filter(BookingItem.booking_id.in_(relevant_ids)).order_by(BookingItem.id.desc()).all()
        for _item_id, booking_id, material_name, rate in item_rows:
            material_key = _norm(material_name)
            if material_key:
                rates.setdefault((booking_id, material_key), _decimal(rate))
    return {"latest_booking": latest_booking, "rates": rates, "amounts": {}}


def _client_snapshot():
    """Load all read-side source rows once for the payables report.

    This avoids one query per client on a page containing hundreds of clients.
    The snapshot is deliberately request-scoped: callers can pass it through
    summary functions and no data is cached across requests.  Relationships
    read while projecting rows are eager-loaded to keep this promise true.
    """
    clients, by_id, by_code, by_name = _client_maps()
    groups = {
        "bookings": defaultdict(list),
        "sales": defaultdict(list),
        "payments": defaultdict(list),
        "pending": defaultdict(list),
        "waives": defaultdict(list),
        "cancels": defaultdict(list),
    }
    unresolved = defaultdict(lambda: {"name": "", "code": "", "rows": []})

    def add(kind, obj, *, client=None, fallback_name=None, fallback_code=None):
        if client:
            groups[kind][client.id].append(obj)
            return
        key_name = _norm(fallback_name or getattr(obj, "client_name", None) or getattr(obj, "client", None))
        key = f"unresolved:{key_name or 'unknown'}"
        unresolved[key]["name"] = fallback_name or getattr(obj, "client_name", None) or getattr(obj, "client", None) or "Unlinked client"
        unresolved[key]["code"] = fallback_code or getattr(obj, "client_code", None) or ""
        unresolved[key]["rows"].append((kind, obj))

    for obj in Booking.query.filter(Booking.is_void == False).all():
        add("bookings", obj, client=_resolve_client(obj, by_id=by_id, by_code=by_code, by_name=by_name))
    for obj in DirectSale.query.options(joinedload(DirectSale.invoice)).filter(DirectSale.is_void == False).all():
        add("sales", obj, client=_resolve_client(obj, by_id=by_id, by_code=by_code, by_name=by_name))
    for obj in Payment.query.options(joinedload(Payment.payment_account)).filter(Payment.is_void == False).all():
        add("payments", obj, client=_resolve_client(obj, by_id=by_id, by_code=by_code, by_name=by_name))
    for obj in PendingBill.query.filter(PendingBill.is_void == False).all():
        add("pending", obj, client=_resolve_pending_client(obj, by_code=by_code, by_name=by_name))
    for obj in WaiveOff.query.filter(WaiveOff.is_void == False).all():
        # Direct-sale discount rows are already represented by
        # DirectSale.discount and the source discount movement; the legacy
        # marker row is an audit mirror, not a second credit.
        if _norm(getattr(obj, "note", None)).startswith("[direct_sale_discount:"):
            continue
        client = by_name.get(_norm(getattr(obj, "client_name", None)))
        if not client and getattr(obj, "client_code", None):
            client = by_code.get(_norm(obj.client_code))
        add("waives", obj, client=client)
    for obj in Entry.query.filter(Entry.type == "CANCEL", Entry.is_void == False).all():
        client = by_code.get(_norm(getattr(obj, "client_code", None))) or by_name.get(_norm(getattr(obj, "client", None)))
        add("cancels", obj, client=client, fallback_name=getattr(obj, "client", None), fallback_code=getattr(obj, "client_code", None))

    return {
        "clients": clients,
        "by_id": by_id,
        "by_code": by_code,
        "by_name": by_name,
        "groups": groups,
        "unresolved": unresolved,
        "cancel_lookup": _cancel_rate_lookup(),
    }


def _client_snapshot_for(client):
    """Request-scoped snapshot for a single client identity.

    Produces the same shape as :func:`_client_snapshot` but only loads the
    source rows that can possibly belong to this client (matched by id, code
    or name), so a per-client financial summary no longer scans every client's
    bookings/sales/payments/pending/waive/cancel rows.

    Attribution parity is guaranteed by re-running the exact same resolver
    helpers used by the full snapshot; the SQL predicates are only a cheap
    superset filter (token-ordered ``LIKE`` for names) and false positives are
    discarded by the resolver.
    """
    clients, by_id, by_code, by_name = _client_maps()
    groups = {
        "bookings": defaultdict(list),
        "sales": defaultdict(list),
        "payments": defaultdict(list),
        "pending": defaultdict(list),
        "waives": defaultdict(list),
        "cancels": defaultdict(list),
    }
    unresolved = defaultdict(lambda: {"name": "", "code": "", "rows": []})

    norm_name = _norm(getattr(client, "name", None))
    norm_code = _norm(getattr(client, "code", None))
    same_name_ids = [
        c.id for c in clients
        if _norm(getattr(c, "name", None)) == norm_name
    ] or [client.id]
    same_name_id_set = set(same_name_ids)

    def name_clause(attr):
        tokens = norm_name.split() if norm_name else []
        if not tokens:
            return None
        pattern = "%" + "%".join(tokens) + "%"
        return func.lower(func.trim(attr)).like(pattern)

    def add_by_resolver(kind, obj, resolved_client):
        if resolved_client is not None and resolved_client.id in same_name_id_set:
            groups[kind][client.id].append(obj)
        else:
            key_name = _norm(getattr(obj, "client_name", None) or getattr(obj, "client", None))
            key = f"unresolved:{key_name or 'unknown'}"
            unresolved[key]["name"] = getattr(obj, "client_name", None) or getattr(obj, "client", None) or "Unlinked client"
            unresolved[key]["code"] = getattr(obj, "client_code", None) or ""
            unresolved[key]["rows"].append((kind, obj))

    # Bookings carry only a name.
    bq = Booking.query.filter(Booking.is_void == False)
    b_name = name_clause(Booking.client_name)
    if b_name is not None:
        bq = bq.filter(b_name)
    for obj in bq.all():
        add_by_resolver("bookings", obj, _resolve_client(obj, by_id=by_id, by_code=by_code, by_name=by_name))

    # Sales carry a code and a name.
    sq = DirectSale.query.filter(DirectSale.is_void == False)
    s_clauses = []
    s_name = name_clause(DirectSale.client_name)
    if s_name is not None:
        s_clauses.append(s_name)
    if norm_code:
        s_clauses.append(func.lower(func.trim(DirectSale.client_code)) == norm_code)
    if s_clauses:
        sq = sq.filter(or_(*s_clauses))
    for obj in sq.options(joinedload(DirectSale.invoice)).all():
        add_by_resolver("sales", obj, _resolve_client(obj, by_id=by_id, by_code=by_code, by_name=by_name))

    # Payments carry a client_id and a name.
    pq = Payment.query.options(joinedload(Payment.payment_account)).filter(Payment.is_void == False)
    p_clauses = [Payment.client_id.in_(same_name_ids)]
    p_name = name_clause(Payment.client_name)
    if p_name is not None:
        p_clauses.append(p_name)
    pq = pq.filter(or_(*p_clauses))
    for obj in pq.all():
        add_by_resolver("payments", obj, _resolve_client(obj, by_id=by_id, by_code=by_code, by_name=by_name))

    # Pending bills carry a code and a name.
    pbq = PendingBill.query.filter(PendingBill.is_void == False)
    pb_clauses = []
    pb_name = name_clause(PendingBill.client_name)
    if pb_name is not None:
        pb_clauses.append(pb_name)
    if norm_code:
        pb_clauses.append(func.lower(func.trim(PendingBill.client_code)) == norm_code)
    if pb_clauses:
        pbq = pbq.filter(or_(*pb_clauses))
    for obj in pbq.all():
        add_by_resolver("pending", obj, _resolve_pending_client(obj, by_code=by_code, by_name=by_name))

    # Waive-off rows: skip the DirectSale.discount audit mirrors.
    wq = WaiveOff.query.filter(WaiveOff.is_void == False)
    wq = wq.filter(~func.lower(func.coalesce(WaiveOff.note, '')).like('[direct_sale_discount:%'))
    w_clauses = []
    w_name = name_clause(WaiveOff.client_name)
    if w_name is not None:
        w_clauses.append(w_name)
    if norm_code:
        w_clauses.append(func.lower(func.trim(WaiveOff.client_code)) == norm_code)
    if w_clauses:
        wq = wq.filter(or_(*w_clauses))
    for obj in wq.all():
        resolved = by_name.get(_norm(getattr(obj, "client_name", None)))
        if not resolved and getattr(obj, "client_code", None):
            resolved = by_code.get(_norm(obj.client_code))
        add_by_resolver("waives", obj, resolved)

    # Booking cancellations are Entry rows of type CANCEL.
    cq = Entry.query.filter(Entry.type == "CANCEL", Entry.is_void == False)
    c_clauses = []
    c_name = name_clause(Entry.client)
    if c_name is not None:
        c_clauses.append(c_name)
    if norm_code:
        c_clauses.append(func.lower(func.trim(Entry.client_code)) == norm_code)
    if c_clauses:
        cq = cq.filter(or_(*c_clauses))
    for obj in cq.all():
        resolved = by_code.get(_norm(getattr(obj, "client_code", None))) or by_name.get(_norm(getattr(obj, "client", None)))
        add_by_resolver("cancels", obj, resolved)

    return {
        "clients": clients,
        "by_id": by_id,
        "by_code": by_code,
        "by_name": by_name,
        "groups": groups,
        "unresolved": unresolved,
        "cancel_lookup": _cancel_rate_lookup(),
    }


def _record_refs(obj, kind) -> set[str]:
    refs = set()
    if kind == "booking":
        values = (getattr(obj, "manual_bill_no", None), getattr(obj, "auto_bill_no", None), f"BK-{getattr(obj, 'id', '')}")
    elif kind == "sale":
        invoice = getattr(obj, "invoice", None)
        values = (
            getattr(obj, "manual_bill_no", None),
            getattr(obj, "auto_bill_no", None),
            getattr(invoice, "invoice_no", None),
            f"DS-{getattr(obj, 'id', '')}",
            f"UNBILLED-{getattr(obj, 'id', '')}",
            f"CSH-{getattr(obj, 'id', '')}",
        )
    else:
        values = (getattr(obj, "bill_no", None), getattr(obj, "source_bill_no", None))
    for value in values:
        if value is not None and str(value).strip():
            refs.add(_norm(value))
    return refs


def _is_derived_pending(pb, known_refs: set[str]) -> bool:
    source_table = _norm(getattr(pb, "source_table", None))
    source_module = _norm(getattr(pb, "source_module", None))
    if source_table in {"booking", "direct_sale"}:
        return True
    if source_table == "invoice" and (
        getattr(pb, "source_id", None) is not None or _norm(getattr(pb, "bill_no", None)) in known_refs
    ):
        return True
    reason = _norm(getattr(pb, "reason", None))
    if reason.startswith("payment received"):
        return True
    if reason.startswith("direct sale") or reason.startswith("booking"):
        return _norm(getattr(pb, "bill_no", None)) in known_refs
    return _norm(getattr(pb, "bill_no", None)) in known_refs


def _cancel_amount(entry, client_name_norm: str, *, snapshot=None) -> Decimal:
    """Resolve legacy cancellation amount without making an entry up."""
    note = getattr(entry, "note", None) or ""
    lookup = (snapshot or {}).get("cancel_lookup")
    entry_id = getattr(entry, "id", None)
    if lookup is not None and entry_id in lookup["amounts"]:
        return lookup["amounts"][entry_id]

    # Match the historical resolver's exact precedence without issuing two
    # queries per cancellation: newest matching booking item rate, note rate,
    # then note amount.
    value = None
    try:
        qty = _decimal(getattr(entry, "qty", 0))
        bill_ref = str(getattr(entry, "bill_no", None) or getattr(entry, "auto_bill_no", None) or "").strip()
        material_key = _norm(getattr(entry, "material", None) or getattr(entry, "booked_material", None))
        if lookup is not None and qty > 0 and bill_ref and material_key:
            booking_id = lookup["latest_booking"].get((client_name_norm, bill_ref))
            rate = lookup["rates"].get((booking_id, material_key)) if booking_id else None
            if rate is not None and rate > 0:
                value = rate * qty
    except Exception:
        value = None
    if value is not None:
        result = max(Decimal("0.00"), _decimal(value))
        if lookup is not None and entry_id is not None:
            lookup["amounts"][entry_id] = result
        return result
    match = re.search(r"(?:rate|price)\s*=\s*([-+]?\d+(?:\.\d+)?)", note, re.I)
    if match:
        result = max(Decimal("0.00"), _decimal(match.group(1)) * _decimal(getattr(entry, "qty", 0)))
    else:
        match = re.search(r"(?:amount|value)\s*=\s*([-+]?\d+(?:\.\d+)?)", note, re.I)
        result = max(Decimal("0.00"), _decimal(match.group(1))) if match else Decimal("0.00")
    # An unresolved cancellation remains visible with zero financial effect;
    # never invent an amount from unrelated bills.
    if lookup is not None and entry_id is not None:
        lookup["amounts"][entry_id] = result
    return result


def _make_client_obligations(client, *, snapshot=None, allocate_remaining=True):
    snapshot = snapshot or _client_snapshot()
    groups = snapshot["groups"]
    client_id = client.id
    # Historical imports can contain duplicate Client master rows with the
    # same name while old source tables retain only a name.  Treat that name
    # as one financial identity for the ledger so a secondary master row does
    # not make bills disappear (or create a second payable).
    same_name_ids = [
        other.id for other in snapshot.get("clients", [])
        if _norm(getattr(other, "name", None)) == _norm(getattr(client, "name", None))
    ] or [client_id]

    def merged(kind):
        result = []
        seen_ids = set()
        for source_id in same_name_ids:
            for obj in groups[kind].get(source_id, []):
                obj_id = getattr(obj, "id", None)
                if obj_id not in seen_ids:
                    result.append(obj)
                    seen_ids.add(obj_id)
        return result

    bookings = merged("bookings")
    sales = merged("sales")
    payments = merged("payments")
    pending = merged("pending")
    waives = merged("waives")
    cancels = merged("cancels")

    obligations = []
    known_refs = set()
    cancel_by_ref = defaultdict(lambda: Decimal("0.00"))
    for entry in cancels:
        amount = _cancel_amount(entry, _norm(client.name), snapshot=snapshot)
        ref = _norm(_bill_ref(entry, "bill_no", "auto_bill_no"))
        if ref and amount > 0:
            cancel_by_ref[ref] += amount

    for booking in bookings:
        refs = _record_refs(booking, "booking")
        known_refs |= refs
        reference = _bill_ref(booking, "manual_bill_no", "auto_bill_no", fallback=f"BK-{booking.id}")
        cancel_credit = sum((cancel_by_ref.get(ref, Decimal("0.00")) for ref in refs), Decimal("0.00"))
        gross = _decimal(getattr(booking, "amount", 0)) + cancel_credit
        embedded_paid = _decimal(getattr(booking, "paid_amount", 0))
        embedded_discount = _decimal(getattr(booking, "discount", 0))
        # Match the legacy-safe booking rule: when a clearly broken legacy
        # row stores less than paid and has a discount, lift the debit to the
        # paid+discount amount, but never lift a cancelled bill a second time.
        if cancel_credit <= 0 and embedded_discount > 0 and gross < embedded_paid:
            gross = embedded_paid + embedded_discount
        embedded_credit = embedded_paid + embedded_discount
        # A booking can be an advance with amount=0 and paid_amount>0.  It is
        # still a real credit movement and must not disappear from the ledger.
        if gross <= 0 and embedded_credit <= 0:
            continue
        obligations.append({
            "key": _source_key("Booking", booking.id),
            "source_type": "Booking",
            "source_id": booking.id,
            "date": _parse_dt(getattr(booking, "date_posted", None)),
            "reference": reference,
            "description": "Booking",
            "gross": gross,
            "embedded_credit": max(Decimal("0.00"), embedded_credit),
            "embedded_paid": max(Decimal("0.00"), embedded_paid),
            "embedded_discount": max(Decimal("0.00"), embedded_discount),
            "source": booking,
        })
    for sale in sales:
        refs = _record_refs(sale, "sale")
        known_refs |= refs
        gross = _decimal(getattr(sale, "amount", 0))
        embedded_paid = _decimal(getattr(sale, "paid_amount", 0))
        embedded_discount = _decimal(getattr(sale, "discount", 0))
        embedded_credit = embedded_paid + embedded_discount
        # Preserve a zero-value sale carrying an advance/payment credit.
        if gross <= 0 and embedded_credit <= 0:
            continue
        obligations.append({
            "key": _source_key("DirectSale", sale.id),
            "source_type": "DirectSale",
            "source_id": sale.id,
            "date": _parse_dt(getattr(sale, "date_posted", None)),
            "reference": _bill_ref(sale, "manual_bill_no", "auto_bill_no", fallback=f"DS-{sale.id}"),
            "description": "Direct Sale",
            "gross": gross,
            "embedded_credit": max(Decimal("0.00"), embedded_credit),
            "embedded_paid": max(Decimal("0.00"), embedded_paid),
            "embedded_discount": max(Decimal("0.00"), embedded_discount),
            "source": sale,
        })

    # PendingBill is a legacy/derived projection.  It is included only when it
    # is not traceable to a real Booking/DirectSale/Invoice source, preventing
    # the common ghost/double-entry problem while preserving manual history.
    for pb in pending:
        amount = _decimal(getattr(pb, "amount", 0))
        if amount <= 0 or bool(getattr(pb, "is_paid", False)):
            continue
        if _is_derived_pending(pb, known_refs):
            continue
        obligations.append({
            "key": _source_key("PendingBill", pb.id),
            "source_type": "PendingBill",
            "source_id": pb.id,
            "date": _parse_dt(getattr(pb, "created_at", None)),
            "reference": _bill_ref(pb, "bill_no", "nimbus_no", fallback=f"PB-{pb.id}"),
            "description": getattr(pb, "reason", None) or "Pending Bill",
            "gross": amount,
            "embedded_credit": Decimal("0.00"),
            "source": pb,
        })

    obligations.sort(key=lambda r: (r["date"], int(r["source_id"] or 0)))

    # Linked waive rows replace the legacy Payment.discount field when they
    # exist.  This is the same de-duplication rule used by the old ledger.
    linked_waives = defaultdict(list)
    standalone_waives = []
    for waive in waives:
        if getattr(waive, "payment_id", None):
            linked_waives[waive.payment_id].append(waive)
        else:
            standalone_waives.append(waive)

    external_credits = []
    payment_movements = []
    for payment in payments:
        amount = _decimal(getattr(payment, "amount", 0))
        payment_type = _norm(getattr(payment, "payment_type", None))
        if payment_type in {"refund", "repayment"} and amount > 0:
            amount = -amount
        reference = _bill_ref(payment, "manual_bill_no", "auto_bill_no", fallback=f"PAY-{payment.id}")
        p_type = "Refund" if amount < 0 else ("Material Return" if payment_type == "material return" else "Payment")
        payment_movements.append((payment, amount, reference, p_type))
        if amount > 0:
            external_credits.append({
                "date": _parse_dt(getattr(payment, "date_posted", None)),
                "amount": amount,
                "source_type": "Payment",
                "source_id": payment.id,
                "reference": reference,
            })
        linked = linked_waives.get(payment.id)
        discount = (
            sum((_decimal(getattr(w, "amount", 0)) for w in linked), Decimal("0.00"))
            if linked else _decimal(getattr(payment, "discount", 0))
        )
        if discount > 0:
            external_credits.append({
                "date": _parse_dt(getattr(payment, "date_posted", None)),
                "amount": discount,
                "source_type": "WaiveOff",
                "source_id": payment.id,
                "reference": reference,
            })

    for waive in standalone_waives:
        amount = _decimal(getattr(waive, "amount", 0))
        if amount > 0:
            external_credits.append({
                "date": _parse_dt(getattr(waive, "date_posted", None)),
                "amount": amount,
                "source_type": "WaiveOff",
                "source_id": waive.id,
                "reference": _bill_ref(waive, "bill_no", fallback=f"WO-{waive.id}"),
            })

    for entry in cancels:
        amount = _cancel_amount(entry, _norm(client.name), snapshot=snapshot)
        if amount > 0:
            external_credits.append({
                "date": _parse_dt(getattr(entry, "date", None), getattr(entry, "time", None)),
                "amount": amount,
                "source_type": "Entry",
                "source_id": entry.id,
                "reference": _bill_ref(entry, "bill_no", "auto_bill_no", fallback=f"CANCEL-{entry.id}"),
            })

    # The bill allocation view is FIFO and non-destructive.  The actual
    # account balance remains the movement sum below; this allocation is only
    # used to display each bill's remaining amount.
    remaining_by_key = {}
    if allocate_remaining:
        for obligation in obligations:
            remaining_by_key[obligation["key"]] = max(
                Decimal("0.00"), obligation["gross"] - obligation["embedded_credit"]
            )
        for credit in sorted(external_credits, key=lambda r: (r["date"], int(r["source_id"] or 0))):
            remaining = credit["amount"]
            for obligation in obligations:
                if remaining <= 0:
                    break
                key = obligation["key"]
                settle = min(remaining_by_key[key], remaining)
                remaining_by_key[key] -= settle
                remaining -= settle
        for obligation in obligations:
            obligation["remaining"] = _float(remaining_by_key[obligation["key"]])

    return {
        "bookings": bookings,
        "sales": sales,
        "payments": payments,
        "payment_movements": payment_movements,
        "waives": waives,
        "cancels": cancels,
        "obligations": obligations,
        "external_credits": external_credits,
        "remaining_by_key": remaining_by_key,
    }


def build_client_financial_ledger(client, *, snapshot=None):
    """Return the complete client financial ledger and reconciled balance."""
    snapshot = snapshot or _client_snapshot()
    details = _make_client_obligations(client, snapshot=snapshot)
    rows = []
    opening = _decimal(getattr(client, "opening_balance", 0))
    if opening != 0:
        rows.append(_row(
            date_value=getattr(client, "opening_balance_date", None) or getattr(client, "created_at", None),
            row_type="OPENING",
            reference="OPENING",
            description="Opening Balance",
            debit=max(opening, Decimal("0.00")),
            credit=max(-opening, Decimal("0.00")),
            source_type="Client",
            source_id=client.id,
            party_id=client.id,
        ))

    for obligation in details["obligations"]:
        rows.append(_row(
            date_value=obligation["date"],
            row_type=obligation["source_type"],
            reference=obligation["reference"],
            description=obligation["description"],
            debit=obligation["gross"],
            credit=obligation.get("embedded_paid", obligation["embedded_credit"]),
            source_type=obligation["source_type"],
            source_id=obligation["source_id"],
            party_id=client.id,
            is_bill=True,
            note=getattr(obligation["source"], "note", "") or getattr(obligation["source"], "reason", "") or "",
            source=obligation["source"],
        ))
        embedded_discount = obligation.get("embedded_discount", Decimal("0.00"))
        if embedded_discount > 0:
            rows.append(_row(
                date_value=obligation["date"],
                row_type="Waive-Off",
                reference=obligation["reference"],
                description=f"Discount / waive-off ({obligation['source_type']})",
                credit=embedded_discount,
                source_type=obligation["source_type"],
                source_id=obligation["source_id"],
                party_id=client.id,
                source=obligation["source"],
            ))

    linked_waives = defaultdict(list)
    for waive in details["waives"]:
        if getattr(waive, "payment_id", None):
            linked_waives[waive.payment_id].append(waive)
    for payment, amount, reference, p_type in details["payment_movements"]:
        rows.append(_row(
            date_value=getattr(payment, "date_posted", None),
            row_type=p_type,
            reference=reference,
            description=f"{p_type} ({getattr(payment, 'method', None) or 'Cash'})",
            debit=max(-amount, Decimal("0.00")),
            credit=max(amount, Decimal("0.00")),
            source_type="Payment",
            source_id=payment.id,
            party_id=client.id,
            note=getattr(payment, "note", "") or "",
            account=getattr(getattr(payment, "payment_account", None), "name", "") or "",
            source=payment,
        ))
        discount = (
            sum((_decimal(getattr(w, "amount", 0)) for w in linked_waives.get(payment.id, [])), Decimal("0.00"))
            if linked_waives.get(payment.id) else _decimal(getattr(payment, "discount", 0))
        )
        if discount > 0:
            rows.append(_row(
                date_value=getattr(payment, "date_posted", None),
                row_type="Waive-Off",
                reference=reference,
                description=f"Waive-Off{': ' + (getattr(payment, 'discount_reason', '') or '') if getattr(payment, 'discount_reason', '') else ''}",
                credit=discount,
                source_type="WaiveOff",
                source_id=payment.id,
                party_id=client.id,
                source=payment,
            ))

    for waive in details["waives"]:
        if getattr(waive, "payment_id", None):
            continue
        rows.append(_row(
            date_value=getattr(waive, "date_posted", None),
            row_type="Waive-Off",
            reference=_bill_ref(waive, "bill_no", fallback=f"WO-{waive.id}"),
            description=f"Waive-Off: {getattr(waive, 'reason', '') or 'Adjustment'}",
            credit=getattr(waive, "amount", 0),
            source_type="WaiveOff",
            source_id=waive.id,
            party_id=client.id,
            note=getattr(waive, "note", "") or "",
            source=waive,
        ))

    for entry in details["cancels"]:
        amount = _cancel_amount(entry, _norm(client.name), snapshot=snapshot)
        rows.append(_row(
            date_value=_parse_dt(getattr(entry, "date", None), getattr(entry, "time", None)),
            row_type="Booking Cancel",
            reference=_bill_ref(entry, "bill_no", "auto_bill_no", fallback=f"CANCEL-{entry.id}"),
            description=f"Booking Return/Cancel ({getattr(entry, 'material', None) or getattr(entry, 'booked_material', None) or '-'})",
            credit=amount,
            source_type="Entry",
            source_id=entry.id,
            party_id=client.id,
            source=entry,
        ))

    _sort_rows(rows)
    closing = _apply_running_balance(rows, convention="client")
    total_debit = _float(sum((_decimal(r["debit"]) for r in rows), Decimal("0.00")))
    total_credit = _float(sum((_decimal(r["credit"]) for r in rows), Decimal("0.00")))
    non_opening_dates = [r["date"] for r in rows if r.get("type") not in {"OPENING", "Opening"} and r.get("date") != datetime.min]
    payment_dates = [
        r["date"] for r in rows if r.get("type") in {"Payment", "Material Return", "Refund"} and r.get("date") != datetime.min
    ]
    payment_dates.extend(
        obligation["date"] for obligation in details["obligations"]
        if obligation.get("embedded_paid", Decimal("0.00")) > 0 and obligation.get("date") != datetime.min
    )
    return {
        "entity": client,
        "entity_type": "client",
        "rows": rows,
        "obligations": details["obligations"],
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": closing,
        "last_transaction_date": max(non_opening_dates) if non_opening_dates else None,
        "last_payment_date": max(payment_dates) if payment_dates else None,
        "status": "Outstanding" if closing > _float(EPS) else ("Credit" if closing < -_float(EPS) else "Settled"),
    }


def _build_client_financial_summary(client, *, snapshot):
    """Project the fields needed by grouped payables without materialising rows.

    This is algebraically equivalent to ``build_client_financial_ledger``:
    obligations are debits, embedded credits/external credits are credits, and
    refunds are negative payment movements (therefore debits).  Full detail
    ledgers continue to use the row builder as their authoritative view.
    """
    details = _make_client_obligations(client, snapshot=snapshot, allocate_remaining=False)
    balance = _decimal(getattr(client, "opening_balance", 0))
    for obligation in details["obligations"]:
        balance += obligation["gross"] - obligation["embedded_credit"]
    balance -= sum((credit["amount"] for credit in details["external_credits"]), Decimal("0.00"))
    # Positive payment movements are already present in external_credits.
    # Negative movements (refunds/repayments) are deliberately not, so add
    # their inverse effect here.
    for _payment, amount, _reference, _ptype in details["payment_movements"]:
        if amount < 0:
            balance -= amount
    if abs(balance) < EPS:
        balance = Decimal("0.00")

    tx_dates = []
    payment_dates = []
    tx_dates.extend(o["date"] for o in details["obligations"] if o["date"] != datetime.min)
    for payment, _amount, _reference, _ptype in details["payment_movements"]:
        dt = _parse_dt(getattr(payment, "date_posted", None))
        if dt != datetime.min:
            tx_dates.append(dt)
            payment_dates.append(dt)
    for waive in details["waives"]:
        dt = _parse_dt(getattr(waive, "date_posted", None))
        if dt != datetime.min:
            tx_dates.append(dt)
    for entry in details["cancels"]:
        dt = _parse_dt(getattr(entry, "date", None), getattr(entry, "time", None))
        if dt != datetime.min:
            tx_dates.append(dt)
    payment_dates.extend(
        obligation["date"] for obligation in details["obligations"]
        if obligation.get("embedded_paid", Decimal("0.00")) > 0 and obligation["date"] != datetime.min
    )
    closing = _float(balance)
    return {
        "entity": client,
        "entity_type": "client",
        "closing_balance": closing,
        "last_transaction_date": max(tx_dates) if tx_dates else None,
        "last_payment_date": max(payment_dates) if payment_dates else None,
        "status": "Outstanding" if closing > _float(EPS) else ("Credit" if closing < -_float(EPS) else "Settled"),
    }


def _summary_from_ledger(ledger) -> dict:
    entity = ledger["entity"]
    return {
        "client": entity,
        "entity": entity,
        "id": entity.id,
        "client_id": entity.id,
        "client_name": entity.name,
        "client_code": getattr(entity, "code", "") or "",
        "name": entity.name,
        "code": getattr(entity, "code", "") or "",
        "outstanding": max(0.0, float(ledger["closing_balance"] or 0)),
        "balance": float(ledger["closing_balance"] or 0),
        "last_transaction_date": ledger["last_transaction_date"],
        "last_payment_date": ledger["last_payment_date"],
        "status": ledger["status"],
        "ledger": ledger,
    }


def _summary_matches(summary, *, client_filter="", amount_operator="", amount_min=None, amount_max=None,
                     exact_amount=None, start_date=None, end_date=None, status="outstanding") -> bool:
    client_filter = _norm(client_filter)
    if client_filter:
        entity = summary["entity"]
        if client_filter not in _norm(getattr(entity, "name", "")) and client_filter not in _norm(getattr(entity, "code", "")):
            return False
    amount = _decimal(summary["outstanding"])
    minimum = _decimal(amount_min) if amount_min not in (None, "") else None
    maximum = _decimal(amount_max) if amount_max not in (None, "") else None
    exact = _decimal(exact_amount) if exact_amount not in (None, "") else None
    op = (amount_operator or "").strip().lower()
    if exact is not None or op in {"eq", "exact", "="}:
        target = exact if exact is not None else (minimum if minimum is not None else maximum)
        if target is not None and abs(amount - target) >= EPS:
            return False
    elif op in {"gt", "greater", "greater_than"}:
        if maximum is not None and not amount > maximum:
            return False
        if minimum is not None and not amount > minimum:
            return False
    elif op in {"lt", "less", "less_than"}:
        if minimum is not None and not amount < minimum:
            return False
        if maximum is not None and not amount < maximum:
            return False
    else:
        if minimum is not None and amount < minimum:
            return False
        if maximum is not None and amount > maximum:
            return False

    tx_date = summary.get("last_transaction_date")
    tx_day = tx_date.date() if isinstance(tx_date, datetime) else tx_date
    start = _date_arg(start_date)
    end = _date_arg(end_date)
    if start and (tx_day is None or tx_day < start):
        return False
    if end and (tx_day is None or tx_day > end):
        return False

    status = (status or "outstanding").lower()
    balance = _decimal(summary["balance"])
    if status in {"outstanding", "unpaid", "debit"} and balance <= EPS:
        return False
    if status in {"settled", "paid", "zero"} and abs(balance) > EPS:
        return False
    if status in {"credit", "negative"} and balance >= -EPS:
        return False
    return True


def build_current_payables(
    *, client_filter="", amount_operator="", amount_min=None, amount_max=None,
    exact_amount=None, start_date=None, end_date=None, status="outstanding",
    page=1, per_page=25, snapshot=None,
):
    """Build grouped current balances; pagination is applied after grouping."""
    snapshot = snapshot or _client_snapshot()
    summaries = []
    canonical_by_name = snapshot.get("by_name", {})
    for client in snapshot["clients"]:
        # A duplicate master name is one account in the legacy financial
        # model. Keep its first stable master row as the clickable summary;
        # detail URLs for other rows still resolve to the same name projection.
        canonical = canonical_by_name.get(_norm(getattr(client, "name", None)))
        if canonical is not None and canonical.id != client.id:
            continue
        ledger = _build_client_financial_summary(client, snapshot=snapshot)
        summary = _summary_from_ledger(ledger)
        if _summary_matches(
            summary,
            client_filter=client_filter,
            amount_operator=amount_operator,
            amount_min=amount_min,
            amount_max=amount_max,
            exact_amount=exact_amount,
            start_date=start_date,
            end_date=end_date,
            status=status,
        ):
            summaries.append(summary)

    # Keep unmatched historical source rows visible in an explicit audit-only
    # bucket rather than silently dropping data.  A source without a current
    # Client master row cannot be selected as an active payable by default.
    if (status or "outstanding").lower() == "all":
        for key, meta in snapshot["unresolved"].items():
            if not meta["rows"]:
                continue
            # Do not manufacture a financial number for an orphan.  The audit
            # endpoint reports it; current payables remains entity-backed.
            continue

    summaries.sort(key=lambda r: (-float(r["outstanding"] or 0), _norm(r["client_name"]), r["id"]))
    total_outstanding = _float(sum((_decimal(r["outstanding"]) for r in summaries), Decimal("0.00")))
    total = len(summaries)
    per_page = min(max(int(per_page or 25), 1), 200)
    page = max(int(page or 1), 1)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    rows = summaries[start:start + per_page]
    for row in rows:
        row.pop("ledger", None)
    return {
        "rows": rows,
        "all_rows": summaries,
        "total_outstanding": total_outstanding,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "filters": {
            "client": client_filter or "",
            "amount_operator": amount_operator or "",
            "amount_min": amount_min if amount_min is not None else "",
            "amount_max": amount_max if amount_max is not None else "",
            "exact_amount": exact_amount if exact_amount is not None else "",
            "start_date": start_date or "",
            "end_date": end_date or "",
            "status": status or "outstanding",
        },
    }


def filter_ledger_rows(rows, *, start_date=None, end_date=None, type_filter="", query="",
                       amount_min=None, amount_max=None, account_filter="", status_filter=""):
    """Filter detail movements and return a carry-forward-safe list."""
    start = _date_arg(start_date)
    end = _date_arg(end_date)
    type_filter = _norm(type_filter)
    query = _norm(query)
    account_filter = _norm(account_filter)
    status_filter = _norm(status_filter)
    min_amount = _decimal(amount_min) if amount_min not in (None, "") else None
    max_amount = _decimal(amount_max) if amount_max not in (None, "") else None

    def date_ok(row):
        dt = row.get("date")
        if dt == datetime.min:
            return True
        day = dt.date()
        return (not start or day >= start) and (not end or day <= end)

    def text_ok(row):
        if not query:
            return True
        hay = " ".join(str(row.get(k) or "") for k in ("type", "reference", "description", "note", "account"))
        return query in _norm(hay)

    def type_ok(row):
        return not type_filter or type_filter in _norm(row.get("type"))

    def account_ok(row):
        if not account_filter:
            return True
        hay = _norm(" ".join(str(row.get(k) or "") for k in ("account", "related_account", "note")))
        return account_filter in hay

    def status_ok(row):
        if not status_filter or status_filter in {"all", "active"}:
            return True
        debit = _decimal(row.get("debit", 0))
        credit = _decimal(row.get("credit", 0))
        balance = _decimal(row.get("balance", 0))
        if status_filter in {"debit", "due", "payable"}:
            return debit > 0 or balance > 0
        if status_filter in {"credit", "paid", "receipt"}:
            return credit > 0 or balance < 0
        if status_filter in {"zero", "settled"}:
            return debit == 0 and credit == 0
        return True

    def amount_ok(row):
        amount = _decimal(row.get("debit", 0)) + _decimal(row.get("credit", 0))
        return (min_amount is None or amount >= min_amount) and (max_amount is None or amount <= max_amount)

    selected = [r for r in rows if date_ok(r) and text_ok(r) and type_ok(r) and account_ok(r) and status_ok(r) and amount_ok(r)]
    # If a start date exists, insert the balance immediately before the first
    # selected movement.  It is a reporting row, not a persisted transaction.
    if start:
        prior = [r for r in rows if r.get("date") != datetime.min and r["date"].date() < start]
        if prior:
            carry_balance = prior[-1].get("balance", 0)
            carry = _row(
                date_value=datetime.combine(start, datetime.min.time()),
                row_type="Carry Forward",
                reference="CARRY-FORWARD",
                description=f"Balance before {start.isoformat()}",
                debit=0,
                credit=0,
            )
            carry["balance"] = float(carry_balance or 0)
            selected.insert(0, carry)
    return selected


def _supplier_rows(supplier):
    rows = []
    opening = _decimal(getattr(supplier, "opening_balance", 0))
    if opening != 0:
        rows.append(_row(
            date_value=getattr(supplier, "opening_balance_date", None) or getattr(supplier, "created_at", None),
            row_type="OPENING",
            reference="OPENING",
            description="Opening Balance",
            debit=max(-opening, Decimal("0.00")),
            credit=max(opening, Decimal("0.00")),
            source_type="Supplier",
            source_id=supplier.id,
            party_id=supplier.id,
        ))
    grns = GRN.query.filter(
        GRN.is_void == False,
        or_(GRN.supplier_id == supplier.id, func.lower(func.trim(GRN.supplier)) == _norm(supplier.name)),
    ).order_by(GRN.date_posted.asc(), GRN.id.asc()).all()
    for grn in grns:
        total = _decimal(calculate_grn_total(grn))
        if total == 0:
            # Keep a zero-value adjustment out of balances, but historical
            # detail remains available in the GRN page itself.
            continue
        item_lines = [
            {
                "name": getattr(item, "mat_name", "") or "",
                "qty": _float(getattr(item, "qty", 0)),
                "rate": _float(getattr(item, "price_at_time", 0)),
                "amount": _float(_decimal(getattr(item, "qty", 0)) * _decimal(getattr(item, "price_at_time", 0))),
            }
            for item in (getattr(grn, "items", None) or [])
            if not bool(getattr(item, "is_void", False))
        ]
        row = _row(
            date_value=getattr(grn, "date_posted", None),
            row_type="GRN",
            reference=_bill_ref(grn, "manual_bill_no", "auto_bill_no", fallback=f"GRN-{grn.id}"),
            description="Goods Receipt",
            credit=total,
            source_type="GRN",
            source_id=grn.id,
            party_id=supplier.id,
            note=getattr(grn, "note", "") or "",
            account=getattr(getattr(grn, "payment_account", None), "name", "") or "",
            source=grn,
        )
        row["item_lines"] = item_lines
        row["supplier_invoice_no"] = getattr(grn, "supplier_invoice_no", "") or ""
        rows.append(row)

        # Older GRNs predate the canonical SupplierPayment auto-row.  Preserve
        # their paid amount as one read-side debit only when no linked active
        # payment exists; new GRNs are not counted twice.
        paid_amount = _decimal(getattr(grn, "paid_amount", 0))
        auto_payment = SupplierPayment.query.filter(
            SupplierPayment.supplier_id == supplier.id,
            SupplierPayment.is_void == False,
            or_(
                and_(SupplierPayment.source_type == "GRN", SupplierPayment.source_id == grn.id),
                func.lower(func.coalesce(SupplierPayment.note, "")).like(f"%[auto_grn_pay:{grn.id}]%"),
            ),
        ).first()
        if paid_amount > 0 and not auto_payment:
            rows.append(_row(
                date_value=getattr(grn, "date_posted", None),
                row_type="Payment",
                reference=f"GRN-PAY-{grn.id}",
                description="Legacy GRN payment",
                debit=paid_amount,
                source_type="GRN",
                source_id=grn.id,
                party_id=supplier.id,
                account=getattr(getattr(grn, "payment_account", None), "name", "") or "",
                source=grn,
            ))

    payments = SupplierPayment.query.filter_by(supplier_id=supplier.id, is_void=False).order_by(
        SupplierPayment.date_posted.asc(), SupplierPayment.id.asc()
    ).all()
    for payment in payments:
        amount = _decimal(getattr(payment, "amount", 0))
        if amount == 0:
            continue
        ptype = _norm(getattr(payment, "payment_type", None))
        is_credit = amount < 0 or ptype in {"refund", "return", "credit", "supplier return"}
        debit = Decimal("0.00") if is_credit else abs(amount)
        credit = abs(amount) if is_credit else Decimal("0.00")
        rows.append(_row(
            date_value=getattr(payment, "date_posted", None),
            row_type="Supplier Refund" if is_credit else "Payment",
            reference=_bill_ref(payment, "manual_bill_no", "auto_bill_no", fallback=f"PAY-{payment.id}"),
            description=("Supplier refund/credit" if is_credit else f"Payment ({getattr(payment, 'method', None) or 'Cash'})"),
            debit=debit,
            credit=credit,
            source_type="SupplierPayment",
            source_id=payment.id,
            party_id=supplier.id,
            note=getattr(payment, "note", "") or "",
            account=getattr(getattr(payment, "payment_account", None), "name", "") or "",
            source=payment,
        ))
    _sort_rows(rows, supplier=True)
    closing = _apply_running_balance(rows, convention="supplier")
    total_debit = _float(sum((_decimal(r["debit"]) for r in rows), Decimal("0.00")))
    total_credit = _float(sum((_decimal(r["credit"]) for r in rows), Decimal("0.00")))
    return {
        "entity": supplier,
        "entity_type": "supplier",
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": closing,
        "last_transaction_date": max((r["date"] for r in rows if r["date"] != datetime.min), default=None),
        "last_payment_date": max((r["date"] for r in rows if r.get("type") in {"Payment", "Supplier Refund"}), default=None),
        "status": "Payable" if closing > _float(EPS) else ("Credit" if closing < -_float(EPS) else "Settled"),
    }


def build_supplier_financial_ledger(supplier, **filters):
    ledger = _supplier_rows(supplier)
    ledger["filtered_rows"] = filter_ledger_rows(ledger["rows"], **filters)
    return ledger


def build_supplier_payable_summaries(suppliers=None):
    """Return authoritative supplier balances with a bounded query count.

    The projection retains the full supplier ledger's legacy-GRN-payment and
    supplier-refund rules, but avoids creating display rows when a dashboard
    needs only closing balances.
    """
    if suppliers is None:
        suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()
    else:
        suppliers = list(suppliers)
    if not suppliers:
        return {}

    grns = GRN.query.options(selectinload(GRN.items)).filter(GRN.is_void == False).all()
    payments = SupplierPayment.query.filter(SupplierPayment.is_void == False).all()
    payments_by_supplier = defaultdict(list)
    for payment in payments:
        payments_by_supplier[payment.supplier_id].append(payment)

    summaries = {}
    for supplier in suppliers:
        balance = _decimal(getattr(supplier, "opening_balance", 0))
        supplier_name = _norm(getattr(supplier, "name", ""))
        supplier_payments = payments_by_supplier.get(supplier.id, [])
        for grn in grns:
            if grn.supplier_id != supplier.id and _norm(grn.supplier) != supplier_name:
                continue
            balance += _decimal(calculate_grn_total(grn))
            paid_amount = _decimal(getattr(grn, "paid_amount", 0))
            marker = f"[auto_grn_pay:{grn.id}]"
            has_auto_payment = any(
                (payment.source_type == "GRN" and payment.source_id == grn.id)
                or marker in (payment.note or "").lower()
                for payment in supplier_payments
            )
            if paid_amount > 0 and not has_auto_payment:
                balance -= paid_amount

        for payment in supplier_payments:
            amount = _decimal(getattr(payment, "amount", 0))
            if amount == 0:
                continue
            payment_type = _norm(getattr(payment, "payment_type", None))
            is_credit = amount < 0 or payment_type in {"refund", "return", "credit", "supplier return"}
            balance += abs(amount) if is_credit else -abs(amount)
        if abs(balance) < EPS:
            balance = Decimal("0.00")
        summaries[supplier.id] = _float(balance)
    return summaries


def _delivery_person_rows(person):
    rows = []
    opening = _decimal(getattr(person, "opening_balance", 0))
    if opening != 0:
        rows.append(_row(
            date_value=getattr(person, "opening_balance_date", None) or getattr(person, "created_at", None),
            row_type="OPENING",
            reference="OPENING",
            description="Opening Balance",
            debit=max(opening, Decimal("0.00")),
            credit=max(-opening, Decimal("0.00")),
            source_type="DeliveryPerson",
            source_id=person.id,
            party_id=person.id,
        ))

    allocations = SaleDeliveryPerson.query.filter_by(
        delivery_person_id=person.id, is_void=False
    ).join(DirectSale, SaleDeliveryPerson.sale_id == DirectSale.id).filter(
        DirectSale.is_void == False
    ).order_by(SaleDeliveryPerson.created_at.asc(), SaleDeliveryPerson.id.asc()).all()
    active_alloc_by_sale = set()
    for alloc in allocations:
        active_alloc_by_sale.add((alloc.sale_id, person.id))
        rent = _decimal(getattr(alloc, "rent_amount", 0))
        if rent <= 0:
            continue
        sale = getattr(alloc, "sale", None)
        ref = _bill_ref(sale, "manual_bill_no", "auto_bill_no", fallback=f"DS-{alloc.sale_id}")
        rows.append(_row(
            date_value=getattr(alloc, "created_at", None) or getattr(sale, "date_posted", None),
            row_type="Delivery Rent",
            reference=ref,
            description=f"Delivery rent ({getattr(sale, 'client_name', '') or 'sale'})",
            debit=rent,
            source_type="SaleDeliveryPerson",
            source_id=alloc.id,
            party_id=person.id,
            note="",
            source=alloc,
        ))

    # Legacy DeliveryRent rows are still historical data.  Include them only
    # where no active allocation already represents the same sale/person.
    legacy = DeliveryRent.query.filter(
        DeliveryRent.is_void == False,
        func.lower(func.trim(DeliveryRent.delivery_person_name)) == _norm(person.name),
    ).order_by(DeliveryRent.date_posted.asc(), DeliveryRent.id.asc()).all()
    for rent in legacy:
        if rent.sale_id and (rent.sale_id, person.id) in active_alloc_by_sale:
            continue
        amount = _decimal(getattr(rent, "amount", 0))
        if amount <= 0:
            continue
        rows.append(_row(
            date_value=getattr(rent, "date_posted", None),
            row_type="Delivery Rent",
            reference=_bill_ref(rent, "bill_no", fallback=f"RENT-{rent.id}"),
            description="Legacy delivery rent",
            debit=amount,
            source_type="DeliveryRent",
            source_id=rent.id,
            party_id=person.id,
            note=getattr(rent, "note", "") or "",
            source=rent,
        ))

    payments = DeliveryPersonPayment.query.filter_by(
        delivery_person_id=person.id, is_void=False
    ).order_by(DeliveryPersonPayment.date_posted.asc(), DeliveryPersonPayment.id.asc()).all()
    for payment in payments:
        # A linked payment whose allocation/sale was voided is an integrity
        # exception and must not continue to affect the live balance.
        allocation = getattr(payment, "allocation", None)
        if allocation is not None and bool(getattr(allocation, "is_void", False)):
            continue
        amount_paid = _decimal(getattr(payment, "amount_paid", 0))
        waive = _decimal(getattr(payment, "waive_off_amount", 0))
        ref = (getattr(payment, "reference", "") or "").strip() or f"DPP-{payment.id}"
        # The payment account is part of the driver ledger view: the same
        # transaction is visible from the driver side and the account side.
        pay_account = getattr(payment, "payment_account", None)
        account_label = getattr(pay_account, "name", "") or ""
        if amount_paid > 0:
            rows.append(_row(
                date_value=getattr(payment, "date_posted", None),
                row_type="Payment",
                reference=ref,
                description=(
                    f"Driver service payment from {account_label}" if account_label
                    else "Driver service payment"
                ),
                credit=amount_paid,
                source_type="DeliveryPersonPayment",
                source_id=payment.id,
                party_id=person.id,
                note=getattr(payment, "note", "") or "",
                account=account_label,
                source=payment,
            ))
        if waive > 0:
            rows.append(_row(
                date_value=getattr(payment, "date_posted", None),
                row_type="Waive-Off",
                reference=ref,
                description="Driver settlement waive-off (no cash movement)",
                credit=waive,
                source_type="DeliveryPersonPayment",
                source_id=payment.id,
                party_id=person.id,
                note=getattr(payment, "note", "") or "",
                source=payment,
            ))

    _sort_rows(rows)
    closing = _apply_running_balance(rows, convention="client")
    total_debit = _float(sum((_decimal(r["debit"]) for r in rows), Decimal("0.00")))
    total_credit = _float(sum((_decimal(r["credit"]) for r in rows), Decimal("0.00")))
    return {
        "entity": person,
        "entity_type": "delivery_person",
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": closing,
        "last_transaction_date": max((r["date"] for r in rows if r["date"] != datetime.min), default=None),
        "last_payment_date": max((r["date"] for r in rows if r.get("source_type") == "DeliveryPersonPayment"), default=None),
        "status": "Payable" if closing > _float(EPS) else ("Credit" if closing < -_float(EPS) else "Settled"),
    }


def build_delivery_person_financial_ledger(person, **filters):
    ledger = _delivery_person_rows(person)
    ledger["filtered_rows"] = filter_ledger_rows(ledger["rows"], **filters)
    return ledger


def financial_integrity_audit() -> dict:
    """Read-only checks for ghost, duplicate and orphaned financial rows."""
    issues = []
    duplicate_account_sources = []
    seen = defaultdict(list)
    for tx in AccountTransaction.query.filter(AccountTransaction.is_void == False).all():
        key = (getattr(tx, "source_type", None), getattr(tx, "source_id", None), getattr(tx, "transaction_type", None))
        if key[0] and key[1] is not None:
            seen[key].append(tx.id)
    for key, ids in seen.items():
        if len(ids) > 1:
            duplicate_account_sources.append({"source": key, "ids": ids})
    if duplicate_account_sources:
        issues.append({"kind": "duplicate_account_transactions", "rows": duplicate_account_sources})

    # Relationship checks are reported, never auto-deleted.  Name/code rows
    # without a current master are kept as audit findings rather than silently
    # promoted to a new client.
    orphan_source_rows = []
    try:
        snapshot = _client_snapshot()
        for key, meta in snapshot.get('unresolved', {}).items():
            for kind, obj in meta.get('rows', []):
                if kind in {'bookings', 'sales', 'payments', 'pending'}:
                    orphan_source_rows.append({
                        'kind': kind, 'id': getattr(obj, 'id', None),
                        'name': meta.get('name', ''), 'code': meta.get('code', ''),
                    })
    except Exception:
        orphan_source_rows = []
    if orphan_source_rows:
        issues.append({
            'kind': 'orphan_client_source',
            'count': len(orphan_source_rows),
            'sample': orphan_source_rows[:25],
        })

    for payment in Payment.query.filter(Payment.is_void == False).all():
        if getattr(payment, "client_id", None) and not Client.query.get(payment.client_id):
            issues.append({"kind": "orphan_payment_client", "id": payment.id, "client_id": payment.client_id})
    for alloc in SaleDeliveryPerson.query.filter(SaleDeliveryPerson.is_void == False).all():
        if not DirectSale.query.get(alloc.sale_id) or not DeliveryPerson.query.get(alloc.delivery_person_id):
            issues.append({"kind": "orphan_delivery_allocation", "id": alloc.id})
    for payment in DeliveryPersonPayment.query.filter(DeliveryPersonPayment.is_void == False).all():
        if not DeliveryPerson.query.get(payment.delivery_person_id):
            issues.append({"kind": "orphan_delivery_payment", "id": payment.id})
        if payment.allocation_id and not SaleDeliveryPerson.query.get(payment.allocation_id):
            issues.append({"kind": "orphan_delivery_payment_allocation", "id": payment.id, "allocation_id": payment.allocation_id})
    for payment in SupplierPayment.query.filter(SupplierPayment.is_void == False).all():
        if not Supplier.query.get(payment.supplier_id):
            issues.append({"kind": "orphan_supplier_payment", "id": payment.id, "supplier_id": payment.supplier_id})

    # Driver payments must reconcile 1:1 with their authoritative account
    # transaction.  Findings are reported, never auto-corrected.
    try:
        from app.services.driver_payments import reconcile_driver_payments
        driver_report = reconcile_driver_payments()
        if not driver_report["ok"]:
            issues.append({
                "kind": "driver_payment_ledger_mismatch",
                "count": driver_report["issue_count"],
                "legacy_unlinked_payments": driver_report["legacy_unlinked_payments"],
                "sample": driver_report["issues"][:25],
            })
    except Exception:
        pass

    return {
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "duplicate_account_transaction_count": len(duplicate_account_sources),
        "checked_at": pk_now().isoformat(),
    }


__all__ = [
    "build_client_financial_ledger",
    "build_current_payables",
    "build_supplier_financial_ledger",
    "build_supplier_payable_summaries",
    "build_delivery_person_financial_ledger",
    "filter_ledger_rows",
    "financial_integrity_audit",
]
