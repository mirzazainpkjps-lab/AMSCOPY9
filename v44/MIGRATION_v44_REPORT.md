# Migration Report — Live DB → v4.4

**Date:** 2026-08-23
**Live DB:** `instance/ahmed_cement.db` (untouched, 6.2 MB, 64 tables)
**Target DB:** `instance/ahmed_cement_v44.db` (fresh, 3.7 MB, 63 tables)
**Migration script:** `MIGRATION_v44_from_live.py`
**Errors:** 6 (all legitimate — bad live data caught by v4.4 constraints)
**FK integrity:** ✅ 0 orphaned rows

## Verification snapshot

From `v_system_health` view on migrated DB:

| Metric | Count |
|---|---:|
| Active clients | 396 |
| Active suppliers | 6 |
| Active materials | 66 |
| Active accounts | 12 |
| Total sales | 2,459 |
| Total payments | 716 |
| Live stock batches | 48 |
| Open pending bills | 468 |
| Open bookings | 338 |

## Row-count mapping (live → v4.4)

| Live table | Live rows | v4.4 table | Migrated rows | Notes |
|---|---:|---|---:|---|
| `user` | 7 | `users` | 7 | Live role strings mapped to v4.4 Admin/Manager/Cashier/Viewer |
| `material_category` | 12 | `categories` (type=material) | 12 | |
| `account_category` | 6 | `categories` (type=account) | 6 | |
| `cash_flow_category` | 12 | `categories` (type=cash_flow_in/out) | 12 | |
| `cash_flow_subcategory` | 10 | `categories` (with parent_id) | 10 | |
| **5 category tables** | **40** | **1 unified `categories`** | **43** | +3 auto-created client cats |
| `client` | 313 | `clients` | 313 + 83 orphan placeholders = **396** | Orphan bookings/sales without matching client got placeholder records |
| `supplier` | 6 | `suppliers` | 6 | Codes auto-generated (SUP-0001, etc.) |
| `material` | 66 | `materials` | 66 | |
| `delivery_person` | 14 | `delivery_persons` | 14 | Codes auto-generated |
| `account` | 12 | `accounts` | 12 | account_type normalized to enum |
| `booking` | 399 | `bookings` | 399 | is_void=0 filter (none voided) |
| `booking_item` | 908 | `booking_items` | 908 | |
| `booking_allocation` | 1,081 | `booking_allocations` | 1,075 | 6 refused by check (over-allocated in live data) |
| `grn` | 48 | `purchases` | 48 | payment_type 'Credit' → 'credit' |
| `grn_item` | 48 | `purchase_items` | 48 | |
| — | — | `stock_batches` | 48 | Derived one batch per purchase_item |
| `grn_allocation` | 6 | `grn_allocations` | 6 | FIFO cost basis preserved |
| `direct_sale` | 2,459 | `sales` | 2,459 | Sale type inferred from payment_method |
| `direct_sale_item` | 4,508 | `sale_items` | 4,508 | |
| `sale_delivery_persons` | 2,710 | `sale_delivery_persons` | 2,710 | Multi-driver splits preserved |
| `delivery_rent` | 972 | `delivery_rents` | 972 | |
| `payment` | 731 | `payments` (party=client) | ~700 | Combined with 3 other pay tables |
| `supplier_payment` | 78 | `payments` (party=supplier) | ~78 | |
| `delivery_person_payment` | 0 | `payments` (party=delivery_person) | 0 | Empty in live |
| `cash_flow_entry` | 0 | `payments` (with cash_flow classification) | 0 | Empty in live |
| **4 payment tables** | **~810** | **1 unified `payments`** | **716** | Excluded amount≤0 rows |
| `waive_off` | 382 | `waive_offs` | 382 | |
| `material_return` + items | 75+102 | `returns` (flat) | 102 | 1 return-item = 1 return row |
| `invoice` | 2,271 | `invoices` | 2,271 | |
| `pending_bill` | 1,565 | `pending_bills` | 1,565 | |
| `audit_log` | 902 | `audit_log` | 902 | Full copy |
| `settings` | 0 | `settings` | 1 (seeded) | Live had no row; default created |
| `bill_counter` | 6 | `bill_counter` | 6 | |

## What was intentionally dropped

