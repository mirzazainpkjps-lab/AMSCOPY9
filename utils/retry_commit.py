"""Retry helper for optimistic-locking / lock-contention commit failures.

`Account` carries ``__mapper_args__ = {'version_id_col': revision}``, so two
requests that touch the same account concurrently are *designed* to collide:
the loser's UPDATE matches 0 rows and SQLAlchemy raises ``StaleDataError``.
That is correct — it prevents a lost update — but nothing retried the loser,
so a legitimate payment was simply dropped and the user was told the save had
failed.

``retry_on_conflict`` re-runs the whole unit of work (re-reading the account
and recomputing the balance from the now-committed state) instead of retrying
just the COMMIT, which would otherwise re-apply a stale in-memory balance.
"""
from __future__ import annotations

import logging
import random
import time

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import StaleDataError

log = logging.getLogger('retry_commit')

DEFAULT_ATTEMPTS = 5
_BASE_SLEEP = 0.02


def _is_transient(exc):
    if isinstance(exc, StaleDataError):
        return True
    if isinstance(exc, OperationalError):
        text = str(getattr(exc, 'orig', exc)).lower()
        return 'database is locked' in text or 'database table is locked' in text
    return False


def retry_on_conflict(work, *, attempts=DEFAULT_ATTEMPTS, label='operation'):
    """Run ``work()`` and retry it if it fails on a transient write conflict.

    ``work`` must be self-contained: it re-reads whatever rows it mutates and
    performs its own ``db.session.commit()``. The session is rolled back and
    expunged between attempts so nothing stale is carried forward.
    """
    from models import db

    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return work()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if not _is_transient(exc):
                raise
            last_exc = exc
            try:
                db.session.rollback()
            except Exception:
                pass
            if attempt == attempts:
                break
            # Full jitter: concurrent losers must not retry in lockstep.
            time.sleep(random.uniform(0, _BASE_SLEEP * (2 ** (attempt - 1))))
            log.info("Retrying %s after write conflict (attempt %d/%d)",
                     label, attempt + 1, attempts)

    log.warning("%s still conflicting after %d attempts", label, attempts)
    raise last_exc
