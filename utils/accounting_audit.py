"""Atomic structured audit events for accounting operations."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from flask import has_request_context, request, session

from models import AccountingAuditLog, db
from utils.money import to_minor


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, 'f')
    return str(value)


def _dump(value):
    if value is None:
        return None
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(',', ':'))


def record_accounting_audit(
    actor,
    *,
    action: str,
    entity_type: str,
    entity_id=None,
    before=None,
    after=None,
    amount_before=None,
    amount_after=None,
    account_before_id=None,
    account_after_id=None,
    party_before_id=None,
    party_after_id=None,
    reason=None,
    module='accounts',
):
    """Append an audit row to the *current* transaction.

    Deliberately does not commit and does not swallow failures: an accounting
    mutation and its audit event either commit together or roll back together.
    """
    ip_address = None
    session_id = None
    if has_request_context():
        forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
        ip_address = forwarded or request.remote_addr
        session_id = session.get('login_sid') or session.get('_id')

    row = AccountingAuditLog(
        module=(module or 'accounts')[:50],
        action=(action or 'Unknown')[:30],
        entity_type=(entity_type or 'Unknown')[:50],
        entity_id=int(entity_id) if entity_id is not None else None,
        user_id=getattr(actor, 'id', None) if actor else None,
        username=(getattr(actor, 'username', None) or None) if actor else None,
        ip_address=(str(ip_address)[:80] if ip_address else None),
        session_id=(str(session_id)[:80] if session_id else None),
        before_json=_dump(before),
        after_json=_dump(after),
        amount_before_minor=(to_minor(amount_before) if amount_before is not None else None),
        amount_after_minor=(to_minor(amount_after) if amount_after is not None else None),
        account_before_id=account_before_id,
        account_after_id=account_after_id,
        party_before_id=party_before_id,
        party_after_id=party_after_id,
        reason=(str(reason).strip()[:500] if reason else None),
    )
    db.session.add(row)
    return row