Per your instructions during v4.2 → v4.4 design:

1. **All FBM rental data** — 5 live tables (`fbm_client`, `fbm_rental`, `fbm_rental_item`, `fbm_cash_drawer_entry`, `fbm_cash_drawer_category`) → not migrated. If FBM data has business value, export from live DB before deleting it.
2. **Google OAuth tokens** in `settings` → not migrated. Reconfigure email backups if needed.
3. **`is_void=1` rows across all tables** → not migrated (v4.4 uses hard-delete, not soft-delete). Live counts show 0 voided rows in most tables anyway.
4. **Void-audit tables** (`booking_allocation_repair_archive`, `cash_flow_reconciliation_audit`, `cash_flow_entry_audit`) → dropped (no soft-delete → no void audit needed).
5. **Legacy `entry` table** (4,663 rows) → not migrated. It's a denormalized activity feed; the source-of-truth data lives in `sales`, `payments`, `bookings`, `purchases` which ARE all migrated.

## The 6 legitimate errors

All 6 are the SAME issue: `booking_allocation` rows in live DB where `qty_dispatched + qty_cancelled > qty_booked`. This is bad data in live (over-allocation). v4.4's CHECK constraint refuses to accept it.

```
booking_allocation 430:  qty_dispatched + qty_cancelled <= qty_booked
booking_allocation 536:  qty_dispatched + qty_cancelled <= qty_booked
booking_allocation 718:  qty_dispatched + qty_cancelled <= qty_booked
booking_allocation 818:  qty_dispatched + qty_cancelled <= qty_booked
booking_allocation 839:  qty_dispatched + qty_cancelled <= qty_booked
booking_allocation 1347: qty_dispatched + qty_cancelled <= qty_booked
```

**Recommendation:** review these 6 in the live DB before switching over. They likely represent booking-fulfilment bugs that got past live's checks.

## User accounts migrated

| Username | Role in v4.4 |
|---|---|
| Admin | Admin |
| Rehman Ahmed | Admin |
| Rizwan Ahmed | Admin |
| Adnan Ahmed | Admin |
| Shujaat Muzaffar | Admin |
| Ahmed Hassan | Admin |
| Mohsan Javed | Cashier |

All passwords carried over (`password_hash` + legacy `password_plain`).

## Wipe scopes now available (from admin UI)

12 pre-seeded scopes, all `requires_admin=1`, most `requires_confirmation_phrase=1`:

`sales_only`, `bookings_only`, `purchases_only`, `payments_only`, `cash_flow_only`, `returns_only`, `stock_counts_only`, `import_logs_only`, `audit_logs_only`, `sessions_only`, `all_transactions`, `factory_reset`

## Next steps

1. **Point Flask app at new DB** — edit `main.py`/`wsgi.py` config to use `instance/ahmed_cement_v44.db`, OR rename the files to swap (`ahmed_cement.db` → `ahmed_cement_live_backup.db` and `ahmed_cement_v44.db` → `ahmed_cement.db`).

2. **Update ORM models** — the SQLAlchemy models in `models/` were written for the live schema. The app WILL NOT WORK against v4.4 as-is because table names and columns differ (`direct_sale` vs `sales`, `payment` vs `payments`, etc.). This is a large app-side change.

3. **Review the 6 over-allocated bookings** in live before deciding whether to fix or accept the data loss.

4. **Test with a copy first** — do not swap the live DB in production until you've run the Flask app against a copy of the v4.4 DB and confirmed every screen works.

5. **Keep both DBs during transition** — instance/ahmed_cement.db is your rollback for the next few weeks.

## Files produced

- `SCHEMA_v4_4.sql` — the schema (2,656 lines, 63 tables, 19 views, 52 triggers, 111 indexes)
- `SCHEMA_v4_4_AUDIT.md` — pre-migration audit report
- `MIGRATION_v44_from_live.py` — this migration script (re-runnable)
- `MIGRATION_v44_REPORT.json` — machine-readable row counts and error list
- `MIGRATION_v44_REPORT.md` — this file
- `instance/ahmed_cement_v44.db` — the migrated database (3.7 MB)
- `instance/ahmed_cement.db` — **UNTOUCHED** live database (6.2 MB)
- `CHAT_LOG_FULL.md` — full design conversation history
