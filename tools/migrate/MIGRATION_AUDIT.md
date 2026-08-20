# AMS Legacy Migration Audit & Implementation Analysis

**Audit date:** 2026-08-18
**Working directory:** `/home/user/ams99`
**Database:** `instance/ahmed_cement.db` (SQLite, 6.4 MB, single-tenant)
**Source legacy export:** `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` (51 sheets, 24,054 active rows, 1.7 MB)
**Migration goal:** Convert the legacy transaction history into a clean opening state (balances, inventory, master data, carry-forward bookings only) and stop importing every historical sale / payment / GRN / entry.

---

## A. Current architecture (models & relationships)

| # | Model | File | Purpose | Migration-relevant columns |
|---|---|---|---|---|
| 1 | `Client` | `models/parties.py:4-25` | Customer master | `code`, `name`, `opening_balance` (Float), `opening_balance_date` (DateTime), `is_active` |
| 2 | `Supplier` | `models/parties.py:28-36` | Vendor master | `name`, `opening_balance`, `opening_balance_date` |
| 3 | `DeliveryPerson` | `models/parties.py:71-79` | Driver master | `name`, `opening_balance`, `opening_balance_date` |
| 4 | `Material` | `models/catalog.py:4-15` | Inventory master | `code`, `name`, `unit_price`, **`total`** (computed from entries), `is_active` |
| 5 | `MaterialCategory` | `models/catalog.py:18-24` | Material group | `name`, `is_active` |
| 6 | `Account` | `models/cash.py:26-54` | Cash / bank / asset | `name`, `type`, `category` (cash/bank), **`balance` + `balance_minor`**, **`opening_balance` + `opening_balance_minor` + `opening_balance_date`** |
| 7 | `AccountTransaction` | `models/cash.py:66-81` | Immutable ledger movement | from/to accounts, amount_minor, void, transaction_type, source_type/source_id |
| 8 | `AccountCategory` | `models/cash.py:60-64` | Account classification | `name`, `is_active` |
| 9 | `AccountReconciliation` | `models/cash.py:99-126` | Per-account closing snapshot | previous/opening/period/expected/actual/final balances, minor units (model exists but **not used today** — empty) |
| 10 | `Booking` | `models/sales.py:30-48` | Customer booking | `client_name`, `amount`, `paid_amount`, `discount`, `manual_bill_no`, `auto_bill_no`, `date_posted`, `is_void` |
| 11 | `BookingItem` | `models/sales.py:50-55` | Booking line | `booking_id`, `material_name`, `qty`, `price_at_time` |
| 12 | `BookingAllocation` | `models/sales.py:57-68` | FIFO consumption by sale | `sale_id`, `sale_item_id`, `booking_item_id`, `qty`, `is_void` |
| 13 | `DirectSale` | `models/sales.py:175-206` | Sale transaction | `client_name`, `client_code`, `amount`, `paid_amount`, `category`, `date_posted`, `is_void` |
| 14 | `DirectSaleItem` | `models/sales.py:225-230` | Sale line | `sale_id`, `product_name`, `qty`, `price_at_time`, `grn_item_id` |
| 15 | `Payment` | `models/sales.py:95-128` | Receipt / refund / waive | `client_id`, `client_name`, `amount`, `payment_type`, `method`, `source_type`, `source_id` |
| 16 | `MaterialReturn` | `models/sales.py:252-265` | Stock return | `client_name`, `return_type` (normal/booked), `amount`, `payment_id` |
| 17 | `MaterialReturnItem` | `models/sales.py:268-277` | Return line | `material_return_id`, `material_name`, `qty`, `unit_rate`, `rent_rate`, `price_at_time` |
| 18 | `Entry` | `models/events.py:4-30` | Material movement IN/OUT/CANCEL | `type`, `material`, `qty`, `client`, `client_code`, `bill_no`, `nimbus_no`, `client_category`, `transaction_category`, `is_void` |
| 19 | `PendingBill` | `models/sales.py:6-28` | Derived open-bill row | `client_code`, `client_name`, `bill_no`, `amount`, `is_paid`, `source_table`, `source_id` |
| 20 | `Invoice` | `models/sales.py:149-167` | Tax invoice | `invoice_no`, `client_code`, `client_name`, `total_amount`, `balance`, `status` |
| 21 | `GRN` | `models/stock.py:33-61` | Purchase receipt | `supplier_id`, `manual_bill_no`, `auto_bill_no` |
| 22 | `GRNItem` | `models/stock.py` | Purchase line | `grn_id`, `mat_name`, `qty`, `price_at_time` |
| 23 | `SupplierPayment` | `models/parties.py:39-66` | Vendor payment | `supplier_id`, `amount`, `method`, `is_void` |
| 24 | `DeliveryRent` | `models/delivery.py:4-30` | Driver rent | `sale_id`, `delivery_person_id`, `amount`, `is_void` |
| 25 | `SaleDeliveryPerson` | `models/delivery.py` | Driver ↔ sale link | `sale_id`, `delivery_person_id`, `bags_delivered`, `rent_amount` |
| 26 | `WaiveOff` | `models/sales.py:131-147` | Discount/waive marker | `payment_id`, `client_name`, `amount`, `is_void` |
| 27 | `User` | `models/parties.py` | System user | `username`, `role`, `status`, password_hash / password_plain |
| 28 | `BillCounter` | `models/sales.py:170-173` | Auto bill number generator | `namespace`, `count` |
| 29 | `Settings` | `models/core.py` | App config | `allow_global_negative_stock` etc. (empty in current DB; app seeds defaults) |
| 30 | `AuditLog` | `models/core.py:19-35` | Mutation audit | JSON before/after, user_id, timestamp |
| 31 | `CashFlowCategory/Subcategory/Party/Entry` | `models/cash.py:130-200` | Cash-flow tracking | Currently empty in DB; app starts fresh |

