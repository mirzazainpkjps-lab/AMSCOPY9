"""Process-wide mutable flags (workers, caches)."""
from __future__ import annotations

HOURLY_BACKUP_WORKER_STARTED = False
HOURLY_BACKUP_LAST_SLOT = None
RECON_WORKER_STARTED = False
RESET_CONTEXT = None
WEASYPRINT_MODULE = None
