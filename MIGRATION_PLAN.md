# AMS Migration Plan — Legacy ALLEXPORT → Fresh Application Database

**Prepared:** 2026-08-17 · **Source:** `Realdata/ALLEXPORT-14-08-2026_05-51PM.xlsx` (51 sheets, 35,717 rows) · **Target:** fresh AMS SQLite DB (`instance/ahmed_cement.db`)

This plan covers the three requested deliverables: **(1) schema & column mapping**, **(2) SQL/ETL purge logic**, and **(3) post-migration verification checklist**. Every number below was computed from the actual legacy export, and the full pipeline was executed end-to-end against a fresh scratch database (24,054 rows imported with 0 failures; post-import audit PASS).

The executable toolkit lives in [`tools/migrate/`](tools/migrate/README.md); this document is the human-readable specification it implements.

---

## 1. Source-of-truth facts

| Metric | Value |
|---|---|
| Legacy file | `Realdata/ALLEXPORT-14-08-2026_05-51PM.xlsx` |
| Sheets (one per table) | 51 |
| Tables in the current app model | 63 (13 new tables start empty — see §2.3) |
| Legacy rows total | 35,717 |
| Rows transferred after purge | **24,054** |
| Rows excluded (voided / cancelled / cascade-orphans / dangling FKs) | **11,663** |

### 1.1 Why the app's built-in importer is *not* sufficient

`_run_full_raw_import_bytes` (`blueprints/import_export/engine.py`) inserts **every** non-empty row from the workbook, filtering only on primary-key duplicates. It does **not** filter `is_void`, `type='CANCEL'`, or cascade orphans. Therefore the purge must happen **before** load: the toolkit builds a cleaned workbook that the app's own importer then restores verbatim.

---

## 2. Schema & Column Mapping

### 2.1 General transformation rules (apply to every table)

| Rule | Transformation |
|---|---|
| Identity | `id` values are preserved 1:1 so all legacy FK references remain valid |
| Voided rows | `WHERE COALESCE(is_void,0) = 0` — never transferred |
| Cancelled entries | `entry` rows with `type='CANCEL'` **or** `transaction_category='Cancel'` excluded even if `is_void=0` |
| Child orphans | child rows referencing a purged parent are excluded (cascade, §3.3) |
| Strings | trimmed; empty string → `NULL` (importer `_normalize_excel_cell`) |
| Booleans | `0/1` (or `true/false`) → SQLite boolean |
| Dates/timestamps | legacy ISO text (`2026-03-09`, `2026-06-06T16:11:00`) → `DATE`/`DATETIME` (importer converts; app stores timezone-naive `Asia/Karachi` local time) |
| Money | `REAL` amount columns kept as-is; authoritative `*_minor` (paisa) mirrors derived post-import (`post_import_enrichment.sql`) |
| New columns absent from export | take model defaults (`NULL`, `0`, `False`) — see §2.2 |

### 2.2 Column-level changes: columns that exist in the model but not in the export

These columns simply do not exist in the legacy file; after a raw import they hold defaults and are backfilled/enriched where the app requires them:

| Table | Model-only columns (added after the export was made) | Post-import handling |
|---|---|---|
| `account` | `balance_minor`, `opening_balance`, `opening_balance_minor`, `opening_balance_date` | derive `balance_minor`; set `opening_balance = balance`, `opening_balance_date = created_at` (enrichment) |
| `account_transaction` | `amount_minor`, `created_at`, `created_by`, `reconciliation_id`, `source_type`, `source_id`, `voided_by`, `voided_at` | `amount_minor` derived; audit columns stay NULL (history) |
| `payment` | `amount_minor`, `discount_minor`, `client_id`, `payment_type`, `source_type`, `source_id`, `idempotency_key`, `created_at/by`, `updated_at/by` | `client_id` backfilled from client name; `payment_type` default `'Receipt'`; `idempotency_key` NULL (no re-posting risk) |
| `supplier_payment` | `amount_minor`, `payment_type`, `source_type`, `source_id`, `idempotency_key`, `created_at/by`, `updated_at/by` | `amount_minor` derived; `payment_type` default `'Payment'` |
| `direct_sale` | `client_code`, `idempotency_key` | `client_code` backfilled from client name (app startup does this too) |
| `direct_sale_item` | `cost_rate_at_sale` | NULL (FIFO cost not present in legacy) |
| `grn_item` | `is_locked` | `0` (unlocked; FIFO allocation is a new feature) |
| `booking` | `receive_in_account_id` | NULL (no FK to `account`) |
| `delivery_person` | `opening_balance`, `opening_balance_date` | `0` / NULL (legacy had no opening balances) |
| `audit_log` | `username` | NULL (derived from `user_id` by the app at runtime) |
| `settings` | (sheet is empty) | app seeds defaults at startup |

