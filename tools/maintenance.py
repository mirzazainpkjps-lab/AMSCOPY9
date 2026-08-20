#!/usr/bin/env python3
"""AMS maintenance CLI for cron/hosting scheduled tasks.

Recommended hourly scheduler entry (from the repository root):
    .venv/bin/python tools/maintenance.py backup-if-due

The command is idempotent within the configured interval and uses a
cross-process lock, so overlapping scheduler and WSGI fallback runs are safe.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.services.maintenance import (
    cleanup_owned_temp,
    create_backup,
    maintenance_status,
    restore_backup,
    run_backup_if_due,
    validate_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe AMS backup and storage maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup-if-due", help="create one current backup only when due")
    sub.add_parser("backup-now", help="create and validate a backup immediately")
    sub.add_parser("status", help="print storage and backup health as JSON")
    sub.add_parser("cleanup-temp", help="remove only stale maintenance-owned temp paths")
    verify = sub.add_parser("verify", help="fully validate one backup without restoring it")
    verify.add_argument("path")
    restore = sub.add_parser("restore", help="restore during a controlled maintenance window")
    restore.add_argument("path")
    restore.add_argument("--confirm", action="store_true", help="required acknowledgement")
    args = parser.parse_args()

    app = create_app({"BACKUP_EMBEDDED_SCHEDULER": False})
    try:
        with app.app_context():
            if args.command == "backup-if-due":
                result = run_backup_if_due(app)
            elif args.command == "backup-now":
                result = create_backup(app, reason="manual-cli")
            elif args.command == "status":
                result = maintenance_status(app)
            elif args.command == "cleanup-temp":
                result = {"removed": cleanup_owned_temp(app)}
            elif args.command == "verify":
                result = validate_backup(args.path)
            elif args.command == "restore":
                if not args.confirm:
                    parser.error("restore requires --confirm and a controlled maintenance window")
                result = restore_backup(app, args.path)
            else:  # pragma: no cover
                parser.error("unknown command")
            print(json.dumps(result, indent=2, default=str))
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