### Sign convention (verified from `app/services/financial_ledgers.py:14-22`)

| Entity | Positive `opening_balance` | Negative `opening_balance` |
|---|---|---|
| Client | Client owes us (receivable) — `debit` | We owe the client (advance) — `credit` |
| Supplier | We owe supplier (payable) — `credit` | Supplier owes us (advance) — `debit` |
| DeliveryPerson | We owe driver (payable) — `credit` | Driver owes us / advance — `debit` |
| Account (cash/bank) | Cash in hand | Overdraft (rare, but column permits) |

---

## B. Current migration architecture (what is broken)

1. **`tools/migrate/05_load_app_db.py`** calls the app's full-raw importer (`_run_full_raw_import_bytes` in `blueprints/import_export/engine.py:294-547`) with `mode='replace_tenant_data'`. The importer reads the cleaned xlsx as a literal dump and inserts every row verbatim via Core INSERTs. **It does exactly what the spec forbids**: it imports every historical sale, payment, GRN, entry, return, etc.

2. **`tools/migrate/post_import_enrichment.sql`** backfills `payment.client_id`, `direct_sale.client_code`, and the `*_minor` money fields. It does **not** compute opening positions from final state.

3. **The current DB is the result of that full-raw import** (committed 2026-08-17). Counts:

   | Table | Count | Table | Count |
   |---|---|---|---|
   | client | 308 | supplier | 6 |
   | material | 66 | material_category | 12 |
   | account | 12 | account_category | 6 |
   | account_transaction | 781 | booking | 387 |
   | booking_item | 885 | booking_allocation | 1060 |
   | direct_sale | 2,410 | direct_sale_item | 4,431 |
   | payment | 708 | material_return | 74 |
   | material_return_item | 101 | entry | 4,576 |
   | pending_bill | 1,534 | invoice | 2,251 |
   | grn | 48 | grn_item | 48 |
   | supplier_payment | 78 | delivery_rent | 969 |
   | sale_delivery_persons | 2,689 | waive_off | 376 |
   | delivery_person | 14 | fbm_cash_drawer_entry | 2 |
   | fbm_cash_drawer_category | 6 | fbm_rental_item | 1 |
   | fbm_rental | 0 | direct_sale_draft | 0 |
   | cash_flow_category | 12 | cash_flow_subcategory | 10 |
   | cash_flow_party | 0 | cash_flow_entry | 0 |
   | cash_flow_difference_adjustment | 1 | cash_flow_reconciliation_audit | 0 |
   | follow_up_reminder | 17 | follow_up_contact | 4 |
   | audit_log | 722 | account_reconciliation | 0 |
   | schema_version | 0 | settings | 0 |
   | user | 7 | bill_counter | 6 |

4. **The legacy clean export** in `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` has 51 sheets with 24,054 active rows. The `purge_report.json` next to it documents the void/cancel/orphan removal.

5. **`tools/migrate/04_run_post_import_audit.py`** has a hard-coded baseline of expected totals (e.g. `material_return.amount = "1225363.30"`) that is **stale** — actual is `1,261,293.80` (delta +35,930.50 from MRs created in the new app after the baseline was captured).

6. **`pre_wipe_backups` is already a no-op** (verified at `app/services/wipe.py:380-397`: `_create_pre_wipe_safety_backups` returns `skipped: True, reason: 'automatic_pre_wipe_backups_disabled'`, no folder is ever created). The only remaining references in code are to the no-op function and the migration's pre-import backup naming (e.g. `instance/migration/pre_import_*.db`).

7. **Import artifacts** route through `instance/.tmp/import_uploads/` and `instance/.tmp/import_reports/` per `app/__init__.py:32-33,88-89` and `app/services/import_artifacts.py:36-50`. Currently `instance/.tmp/` does not exist (it is created lazily on first use). `instance/import_reports/` was already removed.

---

## C. Current booking financial behaviour (what runs on every booking)

When the existing `add_booking` route is called (`app/blueprints/sales/bookings.py:99-191`):

1. Creates a `Booking(amount, paid_amount, discount, manual_bill_no, auto_bill_no, date_posted)` row.
2. Inserts one `BookingItem` per material/qty/rate.
3. Calls `_sync_booking_pending_bill(booking, ...)` (in `app/services/void_rebuild.py:583-637`) which upserts a `PendingBill` for `pending_amount = max(0, amount − discount − paid_amount)`. If `pending_amount == 0` the pending bill is removed/voided.
4. Calls `_sync_booking_paid_into_account(booking, payment_account_id, method)` (in `app/blueprints/sales/bookings.py:5-37` → `app/services/accounting.py:169-209`):
   - Requires `paid_amount > 0` AND a non-NULL cash/bank `payment_account_id`.
   - Creates a `Receipt` `AccountTransaction` with `to_account_id=account.id`, `amount=paid_amount`, `source_type='Booking'`, `source_id=booking.id`.
   - **Increments `account.balance` and `account.balance_minor` by `paid_amount`**.
5. Commits.

**Critical consequence for migration**: if a migration calls the existing `add_booking` route with `paid_amount = amount` and a chosen `payment_account_id`, the route will inject `amount` worth of cash into the chosen account. This is the **double-counting risk** the spec calls out. Therefore the migration **must NOT use the existing route**. It must insert the booking directly via ORM, set `paid_amount = amount` and `receive_in_account_id = NULL` so that the app's `_sync_booking_paid_into_account` will never be triggered for these historical rows (or, if triggered with `paid_amount=0`, it is a no-op per the function logic).