**Legacy-only columns (present in export, absent from model):** none for the transactional tables; `settings` has 3 legacy columns (`ams_openai_api_key`, `notify_daily_time`, `smtp_from`) but the sheet is empty, so nothing is lost. `__AMS_META__` is metadata, not a table.

### 2.3 New tables that start empty (created fresh by `db.create_all()`)

`account_reconciliation`, `accounting_audit_log`, `booking_allocation_repair_archive`, `cash_flow_category`, `cash_flow_entry`, `cash_flow_party`, `cash_flow_subcategory`, `grn_allocation`, `import_history_entry`, `import_job`, `import_upload`, `tenant_wipe_backup_history`, `user_login_session` — none existed in the legacy export; all migrate as empty schemas.

### 2.4 Field-by-field mapping for transactional tables

All columns not listed below map **1:1 with the same name** (`Legacy Column → New Column`) with only the general transformations of §2.1 applied. The tables below list every column that needs a decision.

#### `direct_sale` (2,452 → **2,357** kept)

| Legacy Column | New Column | Transformation |
|---|---|---|
| `is_void` | `is_void` | purge: keep only `= 0` |
| `invoice_id` | `invoice_id` | FK → `invoice.id`; verified 0 dangling |
| `client_name` | `client_name` | historical snapshot kept verbatim |
| *(absent)* | `client_code` | backfill from `client.code` by name (enrichment) |
| `payment_method`, `payment_account_id`, `bank_name`, `account_name`, `account_no` | same | passthrough |
| `amount`, `paid_amount`, `discount` | same | passthrough (REAL) |
| `manual_bill_no` / `auto_bill_no` | same | normalized on flush (`MB NO.x` / `SB-SL-####`) |
| `date_posted` | `date_posted` | ISO → DATETIME |

#### `entry` (9,919 → **4,481** kept — the single biggest purge)

| Legacy Column | New Column | Transformation |
|---|---|---|
| `is_void` | `is_void` | purge `= 1` (5,369 rows) |
| `type` | `type` | **purge `CANCEL` even when `is_void=0`** (69 rows) — legacy booking-cancellation records |
| `transaction_category` | `transaction_category` | **purge `Cancel`** (same 70 rows as `type=CANCEL`) |
| `source_module` / `source_table` / `source_id` | same | cascade: drop rows with `source_table='direct_sale'` and `source_id` → purged sale (178 rows); keep `invoice`/`booking`-sourced rows if their parent exists |
| `qty`, `material`, `client`, `bill_no`, `nimbus_no`, `date`, `time` | same | passthrough |
| `invoice_id` | `invoice_id` | FK → `invoice.id`; 0 dangling |

#### `payment` (724 → **689**)

| Legacy Column | New Column | Transformation |
|---|---|---|
| `is_void` | `is_void` | purge `= 1` (35 rows) |
| *(absent)* | `client_id` | backfill by `client.name` (enrichment); legacy negative amounts = refunds, kept |
| *(absent)* | `payment_type` | default `'Receipt'` |
| `amount`, `discount` | same + `amount_minor`, `discount_minor` | derive minor units |
| `payment_account_id` | same | FK → `account.id`; 0 dangling |

#### `pending_bill` (6,812 → **1,510**)

