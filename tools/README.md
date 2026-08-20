# AMS System — Tools Directory

This directory contains all operational scripts, organized by safety level.
**The production application is in the root (`main.py`, `models.py`, `blueprints/`).**
Nothing in this folder is loaded at runtime.

---

## Quick Reference

| Command | Purpose |
|---------|---------|
| `python tools/consistency_report.py` | Full data integrity check (read-only) |
| `python tools/consistency_report.py --json` | Machine-readable JSON output |
| `python tools/db_write_guard.py --report` | Show write guard log |
| `python tools/repair_controlled/repair_guard.py` | Check repair audit log |

---

## Folder Structure

```
tools/
  consistency_report.py       ← ONE-COMMAND system health check (read-only)
  db_write_guard.py           ← Write-path safety observer (additive, no blocking by default)

  read_only/                  ← Safe to run any time. Never write to DB.
    cash_flow_audit.py          Cash flow per day
    cash_flow_audit_v2.py       Cash flow with date range
    cash_flow_audit_detail.py   Detailed cash flow wrapper
    check_accounts.py           Balance/transaction sum comparison
    check_cash_flow_gaps.py     Dates with missing cash flow records
    check_current_db.py         Read model row counts
    debug_cash_flow29.py        May 29-30 cash flow snapshot
    debug_dates.py              Last 10 cash flow dates
    export_corrupted_remediation_prep.py  Export corrupted SB-CP payments (read-only)
    inspect_bill.py             Read all records for a bill number
    investigate_26_cash_tx.py   May 26 AccountTransaction diagnostic
    investigate_cash_flow_sources.py  Cash flow sources reader
    list_users_and_passwords.py List User records (hashed passwords)
    remediation_dryrun_script.py  Dry-run remediation analyzer (no writes)
    scan_invisible_bills.py     Bills with missing active derived effects
    scan_zip_dbs.py             Find SQLite databases on the filesystem
    validate_erp_consistency.py Lightweight ERP validation via sqlite3
    verify_data_sources.py      Count records in local sqlite

  repair_controlled/          ← Writes to DB. Requires --confirm flag. Takes backup first.
    repair_guard.py             Preflight check (called by all scripts here)
    repair_erp_consistency.py   Rebuild ERP derived ledgers via ORM
    repair_direct_sale_duplicates.py  Void duplicate sale/entry/pending rows (raw sqlite3)
    repair_exact_bill_duplicates.py   Remove exact duplicate items (raw sqlite3)
    fix_accounts_and_test.py    Reset User passwords/roles
    recover_corrupt_db.py       Salvage a corrupt SQLite DB (.recover) or reset it (--fresh)

  tests_isolated/             ← Integration tests. Require AMS_TEST_DB env var.
    test_isolation_guard.py     Guard — aborts if AMS_TEST_DB not set
    run_refund_integration_test.py
    verify_refund_flow.py
    verify_refund_full.py
    verify_refund_quick.py

  deprecated/                 ← Completed one-time operations. DO NOT RUN AGAIN.
    mb8380 fixes               (cleanup_mb8380, fix_mb8380, recover_mb8380, check_*)
    migrate_to_single_store.py (schema migration — DONE)
    import_data.py             (data import — DONE)
    import_missing_table.py    (sale_delivery_persons import — DONE)
    (dead scripts with Windows paths — check_db.py, staging_wipe_test.py, etc.)
```

---

## Safety Levels

### `read_only/` — Zero Risk
Run freely. All scripts open the DB as read-only or only print results.

```bash
python tools/read_only/cash_flow_audit.py
python tools/read_only/inspect_bill.py MB1234
python tools/read_only/check_accounts.py
```

### `repair_controlled/` — Requires Explicit Confirmation
Every script calls `repair_guard.preflight()` which:
1. Requires `--confirm` flag (no accidental runs)
2. Takes a DB backup to `instance/reconcile_backups/` first
3. Logs the action to `instance/repair_audit.log`

```bash
# Will STOP and print instructions unless --confirm is given:
python tools/repair_controlled/repair_erp_consistency.py --confirm
python tools/repair_controlled/repair_erp_consistency.py --confirm --client-id 71
python tools/repair_controlled/repair_exact_bill_duplicates.py --confirm
```

### `tests_isolated/` — Requires Test DB
Every script calls `require_test_db()` which aborts if `AMS_TEST_DB` is not set.

```bash
# Will STOP unless AMS_TEST_DB is set:
AMS_TEST_DB=/tmp/ams_test.db python tools/tests_isolated/verify_refund_quick.py
```

### `deprecated/` — Never Run
These are archived for historical reference. They either:
- Point to Windows paths that don't exist on this system, or
- Were one-time migrations that completed successfully

Running any of these again risks data corruption or duplicate records.

---

## Consistency Report

The fastest way to check system health:

```bash
python tools/consistency_report.py
```

Checks (all read-only):
1. Account.balance vs AccountTransaction ledger sums
2. Material.total vs Entry IN/OUT net stock
3. Orphaned Payments (no linked Client)
4. Orphaned AccountTransactions (referencing missing accounts)
5. DirectSale with missing stock Entry rows
6. Credit DirectSale with missing PendingBill
7. Active Invoices with no linked DirectSale
8. Bookings with missing PendingBill
9. Health snapshot freshness

```bash
# JSON output (for scripts/CI):
python tools/consistency_report.py --json

# Exit code 1 on FAIL (for CI):
python tools/consistency_report.py --fail-on-error
```

---

## Write Guard (Observe Mode)

The write guard (`tools/db_write_guard.py`) can be imported anywhere to log
direct sqlite3 write attempts against the production database.

Currently in **observe mode** — it logs warnings but never blocks.

```bash
# View log of observed raw sqlite3 writes:
python tools/db_write_guard.py --report

# Clear log:
python tools/db_write_guard.py --clear --report
```

To enable in a script:
```python
import sys, os
sys.path.insert(0, '.')
from tools.db_write_guard import WriteGuard  # or: observe()
```

Environment variable control:
```
AMS_WRITE_GUARD=observe    (default) log warnings, never block
AMS_WRITE_GUARD=enforce    raise RuntimeError on unsafe direct writes
AMS_WRITE_GUARD=off        disable entirely
```
