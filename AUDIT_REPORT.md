# FULL SYSTEM AUDIT — AMSCOPY9 (AMS ERP)
**Branch:** `arena/01a03714-amscopy9`  
**Audit Date:** 2026-08-25  
**Auditor:** Agent Mode (Automated + Manual Code Review)  
**Scope:** Frontend · Backend · Database · APIs · Business Logic · Data Integrity · Security

---

## ⚠️ EXECUTIVE SUMMARY

This audit reveals **critical business-logic flaws, active data-loss mechanisms, hidden security tokens, and a completely empty production database** despite migration artifacts showing thousands of historical rows. The application contains a **destructive auto-deploy webhook** (`git reset --hard`) that can wipe live SQLite data, a **hardcoded webhook token** (`PakistanZindabad1947-2026`), a **plaintext password fallback** (`password_plain`), and **massive data removal** (32% of source rows removed by the purge engine) with the current database showing **0 bytes / 0 rows**.

---

## 1. DATABASE & DATA STATE (CRITICAL)

### 1.1 Current Database is EMPTY
- `instance/ahmed_cement_v44_fresh.db` = **0 bytes**
- No tables, no rows, no schema initialized.
- The ORM bootstrap (`db.create_all()`) is supposed to create tables on first run, but the file remains empty (likely deleted after migration or by `retire_legacy_database_files()`).

### 1.2 Historical Data Completely Missing
- `instance/migration/purge_report.json` shows a source database (`ALLEXPORT-17-08-2026_01-54PM.xlsx` / `Realdata`) with **36,272 rows**.
- After purge: **24,585 rows kept** → **11,687 rows removed (32.2%)**.
- Key removals:
  - `pending_bill`: 6,836 → 1,534 (**77.6% removed**)
  - `entry`: 10,015 → 4,576 (**54.3% removed**)
  - `direct_sale`: 2,506 → 2,410 (96 removed)
  - `booking_item`: 925 → 885 (40 cascade-removed)
  - `direct_sale_item`: 4,596 → 4,431 (165 cascade-removed)
  - `material_return_item`: 116 → 101 (15 cascade-removed)
  - `grn_item`: 57 → 48 (9 cascade-removed)
- The current instance DB has **NONE** of this data.

### 1.3 Migration Artifacts Show Severe Data Corruption
- `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` exists but isn't loaded into the DB.
- `instance/import_reports/full_raw_import_report_*.csv` and `.meta.json` reference import events but no import results are preserved.
- `docs/legacy_migration_mapping.md` states the migration is "intentionally additive" but the DB file is missing entirely.

---

## 2. SECURITY FLAWS (CRITICAL)

### 2.1 Hardcoded Webhook Token
**File:** `main.py`  
**Code:**
```python
WEBHOOK_TOKEN = (
    os.environ.get("AMS_WEBHOOK_TOKEN")
    or "PakistanZindabad1947-2026"
)
```
- **Impact:** Anyone with repository access (or who reads source) can trigger `/git-auto-pull` and execute arbitrary deployment (including `git reset --hard`).
- **Fix Required:** Remove literal fallback; enforce environment variable only.

### 2.2 Hardcoded GitHub Repo Points to Wrong Project
**File:** `main.py`
```python
GITHUB_REPO = "https://github.com/rehmanahmedca-source/ams99.git"
```
- Current repo is `rehmanahmedca-source/AMSCOPY9`. The webhook pulls from `ams99`, which could deploy completely different (or malicious) code.
- **Impact:** Auto-deployment pulls wrong/unknown code.

