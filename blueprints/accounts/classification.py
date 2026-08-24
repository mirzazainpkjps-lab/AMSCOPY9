"""Account classification registry — the single authoritative definition of the
Category → Subcategory → Account Type → {channel, linked entity, detail fields}
hierarchy used by the Account Create / Edit forms.

Why a registry?
---------------
The old Account form exposed three independent, overlapping selectors
(``Group``, ``Type``, ``Channel``) that allowed contradictory combinations such
as ``Expense + Bank + Client Account``.  This module replaces that with one
controlled hierarchy so an invalid combination can never be saved.

The registry is intentionally *data only*: the create/edit views and the
server-side validation both read from here, and the templates receive a JSON
projection of it for client-side cascading.  No accounting/ledger behaviour
lives in this file — it only governs how an account is *classified* and which
fields a given classification requires.

Legacy compatibility
--------------------
Existing accounts predate this hierarchy.  ``legacy_to_classification`` maps the
old ``source_category`` / ``account_type`` / ``category`` tuple to a valid new
classification so historical rows keep loading and functioning after the schema
upgrade.  The legacy columns themselves are never deleted.
"""
from __future__ import annotations

# Channel / medium vocabulary.  ``ledger_only`` means the balance is tracked on
# the account ledger only (no physical cash / bank / wallet instrument).
CHANNELS = ("cash", "bank", "digital_wallet", "ledger_only", "other")

# Human labels for the UI.
CHANNEL_LABELS = {
    "cash": "Cash",
    "bank": "Bank",
    "digital_wallet": "Digital Wallet",
    "ledger_only": "Ledger Only",
    "other": "Other",
}

# Linked-entity vocabulary.  ``client`` / ``supplier`` resolve to FKs on the
# Account; the rest are stored as a free-text name on ``linked_party_name``.
ENTITY_TYPES = ("none", "client", "supplier", "partner", "worker", "vehicle", "party")

ENTITY_LABELS = {
    "none": "None",
    "client": "Client",
    "supplier": "Supplier",
    "partner": "Partner",
    "worker": "Worker",
    "vehicle": "Vehicle",
    "party": "Other Party",
}

# Account status vocabulary (PART 8).  ``active`` maps to is_active=True; the
# other two preserve history while hiding / blocking the account.
STATUSES = ("active", "inactive", "archived")


def _node(channel, entity="none"):
    """Build a leaf definition.

    ``channel`` may be a single channel string (forced) or a list of allowed
    channels (first entry is the default).  ``entity`` declares the required
    linked entity type for this classification (``none`` = no link required).
    """
    if isinstance(channel, (list, tuple)):
        allowed = list(channel)
        default = allowed[0]
    else:
        allowed = [channel]
        default = channel
    return {"channels": allowed, "default_channel": default, "entity": entity}