---

## D. Current ledger behaviour for opening balances

- **Client opening** (`Client.opening_balance`, `Client.opening_balance_date`): read by `build_client_financial_ledger()` in `app/services/financial_ledgers.py:786-802` and the client financial ledger view at `app/blueprints/ledgers/_client_financial_ledger.py:327-342`. Rendered as a single `OPENING` row with `debit = max(opening, 0)` / `credit = max(-opening, 0)`. Running balance uses `debit − credit` for clients (positive = client owes us).
- **Supplier opening** (`Supplier.opening_balance`): same convention, but the supplier ledger uses `credit − debit` running balance (positive = we owe supplier).
- **DeliveryPerson opening** (`DeliveryPerson.opening_balance`): same convention, view at `app/blueprints/ledgers/delivery_person.py:98-108`.
- **Account opening** (`Account.opening_balance` + `opening_balance_minor` + `opening_balance_date`): read by `app/services/payments_crud.py:700-777` to compute the per-account baseline. `Account.balance` is recomputed from `opening + sum(transactions)` whenever the app reconciles; the app accepts both `balance` and `balance_minor` as authoritative but the **minor** form is the source of truth per the `sync_money_fields` events listener in `models/events.py:19-30`.
- **Material opening**: **no `opening_qty` column exists**. `Material.total` is recomputed by `_rebuild_material_totals()` (`app/services/void_rebuild.py:1311-1345`) as `SUM(IN) − SUM(OUT)` of all non-void entries.

**Therefore** the only way to seed opening stock without a schema change is to insert a synthetic `Entry(type='IN', nimbus_no='Opening Stock', bill_no='OPENING-STOCK', client=NULL, client_code=NULL, client_category='Opening Stock', transaction_category='Opening Stock', qty=verified_opening_qty, date=migration_date)`. The stock ledger filter (e.g. `app/blueprints/ledgers/_client_financial_ledger.py:305-323` and the `_rebuild_material_totals` query) does not exclude `nimbus_no='Opening Stock'`, so the synthetic entry is added into `Material.total` exactly as required, and it is identifiable in any report that shows `nimbus_no` because the value is `'Opening Stock'`. **No schema change is required.**

---

## E. State of the current (already-imported) database

### E.1 Master data

| Master | Count | Notes |
|---|---|---|
| `client` | 308 | 195 booking names, 326 entry names; 89 entry names not in master, 1 booking name not in master (likely a typo/legacy residue) |
| `supplier` | 6 | clean |
| `material` | 66 | 12 categories |
| `account` | 12 | 6 cash/bank real + 6 test/legacy/HDC |
| `delivery_person` | 14 | 13 active |
| `user` | 7 | 6 admin, 1 user; 1 inactive (Ahmed Hassan) |

### E.2 Account balances (current state, will become opening balances after migration)

| ID | Account | Type | Category | Balance |
|---|---|---|---|---|
| 1 | (MCB 77661) ADNAN AHMED | company | bank | 7,714,824.80 |
| 2 | FBM CASH | company | cash | 18,255,585.81 |
| 3 | (AL HABIB 2607) ADNAN AHMED | company | bank | 271,500.00 |
| 4 | (ALFALAH 37737) FAZAL BUILDING MATERIAL JPS | company | bank | 4,778,739.20 |
| 5 | AJ | Personal | cash | −100,000.00 |
| 9 | HDC CASH | company | cash | 0.00 |
| 10 | HDC BANK | company | bank | 650,000.00 |
| 12 | COMPANY CASH | company | cash | 179,141,950.00 |
| **Total** | | | | **210,712,599.81** |

(Accounts 6, 7, 8, 11 are test accounts with zero balance.)

### E.3 Client opening balances (already set on 124 clients)

| Stat | Value |
|---|---|
| Clients with non-zero opening_balance | 124 |
| Sum of all client opening_balance | 6,237,723.00 |
| Sum of negative client opening_balance | −483,492.00 |

These `opening_balance` values are not used for anything in the running app today (the historical transactions are also present and the financial ledger uses them). They were loaded as a historical snapshot but the live balances derived from the transactions override them in `build_client_financial_ledger`. After the new clean-state migration, these will become the **authoritative** opening positions.

### E.4 Supplier opening balances (3 non-zero)

| ID | Supplier | Opening |
|---|---|---|
| 1 | Gujr Trader DG Kohat Fauji | 2,394,569.00 |
| 4 | Zia Traders | 1,430,000.00 |
| 5 | ISM STEEL | 104,590.00 |
| **Total** | | **3,929,159.00** |

### E.5 Material stock (current `Material.total`)

Most materials are in negative territory (legacy over-dispatch, preserved by `allow_global_negative_stock`). Total signed sum across 66 materials = approximately **−410,000** units, with extremes at −81,013.8 (12MM STEEL) to +11,392 (TILE 1ST GAGGO MANDI, BRICKS-related, FECTO).

### E.6 Bookings (current state, all `is_void=0`)

| Stat | Value |
|---|---|
| Active bookings | 387 |
| Total amount | 137,265,708.43 |
| Total paid_amount | 88,604,330.99 |
| Date range | 2025-07-10 → 2026-08-16 |
| Distinct client names in bookings | 195 |
| Bookings referencing client_name not in master | 1 (must be resolved before migration) |

### E.7 Transactions (the clutter that must be removed in a clean-state migration)

