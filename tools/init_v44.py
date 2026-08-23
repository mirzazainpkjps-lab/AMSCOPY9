#!/usr/bin/env python3
"""Initialize a clean v4.4 database without importing legacy data.

Usage:
    python tools/init_v44.py [path]

The command refuses to overwrite an existing database. Delete/rename the target
only after taking a backup if a fresh reset is really intended.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.services.v44_schema import initialize_v44_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an empty AMS v4.4 database")
    parser.add_argument(
        "path",
        nargs="?",
        default="instance/ahmed_cement_v44_fresh.db",
        help="target SQLite file (default: instance/ahmed_cement_v44_fresh.db)",
    )
    args = parser.parse_args()
    path = Path(args.path)
    if path.exists() and path.stat().st_size:
        raise SystemExit(f"Refusing to overwrite existing database: {path}")
    created = initialize_v44_database(str(path))
    print(f"Created fresh v4.4 database: {path}" if created else f"Already initialized: {path}")
    print("Business data is empty; only roles, permissions, wipe scopes, and Admin were seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
