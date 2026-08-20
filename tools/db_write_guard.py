"""
DB Write Guard — Observe Mode
==============================
Detects and logs unsafe direct-sqlite3 write attempts made outside the ORM.

Usage (observe mode — no blocking):
    from tools.db_write_guard import WriteGuard
    WriteGuard.observe()          # install hooks, log warnings only
    WriteGuard.enforce()          # install hooks, raise on unsafe writes

Standalone check:
    python tools/db_write_guard.py --report

The guard wraps the stdlib sqlite3.Connection.execute() method so that any
script importing sqlite3 and connecting directly to the production DB will
have its write statements intercepted and logged.

Configuration:
    AMS_WRITE_GUARD=observe   (default) — log warnings, never block
    AMS_WRITE_GUARD=enforce   — raise RuntimeError on unsafe writes
    AMS_WRITE_GUARD=off       — guard disabled entirely

Safe list:
    Scripts listed in SAFE_WRITE_SCRIPTS may write directly (e.g., migration
    scripts explicitly approved for a one-time run).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sqlite3
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("ams.write_guard")

_WRITE_KEYWORDS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE)\b",
    re.IGNORECASE,
)

_PROD_DB_NAMES = {"ahmed_cement.db"}

_guard_lock = threading.Lock()
_original_execute: Callable | None = None
_original_executemany: Callable | None = None
_mode: str = "observe"
_guard_installed = False

SAFE_WRITE_SCRIPTS: set[str] = set()

LOG_PATH = Path("instance") / "write_guard.log"


def _is_prod_db(connection: sqlite3.Connection) -> bool:
    try:
        db_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        return db_path.name in _PROD_DB_NAMES
    except Exception:
        return False


def _caller_frame() -> str:
    stack = traceback.extract_stack()
    frames = []
    for frame in reversed(stack):
        fname = Path(frame.filename).name
        if fname in {"db_write_guard.py", "sqlite3.py"}:
            continue
        frames.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
        if len(frames) >= 4:
            break
    return " <- ".join(frames)


def _log_write_event(sql: str, caller: str, blocked: bool) -> None:
    status = "BLOCKED" if blocked else "OBSERVED"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{status}] SQL={sql[:120]!r}  caller={caller}\n"
    logger.warning(line.strip())
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _make_guarded_execute(original_fn: Callable) -> Callable:
    @functools.wraps(original_fn)
    def guarded(self: sqlite3.Connection, sql: str, *args, **kwargs):
        if _WRITE_KEYWORDS.match(sql or "") and _is_prod_db(self):
            caller = _caller_frame()
            script_name = Path(sys.argv[0]).name if sys.argv else ""
            if script_name in SAFE_WRITE_SCRIPTS:
                return original_fn(self, sql, *args, **kwargs)
            blocked = _mode == "enforce"
            _log_write_event(sql, caller, blocked=blocked)
            if blocked:
                raise RuntimeError(
                    f"[AMS WriteGuard] Direct sqlite3 write to production DB blocked.\n"
                    f"SQL: {sql[:200]}\n"
                    f"Use main.py ORM paths or set AMS_WRITE_GUARD=observe to allow.\n"
                    f"Caller: {caller}"
                )
        return original_fn(self, sql, *args, **kwargs)
    return guarded


def observe() -> None:
    """Install guard in observe mode — log unsafe writes, never block."""
    _install(mode="observe")


def enforce() -> None:
    """Install guard in enforce mode — raise RuntimeError on unsafe writes."""
    _install(mode="enforce")


def uninstall() -> None:
    """Remove the guard and restore original sqlite3 execute methods."""
    global _original_execute, _original_executemany, _guard_installed
    with _guard_lock:
        if not _guard_installed:
            return
        if _original_execute:
            sqlite3.Connection.execute = _original_execute
        if _original_executemany:
            sqlite3.Connection.executemany = _original_executemany
        _original_execute = None
        _original_executemany = None
        _guard_installed = False


def _install(mode: str = "observe") -> None:
    global _original_execute, _original_executemany, _guard_installed, _mode
    env_mode = os.environ.get("AMS_WRITE_GUARD", mode).lower().strip()
    if env_mode == "off":
        return
    with _guard_lock:
        if _guard_installed:
            return
        _mode = env_mode if env_mode in {"observe", "enforce"} else "observe"
        _original_execute = sqlite3.Connection.execute
        _original_executemany = sqlite3.Connection.executemany
        sqlite3.Connection.execute = _make_guarded_execute(_original_execute)
        sqlite3.Connection.executemany = _make_guarded_execute(_original_executemany)
        _guard_installed = True
        logger.info(f"[AMS WriteGuard] Installed in {_mode!r} mode. Log: {LOG_PATH}")


def status() -> dict:
    return {
        "installed": _guard_installed,
        "mode": _mode,
        "log_path": str(LOG_PATH),
        "safe_scripts": list(SAFE_WRITE_SCRIPTS),
    }


def print_report() -> None:
    print("=" * 60)
    print("AMS DB Write Guard — Status Report")
    print("=" * 60)
    s = status()
    print(f"  Guard installed : {s['installed']}")
    print(f"  Mode            : {s['mode']}")
    print(f"  Log path        : {s['log_path']}")
    print(f"  Safe scripts    : {s['safe_scripts'] or '(none)'}")
    print()
    if LOG_PATH.exists():
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if lines:
            print(f"  Last {min(20, len(lines))} log entries:")
            for ln in lines[-20:]:
                print(f"    {ln}")
        else:
            print("  Log file exists but is empty.")
    else:
        print("  No write events logged yet.")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="AMS DB Write Guard report")
    ap.add_argument("--report", action="store_true", help="Print write guard log report")
    ap.add_argument("--clear", action="store_true", help="Clear the write guard log")
    ns = ap.parse_args()
    if ns.clear and LOG_PATH.exists():
        LOG_PATH.unlink()
        print(f"Cleared: {LOG_PATH}")
    print_report()