- 2,410 `direct_sale` rows
- 4,431 `direct_sale_item` rows
- 708 `payment` rows
- 4,576 `entry` rows
- 781 `account_transaction` rows
- 74 `material_return` + 101 `material_return_item` rows
- 1,534 `pending_bill` rows
- 2,251 `invoice` rows
- 48 `grn` + 48 `grn_item` rows
- 78 `supplier_payment` rows
- 969 `delivery_rent` rows
- 2,689 `sale_delivery_persons` rows
- 376 `waive_off` rows
- 1,060 `booking_allocation` rows

After a clean-state migration, **every one of these tables must end at 0 rows** (with the exception of `pending_bill` and `invoice`, which are derived — see below — and the carry-forward `booking` + `booking_item` rows).

---

## F. Pre-migration discrepancy inventory (legacy export vs. live app)

These are the known anomalies already present in the live DB. They were preserved from the full-raw import and do NOT block the new clean-state migration; they will simply be discarded with the rest of the historical clutter.

| # | Discrepancy | Count | Cause | Migration impact |
|---|---|---|---|---|
| 1 | `material_return.return_type IS NULL` | 7 (₨ 287,764.20) | Legacy export had empty `return_type` for these 7 rows | Wiped with all MRs |
| 2 | `direct_sale.client_code IS NULL` (unmatched legacy name) | 98 (₨ 1,868,685.60) | 84 distinct unregistered cash/walking customers | Wiped with all sales; ₨ 1.87M preserved only inside `Account.balance` (it left the cash/bank when the sale was paid). **If you want these visible by client, opt-in flag `--register-unmatched-clients` will create 84 Client rows. Default: drop silently, report in migration report.** |
| 3 | `entry.client_code IS NULL` | 215 (167 under `nimbus_no='Direct Sale'`, 48 orphan stock entries) | Children of #2 plus 5 "Faizan Fecto" stock entries with no client master | Wiped with all entries |
| 4 | `payment.manual_bill_no` / `booking.manual_bill_no` literal "None" / "None11" / "None11111" / "None" | 4 (₨ 832,000 + 129,360) | Legacy `f"MB NO.{bill}"` with Python `None` got stringified | Wiped with all data |
| 5 | `direct_sale` amount = 2× items total | 3 (id 2106, 2107, 2177; +₨ 20,860) | Legacy doubled amount, items correct | Wiped |
| 6 | `booking_item` over-allocated | 5 (booking_item ids 471, 616, 618, 751, 873) | Legacy allocation qty > booking qty | Wiped; carry-forward bookings will have fresh unallocated qty |
| 7 | `invoice` with `INV-*` bill_no but no `direct_sale` parent | 5 (₨ 0) | Invoice-only dispatches in legacy | Wiped |
| 8 | `pending_bill` with `client_code='OPEN-KHATA'` but `client_name='IMRAN ASHRAF'` (not in master) | 1 (₨ 29,000) | Importer defaulting | Wiped |
| 9 | `Test Refund Client` / `Test Refund Full` bookings | 7 (id 213-219) | In-app test data | Wiped with the rest |
| 10 | Booking id 95 (JAMEEL SB JAGWAN) with `amount=0` and `paid_amount=555,000` | 1 | Pure advance — booking interface has no separate advance type | Will be re-computed by migration; if remaining qty is 0, not carried forward. If remaining > 0, it is re-created as a normal carry-forward booking. |
| 11 | Audit baseline `material_return.amount = 1,225,363.30` in `tools/migrate/04_run_post_import_audit.py:42` | stale | Baseline captured before post-import MRs were added | Will be refreshed to `1,261,293.80` in the new audit script |

---

## G. Proposed migration mapping (source → destination)

The source is the legacy `ALLEXPORT-CLEAN-17-08-2026.xlsx`. The migration reads the xlsx with pandas, computes the final state independently (using the same formulas the live app uses), and writes the opening state directly via the ORM.

| Destination field | Source from legacy xlsx | Formula |
|---|---|---|
| `Client.opening_balance` + `opening_balance_date` | sum of every active `payment` row where `client_name` matches, + every active `direct_sale` row (credit) + every active `material_return` row + active `waive_off` debits − cancellations, sign per AMS convention | The migration replicates the SQL of `build_client_financial_ledger()` and takes `closing_balance` (which already starts from `opening_balance`, so we replace rather than add). **For the clean-state migration, every client's historical `opening_balance` in the clean export is set to 0 first, then the final state is the computed balance.** |
| `Supplier.opening_balance` + `opening_balance_date` | sum of every active `supplier_payment` (credit) − every active `grn` × item price (debit), sign per AMS convention | The migration replicates the SQL of `build_supplier_financial_ledger()`. |
| `DeliveryPerson.opening_balance` + `opening_balance_date` | sum of every active `delivery_rent` owed − `delivery_person_payment` paid (currently 0 rows), sign per AMS convention | Replicates `build_delivery_person_financial_ledger()`. |
| `Account.opening_balance` + `opening_balance_minor` + `opening_balance_date` | `account.balance − SUM(account_transaction amount, is_void=0)` from the legacy | Direct — this is the verified closing balance. |
| `Account.balance` + `Account.balance_minor` (so the displayed balance is correct from day one) | set to the same value as `opening_balance` so the account UI shows the right balance before any new transaction |  |
| `Material.total` (stock opening) | `material.total − SUM(Entry IN − OUT)` from the legacy, restricted to active entries only | The same formula `_rebuild_material_totals()` uses, restricted to active entries. Inserted as one synthetic `Entry(type='IN', nimbus_no='Opening Stock', ...)` per material so the next stock rebuild picks it up. **The synthetic entry is clearly tagged `nimbus_no='Opening Stock'`, `client_category='Opening Stock'`, `transaction_category='Opening Stock'`, `bill_no='OPENING-STOCK'`, `client=NULL`, `client_code=NULL`.** |
| `Booking` (carry-forward only) | for every active legacy booking where `SUM(BookingItem.qty) − valid_dispatched_qty + valid_returned_qty − cancelled_qty > 0`, create a new `Booking(amount = remaining_qty × original_price_at_time, paid_amount = amount, discount = 0)` and one `BookingItem(booking_id, material_name, qty=remaining_qty, price_at_time=original_rate)` per remaining line | Rate and date preserved exactly; no rounding; no recalculation. |
| `BookingAllocation` | **NOT migrated** (empty). New sales against the carry-forward booking will allocate normally going forward. | empty |
| `DirectSale`, `DirectSaleItem`, `Payment`, `MaterialReturn`, `MaterialReturnItem`, `GRN`, `GRNItem`, `Invoice`, `Entry` (except Opening Stock), `SaleDeliveryPerson`, `DeliveryRent`, `SupplierPayment`, `WaiveOff`, `PendingBill`, `AccountTransaction`, `fbm_cash_drawer_entry`, `audit_log`, `direct_sale_draft`, `fbm_cash_drawer_category`, `fbm_rental_*`, `follow_up_*`, `cash_flow_*`, `AccountReconciliation`, `user_login_session`, `import_*`, `tenant_wipe_backup_history`, `schema_version`, `settings`, `staff_email`, `system_lock`, `recon_basket`, `root_*`, `future_account_audit_log` | **NOT migrated** (empty) | empty |
| `MaterialCategory`, `Account`, `AccountCategory`, `Supplier`, `DeliveryPerson`, `Client`, `User`, `BillCounter` | migrated 1:1 | required for app boot |