| Legacy Column | New Column | Transformation |
|---|---|---|
| `is_void` | `is_void` | purge `= 1` (**5,302 rows — 78% of the table**) |
| `source_module`/`source_table`/`source_id` | same | cascade: `source_table='direct_sale'`→purged sale (34 rows); `source_table='booking'`→purged booking (15 rows) |
| `bill_no`, `bill_kind` | same | `bill_kind` re-derived on flush from `bill_no` |
| `amount`, `is_paid`, `is_cash`, `is_manual` | same | passthrough |

#### `invoice` (2,197 → **2,197**) — no voids in legacy; statuses `OPEN 954 / PAID 1,220 / PARTIAL 23`

#### `account_transaction` (838 → **759**)

| Legacy Column | New Column | Transformation |
|---|---|---|
| `is_void` | `is_void` | purge `= 1` (79 rows) |
| `from_account_id` / `to_account_id` | same | FK → `account.id`; 0 dangling; used for ledger identity §4.2 |
| `transaction_type` | same | passthrough (Receipt/Loss/Payment/Transfer/Adjustment/Expense/Supplier Payment) |
| *(absent)* | `amount_minor` | derived |

#### `booking` (398 → **381**) · `booking_item` (905 → **865**, cascade 40) · `grn` (58 → **48**) · `grn_item` (57 → **48**, cascade 9) · `material_return` (83 → **72**, cascade 11) · `material_return_item` (113 → **98**, cascade 15) · `delivery_rent` (987 → **963**, cascade 24) · `sale_delivery_persons` (2,745 → **2,633**, cascade 110) · `waive_off` (377 → **370**, cascade 4) · `supplier_payment` (78 → **78**) · `fbm_cash_drawer_entry` (2 → 2) · `follow_up_reminder` (19 → **16**, cascade 3) · `follow_up_contact` (4 → 4) · `direct_sale_item` (4,503 → **4,339**, cascade 164) · `delivery_person_payment` (0 → 0)

All follow the same pattern: `is_void=0` filter + cascade rule from §3.3; remaining columns pass through 1:1.

#### `booking_allocation` (1,309 → **1,007**) — special case

| Legacy Column | New Column | Transformation |
|---|---|---|
| `is_void` | `is_void` | purge `= 1` (152 rows) |
| `sale_id`, `sale_item_id` | same | FK checks against kept `direct_sale`/`direct_sale_item` |
| `booking_item_id` | same | **purge rows whose `booking_item_id` is missing from legacy data or belongs to a purged booking** (161 rows) — these are dangling allocation links (see §6 caveats) |

#### Master tables (kept in full)

`client` (305), `material` (66), `material_category` (12), `account` (12), `account_category` (6), `supplier` (6), `delivery_person` (13), `user` (7), `bill_counter` (6), `audit_log` (694), `fbm_cash_drawer_category` (6), `fbm_rental_item` (1), `direct_sale_draft` (2), `cash_flow_difference_adjustment` (1), plus empty config sheets (`settings`, `schema_version`, `root_*`, `staff_email`, `system_lock`, `recon_basket`, `fbm_client`, `fbm_rental`, `delivery`, `delivery_item`, `future_account_audit_log`, `tenant_wipe_backup_history`, `cash_flow_reconciliation_audit`). All map 1:1. **Inactive master rows are preserved** (`client.is_active=0`: 3 of 305; `material.is_active=0`: 1 of 66) because transactional FKs reference them; they are reported, not purged.

---

## 3. SQL / ETL purge logic

### 3.1 Voided-record filter (per-table)

```sql
-- Canonical filter applied to every transactional table
SELECT * FROM <table>
WHERE COALESCE(is_void, 0) = 0;
```

The legacy export is a literal table dump, so the ETL simply drops rows before
writing the clean workbook.  Rows removed by `is_void` per table:

| Table | voided rows | Table | voided rows |
|---|---|---|---|
| pending_bill | 5,302 | delivery_rent | 24 |
| entry | 5,369 | material_return | 11 |
| direct_sale | 95 | grn | 10 |
| account_transaction | 79 | waive_off | 7 |
| booking_allocation | 152 | booking | 17 |
| sale_delivery_persons | 112 | grn_item | 0 |
| payment | 35 | invoice / supplier_payment / others | 0 |

