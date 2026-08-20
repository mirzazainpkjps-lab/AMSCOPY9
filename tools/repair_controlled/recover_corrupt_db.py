#!/usr/bin/env python3
"""
Recover a corrupt SQLite database (AMS production DB)
=====================================================
Detects ``sqlite3.DatabaseError: database disk image is malformed`` /
``SQLITE_CORRUPT`` and rebuilds the cleanest possible copy using the sqlite3
CLI's ``.recover`` command, which salvages every page SQLite can still read.

Also offers a ``--fresh`` mode to discard a corrupt file and let the app
recreate an empty database.

Safety guarantees
-----------------
  * ``--confirm`` is required before anything is moved or removed.
  * The original ``.db`` plus its ``-wal`` / ``-shm`` files are always copied
    aside first (never deleted — they are only renamed with a timestamp).
  * The corrupt original is preserved; nothing is overwritten in place.

Usage
-----
    # Read-only: is the DB actually corrupt?
    python tools/repair_controlled/recover_corrupt_db.py --check

    # Salvage as much data as possible into a fresh copy, then swap it in:
    python tools/repair_controlled/recover_corrupt_db.py --confirm

    # Give up on the corrupt file and let the app start empty:
    python tools/repair_controlled/recover_corrupt_db.py --confirm --fresh

    # Operate on a non-default DB path:
    python tools/repair_controlled/recover_corrupt_db.py --confirm --db /path/to/db.sqlite

Requirements
------------
    The recovery path uses the ``sqlite3`` command-line tool (the ``.recover``
    dot-command is not exposed by Python's sqlite3 module). Install it with:

        Termux:  pkg install sqlite
        Debian/Ubuntu: apt install sqlite3
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _REPO_ROOT / "instance" / "ahmed_cement.db"
_BACKUP_ROOT = _REPO_ROOT / "instance" / "recover_backups"
_AUDIT_LOG = _REPO_ROOT / "instance" / "repair_audit.log"


def _resolve_db_path(cli_value: str | None) -> Path:
    env = os.environ.get("APP_DB_PATH", "").strip()
    raw = cli_value or env
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_DB


def _quick_check(path: Path) -> tuple[bool, str]:
    """Return (is_ok, detail). True only when SQLite reports ``ok``."""
    try:
        con = sqlite3.connect(path)
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if len(rows) == 1 and rows[0][0] == "ok":
        return True, "ok"
    detail = " | ".join(" ".join(str(c) for c in r) for r in rows[:5]) or "unknown"
    return False, detail


def _table_counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for t in tables:
            counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return counts


def _log(message: str) -> None:
    print(message)


def _audit(script_label: str, description: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    argv = " ".join(sys.argv[1:]) or "(no args)"
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(
                f"[{ts}] script={script_label!r} user={user!r} args={argv!r} "
                f"description={description!r}\n"
            )
    except Exception as exc:
        _log(f"[WARN] could not write audit log: {exc}")


def _backup(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = _BACKUP_ROOT / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            shutil.copy2(src, dest_dir / src.name)
            _log(f"  backed up: {src.name}")
    return dest_dir


def _require_sqlite3_cli() -> str:
    exe = shutil.which("sqlite3")
    if not exe:
        _log(
            "\nERROR: the `sqlite3` command-line tool is not installed.\n"
            "  Termux:        pkg install sqlite\n"
            "  Debian/Ubuntu: apt install sqlite3\n"
            "The Python sqlite3 module cannot run the `.recover` command.\n"
        )
        sys.exit(1)
    return exe


def _recover(db_path: Path, exe: str, keep_sql: bool) -> Path:
    """Run sqlite3's `.recover`, import the dump, return the recovered file."""
    work = _BACKUP_ROOT / "recovered"
    work.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump = work / f"recovered_{stamp}.sql"
    recovered = work / f"recovered_{stamp}.db"

    _log("\n[1/3] Extracting recoverable SQL from the corrupt file…")
    with open(dump, "w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [exe, str(db_path), ".recover"],
            stdout=fh,
            stderr=subprocess.PIPE,
            text=True,
        )
    if proc.returncode != 0:
        _log(f"[WARN] sqlite3 .recover exited {proc.returncode}: {proc.stderr.strip()}")
    if dump.stat().st_size == 0:
        _log("\nERROR: .recover produced no output — nothing recoverable was found.\n"
             "  Try `--fresh` to start over with an empty database instead.\n")
        sys.exit(1)

    _log(f"  recovered SQL dump: {dump} ({dump.stat().st_size} bytes)")

    _log("\n[2/3] Loading the recovered SQL into a clean database…")
    with open(dump, "r", encoding="utf-8") as fh:
        proc = subprocess.run(
            [exe, str(recovered)],
            stdin=fh,
            stderr=subprocess.PIPE,
            text=True,
        )
    if proc.returncode != 0:
        _log(f"\nERROR: failed to import recovered SQL (exit {proc.returncode}):\n"
             f"{proc.stderr.strip()[:2000]}\n")
        sys.exit(1)

    if not keep_sql:
        dump.unlink(missing_ok=True)
    return recovered