### G.1 The carry-forward booking "Paid = Amount" trick

For every carried-forward booking:
- `Booking.amount = remaining_qty × original_price_at_time` (exact)
- `Booking.paid_amount = amount` so `Due = 0` in the UI
- `Booking.discount = 0`
- `Booking.receive_in_account_id = NULL` so the app never tries to post a `Receipt` on the next edit (the function `_sync_booking_paid_into_account` raises ValueError if `paid_amount > 0` AND `account is None`, so we set it to NULL AND we ensure the migration never calls `_sync_booking_paid_into_account` for these rows)
- The migration inserts the `Booking` and `BookingItem` rows **directly** via ORM, bypassing `add_booking` and its side effects. After commit, the app's `_rebuild_client_ledger` for a client with no `DirectSale` and no `Payment` will see the booking as `paid_amount=amount` and produce no `PendingBill` — which is correct, because the historical balance is now represented by `Client.opening_balance`.

### G.2 The opening-stock entry approach

- One `Entry` per material: `type='IN'`, `nimbus_no='Opening Stock'`, `bill_no='OPENING-STOCK'`, `client=NULL`, `client_code=NULL`, `client_category='Opening Stock'`, `transaction_category='Opening Stock'`, `qty=verified_opening_qty`, `date=migration_date`, `is_void=False`.
- `Material.total` is set directly to the verified opening qty as well, so the stock report shows the right value even before any rebuild.
- The next `rebuild_material_totals()` call (triggered by any new sale/return) will recompute `Material.total` from the entries, which still includes the opening entry, so the value is stable.
- The opening entries are excluded from per-client material ledgers by the `nimbus_no` filter at `app/blueprints/ledgers/_client_financial_ledger.py:310-319` and the `_client_material_returnable_qty_map` / `_client_booked_material_returnable_qty_map` filters, which only consider `Direct Sale` / `Booking Delivery` / `Booking Cancel` / `Material Return` nimbus values.

### G.3 Booking allocation / dispatch carry-over

- Carry-forward bookings have `BookingAllocation = 0` (empty).
- The app's existing `add_direct_sale` route reads `booking_balances[mat] = booked − delivered + returned` and allocates against the booking automatically. With a fresh carry-forward booking of `remaining_qty` against which no `DirectSale` exists yet, the available booking balance is the full `remaining_qty`, so the new sale's booking-fulfilment portion works exactly as if the sale had been created the day after the original booking.
- For "Booked + Credit" (Mixed Transaction) and "Credit Customer" sales, no booking is consumed. For "Cash" and "Open Khata" sales, no booking is consumed (existing logic in `app/blueprints/sales/_direct_sales_add_direct_sale.py:175-220`).

---

## H. Pipeline design

I will create `tools/migrate/06_opening_state_migration.py` that:

1. Validates the database does not already have non-master data (refuses to run on a populated DB unless `--force-wipe` is passed). If populated, the script runs only if the user has explicitly opted in.
2. Backs up the existing DB to `instance/migration/pre_opening_<timestamp>.db`.
3. Wipes all transactional tables inside a single SQL `DELETE FROM ...` (or a single `db.session.execute(table.delete())` per table wrapped in a single transaction). **Master tables preserved**: `client`, `supplier`, `delivery_person`, `material`, `material_category`, `account`, `account_category`, `user`, `bill_counter`. All others are wiped: `direct_sale`, `direct_sale_item`, `payment`, `pending_bill`, `invoice`, `account_transaction`, `booking`, `booking_item`, `booking_allocation`, `entry`, `grn`, `grn_item`, `supplier_payment`, `material_return`, `material_return_item`, `delivery_rent`, `sale_delivery_persons`, `waive_off`, `fbm_cash_drawer_entry`, `fbm_cash_drawer_category`, `fbm_rental_item`, `fbm_rental`, `fbm_client`, `direct_sale_draft`, `cash_flow_category`, `cash_flow_subcategory`, `cash_flow_party`, `cash_flow_entry`, `cash_flow_difference_adjustment`, `cash_flow_reconciliation_audit`, `follow_up_contact`, `follow_up_reminder`, `audit_log`, `account_reconciliation`, `import_history_entry`, `import_job`, `import_upload`, `tenant_wipe_backup_history`, `user_login_session`, `schema_version`, `settings`, `staff_email`, `system_lock`, `recon_basket`, `root_backup_*`, `root_recovery_code`, `future_account_audit_log`, `cash_flow_reconciliation_audit`, `delivery_person_payment`, `delivery`, `delivery_item`.
4. Reads `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx` with pandas + openpyxl. No Flask context required for reads.
5. Recomputes every entity's verified closing balance from the legacy sheets, mirroring the live app's logic (so the migrated opening positions match what the legacy system considers the truth).
6. Writes a preliminary `migration_report.json` to `instance/migration/opening_migration_<timestamp>.json` containing every balance comparison.
7. Inside a single DB transaction:
   - Sets `Client.opening_balance` + `opening_balance_date` per client.
   - Sets `Supplier.opening_balance` + `opening_balance_date` per supplier.
   - Sets `DeliveryPerson.opening_balance` + `opening_balance_date` per driver.
   - Sets `Account.opening_balance` + `opening_balance_minor` + `opening_balance_date` and `Account.balance` + `Account.balance_minor` per account.
   - Inserts one synthetic `Entry` per material for opening stock.
   - Sets `Material.total` to the verified opening value.
   - Inserts carry-forward `Booking` + `BookingItem` rows. **Does NOT call `_sync_booking_paid_into_account`** and **does NOT call `_sync_booking_pending_bill`** to avoid creating side-effect `AccountTransaction` / `PendingBill` rows.
   - Updates `BillCounter` so that the next auto bill number in each namespace is **strictly greater** than the max sequence seen in the clean export (e.g. if max `SB-RTN-####` is 1084, set `BillCounter.namespace='RTN', count=1084`; the next call to `get_next_bill_no('RTN')` will return `1085`).
   - Preserves `User` rows; wipes `password_plain` (per existing `post_import_enrichment.sql` behaviour).
8. Re-runs `rebuild_all_erp_consistency()` (which calls `rebuild_client_ledger` for all clients) to verify the system is internally consistent.
9. Calls `verify_post_import_integrity` (the existing `import_artifacts.verify_post_import_integrity`) on the SQLite.
10. Updates the final `migration_report.json` with the verification outcomes and the line `MIGRATION VERIFIED SUCCESSFULLY` only if all critical checks pass.

The script is a single atomic transaction. On any failure, it rolls back the entire DB and the user is left with the pre-migration backup at `instance/migration/pre_opening_<timestamp>.db`.

### H.1 Command-line interface

```text
python tools/migrate/06_opening_state_migration.py \
    --source instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx \
    --db instance/ahmed_cement.db \
    [--report instance/migration/opening_migration_report.json] \
    [--continue-on-verify-fail] \
    [--register-unmatched-clients] \
    [--opening-date 2026-08-18] \
    [--dry-run] \
    [--confirm]
```

`--dry-run` performs the full read + final-state computation + balance comparison, then exits without touching the DB. The report is written to the path given by `--report`. **`--confirm` is required for any non-dry-run execution.** The script refuses to run on a populated DB unless `--force-wipe` is also passed (a safety net so the user does not accidentally wipe a non-trivial DB).

### H.2 Idempotency

- On a re-run after a successful migration, the script detects the synthetic opening entries (`nimbus_no='Opening Stock'`, `bill_no='OPENING-STOCK'`) and the carry-forward bookings (any booking whose `auto_bill_no` starts with `SB-BK-CF-`), voids them, and re-creates them from the source. This makes the script safe to re-run.
- On a re-run after a failed migration, the backup at `instance/migration/pre_opening_<timestamp>.db` is the source of truth; the script detects the failure state and offers to restore it.

---

## I. Verification (post-migration, all in the same script)

The script verifies:

| # | Check | Method |
|---|---|---|
| 1 | Old client final balance = new `Client.opening_balance` | for every client: replicate the SQL of `build_client_financial_ledger` from the legacy xlsx and compare |
| 2 | Old account final balance = new `Account.opening_balance` | per account: `opening = account.balance − SUM(account_transaction amount, is_void=0)` |
| 3 | Old material final stock = new `Material.total` (= synthetic opening entry qty) | per material: verified_opening = material.total − sum of all active entries, compare to inserted opening entry qty |
| 4 | Old supplier final balance = new `Supplier.opening_balance` | per supplier: sum of all `supplier_payment` + `grn_item × price`, sign per AMS convention |
| 5 | Old delivery person final balance = new `DeliveryPerson.opening_balance` | per driver: sum of all `delivery_rent` owed |
| 6 | New `DirectSale` count = 0 | `SELECT COUNT(*) FROM direct_sale` |
| 7 | New `Payment` count = 0 | `SELECT COUNT(*) FROM payment` |
| 8 | New `AccountTransaction` count = 0 | `SELECT COUNT(*) FROM account_transaction` |
| 9 | New `Entry` count = only opening stock entries | `SELECT COUNT(*) FROM entry WHERE nimbus_no = 'Opening Stock'` should equal the number of materials; everything else = 0 |
| 10 | New `Booking` count = number of active legacy bookings with positive remaining qty | compare |
| 11 | New `BookingItem` count = sum of (active legacy bookings × remaining item count) | compare |
| 12 | New `MaterialReturn` count = 0 | |
| 13 | New `GRN` count = 0 | |
| 14 | New `SaleDeliveryPersons` count = 0 | |
| 15 | New `DeliveryRent` count = 0 | |
| 16 | New `PendingBill` count = 0 | |
| 17 | New `direct_sale_item`, `material_return_item`, `booking_allocation`, `grn_item`, `supplier_payment`, `waive_off`, `delivery_person_payment`, `delivery`, `delivery_item`, `fbm_cash_drawer_entry` counts = 0 |  |
| 18 | `PRAGMA integrity_check` = ok |  |
| 19 | `rebuild_all_erp_consistency()` reports no exceptions |  |
| 20 | For every carry-forward booking: `amount = qty × price_at_time` and `paid_amount = amount` (so Due = 0) | per-booking assertion |
| 21 | For every account: `balance = opening_balance` AND `balance_minor = opening_balance_minor` (no `AccountTransaction` rows exist) | per-account assertion |
| 22 | `password_plain` is NULL for every `User` |  |