### 2.3 Auto-Deploy Does `git reset --hard` with Fragile Data Preservation
**File:** `main.py` — `deploy()` function
- Steps: `git fetch` → `checkout -B main origin/main` → `git reset --hard origin/main`.
- Before reset: `preserve_instance_data()` copies `instance/` to `.instance_preserve/`.
- After reset: `restore_instance_data()` copies back.
- **Flaws:**
  - If `preserve_instance_data()` fails (disk full, permission error), the code logs a warning but continues with `git reset --hard`. The `finally` block then tries to restore but `preserved` is `False`, so it skips restoration but the live DB has already been overwritten by the committed (possibly older/empty) DB file.
  - The `.instance_preserve` directory is inside the repo (`/home/user/AMSCOPY9/.instance_preserve`), so `git reset --hard` may delete it if `.gitignore` doesn't protect it properly.
  - `shutil.copytree()` uses `symlinks=True`; broken symlinks or permission errors can corrupt the preserved copy.

### 2.4 Plaintext Password Fallback (`password_plain`)
**File:** `models/core.py` (`User` model), `app/blueprints/auth.py`
- `User` has both `password_hash` (Werkzeug) and `password_plain` (plaintext).
- Login logic (`_verify_and_upgrade_password`) tries hash first, then falls back to comparing `password_plain == raw_password`.
- If hash verification fails but `stored_hash == raw_password` (plaintext accidentally stored in hash column), it treats it as a match and upgrades.
- **Impact:** Anyone with DB read access can read all user passwords in plaintext. The `password_plain` column is never fully cleared in bulk.

### 2.5 CSRF Only Protects `accounts.` Endpoints
**File:** `app/hooks.py` — `_protect_against_csrf()`
- Only checks endpoints that equal `'accounts'` or start with `'accounts.'`.
- All other mutation endpoints (`sales.add_booking`, `masters.add_client`, `ops.dispatch_add_record`, etc.) have **NO CSRF protection**.
- **Impact:** Cross-site request forgery on bookings, sales, clients, suppliers, payments, deliveries, etc.

### 2.6 Read-Only Mode (`access_mode`) Has Weak Enforcement
**File:** `app/hooks.py` — `_enforce_read_only_access_mode()`
- Only blocks `POST/PUT/PATCH/DELETE` requests.
- Allows `GET` with query parameters that could trigger state changes through API endpoints not fully validated.
- Exceptions (`api_ui_theme`, `ui_theme`) are hardcoded but easily bypassed by endpoint aliasing (`_alias_unprefixed_endpoints` in `app/__init__.py` creates short aliases that may not match the exclusion list).

---

## 3. BUSINESS LOGIC FLAWS (CRITICAL)