def _verify_and_report(original: Path, recovered: Path) -> None:
    ok, detail = _quick_check(recovered)
    _log("\n[3/3] Verifying the recovered database…")
    if not ok:
        _log(f"  ERROR: recovered DB still fails quick_check: {detail}")
        sys.exit(1)
    _log("  recovered DB passes quick_check (ok)")

    # Best-effort row-count comparison so the operator can see what was lost.
    try:
        orig_counts = _table_counts(original)
    except Exception:
        orig_counts = {}
    rec_counts = _table_counts(recovered)

    _log(f"  tables recovered: {len(rec_counts)}")
    if orig_counts:
        lost = 0
        for t, n in sorted(orig_counts.items()):
            r = rec_counts.get(t, 0)
            if r < n:
                _log(f"  !! {t}: {n} -> {r} rows ({n - r} unrecoverable)")
                lost += n - r
        if lost:
            _log(f"\n  NOTE: {lost} total row(s) could not be recovered. "
                 "Back up / reconcile any affected data from paper records.")
        else:
            _log("  all row counts match the original — no data appears to be missing.")
    else:
        _log("  (could not read the original file for a row-count comparison)")


def _swap_in(original: Path, recovered: Path, backup_dir: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parked = Path(str(original) + f".corrupt-{stamp}")

    # Move the corrupt file (and its WAL/shm) out of the way, keep it.
    original.rename(parked)
    _log(f"\n  parked corrupt file as: {parked.name}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(original) + suffix)
        if sidecar.exists():
            parked_sidecar = Path(str(parked) + suffix)
            sidecar.rename(parked_sidecar)
            _log(f"  parked {sidecar.name} as {parked_sidecar.name}")

    # Move the recovered copy into place.
    shutil.move(str(recovered), str(original))
    _log(f"  installed recovered database as: {original.name}")

    _log(f"\nDONE. Backup of the corrupt files: {backup_dir}")
    _log("Next step: restart the app (flask run / your normal launcher).")


def _fresh_start(original: Path, backup_dir: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parked = Path(str(original) + f".corrupt-{stamp}")
    original.rename(parked)
    _log(f"\n  parked corrupt file as: {parked.name}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(original) + suffix)
        if sidecar.exists():
            sidecar.rename(Path(str(parked) + suffix))
            _log(f"  parked {sidecar.name}")

    snapshot = original.parent / "health_snapshot.json"
    if snapshot.exists():
        snapshot.rename(snapshot.with_suffix(".json.bak"))
        _log(f"  moved health_snapshot.json -> health_snapshot.json.bak "
             "(so the startup drop-guard does not block the empty DB)")

    _log(f"\nDONE. Backup of the corrupt files: {backup_dir}")
    _log("Next step — start the app ONCE with empty-DB allowed so it rebuilds schema:\n")
    _log("    ALLOW_EMPTY_DB=1 python main.py   # or: ALLOW_EMPTY_DB=1 flask run\n")
    _log("After the first successful start you can drop ALLOW_EMPTY_DB=1 again.")
    _log("A default admin (Admin / Admin@fbm12345) is recreated automatically.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover a corrupt SQLite AMS database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="Database path (default: APP_DB_PATH or instance/ahmed_cement.db)")
    parser.add_argument("--check", action="store_true",
                        help="Read-only: report integrity, make no changes.")
    parser.add_argument("--confirm", action="store_true",
                        help="Required to actually move/remove files.")
    parser.add_argument("--fresh", action="store_true",
                        help="Discard the corrupt file and prepare a fresh empty DB.")
    parser.add_argument("--keep-sql", action="store_true",
                        help="Keep the intermediate recovered .sql dump.")
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    script_label = Path(__file__).name

    if args.check:
        ok, detail = _quick_check(db_path)
        print(f"DB: {db_path}")
        print(f"integrity: {'ok' if ok else 'CORRUPT — ' + detail}")
        return 0 if ok else 1

    if not db_path.exists():
        _log(f"ERROR: database not found at {db_path}")
        return 1

    ok, detail = _quick_check(db_path)
    _log(f"DB: {db_path}")
    _log(f"quick_check: {'ok' if ok else detail}")
    if ok:
        _log("\nThis database passes quick_check — it does not appear to be corrupt.")
        _log("If the app still errors, check for a stale -wal/-shm next to the file,"
             " or run `python tools/consistency_report.py` for data-level issues.")
        return 0

    if not args.confirm:
        _log("\n[AMS RepairGuard] STOPPED — this script moves/removes database files.\n"
             "  Re-run with --confirm to proceed:\n\n"
             f"    python tools/repair_controlled/{script_label} --confirm\n"
             "  (or --confirm --fresh to start over empty)\n")
        return 1

    backup_dir = _backup(db_path)
    _audit(script_label, description=f"{'fresh start' if args.fresh else 'recover'} for {db_path}")
    _log(f"\nBacked up the corrupt file(s) to: {backup_dir}\n")

    if args.fresh:
        _fresh_start(db_path, backup_dir)
        return 0

    exe = _require_sqlite3_cli()
    recovered = _recover(db_path, exe, args.keep_sql)
    _verify_and_report(db_path, recovered)
    _swap_in(db_path, recovered, backup_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("\nAborted.")
        sys.exit(130)