### 3.2 Cancelled-entry filter (the is_void=0 trap)

```sql
-- Legacy "booking cancellation" rows are marked CANCEL but NOT voided.
-- They must be excluded even though is_void = 0 (69 rows in the export).
SELECT * FROM entry
WHERE is_void = 0
  AND ( UPPER(COALESCE(type,'')) = 'CANCEL'
     OR UPPER(COALESCE(transaction_category,'')) = 'CANCEL' );
```

### 3.3 Cascade-orphan filters (children of purged parents)

```sql
-- Example pattern; the toolkit applies every rule below automatically.
DELETE FROM booking_item          WHERE booking_id  IN (SELECT id FROM booking      WHERE COALESCE(is_void,0)=1);
DELETE FROM direct_sale_item      WHERE sale_id     IN (SELECT id FROM direct_sale  WHERE COALESCE(is_void,0)=1);
DELETE FROM booking_allocation    WHERE sale_id     IN (SELECT id FROM direct_sale  WHERE COALESCE(is_void,0)=1);
DELETE FROM booking_allocation    WHERE sale_item_id NOT IN (SELECT id FROM direct_sale_item);
DELETE FROM booking_allocation    WHERE booking_item_id NOT IN (SELECT id FROM booking_item);
DELETE FROM entry                 WHERE source_table='direct_sale' AND source_id IN (SELECT id FROM direct_sale WHERE COALESCE(is_void,0)=1);
DELETE FROM pending_bill          WHERE source_table='direct_sale' AND source_id IN (SELECT id FROM direct_sale WHERE COALESCE(is_void,0)=1);
DELETE FROM pending_bill          WHERE source_table='booking'     AND source_id IN (SELECT id FROM booking     WHERE COALESCE(is_void,0)=1);
DELETE FROM delivery_rent         WHERE sale_id IN (SELECT id FROM direct_sale WHERE COALESCE(is_void,0)=1);
DELETE FROM sale_delivery_persons WHERE sale_id IN (SELECT id FROM direct_sale WHERE COALESCE(is_void,0)=1);
DELETE FROM waive_off             WHERE payment_id IN (SELECT id FROM payment WHERE COALESCE(is_void,0)=1);
DELETE FROM material_return       WHERE payment_id IN (SELECT id FROM payment WHERE COALESCE(is_void,0)=1);
DELETE FROM grn_item              WHERE grn_id IN (SELECT id FROM grn WHERE COALESCE(is_void,0)=1);
DELETE FROM material_return_item  WHERE material_return_id NOT IN (SELECT id FROM material_return);
DELETE FROM follow_up_reminder    WHERE pending_bill_id NOT IN (SELECT id FROM pending_bill);
DELETE FROM follow_up_contact     WHERE pending_bill_id NOT IN (SELECT id FROM pending_bill);
```

Rows removed by cascade (actual counts):

| Rule | rows | Rule | rows |
|---|---|---|---|
| `booking_item → voided booking` | 40 | `waive_off → voided payment` | 4 |
| `direct_sale_item → voided direct_sale` | 164 | `material_return → voided payment` | 11 |
| `booking_allocation → voided direct_sale` | 60 | `grn_item → voided grn` | 9 |
| `entry.source_id → voided direct_sale` | 178 | `material_return_item → voided material_return` | 15 |
| `pending_bill.source_id → voided direct_sale` | 34 | `follow_up_reminder → voided pending_bill` | 3 |
| `pending_bill.source_id → voided booking` | 15 | `booking_allocation → booking_item never existed` | 129 |
| `delivery_rent → voided direct_sale` | 24 | `booking_allocation → booking_item of voided booking` | 32 |
| `sale_delivery_persons → voided direct_sale` | 110 | | |

(Some rows match two predicates, e.g. an allocation of a voided sale that also has a dangling `booking_item_id`; the toolkit de-duplicates. Net effect: 11,663 rows removed across all sheets.)

### 3.4 Why no post-load DELETE is needed