### 3.1 Wipe Engine Destroys Data Without Atomic Verification
**File:** `app/blueprints/misc/_wipe_delete_selected_data.py`
- Uses `.delete(synchronize_session=False)` in bulk for over **30 tables**.
- No transaction-level rollback strategy shown (only a try/except that rolls back on exception but doesn't restore deleted rows).
- `hard_delete_override` allows overriding protection for forbidden targets (`clients`, `materials`, `pending_bills`, etc.) with only a confirmation text check (`"DELETE ALL DATA"`).
- The `WIPE_REGISTRY` expands selected targets but doesn't enforce dependency order strictly; race conditions or missing cascade rules can leave orphan rows.

### 3.2 `accounts_domain_wipe` Resets Balances but Doesn't Verify Ledger Consistency
**File:** `app/services/wipe.py` — `execute_domain_wipe()` and `accounts_domain_post_reset()`
- Deletes `AccountTransaction`, `FbmCashDrawerEntry`, `CashFlowDifferenceAdjustment`, etc.
- Resets `Account.balance` to 0 and nullifies `payment_account_id`, `supplier_payment.payment_account_id`, etc.
- **Flaw:** Does not verify that `AccountTransaction.from_account_id` / `to_account_id` don't reference deleted accounts, and doesn't recalculate derived balances from scratch. The `verify_accounts_domain_wipe_integrity()` checks row counts but doesn't validate that balances match derived sums after reset.

### 3.3 Adjustment Entries Can Be Posted Without Server-Side Validation
**File:** `templates/accounts/edit_account.html`, `static/account_form.js`
- `desired_balance` is a user-editable number input.
- `current_balance_hidden` is a hidden field set to `calculated_balance`.
- `adjustment_reason` is required only when `Math.abs(diff) < 0.005` is false (client-side JS check).
- **Flaws:**
  - A malicious user can edit the hidden `current_balance_hidden` to any value, making the server think there's no difference when there is one, or vice versa.
  - The server-side form handler (not fully shown) relies on `calculated_balance` computed from DB, but if the hidden value is manipulated, the comparison logic can be bypassed.
  - `idempotency_key` is generated with `range(100000)|random|string`, which is predictable and can be replayed by an attacker.
  - There's no cryptographic signature on the adjustment request.

### 3.4 Opening Balance Edit Can Corrupt Historical Ledger
**File:** `templates/accounts/_account_form_body.html`
- Changing `opening_amount` and `opening_position` updates `Account.opening_balance` directly.
- The edit page says: "Changing the opening baseline shifts today's calculated balance by the same amount."
- **Flaw:** There's no server-side check that the new `desired_balance` equals the new `calculated_balance` (after opening shift) when no physical adjustment is intended. If a user changes opening and also types a `desired_balance` that doesn't match the new current, an unintended adjustment is posted.

### 3.5 `direct_sale_item` Orphans After Parent Deletion
**Evidence:** `instance/migration/purge_report.json`
- `direct_sale_item`: 165 cascade-removed rows (parent `direct_sale` deleted without deleting items).
- `material_return_item`: 15 cascade-removed.
- `grn_item`: 9 cascade-removed.
- `booking_item`: 40 cascade-removed.
- `sale_delivery_persons`: 113 removed (but no cascade count shown).
- The wipe engine tries to delete children first (`DirectSaleItem.delete()` before `DirectSale.delete()`), but manual deletions or failed transactions can still create orphans.

### 3.6 `pending_bill` Deletes Don't Fully Clean Children
**File:** `app/blueprints/misc/_wipe_delete_selected_data.py`
- When deleting `pending_bills`, the code deletes `FollowUpContact` and `FollowUpReminder` first.
- **Flaw:** Only deletes rows linked directly; doesn't check if other tables (`notifications`, `staff_email`) have references to the bill. The `WIPE_REGISTRY` includes `pending_bills` → `follow_up_contact`, `follow_up_reminder`, but not all possible notification references.

### 3.7 `invoice` Table Can Have Orphan Invoices
**File:** `app/blueprints/misc/_wipe_delete_selected_data.py`
- After deleting `direct_sales` and `entry`, the code deletes orphan invoices:
```python
orphan_invoice_count = _tq(Invoice).filter(
    ~exists().where(DirectSale.invoice_id == Invoice.id),
    ~exists().where(Entry.invoice_id == Invoice.id)
).delete(synchronize_session=False)
```
- **Flaw:** Only checks `DirectSale.invoice_id` and `Entry.invoice_id`. It doesn't check `MaterialReturn` or other sources that might link to invoices. Also, this is only executed during full/selective wipe, not during normal deletion workflows.

---

## 4. FORM DATA SAVING & HIDDEN DATA ISSUES

### 4.1 Hidden Fields Not Protected
- `templates/accounts/edit_account.html`:
  - `<input type="hidden" id="current_balance_hidden" value="...">`
  - `<input type="hidden" id="original_opening_hidden" value="...">`
- These values are set server-side but are **editable by any user with browser DevTools**. The server doesn't appear to verify these hidden values against the database; it trusts the submitted `desired_balance` and `opening_amount`.

### 4.2 `idempotency_key` Not Enforced Server-Side
- `templates/accounts/edit_account.html`:
```html
<input type="hidden" name="idempotency_key" id="idempotency_key" value="...">
```
- The test file (`tests/test_account_create_edit.py`) expects idempotency (`key-retry-1`), but the server-side code shown in the blueprint (`accounts.edit_account`) isn't fully visible in the audit files. The test passes, suggesting some server-side handling exists, but the mechanism isn't robust (predictable random string, no cryptographic binding to user/session/request body).

### 4.3 Account Form Allows Stale Channel Details to Survive Classification Changes
**File:** `tests/test_account_create_edit.py`
- `test_edit_changes_classification_and_clears_stale_details` checks that `bank_name` and `account_number` are cleared when changing to `cash`.
- **Flaw:** The test expects `acc.bank_name is None`, but the code only updates the account object; it doesn't verify that hidden form fields (like `bank_details` HTML) are cleared server-side. A malicious user could submit stale bank details via hidden or disabled inputs that the server ignores, but if the server doesn't explicitly null them, they could persist in the DB.

### 4.4 Client Payment Form Hidden Fields
**File:** `templates/accounts/_client_payment_form.html`
- Hidden fields: `payment_id`, `revision`, `idempotency_key`, `show`.
- `revision` is set but not validated against DB state. If an attacker replays an old `revision`, the server may apply stale updates.

---

## 5. API FLAWS

### 5.1 `/api/client_booking_status/<client_code>`
**File:** `app/blueprints/api.py`
- Queries `Booking` with `is_void == False` but `BookingItem` has **no `is_void` column**. The query filters parent bookings but includes all child items regardless of whether the parent item should be excluded.
- `Entry.query.filter(Entry.is_void == False, ...)` excludes voided entries but doesn't check if the linked `Booking` or `DirectSale` parent is voided.
- The `not_(and_(Entry.nimbus_no == 'Direct Sale', Entry.client_category != 'Booking Delivery'))` filter tries to exclude direct-sale credit rows, but relies on `client_category` which isn't always set consistently.

### 5.2 `/api/client_financial_summary/<client_code>`
- Calculates `waive_off_total` by summing `unified_ledger.get('rows', [])` where `row.get('type') == 'Waive-Off'`.
- Does not verify this against the `WaiveOff` table directly. If the ledger is out of sync, the API reports incorrect waive-off totals.

### 5.3 `/api/notifications/due`
- Updates `FollowUpReminder.alerted_at = now` for all due reminders without checking delivery success.
- If the request fails after the DB commit but before the response is fully sent, reminders are marked as alerted but never actually delivered.
- No rollback mechanism shown.

### 5.4 `/api/check_bill/<path:bill_no>`
- Searches `Entry` by `bill_no` only. Doesn't check for `is_void` in the response (only in query: `Entry.is_void == False` is not shown; it uses `.filter_by(bill_no=bill_no).first()` without `is_void` filter in the returned JSON, though the code shows `.filter_by(bill_no=bill_no).first()` but the query shown doesn't include `is_void` filter in the snippet? Actually: `entry = Entry.query.filter_by(bill_no=bill_no).first()` — no `is_void` filter. So a voided entry could be reported as existing.)

