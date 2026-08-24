"""Shared Account Create/Edit form logic.

Both the create and edit views delegate to :func:`validate_account_form` so the
classification, channel, linked-entity and detail-field validation rules are
guaranteed identical (PART 10 / PART 19).  The helper returns a cleaned dict of
Account attributes (new classification columns + the legacy columns kept in sync
for backward compatibility).  It raises ``ValueError`` on any invalid input so
the calling view can flash the message and re-render.
"""
from __future__ import annotations

from .classification import (
    CHANNELS,
    STATUSES,
    account_types,
    allowed_channels,
    channel_detail_fields,
    channel_needs_bank_details,
    channel_needs_cash_details,
    channel_needs_wallet_details,
    default_channel,
    is_valid_triple,
    legacy_account_type_for,
    legacy_category_for,
    legacy_group_for,
    required_entity,
    resolve_node,
    subcategories,
)


# Adjustment-reason vocabulary surfaced as a dropdown (PART 12).  ``Other``
# unlocks a free-text explanation.
ADJUSTMENT_REASONS = [
    "Physical cash verification",
    "Bank reconciliation",
    "Historical correction",
    "Data migration correction",
    "Opening position correction",
    "Accounting correction",
    "Other",
]


def _strip(val):
    return (val if isinstance(val, str) else (val or "")).strip()


def _form_get(form, key, default=""):
    """Read a value from a Werkzeug form/multidict or plain dict."""
    if form is None:
        return default
    try:
        val = form.get(key, default)
    except AttributeError:
        return default
    return val if val is not None else default


def _resolve_linked_entity(entity_type, form):
    """Resolve the linked-entity reference for the given type.

    Returns a dict with the four linked_* attributes.  Raises ValueError when a
    required reference is missing or does not resolve to a real row.
    """
    out = {
        "linked_entity_type": entity_type,
        "linked_client_id": None,
        "linked_supplier_id": None,
        "linked_party_name": None,
    }
    if entity_type in ("none", "", None):
        out["linked_entity_type"] = "none"
        return out

    if entity_type == "client":
        from models import Client
        cid = _form_get(form, "linked_client_id")
        client = None
        if cid:
            try:
                client = Client.query.get(int(cid))
            except (TypeError, ValueError):
                client = None
        if not client:
            raise ValueError("Please select a valid linked client for this account type.")
        out["linked_client_id"] = client.id
        out["linked_party_name"] = client.name
        return out

    if entity_type == "supplier":
        from models import Supplier
        sid = _form_get(form, "linked_supplier_id")
        supplier = None
        if sid:
            try:
                supplier = Supplier.query.get(int(sid))
            except (TypeError, ValueError):
                supplier = None
        if not supplier:
            raise ValueError("Please select a valid linked supplier for this account type.")
        out["linked_supplier_id"] = supplier.id
        out["linked_party_name"] = supplier.name
        return out

    # partner / worker / vehicle / party → free-text name
    name = _strip(_form_get(form, "linked_party_name"))
    if not name:
        label = {"partner": "partner", "worker": "worker", "vehicle": "vehicle", "party": "party"}.get(
            entity_type, "party"
        )
        raise ValueError(f"Please enter the linked {label} name for this account type.")
    out["linked_party_name"] = name
    return out


def _collect_channel_details(channel, form):
    """Return only the channel-specific detail fields relevant to ``channel``.

    Incompatible legacy fields are dropped so a Channel change never leaves stale
    active data behind (PART 11).
    """
    relevant = channel_detail_fields(channel)
    out = {
        "cash_location": None,
        "cash_responsible": None,
        "bank_name": None,
        "account_holder_name": None,
        "account_number": None,
        "branch_code": None,
        "wallet_provider": None,
        "wallet_number": None,
        "wallet_holder": None,
    }
    if "cash_location" in relevant:
        out["cash_location"] = _strip(_form_get(form, "cash_location")) or None
        out["cash_responsible"] = _strip(_form_get(form, "cash_responsible")) or None
    if "bank_name" in relevant:
        out["bank_name"] = _strip(_form_get(form, "bank_name")) or None
        out["account_holder_name"] = _strip(_form_get(form, "account_holder_name")) or None
        out["account_number"] = _strip(_form_get(form, "account_number")) or None
        out["branch_code"] = _strip(_form_get(form, "branch_code")) or None
    if "wallet_provider" in relevant:
        out["wallet_provider"] = _strip(_form_get(form, "wallet_provider")) or None
        out["wallet_number"] = _strip(_form_get(form, "wallet_number")) or None
        out["wallet_holder"] = _strip(_form_get(form, "wallet_holder")) or None
    return out