# The authoritative hierarchy.  Keys are (Category, Subcategory, Account Type).
CLASSIFICATION = {
    "Assets": {
        "label": "Assets",
        "description": "Money, funds, or value owned by the business or receivable by the business.",
        "subcategories": {
            "Cash": {
                "label": "Cash",
                "account_types": {
                    "Main Cash": _node("cash"),
                    "Petty Cash": _node("cash"),
                    "Cash Drawer": _node("cash"),
                    "Site Cash": _node("cash"),
                    "Temporary Cash": _node("cash"),
                },
            },
            "Bank": {
                "label": "Bank",
                "account_types": {
                    "Operating Bank": _node("bank"),
                    "Collection Bank": _node("bank"),
                    "Savings Bank": _node("bank"),
                    "Settlement Bank": _node("bank"),
                },
            },
            "Digital Wallet": {
                "label": "Digital Wallet",
                "account_types": {
                    "Mobile Wallet": _node("digital_wallet"),
                    "Payment App": _node("digital_wallet"),
                },
            },
            "Client Receivables": {
                "label": "Client Receivables",
                "account_types": {
                    "Client Ledger": _node("ledger_only", "client"),
                    "Client Advance Receivable": _node("ledger_only", "client"),
                },
            },
            "Advances Given": {
                "label": "Advances Given",
                "account_types": {
                    "Advance to Worker": _node("ledger_only", "worker"),
                    "Advance to Party": _node("ledger_only", "party"),
                    "Staff Advance": _node("ledger_only", "worker"),
                },
            },
            "Other Assets": {
                "label": "Other Assets",
                "account_types": {
                    "General Asset": _node(["ledger_only", "cash", "bank", "other"]),
                    "Security Deposit Given": _node("ledger_only", "party"),
                },
            },
        },
    },
    "Liabilities": {
        "label": "Liabilities",
        "description": "Money or obligations owed by the business.",
        "subcategories": {
            "Supplier Payables": {
                "label": "Supplier Payables",
                "account_types": {
                    "Supplier Ledger": _node("ledger_only", "supplier"),
                    "Supplier Outstanding": _node("ledger_only", "supplier"),
                },
            },
            "Client Advances": {
                "label": "Client Advances",
                "account_types": {
                    "Client Advance Received": _node("ledger_only", "client"),
                },
            },
            "Loans Payable": {
                "label": "Loans Payable",
                "account_types": {
                    "Bank Loan": _node("ledger_only", "party"),
                    "Personal Loan": _node("ledger_only", "party"),
                    "Partner Loan": _node("ledger_only", "partner"),
                    "External Loan": _node("ledger_only", "party"),
                },
            },
            "Partner / External Payables": {
                "label": "Partner / External Payables",
                "account_types": {
                    "Partner Payable": _node("ledger_only", "partner"),
                    "External Payable": _node("ledger_only", "party"),
                },
            },
            "Expenses Payable": {
                "label": "Expenses Payable",
                "account_types": {
                    "Expense Payable": _node("ledger_only"),
                },
            },
            "Other Liabilities": {
                "label": "Other Liabilities",
                "account_types": {
                    "General Liability": _node("ledger_only"),
                },
            },
        },
    },
    "Equity / Own Funds": {
        "label": "Equity / Own Funds",
        "description": "Capital, owner funds, partner funds, and other ownership-related balances.",
        "subcategories": {
            "Owner Capital": {
                "label": "Owner Capital",
                "account_types": {
                    "Owner Capital": _node(["ledger_only", "cash", "bank"]),
                },
            },
            "Partner Capital": {
                "label": "Partner Capital",
                "account_types": {
                    "Partner Capital": _node("ledger_only", "partner"),
                },
            },
            "Owner Drawings / Withdrawals": {
                "label": "Owner Drawings / Withdrawals",
                "account_types": {
                    "Owner Drawings": _node("ledger_only"),
                },
            },
            "Retained Funds": {
                "label": "Retained Funds",
                "account_types": {
                    "Retained Earnings": _node("ledger_only"),
                },
            },
            "Other Equity": {
                "label": "Other Equity",
                "account_types": {
                    "General Equity": _node("ledger_only"),
                },
            },
        },
    },
    "Income": {
        "label": "Income",
        "description": "Accounts representing business income.",
        "subcategories": {
            "Sales Income": {
                "label": "Sales Income",
                "account_types": {
                    "Sales Income": _node("ledger_only"),
                },
            },
            "Service Income": {
                "label": "Service Income",
                "account_types": {
                    "Service Income": _node("ledger_only"),
                },
            },
            "Other Income": {
                "label": "Other Income",
                "account_types": {
                    "Other Income": _node("ledger_only"),
                },
            },
        },
    },
    "Expenses": {
        "label": "Expenses",
        "description": "Accounts representing business expenses.",
        "subcategories": {
            "Operating Expenses": {
                "label": "Operating Expenses",
                "account_types": {"Operating Expense": _node("ledger_only")},
            },
            "Delivery & Transport": {
                "label": "Delivery & Transport",
                "account_types": {"Delivery & Transport": _node("ledger_only")},
            },
            "Salaries & Labour": {
                "label": "Salaries & Labour",
                "account_types": {"Salary & Labour": _node("ledger_only")},
            },
            "Rent & Utilities": {
                "label": "Rent & Utilities",
                "account_types": {"Rent & Utilities": _node("ledger_only")},
            },
            "Administrative Expenses": {
                "label": "Administrative Expenses",
                "account_types": {"Administrative Expense": _node("ledger_only")},
            },
            "Other Expenses": {
                "label": "Other Expenses",
                "account_types": {"Other Expense": _node("ledger_only")},
            },
        },
    },
    "External / Clearing": {
        "label": "External / Clearing",
        "description": "Partner settlements, intercompany balances, temporary clearing accounts.",
        "subcategories": {
            "Partner Clearing": {
                "label": "Partner Clearing",
                "account_types": {"Partner Clearing": _node("ledger_only", "partner")},
            },
            "Intercompany / Company Transfer": {
                "label": "Intercompany / Company Transfer",
                "account_types": {"Intercompany Transfer": _node("ledger_only")},
            },
            "External Settlement": {
                "label": "External Settlement",
                "account_types": {"External Settlement": _node("ledger_only", "party")},
            },
            "Suspense / Temporary Clearing": {
                "label": "Suspense / Temporary Clearing",
                "account_types": {"Suspense / Temporary": _node("ledger_only")},
            },
        },
    },
}


