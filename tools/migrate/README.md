# AMS Legacy Data Migration Toolkit

Extract → Clean → Transform → Load for the legacy AMS `ALLEXPORT` workbook into a
fresh application database.  Everything here is pure pandas + stdlib (no app import
needed for steps 1–3), so it runs on any machine that can open the xlsx.

```
tools/migrate/
  README.md                       ← you are here
  _migrate_common.py              shared purge rules + cascade logic
  01_audit_legacy.py              read-only audit of the legacy workbook (gate 1)
  02_build_clean_export.py        build the purge-cleaned, import-ready xlsx
  03_verify_clean_export.py       prove the clean workbook is leak-free (gate 2)
  04_run_post_import_audit.py     run the SQL audit against the migrated DB (gate 3)
  05_load_app_db.py               load the clean export into the app DB (--confirm)
  post_import_audit.sql           the SQL verification checklist (readable form)
  post_import_enrichment.sql      derived-column backfills + hardening after import
```

## Why this exists

The app's own full-raw importer restores the export **verbatim** — it does not
filter `is_void`, so voided/cancelled records would otherwise be migrated
intact.  This toolkit applies the purge contract *before* the importer runs,
then verifies the result three times (legacy file, clean file, migrated DB).

## Run order (with gates)

```bash
# 0) prerequisites
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1) audit the legacy export (read-only). Gate: must print RESULT: PASS.
.venv/bin/python tools/migrate/01_audit_legacy.py

# 2) build the cleaned workbook + purge report (writes instance/migration/)
.venv/bin/python tools/migrate/02_build_clean_export.py
#    → instance/migration/ALLEXPORT-CLEAN-<timestamp>.xlsx

# 3) verify the cleaned workbook. Gate: must print RESULT: PASS.
.venv/bin/python tools/migrate/03_verify_clean_export.py \
    --clean instance/migration/ALLEXPORT-CLEAN-<timestamp>.xlsx
```

### 4) Load into the fresh application database

* **Scripted (recommended, what was executed):**
  ```bash
  .venv/bin/python tools/migrate/05_load_app_db.py --confirm
  #   → backs up DB, runs the app's own full-raw importer with the clean file,
  #     backfills client_code, applies post_import_enrichment.sql
  ```
* **App UI (equivalent):** Import & Export → Full Raw Import → choose the
  `ALLEXPORT-CLEAN-<timestamp>.xlsx` file, mode *replace tenant data*.  This is
  the exact code path the script drives (verified: 24,054 rows, 0 failures).

Do **not** load the original `ALLEXPORT-14-08-2026_05-51PM.xlsx` — it contains
11,663 rows that must never be transferred (see `MIGRATION_PLAN.md`).

### 5) Enrich + audit the migrated database

```bash
# derived columns (money minor units), client_id / client_code backfills,
# plaintext-password wipe:
sqlite3 instance/ahmed_cement.db < tools/migrate/post_import_enrichment.sql

# post-migration audit. Gate: must print RESULT: PASS.
.venv/bin/python tools/migrate/04_run_post_import_audit.py --db instance/ahmed_cement.db

# optional: app's own read-only consistency report
.venv/bin/python tools/consistency_report.py
```

## Purge contract (implemented in `_migrate_common.py`)

1. **Voided:** every row with `is_void == 1` is dropped.
2. **Cancelled:** `entry` rows with `type='CANCEL'` or
   `transaction_category='Cancel'` are dropped even when `is_void == 0`
   (legacy: 69 such rows were *not* flagged voided — they would have leaked).
3. **Cascade orphans:** active children of purged parents are dropped
   (`booking_item→booking`, `direct_sale_item→direct_sale`,
   `entry/pending_bill→direct_sale`, `pending_bill→booking`, `delivery_rent→direct_sale`,
   `sale_delivery_persons→direct_sale`, `waive_off→payment`,
   `material_return→payment`, `grn_item→grn`, `material_return_item→material_return`,
   `follow_up_reminder/contact→pending_bill`, …).
4. **Dangling references:** `booking_allocation` rows whose `booking_item_id`
   does not exist in the legacy data are dropped (legacy: 129 rows).

## What the gates guarantee (verified on the 2026-08-14 export)

| Check | Result |
|---|---|
| Rows in source / kept / removed | 35,717 / 24,054 / 11,663 |
| Voided rows remaining after clean | 0 |
| Cancelled entries remaining after clean | 0 |
| Dangling FKs after clean | 0 |
| Account balance vs ledger (12 accounts) | 0 mismatches |
| Material total vs entry net (66 materials) | 0 mismatches |
| Duplicate natural keys | 0 |
| Bill counters vs max sequence in data | all counters = max+1 (safe) |
| Fresh-DB import of clean file | 24,054 rows, 0 failed |
| Post-import SQL audit | PASS (all checks) |

## Tuning

* `--source` / `--clean` / `--db` flags point at alternate files.
* The purge rules live in `CASCADE_RULES` inside `_migrate_common.py`; adjust
  there only if the business decision on a rule changes (e.g. keep cancelled
  entries).  If you change a rule, re-run 01 → 02 → 03.
* Expected money totals for the `TOTAL` query in `post_import_audit.sql` are
  hard-coded in `04_run_post_import_audit.py` (`EXPECTED_TOTALS`) — update them
  if the source export changes.