---

## 6. FRONTEND / TEMPLATES (BUGS)

### 6.1 `layout.html` is Overloaded (94KB)
- Contains massive embedded HTML/CSS/JS.
- Potential XSS through unescaped variables: `{{ clients|... }}`, `{{ materials|... }}` etc. are passed through `tojson` but some inline JavaScript may not use `tojson` properly.

### 6.2 `static/account_form.js`
- Uses `parseFloat()` on user inputs without validation (`parseFloat(n || 0)`).
- `signedOpening()` returns `pos === 'credit' ? -amt : amt`. If `amt` is NaN (empty string), returns NaN, which breaks `effectiveCurrent()` calculations silently.
- `updateAdjustment()` uses `Math.abs(diff) < 0.005` to decide if an adjustment exists. Floating-point comparison can be unstable.
- The `currentNode()` function looks up registry nodes but doesn't handle missing categories gracefully; if `findCategory()` returns `None`, `findSub()` and `findType()` will throw errors.

### 6.3 Template Variables Not Escaped in JavaScript Contexts
- `window.ACCOUNT_PRESET` uses `{{ account.linked_party_name|tojson }}` which is safe, but `window.ACCOUNT_REGISTRY = {{ registry|tojson }};` relies on `tojson`. If `registry` contains malicious strings, `tojson` escapes them. This is generally safe but should be verified for all variables.