def categories():
    """Return ordered list of category names."""
    return list(CLASSIFICATION.keys())


def category_description(category):
    cat = CLASSIFICATION.get(category)
    return cat["description"] if cat else ""


def subcategories(category):
    """Return ordered list of subcategory names for a category (or [] if invalid)."""
    cat = CLASSIFICATION.get(category)
    if not cat:
        return []
    return list(cat["subcategories"].keys())


def account_types(category, subcategory):
    """Return ordered list of account type names for a (category, subcategory)."""
    cat = CLASSIFICATION.get(category)
    if not cat:
        return []
    sub = cat["subcategories"].get(subcategory)
    if not sub:
        return []
    return list(sub["account_types"].keys())


def resolve_node(category, subcategory, account_type):
    """Return the leaf definition dict for a classification triple, or None."""
    cat = CLASSIFICATION.get(category)
    if not cat:
        return None
    sub = cat["subcategories"].get(subcategory)
    if not sub:
        return None
    return sub["account_types"].get(account_type)


def is_valid_triple(category, subcategory, account_type):
    return resolve_node(category, subcategory, account_type) is not None


def allowed_channels(category, subcategory, account_type):
    node = resolve_node(category, subcategory, account_type)
    return list(node["channels"]) if node else []


def default_channel(category, subcategory, account_type):
    node = resolve_node(category, subcategory, account_type)
    return node["default_channel"] if node else "ledger_only"


def required_entity(category, subcategory, account_type):
    node = resolve_node(category, subcategory, account_type)
    return node["entity"] if node else "none"


def validate_channel(category, subcategory, account_type, channel):
    """Return True if ``channel`` is permitted for the classification."""
    return channel in allowed_channels(category, subcategory, account_type)


# ---------------------------------------------------------------------------
# Legacy → new mapping (PART 17).  Best-effort, never destructive.  Unmappable
# rows fall back to a valid generic Asset classification so they keep working.
# ---------------------------------------------------------------------------
LEGACY_FALLBACK = ("Assets", "Other Assets", "General Asset")


def legacy_to_classification(source_category, account_type, category_channel):
    """Map an existing account's old (group, type, channel) to the new triple.

    The original columns are preserved by the caller; this only decides what to
    write into the new ``class_*`` columns.  Every return value is a guaranteed
    valid triple per :data:`CLASSIFICATION`.
    """
    group = (source_category or "").strip().lower()
    atype = (account_type or "").strip().lower()
    chan = (category_channel or "cash").strip().lower()

    # --- By account_type keyword (most specific signal) ---
    if "client" in atype and ("receivable" in atype or "ledger" in atype or atype == "client"):
        return ("Assets", "Client Receivables", "Client Ledger")
    if "supplier" in atype:
        return ("Liabilities", "Supplier Payables", "Supplier Ledger")
    if "loan" in atype:
        return ("Liabilities", "Loans Payable", "External Loan")

    # --- By transaction group ---
    if group == "clients":
        return ("Assets", "Client Receivables", "Client Ledger")
    if group == "external" and "supplier" in atype:
        return ("Liabilities", "Supplier Payables", "Supplier Ledger")
    if group == "external":
        return ("External / Clearing", "External Settlement", "External Settlement")
    if group == "loan":
        return ("Liabilities", "Loans Payable", "External Loan")
    if group == "own funds":
        return ("Equity / Own Funds", "Owner Capital", "Owner Capital")
    if group in ("expense",) or "expense" in atype:
        return ("Expenses", "Operating Expenses", "Operating Expense")

    # --- By instrument (Company money) ---
    if chan == "bank":
        return ("Assets", "Bank", "Operating Bank")
    if group == "company" or not group:
        return ("Assets", "Cash", "Main Cash") if chan != "bank" else ("Assets", "Bank", "Operating Bank")

    return LEGACY_FALLBACK