The purge runs on the **source workbook** (`02_build_clean_export.py`). The app's importer then performs a clean-room load into the fresh DB — nothing voided or orphaned ever touches the target, so the target starts with clean unique keys, clean FKs, and no history of purged rows.

### 3.5 Post-import enrichment (sequence / unique / FK hardening)

```sql
-- post_import_enrichment.sql (runs after the import; all idempotent)
UPDATE account SET balance_minor = CAST(ROUND(COALESCE(balance,0)*100) AS INTEGER), ...;
UPDATE account_transaction SET amount_minor = CAST(ROUND(COALESCE(amount,0)*100) AS INTEGER);
UPDATE payment SET amount_minor=..., discount_minor=...;
UPDATE supplier_payment SET amount_minor=...;
UPDATE payment SET client_id = (SELECT c.id FROM client c
    WHERE LOWER(TRIM(c.name)) = LOWER(TRIM(payment.client_name)) LIMIT 1)
  WHERE client_id IS NULL AND TRIM(COALESCE(client_name,'')) <> '';
UPDATE direct_sale SET client_code = (SELECT c.code FROM client c
    WHERE LOWER(TRIM(c.name)) = LOWER(TRIM(direct_sale.client_name)) LIMIT 1)
  WHERE TRIM(COALESCE(client_code,'')) = '';
UPDATE user SET password_plain = NULL;   -- security: never ship legacy plaintext
```

**Sequence safety (verified):** SQLite allocates the next `id` as `MAX(id)+1`, so
no reset is needed. `bill_counter` was imported verbatim and each namespace
counter is exactly `max sequence in data + 1`:

| namespace | counter | max SB-#### in data | collision? |
|---|---|---|---|
| GEN | 1000 | — | no |
| BK | 1391 | 1390 | no |
| SL | 3449 | 3448 | no |
| CP | 1695 | 1694 | no |
| RTN | 1083 | 1082 | no |
| GRN | 1058 | 1057 | no |

---

## 4. Data Verification Checklist (post-migration audit)

Run `tools/migrate/04_run_post_import_audit.py` or `post_import_audit.sql`
against the migrated DB.  A healthy database returns **zero rows** for checks
1–5 and 7–8, and the expected totals for check 10.

### 4.1 No voided records leaked

```sql
SELECT 'VOID LEAK' AS check_name, 'booking' AS tbl, COUNT(*) AS violations
FROM booking WHERE COALESCE(is_void,0) != 0
UNION ALL /* ... every table with is_void ... */;
-- EXPECTED: 0 rows
```

### 4.2 No cancelled entries leaked

```sql
SELECT COUNT(*) FROM entry
WHERE COALESCE(is_void,0) = 0
  AND (UPPER(COALESCE(type,'')) = 'CANCEL'
    OR UPPER(COALESCE(transaction_category,'')) = 'CANCEL');
-- EXPECTED: 0
```

### 4.3 Zero orphaned foreign keys

```sql
-- Pattern (28 FK pairs are checked; see post_import_audit.sql)
SELECT COUNT(*) FROM booking_allocation b
WHERE NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = b.sale_id);
-- EXPECTED: 0 for every pair
```

### 4.4 Ledger identity — accounts (debits/credits balance perfectly)

```sql
SELECT * FROM (
  SELECT a.name, ROUND(ABS(a.balance - (
      SELECT COALESCE(SUM(CASE WHEN t.to_account_id = a.id THEN t.amount ELSE 0 END),0)
           - COALESCE(SUM(CASE WHEN t.from_account_id = a.id THEN t.amount ELSE 0 END),0)
      FROM account_transaction t WHERE COALESCE(t.is_void,0) = 0)), 2) AS diff
  FROM account a) WHERE diff > 0.01;
-- EXPECTED: 0 rows (verified: 12/12 accounts match)
```

### 4.5 Ledger identity — inventory (material totals)

```sql
SELECT * FROM (
  SELECT m.name, ROUND(ABS(m.total - (
      SELECT COALESCE(SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN e.qty
                               WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -e.qty ELSE 0 END),0)
      FROM entry e WHERE COALESCE(e.is_void,0)=0 AND TRIM(COALESCE(e.material,'')) = TRIM(m.name))), 2) AS diff
  FROM material m) WHERE diff > 0.01;
-- EXPECTED: 0 rows (verified: 66/66 materials match)
```