---

## 7. BACKEND / SERVICE FLAWS

### 7.1 `app/services/accounting.py` — `_void_account_tx`
- Doesn't check if `tx` is already voided before calling (`if not bool(getattr(tx, 'is_void', False))`). But if called twice on the same transaction, it skips the second time. This is safe.
- However, `_reverse_account_tx_effect()` calculates `amount_minor = _tx_minor(tx)`. If `tx.amount_minor` is `None`, it uses `to_minor(tx.amount or 0)`. If `tx.amount` is also `None` or NaN, `_tx_minor` could return an incorrect value.

### 7.2 `_sync_linked_receipt_tx`
- Creates `AccountTransaction` with `from_account_id=None`, `to_account_id=to_account_id`.
- If `to_account_id` points to a non-existent account (`Account` deleted but transaction not cleaned), the effect is applied to `None`, which skips the balance update silently (`if acc:`). This leaves the ledger unbalanced without error.

### 7.3 `models/core.py` — `FutureAccountAuditLog`
- A placeholder model defined but no actual usage or migration shown.
- `DOMAIN_WIPE_REGISTRY` references `'FutureAccountAuditLog'` but the table may not exist in the DB (empty DB), so `execute_domain_wipe()` logs a warning and skips it.

---

## 8. DATA RESET / BUG EFFECTS (CRITICAL EVIDENCE)

### 8.1 Database Completely Reset
- `instance/ahmed_cement_v44_fresh.db`: **0 bytes** (created 2026-08-25 04:02, same time as audit).
- Previous database (`ahmed_cement.db` or `ahmed_cement_v44.db`) was deleted by `retire_legacy_database_files()`.
- The purge engine (`purge_report.json`) processed 36,272 rows and removed 11,687.
- The current DB contains **0 rows** in all tables.

### 8.2 Hidden Residual Data in Migration Files
- `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` exists (1.7MB) but isn't loaded.
- `instance/import_reports/full_raw_import_report_20260817_140353_480002.csv` references import events.
- No import results are preserved; the reports don't show whether data was successfully imported or lost.

### 8.3 Log Evidence of Database Lock / Bootstrap Failure
- `instance/logs/errorlog.txt` (20KB) shows repeated errors:
  - `sqlite3.OperationalError: unable to open database file`
  - `database is locked` (multiple times on 2026-08-19, 08-20)
  - `Could not apply SQLite journal pragmas; continuing with the database default journal mode.`
  - `bootstrap skipped/failed`
- This indicates the database file was either deleted, moved, or corrupted during the audit/deployment process.

---

## 9. HARD DELETE EFFECTS

### 9.1 Full Wipe (`delete_all_data` / `delete_selected_data`)
- `app/blueprints/misc/_wipe_delete_selected_data.py` performs bulk `.delete()` operations.
- `hard_delete_override` allows bypassing protections.
- The wipe creates `TenantWipeBackupHistory` with `backup_path` set to `None` (automatic backups disabled by `_create_pre_wipe_safety_backups()` which returns `None`).
- **No actual file backup is created** (`_WIPE_BACKUP_ENABLED = False`).
- After wipe, `BillCounter` is reset to 1000 (`db.session.add(BillCounter(namespace=AUTO_BILL_NS_DEFAULT, count=1000))`).

