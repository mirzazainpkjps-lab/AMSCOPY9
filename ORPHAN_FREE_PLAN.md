# ORPHAN-FREE APP PLAN — AMSCOPY9
Branch: arena/01a03714-amscopy9
Based on: ORPHAN_SCENARIO_AUDIT.md (full audit), DB inspection, wipe review, model mapping, purge evidence
Plan date: 2026-08-25

---

## GOAL
No orphan data until deliberately deleted. Every parent deletion either:
- Deletes children first (ordered cascade), OR
- Prevents parent deletion (if children exist and user hasn't confirmed), OR
- Archives parent (soft delete) preserving all child links
No hidden data corruption. No silent DB overwrite. No broken foreign keys allowed silently.

---

## P0 — STOP DATA LOSS IMMEDIATELY (Today)

### P0-A. Lock auto-deploy from destroying instance/
File: `main.py`
- Change `WEBHOOK_TOKEN` fallback to raise error (not hardcoded string)
  ```python
  WEBHOOK_TOKEN = os.environ.get("AMS_WEBHOOK_TOKEN")
  if not WEBHOOK_TOKEN:
      raise ValueError("AMS_WEBHOOK_TOKEN must be set in environment")
  ```
- Change `GITHUB_REPO` to current repo (`rehmanahmedca-source/AMSCOPY9`)
- In `deploy()`: abort (`sys.exit(1)` or raise) if `preserve_instance_data()` fails (`preserved` is False) BEFORE `git reset --hard`
- Move `.instance_preserve/` OUTSIDE repo (e.g., `/home/user/instance_preserve/` or `/tmp/ams_preserve/`) so `git reset --hard` never touches it
- Verify preservation with checksum (`hashlib.md5`) before and after copy; restore only if checksums match

### P0-B. Create missing v44 schema file (or disable reference)
File: `v44/SCHEMA_v4_4.sql` (needs creation)
OR edit `app/services/v44_schema.py`: remove `schema_file.exists()` requirement and rely fully on ORM bootstrap (current behavior is fine for basic start, but schema file should exist for full domain tables)
Quick fix: create empty/minimal `v44/SCHEMA_v4_4.sql` with basic table creation so reference stops failing, or rely on `db.create_all()` (already working) but document that ORM is the authoritative schema source.
Given current DB works (1.4MB, ORM bootstrap OK), the simplest fix is to create the file so warnings stop:
```sql
-- Minimal v4.4 schema placeholder; ORM creates full schema via db.create_all()
CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY, version INTEGER DEFAULT 1, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP);
```

### P0-C. Enforce SQLite foreign keys globally
File: `app/services/v44_schema.py`, `models/__base__.py`, `main.py`
- Ensure `PRAGMA foreign_keys = ON` runs on EVERY connection (not just bootstrap)
- In `models/__base__.py`, add `event.listen(db.engine, 'connect', lambda conn, rec: conn.execute('PRAGMA foreign_keys=ON'))`
- This prevents insertion of orphan FKs silently (SQLite will reject with error, making orphans visible)

### P0-D. Back up database before ANY wipe operation
File: `app/services/wipe.py`, `app/blueprints/misc/_wipe_delete_selected_data.py`
- Re-enable `_WIPE_BACKUP_ENABLED` (`constants.py`: change `False` to `True` temporarily) OR implement manual backup in `_complete_intentional_wipe_workflow()`
- Before any `.delete()`, create SQLite backup (`sqlite3 db_path ".backup to backup_path"`) and verify file exists and size > 0 before proceeding
- Don't proceed with wipe if backup fails (raise exception, rollback)

---

## P1 — DATABASE INTEGRITY (This week)

### P1-A. Add missing ON DELETE CASCADE to SQLite schema
Since SQLite doesn't enforce cascade by default, add `ON DELETE CASCADE` to all child FKs that have ORM `cascade='all, delete-orphan'`:
File: Create migration script `tools/repair_cascade_fks.py` (or add to `models/__base__.py` via `db.create_all()` with updated FK definitions — but SQLite needs schema-level `ON DELETE CASCADE`)

Tables needing cascade:
- `booking_item` (`booking_id` -> `booking.id`) — add `ON DELETE CASCADE`
- `direct_sale_item` (`sale_id` -> `direct_sale.id`) — add `ON DELETE CASCADE`
- `grn_item` (`grn_id` -> `grn.id`) — add `ON DELETE CASCADE`
- `material_return_item` (`material_return_id` -> `material_return.id`) — add `ON DELETE CASCADE`
- `delivery_item` (`delivery_id` -> `delivery.id`) — add `ON DELETE CASCADE`
- `cash_flow_subcategory` (`category_id` -> `cash_flow_category.id`) — add `ON DELETE CASCADE` (currently MANDATORY, no ORM cascade)
- `cash_flow_entry_audit` (`entry_id` -> `cash_flow_entry.id`) — add `ON DELETE CASCADE`
- `follow_up_contact` (`pending_bill_id` -> `pending_bill.id`) — add `ON DELETE CASCADE`
- `follow_up_reminder` (`pending_bill_id` -> `pending_bill.id`) — add `ON DELETE CASCADE`
- `booking_allocation` (`sale_id`, `sale_item_id`, `booking_item_id`) — add `ON DELETE CASCADE`
- `grn_allocation` (`sale_id`, `sale_item_id`, `grn_item_id`) — add `ON DELETE CASCADE`
- `sale_delivery_persons` (`sale_id`, `delivery_person_id`) — add `ON DELETE CASCADE`

Implementation: SQLite `ALTER TABLE` doesn't support adding cascade easily; best approach is to recreate tables with new FK definitions in a migration script (`app/services/maintenance.py` or new `tools/repair_cascade_fks.py`), or rely on ORM-level cascade (`cascade='all, delete-orphan'`) to delete children before parents in Python code.

Given ORM cascade exists for some but not all, the immediate fix is:
1. For tables WITHOUT ORM cascade (`cash_flow_subcategory`, `cash_flow_entry_audit`, `sale_delivery_persons`, `booking_allocation`, `grn_allocation`, `delivery_item`, `follow_up_contact`, `follow_up_reminder`): add `cascade='all, delete-orphan'` to the relationship in `models/*.py` files
2. For SQLite-level enforcement: add `ON DELETE CASCADE` in a migration script

### P1-B. Fix wipe engine to delete in dependency order (not independently)
File: `app/services/wipe.py` — `execute_domain_wipe()`
Current behavior: deletes each model independently with `.delete()`.
Fix: implement dependency graph and delete in reverse dependency order (children first, parents last).

Dependency graph (from FK mapping):
- Parents: `client`, `supplier`, `account`, `material_category`, `cash_flow_category`, `delivery_person`, `direct_sale`, `booking`, `grn`, `invoice`, `pending_bill`, `material_return`, `payment`, `cash_flow_difference_adjustment`, `cash_flow_entry`
- Children (delete FIRST): `sale_delivery_persons`, `delivery_person_payment`, `delivery_rent`, `delivery_item`, `booking_allocation`, `grn_allocation`, `direct_sale_item`, `material_return_item`, `grn_item`, `entry`, `booking_item`, `pending_bill` (wait, pending_bill has no children tracked; but `follow_up_contact` and `follow_up_reminder` reference it)

Correct order for full wipe (`ALL_WIPE_TARGETS`):
1. Deepest leaf nodes (no children): `cash_flow_entry_audit`, `cash_flow_reconciliation_audit`, `accounting_audit_log`, `audit_log`, `future_account_audit_log`, `system_lock` (protected, skip)
2. Child allocations/link tables: `sale_delivery_persons`, `delivery_person_payment`, `delivery_rent`, `delivery_item`, `booking_allocation`, `grn_allocation`, `direct_sale_item`, `material_return_item`, `grn_item`, `entry`, `booking_item`, `follow_up_contact`, `follow_up_reminder`
3. Intermediate parents: `direct_sale`, `booking`, `grn`, `material_return`, `invoice`, `pending_bill`, `cash_flow_entry`, `cash_flow_subcategory`, `cash_flow_party`
4. Master tables: `client`, `supplier`, `delivery_person`, `material`, `material_category`, `cash_flow_category`, `fbm_client`, `fbm_rental_item`, `fbm_rental`, `account_category`
5. Core/domain parents: `direct_sale_draft`, `direct_sale` (wait, direct_sale should be after its items), `account` (should be after transactions), `payment`, `supplier_payment`, `delivery_rent`
6. Final cleanup: `account_transaction`, `fbm_cash_drawer_entry`, `fbm_cash_drawer_category`, `cash_flow_difference_adjustment`, `cash_flow_reconciliation_audit`, `delivery_person_payment`

Simpler approach for wipe fix: keep current independent `.delete()` but add pre-check that verifies after deletion that no orphan rows remain in any table that references deleted parents. If orphans found, rollback.
Even simpler: rely on ORM cascade for relationships that have it (`BookingItem`, `DirectSaleItem`, etc.) and manually delete child tables first in the wipe code (as already partially done in preview map for some datasets, but not fully ordered).

Quick fix in `execute_domain_wipe()`:
```python
# Before deleting parents, explicitly delete all child tables in order
child_order = [
    'sale_delivery_persons', 'delivery_person_payment', 'delivery_rent',
    'delivery_item', 'booking_allocation', 'grn_allocation',
    'direct_sale_item', 'material_return_item', 'grn_item', 'entry',
    'booking_item', 'follow_up_contact', 'follow_up_reminder',
    'cash_flow_entry_audit', 'cash_flow_reconciliation_audit',
    'direct_sale', 'booking', 'grn', 'material_return', 'invoice',
    'pending_bill', 'cash_flow_entry', 'cash_flow_subcategory',
    'fbm_rental', 'fbm_rental_item', 'direct_sale_draft',
    # ... then parents
]
```
But the current code already includes some of these in the wipe targets. The main issue is that `accounts_domain` deletes `AccountTransaction` but doesn't delete `DirectSaleItem`, `GRNItem`, etc. (those are in other datasets). When selecting only `accounts_domain`, only `AccountTransaction` is deleted, leaving `DirectSaleItem` with `grn_item_id` references intact (not affected directly). But `AccountTransaction` doesn't reference these directly; it's only linked to `Account`. So `accounts_domain` wipe doesn't create orphans in `DirectSaleItem`.

The main orphan creation from partial wipe is when user selects `direct_sales` but forgets `sale_delivery_person`. The fix is to enforce that selecting a parent dataset also selects its mandatory child datasets (or delete them together).
Quick fix in `_wipe_delete_selected_data.py`: before performing delete, expand targets to include all mandatory child tables based on FK dependency graph. Then delete in order.

### P1-C. Add dependency expansion to wipe engine
File: `app/blueprints/misc/_wipe_delete_selected_data.py`
Before delete, compute expanded targets:
```python
MANDATORY_CHILDREN = {
    'direct_sales': ['direct_sale_item', 'sale_delivery_person', 'delivery_rent', 'grn_allocation', 'booking_allocation'],
    'bookings': ['booking_item', 'booking_allocation'],
    'grn': ['grn_item', 'grn_allocation'],
    'material_returns': ['material_return_item'],
    'payments': ['waive_off'],
    'accounts_domain': ['account_transaction', 'fbm_cash_drawer_entry', 'fbm_cash_drawer_category', 'cash_flow_difference_adjustment', 'cash_flow_reconciliation_audit'],
    'cash_flow_entries': ['cash_flow_entry_audit', 'cash_flow_reconciliation_audit'],
    'cash_flow_categories': ['cash_flow_subcategory', 'cash_flow_entry'],
    'delivery_persons': ['sale_delivery_person', 'delivery_person_payment', 'delivery_rent'],
    'clients': ['payment', 'pending_bill', 'recon_basket', 'invoice', 'entry', 'direct_sales', 'booking', 'material_return'],
    'suppliers': ['supplier_payment', 'grn', 'recon_basket'],
    'delivery_rents': ['delivery_rent'],
    'fbm_rentals': ['fbm_rental', 'fbm_rental_item', 'fbm_client'],
}
```
When user selects a parent, automatically include all mandatory children in delete list. If user tries to delete parent without children and children contain data, either:
- Reject the delete with message showing linked modules (like cash flow delete messages now added), OR
- Auto-expand and delete together (current behavior for full wipe with `hard_delete_override` should remain, but granular wipe should either expand or block)

Given user's request ("if data is linked to 2 or more modules it should say to delete from that modules too"), the best approach is:
- For granular wipe: check linked modules before delete; if linked modules exist, flash message listing them (like `delete_cf_category` now does); block delete unless user confirms hard override and selects linked modules too.
- For full wipe (`hard=True`, ALL targets): proceed with expanded list (current behavior, but ensure order is correct).

---

## P2 — MANUAL DELETE / EDIT CLEANUP (This month)

### P2-A. Add cascade/delete cleanup to all manual delete routes
File: `blueprints/masters/delete_client.py`, `delete_supplier.py`, `delete_account_category_route` (already checks `is_used`), etc.
- Before hard-deleting any master (Client, Supplier, MaterialCategory, CashFlowCategory, DeliveryPerson), check if any child table has references using a query similar to `delete_account` reference loop
- If references exist, flash message listing linked modules (like cash flow delete messages) and either block or require confirmation
- If no references exist, delete parent and any remaining nullable child references (set to NULL or delete children as appropriate)

### P2-B. Ensure `original_opening_hidden` and `current_balance_hidden` are verified server-side
File: `blueprints/accounts/accounts_crud.py` (already edited in previous fix)
- Add cryptographic checksum comparison: compute `hash('md5')` of server-computed value and compare to submitted `data-checksum` (for `original_opening_hidden` which uses md5 hash)
- For `current_balance_hidden` (substring checksum), compare first 8 chars of server `calculated_balance` string against submitted value
- Reject edit if checksum mismatch, or at minimum raise ValueError with clear message

### P2-C. Enforce `is_void` checks in account delete reference count
File: `blueprints/accounts/accounts_crud.py`
- Change reference count loop to exclude `is_void == True` transactions (`filter(AccountTransaction.is_void == False)` or count only non-voided)
- This allows deleting accounts that only have voided historical transactions (no active references)

---

## P3 — MIGRATION / IMPORT SAFETY (Next sprint)

### P3-A. Pre-import parent validation
File: `blueprints/import_export/pages.py` or `engine.py`
- Before importing any module (`accounts`, `clients`, `sales`, etc.), verify all parent references exist in current DB
- If parent missing (e.g., Client deleted since export), either skip child row or create placeholder parent (based on configuration)
- Don't create orphan rows silently

### P3-B. Load cleaned data properly
File: `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx`
- Import this file using import engine or build script (`tools/init_v44.py` or `build_demo_db.py`)
- After import, verify counts match purge report (24,585 rows expected) and no orphan FKs exist (`sqlite3` query checking all nullable FK columns for NULL references that shouldn't exist based on parent counts)

---

## P4 — VERIFICATION / TESTING (Continuous)

### P4-A. Add automated orphan detection query
File: Create `tests/test_orphan_detection.py` or `tools/read_only/check_db_integrity.py`
- Query all child tables for parent references that point to non-existent parents (e.g., `SELECT * FROM booking_allocation WHERE sale_id NOT IN (SELECT id FROM direct_sale)`)
- Report any rows found; fail CI/test if orphans detected
- Include both MANDATORY and NULLABLE FK checks

### P4-B. Add wipe verification after granular wipe
File: `tests/test_wipe_granular.py` (already exists)
- After any granular wipe, run orphan detection query; assert zero orphans
- After full wipe, verify domain model emptiness AND verify protected tables untouched
- Verify `AccountTransaction` count is 0 after `accounts` wipe, but `Account` balance is 0 (not deleted) — confirm expected behavior

---

=== FILE REFERENCES ===
- Main deploy/security: `main.py`
- Wipe engine: `app/services/wipe.py`, `app/blueprints/misc/_wipe_delete_selected_data.py`
- Wipe registry: `app/services/constants.py` (`DOMAIN_WIPE_REGISTRY`)
- Hidden fields server verification: `blueprints/accounts/accounts_crud.py` (line 325+)
- Hidden fields templates: `templates/accounts/edit_account.html`, `templates/accounts/_account_form_body.html`
- Float fix: `static/account_form.js`
- Idempotency fix: `templates/accounts/edit_account.html`
- Cash flow delete messages: `app/services/cash_flow_svc.py` (`delete_cf_category`, `delete_cf_subcategory`, `delete_cf_party`)
- Model relationships: `models/sales.py`, `models/parties.py`, `models/delivery.py`, `models/stock.py`, `models/catalog.py`, `models/cash.py`, `models/core.py`
- Schema/bootstrap: `app/services/v44_schema.py`, `v44/SCHEMA_v4_4.sql` (missing)
- Purge evidence: `instance/migration/purge_report.json`
- DB file: `instance/ahmed_cement_v44_fresh.db` (1.4MB, rebuilt)
- Audit reports: `AMSCOPY9/AUDIT_REPORT.md`, `AMSCOPY9/ORPHAN_SCENARIO_AUDIT.md`
- Continuation: `CONTINUATION_PROMPT.md`, `CONTINUATION_SUMMARY.md`
FINAL