If any of #1–#6, #18, #20, #21 fail, the script reports the failures and **aborts** (unless `--continue-on-verify-fail` is set). Other failures are reported but not aborted.

---

## J. Cleanup of obsolete files / folders

- `instance/import_reports/` (already removed; was leftover from a previous full-raw import).
- `instance/import_uploads/` (never existed; future full-raw imports go to `instance/.tmp/import_uploads/` per `app/__init__.py:32-33,88-89`).
- `instance/pre_wipe_backups/` (never created; the wipe code is already a no-op per `app/services/wipe.py:380-397`).
- `tools/migrate/05_load_app_db.py` (becomes obsolete; I will leave a thin stub that prints "deprecated, use 06_opening_state_migration.py" so any tooling that calls it does not crash).
- `tools/migrate/04_run_post_import_audit.py` (the old baseline numbers will be wrong for the new schema; I will rewrite it to verify the new opening state — same list of checks as section I above, plus the stale-baseline refresh).
- `tools/migrate/post_import_enrichment.sql` (becomes obsolete; replaced by the new script's enrichment step).
- `tools/migrate/02_build_clean_export.py`, `03_verify_clean_export.py`, `01_audit_legacy.py` (kept; they are still useful for re-running the clean export pipeline if a fresh legacy dump becomes available).

---

## K. What I will NOT touch

- `app/blueprints/sales/bookings.py` — no business-logic changes.
- `app/blueprints/sales/returns.py` — no business-logic changes.
- `app/blueprints/sales/_direct_sales_add_direct_sale.py` — no business-logic changes.
- `app/services/accounting.py` — no business-logic changes (the migration calls `_sync_booking_paid_into_account` is **deliberately not made**; this is not a code change, it is a runtime decision in the new script).
- `app/services/void_rebuild.py` — no business-logic changes.
- `app/services/financial_ledgers.py` — no business-logic changes.
- The `Booking`, `Payment`, `DirectSale`, `Entry`, `Account`, `Client`, `Supplier`, `DeliveryPerson`, `Material` model definitions — no schema changes.
- The `build_*_financial_ledger`, `_rebuild_material_totals`, `_apply_account_tx_effect`, `_sync_booking_paid_into_account`, `_sync_booking_pending_bill` functions — no logic changes. I will **call** the first two (read-only) but never modify them.
- All existing routes, templates, and forms.
- All existing tests (`tests/`).

---

## L. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Migration script has a bug → corrupted DB | Low | High | Backup before wipe; single transaction; rollback on error; `instance/migration/pre_opening_<timestamp>.db` is the source of truth for restore |
| 2 | Final-state calculation differs from what the live app would compute | Low | High | Use the same SQL/logic the live app uses today (mirror of `build_*_financial_ledger`, `_rebuild_material_totals`, account balance formula); verify equality in the migration report |
| 3 | 84 unmatched legacy direct_sale client names disappear from reporting | Medium | Low | Reported in the migration report; opt-in `--register-unmatched-clients` to auto-create them as Client records |
| 4 | Synthetic "Opening Stock" Entry rows look like transactions to some reports | Low | Low | Clearly tagged `nimbus_no='Opening Stock'`, `client_category='Opening Stock'`, `transaction_category='Opening Stock'`, `bill_no='OPENING-STOCK'`, `client=NULL`, `client_code=NULL`; no per-client ledger filter includes them |
| 5 | A booking with `paid_amount=amount` and `receive_in_account_id=NULL` somehow triggers `_sync_booking_paid_into_account` (which would then post a duplicate cash movement) | Low | High | The function is called from `add_booking` and `edit_booking`; the migration inserts directly via ORM and never calls these routes. The next user edit of a carry-forward booking would invoke `_sync_booking_paid_into_account` which would raise `ValueError` if the user picks no cash/bank account. Acceptable: the user is forced to consciously re-link the paid amount to a real account if they want to edit, which is the safe behaviour. (Documented in the migration report.) |
| 6 | Old legacy bill numbers (e.g. `MB NO.8746`) clash with new auto bill numbers in the migrated Booking | Low | Low | New bookings get **fresh** auto bill numbers `SB-BK-CF-####` (a new namespace `BKCF` reserved for carry-forward bookings); the legacy `manual_bill_no` is preserved only on carry-forward bookings if the original was a manual bill, otherwise NULL |
| 7 | `Account.opening_balance` value is the **same** as `Account.balance` at the moment of migration; if the user does a reconciliation later, the opening is independent from the balance because the app reads opening from the column, not from balance | Low | Low | Confirmed by reading `app/services/payments_crud.py:700-777` |
| 8 | The `password_plain` legacy field is wiped per existing `post_import_enrichment.sql` | Low | Low | Preserved; the new script sets `password_plain = NULL` for every user after import |
| 9 | `pk_now()` is timezone-sensitive | Low | Low | The system uses Asia/Karachi local time, confirmed by reading `models/__base.py` and `app/services/time_money.py` |
| 10 | The `audit_log` table (722 rows) is wiped; future audit log starts empty | Medium | Low | I will export the legacy `audit_log` rows to `instance/migration/legacy_audit_log_<timestamp>.json` before wipe, so nothing is permanently lost |
| 11 | The `BillCounter` namespaces (BK, CP, GRN, RTN, SL) are set to a value that collides with existing manual bill numbers in the new app | Low | Low | The migration reads the max `SB-XX-####` from the clean export for each namespace and sets `BillCounter.count = max + 1`, so auto bill numbers never collide. If a manual bill number was inserted later and exceeds the counter, the existing `find_bill_conflict` check (in `app/services/billing.py`) catches it. |
| 12 | The 7 test bookings (id 213-219) and the 1 zero-amount advance booking (id 95) confuse the carry-forward logic | Low | Low | They are included in the "active bookings with positive remaining qty" query like any other. If they have remaining qty, they are carried forward with `amount = remaining_qty × original_rate`. The user can manually void them in the new app if undesired. |
| 13 | The `Test Cash Account` rows (6, 7, 8, 11) become accounts with zero balance, not part of the verified opening | Low | Low | They are migrated as accounts with `opening_balance = 0`, `opening_balance_date = migration_date`. The user can void them in the new app if undesired. |
| 14 | The migration runs against a non-default database (e.g. a developer's local copy) | Low | High | The script requires `--db instance/ahmed_cement.db` (the path the user typed on the command line); it never defaults. It logs the target DB path and the source xlsx path on entry. |

---

## M. Acceptance criteria (mirroring spec section 26)

```text
OLD FINAL STATE
        ↓
    MIGRATION
        ↓
NEW OPENING STATE

Financial balances      = EXACT
Client balances         = EXACT
Supplier balances       = EXACT
Opening inventory       = EXACT
Remaining bookings      = EXACT
Booking rates            = EXACT
Booking quantities       = EXACT
Duplicate payments       = NONE
Duplicate sales          = NONE
Duplicate inventory      = NONE
Historical transaction   = NOT imported
Current sales            = ZERO
Current payments         = ZERO
Current expenses         = ZERO
Migration errors         = ZERO
```

And for every carried-forward booking:

```text
Original Rate × Verified Remaining Quantity = New Booking Amount
New Booking Amount = New Paid Amount
New Due Amount = 0
Financial opening balance = Actual old outstanding client balance
New cash/bank movement generated by migration = 0
```

The migration script enforces all of the above in a single report. **`MIGRATION VERIFIED SUCCESSFULLY` is printed only when all critical checks pass.**

---

## N. File map (where the new code lives)

| File | Action | Reason |
|---|---|---|
| `tools/migrate/06_opening_state_migration.py` | **CREATE** | The new clean-state migration entry point. Single file, no module spread. |
| `tools/migrate/MIGRATION_AUDIT.md` | **CREATE** | This document. |
| `tools/migrate/_opening_state_common.py` | **CREATE** | Shared helpers: `compute_legacy_state(source_xlsx)`, `wipe_transactional_tables(session)`, `apply_opening_state(session, state)`, `verify_opening_state(session)`. |
| `tools/migrate/04_run_post_import_audit.py` | **EDIT** | Replace hard-coded baseline with a read of the latest `opening_migration_report.json` and the section-I verification queries. |
| `tools/migrate/05_load_app_db.py` | **EDIT** | Replace contents with a 5-line stub that prints "deprecated, use 06_opening_state_migration.py". |
| `tools/migrate/post_import_enrichment.sql` | **EDIT** | Mark as deprecated (move to `tools/migrate/deprecated/`). |
| `tools/migrate/post_import_audit.sql` | KEEP | Still useful for ad-hoc checks of the legacy export |
| `tools/migrate/01_audit_legacy.py`, `02_build_clean_export.py`, `03_verify_clean_export.py` | KEEP | Re-run if a fresh legacy dump becomes available |
| `app/`, `blueprints/`, `models/`, `templates/`, `tests/`, `utils/` | **NO CHANGES** | Working application stays untouched |

---

## O. Implementation order (when the user approves the plan)

1. **Read approval**: confirm the design points F (booking "Paid = Amount" trick), G.2 (synthetic opening entries), and H.1 (`--register-unmatched-clients` flag).
2. Create `tools/migrate/MIGRATION_AUDIT.md` (this file). ✅
3. Create `tools/migrate/_opening_state_common.py` with the read/compute/verify helpers.
4. Create `tools/migrate/06_opening_state_migration.py` with the CLI and the orchestration.
5. Replace `tools/migrate/05_load_app_db.py` with a deprecation stub.
6. Rewrite `tools/migrate/04_run_post_import_audit.py` to consume the new report format.
7. Run the script in `--dry-run` mode against the current DB and against the legacy export. Inspect the report.
8. Once the report is clean, run the script in real mode against a fresh DB (the `instance/ahmed_cement.db` is the production DB — the user should back it up first, then run, then verify with the audit script).
9. After migration, smoke-test by opening the new app and checking: (a) 308 client master records present, (b) all client opening balances shown on the client ledger, (c) all 66 material opening stocks shown in the stock report, (d) 387 carry-forward bookings present with `amount > 0` and `paid_amount = amount` and `due = 0`, (e) no `DirectSale` / `Payment` / `AccountTransaction` / `GRN` / `MaterialReturn` rows, (f) a new sale created against a carry-forward booking reduces its remaining qty correctly and posts one new `AccountTransaction` for the cash received.

---

## P. Open questions for the user

I have all the information I need. There are no open architecture questions — the spec is precise and the codebase is fully audited. I will proceed to implementation as soon as the user approves this audit document.
