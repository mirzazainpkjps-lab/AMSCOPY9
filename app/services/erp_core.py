"""Compatibility alias.

Prefer `from app.services.api import rebuild_pending_bills` in new code.
This module re-exports the explicit public API only — it does not merge
module globals or wire functions into each other.
"""
from app.services.api import *  # noqa: F401,F403
from app.services.api import __all__  # noqa: F401