def validate_account_form(form, *, is_edit=False):
    """Validate the create/edit form and return a cleaned attribute dict.

    Server-side is the final authority (PART 19): every dropdown value is
    re-checked against the registry here, never trusted from the browser.

    Raises ``ValueError`` with a user-facing message on any invalid combination.
    """
    name = _strip(_form_get(form, "name"))
    if not name:
        raise ValueError("Account name is required.")

    category = _strip(_form_get(form, "class_category"))
    subcategory = _strip(_form_get(form, "class_subcategory"))
    atype = _strip(_form_get(form, "class_account_type"))
    if not category:
        raise ValueError("Please select a category.")
    if not subcategory:
        raise ValueError("Please select a subcategory.")
    if not atype:
        raise ValueError("Please select an account type.")

    if not is_valid_triple(category, subcategory, atype):
        raise ValueError("The selected category / subcategory / account type combination is not valid.")

    node = resolve_node(category, subcategory, atype)
    if node is None:  # defensive; is_valid_triple already guarded
        raise ValueError("Invalid classification.")

    # Channel: forced when the node allows exactly one, selectable otherwise.
    submitted_channel = _strip(_form_get(form, "channel")).lower()
    allowed = allowed_channels(category, subcategory, atype)
    if submitted_channel and submitted_channel not in allowed:
        raise ValueError("The selected channel is not valid for this account type.")
    channel = submitted_channel or default_channel(category, subcategory, atype)
    if channel not in allowed:
        channel = allowed[0]

    # Channel-specific detail validation (PART 6 / PART 19).
    if channel_needs_bank_details(channel):
        if not _strip(_form_get(form, "bank_name")):
            raise ValueError("Bank name is required for a bank account.")
        if not _strip(_form_get(form, "account_holder_name")):
            raise ValueError("Account holder name is required for a bank account.")
        if not _strip(_form_get(form, "account_number")):
            raise ValueError("Account number / IBAN is required for a bank account.")
    if channel_needs_wallet_details(channel):
        if not _strip(_form_get(form, "wallet_provider")):
            raise ValueError("Wallet provider is required for a digital wallet account.")
        if not _strip(_form_get(form, "wallet_number")):
            raise ValueError("Wallet number is required for a digital wallet account.")

    # Linked entity (PART 4): required when the classification demands one.
    entity_type = required_entity(category, subcategory, atype)
    linked = _resolve_linked_entity(entity_type, form)

    # Channel-specific detail fields (incompatible ones cleared).
    details = _collect_channel_details(channel, form)

    # Status (PART 8).
    status = _strip(_form_get(form, "account_status")).lower() or "active"
    if status not in STATUSES:
        raise ValueError("Please select a valid account status.")

    note = _strip(_form_get(form, "note")) or None

    submitted_source_category = _strip(_form_get(form, "source_category"))
    if submitted_source_category:
        from models import AccountCategory, db
        from sqlalchemy import func
        # double check if this category exists or create it if not (defensive)
        exists = AccountCategory.query.filter(
            func.lower(func.trim(AccountCategory.name)) == submitted_source_category.lower()
        ).first()
        if not exists:
            new_cat = AccountCategory(name=submitted_source_category)
            db.session.add(new_cat)
            db.session.commit()

    cleaned = {
        "name": name,
        "note": note,
        "class_category": category,
        "class_subcategory": subcategory,
        "class_account_type": atype,
        "channel": channel,
        "account_status": status,
        # Legacy columns kept in sync so existing payment/KPI/transfer code
        # keeps working unchanged (PART 17).
        "category": legacy_category_for(channel),
        "source_category": submitted_source_category or legacy_group_for(category, subcategory),
        "account_type": legacy_account_type_for(category, subcategory, atype),
        "type": legacy_account_type_for(category, subcategory, atype),
        "is_active": status == "active",
    }
    cleaned.update(linked)
    cleaned.update(details)
    return cleaned


def cascade_options():
    """Lightweight option lists for template rendering (non-JSON path)."""
    return {
        "channels": list(CHANNELS),
        "statuses": list(STATUSES),
        "adjustment_reasons": list(ADJUSTMENT_REASONS),
    }