### 4.6 Client ledger summary (informational; 305 rows)

Uses the app's own identity: `opening_balance + Σ booking.amount + Σ direct_sale.amount − Σ booking.paid − Σ ds.paid − Σ payments(≥0) + Σ payments(<0) − Σ waive_off`. Export to Excel and eyeball per-client totals against the legacy manual ledger.

### 4.7 Sequence / unique-key integrity

```sql
-- bill counters must be > max sequence in data (see post_import_audit.sql §7)
-- duplicate natural keys must be 0:
SELECT code FROM client GROUP BY UPPER(TRIM(code)) HAVING COUNT(*) > 1;  -- EXPECTED: 0
```

### 4.8 Expected money totals (must match the clean-source sums)

| Ledger total | Expected (clean) |
|---|---|
| direct_sale.amount | 23,719,927.40 |
| direct_sale.paid_amount | 7,291,719.50 |
| payment.amount | 53,923,466.95 |
| pending_bill.amount | 16,519,225.37 |
| invoice.total_amount | 19,467,450.87 |
| invoice.balance | 18,822,430.57 |
| account_transaction.amount | 288,365,494.23 |
| booking.amount | 133,672,972.73 |
| booking.paid_amount | 88,044,330.99 |
| waive_off.amount | 350,430.49 |
| material_return.amount | 1,225,363.30 |
| supplier_payment.amount | 28,392,809.99 |
| delivery_rent.amount | 1,263,529.00 |

### 4.9 Final smoke test

Run the app's own read-only `python tools/consistency_report.py` — the same
checks (account balances, material totals, orphaned payments/transactions,
sales↔entries, invoices↔sales, bookings↔pending bills) must report **OK / no
issues**.

---

## 5. Operational runbook

```bash
.venv/bin/python tools/migrate/01_audit_legacy.py                       # gate 1: PASS
.venv/bin/python tools/migrate/02_build_clean_export.py                 # → instance/migration/ALLEXPORT-CLEAN-*.xlsx
.venv/bin/python tools/migrate/03_verify_clean_export.py --clean <file> # gate 2: PASS
# fresh DB → Import & Export → Full Raw Import → clean file (24,054 rows, 0 failed)
sqlite3 instance/ahmed_cement.db < tools/migrate/post_import_enrichment.sql
.venv/bin/python tools/migrate/04_run_post_import_audit.py              # gate 3: PASS
.venv/bin/python tools/consistency_report.py                            # app health check: OK
```

---

## 6. Known legacy data caveats (handled, but worth knowing)

1. **69 `entry` rows are cancelled but not flagged voided** (`type='CANCEL'`,
   `transaction_category='Cancel'`, note `Booking cancellation|rate=…|amount=…`).
   The plan excludes them; without this rule they would silently migrate.
2. **`booking_allocation` has 161 rows with broken booking-item references**
   (129 point at `booking_item` ids that never existed in the data, 32 point at
   items belonging to voided bookings). They are dropped along with the 152
   voided rows and 60 rows under voided sales (sets overlap; 302 unique rows
   removed, 1,309 → 1,007). Allocation-only sales (mostly `amount=0` linkage
   rows for client *ZEESHAN ATTARI SB JPS* etc.) keep their sale/sale-item
   records intact.
3. **Negative stock is a legacy reality:** 49 of 66 materials have `total < 0`.
   This is preserved (the app allows `allow_global_negative_stock`); the
   material↔entry identity still holds.
4. **`payment` rows with negative amounts** (61) are refunds; kept as-is.
5. **`account_transaction` includes one 200,000,000 `Adjustment`** — a legacy
   opening-balance-style adjustment; kept as-is (it is part of the balanced
   12-account ledger).
6. **Inactive masters preserved:** 3 inactive clients and 1 inactive material
   are transferred for FK integrity and reported, not purged.
7. **Legacy plaintext passwords** in `user.password_plain` are wiped post-import;
   `password_hash` remains authoritative for login.