# ---------------------------------------------------------------------------
# New classification → legacy column synchronization (PART 17 compat).
# Keeps ``source_category`` (used by the loan/transfer flow) and
# ``account_type`` (used by ``_company_accounts``) meaningful so existing
# receive/pay/KPI logic keeps working unchanged.
# ---------------------------------------------------------------------------
def legacy_group_for(category, subcategory):
    """Derive the old ``source_category`` group from the new classification."""
    cs = (category or "").strip()
    sub = (subcategory or "").strip()
    if cs == "Assets":
        if sub in ("Client Receivables", "Advances Given"):
            return "Clients"
        return "Company"
    if cs == "Liabilities":
        if sub == "Loans Payable":
            return "Loan"
        if sub == "Client Advances":
            return "Clients"
        return "External"
    if cs == "Equity / Own Funds":
        return "Own Funds"
    if cs == "External / Clearing":
        return "External"
    if cs == "Expenses":
        return "External"
    return "Company"


# Maps the new classification to a legacy ``account_type`` value understood by
# the existing payment/KPI code (notably ``_company_accounts`` which filters on
# ``account_type == 'company'``).
def legacy_account_type_for(category, subcategory, account_type):
    cs = (category or "").strip()
    sub = (subcategory or "").strip()
    if cs == "Assets":
        if sub in ("Client Receivables",):
            return "client"
        if sub == "Advances Given":
            return "client"
        return "company"
    if cs == "Liabilities":
        if sub == "Supplier Payables":
            return "supplier"
        if sub == "Loans Payable":
            return "loan"
        return "external"
    if cs == "Equity / Own Funds":
        return "personal"
    if cs == "External / Clearing":
        return "external"
    if cs == "Expenses":
        return "expense"
    if cs == "Income":
        return "income"
    return "other"


def legacy_category_for(channel):
    """Derive the legacy ``category`` ('cash'/'bank') from the new channel.

    Payment flows only recognise cash/bank.  Digital wallets behave like
    transferable bank funds (selected response).  Ledger-only / other keep a
    neutral cash placeholder so the NOT NULL column stays valid.
    """
    ch = (channel or "cash").strip().lower()
    if ch == "bank":
        return "bank"
    if ch == "digital_wallet":
        return "bank"
    return "cash"


def channel_needs_bank_details(channel):
    return (channel or "").strip().lower() == "bank"


def channel_needs_wallet_details(channel):
    return (channel or "").strip().lower() == "digital_wallet"


def channel_needs_cash_details(channel):
    return (channel or "").strip().lower() == "cash"


def channel_detail_fields(channel):
    """Return the set of channel-specific field names relevant for ``channel``."""
    ch = (channel or "").strip().lower()
    if ch == "cash":
        return {"cash_location", "cash_responsible"}
    if ch == "bank":
        return {"bank_name", "account_holder_name", "account_number", "branch_code"}
    if ch == "digital_wallet":
        return {"wallet_provider", "wallet_number", "wallet_holder"}
    return set()


def registry_json():
    """Project the registry into a JSON-serialisable structure for the client.

    The frontend uses this to cascade the subcategory / account-type / channel
    selectors and to reveal the required linked-entity field without a round
    trip.
    """
    out = {"categories": []}
    for cat_name, cat in CLASSIFICATION.items():
        cat_node = {
            "name": cat_name,
            "label": cat.get("label", cat_name),
            "description": cat.get("description", ""),
            "subcategories": [],
        }
        for sub_name, sub in cat["subcategories"].items():
            sub_node = {
                "name": sub_name,
                "label": sub.get("label", sub_name),
                "account_types": [],
            }
            for at_name, node in sub["account_types"].items():
                sub_node["account_types"].append({
                    "name": at_name,
                    "channels": node["channels"],
                    "default_channel": node["default_channel"],
                    "entity": node["entity"],
                })
            cat_node["subcategories"].append(sub_node)
        out["categories"].append(cat_node)
    out["channels"] = [{"value": c, "label": CHANNEL_LABELS[c]} for c in CHANNELS]
    out["entities"] = [{"value": e, "label": ENTITY_LABELS[e]} for e in ENTITY_TYPES]
    out["statuses"] = list(STATUSES)
    return out