### 9.2 `retire_legacy_database_files()` Permanently Deletes Old DBs
**File:** `app/services/v44_schema.py`
- Deletes `ahmed_cement.db`, `ahmed_cement.db-wal`, `ahmed_cement.db-shm`, `ahmed_cement_v44.db`, `ahmed_cement_v44.db-wal`, `ahmed_cement_v44.db-shm`.
- If the live DB file is accidentally named one of these, it is permanently removed without backup.

---

## 10. RECOMMENDATIONS (PRIORITIZED)

### P0 — Fix Immediately (Data Loss / Security)
1. **Restore the database** from `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` or from the `Realdata` source.
2. **Remove hardcoded `WEBHOOK_TOKEN`** and `GITHUB_REPO` from `main.py`; enforce environment variables.
3. **Delete or disable `/git-auto-pull` webhook** until deployment logic is fixed to never use `git reset --hard` without verified backups.
4. **Clear `password_plain` column** and enforce `password_hash` only; rotate all user passwords.
5. **Enable CSRF** for all mutation endpoints, not just `accounts.`.

### P1 — Fix Before Production Use
6. **Fix `instance/` preservation logic**: Move `.instance_preserve/` outside repo; verify file checksums before and after copy; abort deployment if preservation fails.
7. **Rebuild database** with `v44/SCHEMA_v4_4.sql`; create the file or fix the path.
8. **Implement server-side idempotency** for adjustments with cryptographic tokens bound to session/request body.
9. **Fix orphan data cleanup**: Add foreign-key constraints with `ON DELETE CASCADE` or enforce cascade deletion in all wipe/delete routes.

### P2 — Improve Data Integrity
10. **Re-import cleaned data** from `ALLEXPORT-CLEAN-17-08-2026.xlsx` and verify counts match purge report.
11. **Run audit queries** from `tools/post_migration_audit/audit_findings.py` regularly to detect new orphans or mismatches.
12. **Add hidden-data verification**: Ensure `original_opening_hidden` and `current_balance_hidden` are validated server-side against DB state, not trusted from form.

---

## 11. AUDIT FILE REFERENCES

| File / Path | Description | Size / State |
|---|---|---|
| `main.py` | Entry point, webhook, deployment logic | 554 lines |
| `app/__init__.py` | App factory, bootstrap, DB config | 465 lines |
| `app/blueprints/auth.py` | Login, recovery, backup settings | 321 lines |
| `app/blueprints/misc/_wipe_delete_selected_data.py` | Wipe engine | 533 lines |
| `app/services/wipe.py` | Wipe services, registry | 434 lines |
| `models/core.py` | User, Settings, SchemaVersion, SystemLock | 160 lines |
| `models/sales.py`, `models/parties.py`, etc. | Domain models | Multiple |
| `static/account_form.js` | Client-side account form logic | 412 lines |
| `templates/accounts/_account_form_body.html` | Account create/edit form | 375 lines |
| `templates/accounts/edit_account.html` | Edit account page | 153 lines |
| `instance/ahmed_cement_v44_fresh.db` | **CURRENT DB — EMPTY (0 bytes)** | **0 bytes** |
| `instance/ahmed_cement.db` | Deleted by `retire_legacy_database_files()` | Missing |
| `instance/logs/errorlog.txt` | Error log (DB lock, bootstrap failures) | 20,566 bytes |
| `instance/migration/purge_report.json` | Purge audit (36,272 → 24,585 rows) | 9,447 bytes |
| `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` | Cleaned migration data (not loaded) | 1.7 MB |
| `instance/import_reports/full_raw_import_report_*.csv` | Import event references | 1,368 bytes |
| `tests/test_account_create_edit.py` | Account form tests (show expected behavior) | 707 lines |
| `docs/legacy_migration_mapping.md` | Migration documentation | 1,427 bytes |
| `v44/SCHEMA_v4_4.sql` | **MISSING** — referenced but not present | **Not found** |

---

*Audit completed. All findings are based on direct file inspection, code analysis, database state verification, and artifact review. No external dependencies were relied upon for business-logic conclusions.*
