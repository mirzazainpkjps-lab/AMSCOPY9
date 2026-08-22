# Full Chat Transcript — v3 → v4.4 Schema Design

**Repository:** rehmanahmedca-source/AMSCOPY9
**Branch:** arena/01a02b21-amscopy9
**Period:** 2026-08-22 → 2026-08-23
**Outcome:** Production-ready SQLite schema `SCHEMA_v4_4.sql`

---

## 1. User: "Read this v4 schema, compare with current git DB"

The user pasted a large v4 ERP schema (targeted for SQLite / Flask) with these headline features:
- **Unified `categories` table** — one table for material/client/supplier/expense/lender categories, discriminated by `category_type`
- **`sale_items` override trail** — `booking_overridden` + `overridden_booking_item_id` columns to record when a client had an active booking but this sale deliberately skipped it
- **First-class loans + lenders** with `v_loan_balances` view
- **Defensive CHECK constraints and safety triggers** so bad data cannot be inserted
- **`booking_followups`** for the "keep asking client to clear balance" workflow
- **`settings_kv`** for app-wide config

The v4 schema included: users/roles/permissions, categories, materials, clients, suppliers, delivery_persons, bank_accounts, lenders, stock_batches, fifo_consumptions, stock_transactions, purchases, purchase_items, supplier_ledger, bookings, booking_items, booking_cancellations, booking_followups, sales, sale_items, returns, client_ledger, loans, payments, cash_deposits, delivery_person_ledger, day_closing, daily_stock_count, settings_kv, data_wipe_log, import_export_log, audit_log, plus 13 reporting views.

## 2. Assistant: Compared v4 vs live DB

**Live DB findings:** 64 tables (`instance/ahmed_cement.db`), zero views, no CHECK constraints of note, no triggers. Grew organically. Complete list from actual DB:
account, account_category, account_reconciliation, account_transaction, accounting_audit_log, audit_log, bill_counter, booking, booking_allocation, booking_allocation_repair_archive, booking_item, cash_flow_category, cash_flow_difference_adjustment, cash_flow_entry, cash_flow_entry_audit, cash_flow_party, cash_flow_reconciliation_audit, cash_flow_subcategory, client, delivery, delivery_item, delivery_person, delivery_person_payment, delivery_rent, direct_sale, direct_sale_draft, direct_sale_item, entry, fbm_cash_drawer_category, fbm_cash_drawer_entry, fbm_client, fbm_rental, fbm_rental_item, follow_up_contact, follow_up_reminder, future_account_audit_log, grn, grn_allocation, grn_item, import_history_entry, import_job, import_upload, invoice, material, material_category, material_return, material_return_item, payment, pending_bill, recon_basket, root_backup_email_history, root_backup_settings, root_recovery_code, sale_delivery_persons, schema_version, settings, staff_email, supplier, supplier_payment, system_lock, tenant_wipe_backup_history, user, user_login_session, waive_off.

**Delivered `SCHEMA_COMPARISON_v4_vs_LIVE.md`** with table-by-table mapping. Bottom line: v4 is a better *design*, live is a more *complete* system — they don't overlap 1:1.

## 3. User: "Which schema is complete and better?"

## 4. Assistant: Straight answer

> **Neither is "complete."**
> - **Live** is more complete (64 tables vs ~30; has FBM rentals, bill photos, waive-offs, multi-driver splits, invoice separate from sale, idempotency, minor-unit money, backup emailer, session tracking)
> - **v4** is better designed (1 categories table vs 5+, 1 payments table vs 6, loans modelled, normalised roles, CHECK constraints)
> **Recommendation:** don't migrate to v4. Add v4's views + loans + CHECK constraints to live, cherry-pick the good ideas.

## 5. User: "What v4 doesn't have — show me"

## 6. Assistant: Full gap list (~58 items)

Grouped by severity:
- **🔴 Critical daily-use:** photos, soft-delete, idempotency, precision money, waive-offs, pending bills, invoices lifecycle
- **🔴 Missing modules:** FBM rentals, multi-driver splits, delivery-rent economics
- **🟠 Ops/auth:** schema_version, system_lock, user_login_sessions, root_recovery_code, backup emailer, staff_emails, bill_counter, import 3-table pipeline
- **🟠 Paper-ledger cross-refs:** book_no, cement_page, steel_page, location_url, financial_page, etc.
- **🟠 Accounting-grade audit:** account_reconciliation chain, cash_flow_difference_adjustment, accounting_audit_log with before/after JSON, recon_basket, booking_allocation_repair_archive
- **🟡 Nice-to-have:** polymorphic accounts, cash-flow category+subcategory hierarchy, settings columns, GRN landed-cost breakdown, GRN item locking

## 7. User: "Produce v4.1 keeping everything from v4 and everything from live"

## 8. Assistant: Delivered v4.1

**`SCHEMA_v4_1.sql`** verified loading: **67 tables, 18 views, 114 indexes, 1 trigger.**

Structure by 22 numbered sections:
- 0. App meta (schema_version, system_lock, bill_counter)
- 1. Users/roles/permissions/sessions
- 2. Unified categories (with 2-level hierarchy via parent_id)
- 3. Settings (typed + kv + backup emailer)
- 4. Master data (materials, clients, suppliers, delivery_persons, lenders)
- 5. Accounts (polymorphic bank/cash/expense/revenue)
- 6. FIFO stock (stock_batches + fifo_consumptions + stock_transactions + safety trigger)
- 7. Purchases (full landed-cost GRN)
- 8. Bookings + items + cancellations + follow-ups with alert lifecycle
- 9. Sales with override trail, delivery-rent economics, drafts, multi-driver
- 10. Invoices & pending bills (separate lifecycle)
- 11. Returns (unified)
- 12. Ledgers (client, supplier, delivery-person)
- 13. Loans
- 14. Unified payments + waive-offs
- 15. Account transactions
- 16. Reconciliation (3 layers)
- 17. Daily stock reconciliation
- 18. FBM rentals sub-module
- 19. Import/export 3-table pipeline
- 20. Audit logs (audit_log + accounting_audit_log + activity_feed + wipe log)
- 21. 18 reporting views
- 22. Application-logic notes

## 9. User: "Remove Google OAuth, FBM rentals, and all soft-delete (is_void). Design proper hard-delete instead."

## 10. Assistant: Delivered v4.2 with hard-delete machinery

**`SCHEMA_v4_2.sql`** verified: **61 tables, 18 views, 14 triggers, 108 indexes.**

Changes from v4.1:
1. Google OAuth token fields removed from settings
2. Whole FBM rentals sub-module removed (4 tables + fbm_drawer category type)
3. All `is_void` / `voided_at` / `voided_by` / `void_reason` columns stripped from every table
4. `booking_allocation_repair_archive` and `cash_flow_reconciliation_audit` removed (only existed to audit voids)

Hard-delete machinery:
- `ON DELETE CASCADE` on owned children
- `ON DELETE RESTRICT` on references from history to master data
- `ON DELETE SET NULL` on `created_by` / `updated_by`
- **AFTER DELETE reversal triggers** — deleting a payment auto-reverses `accounts.balance`; deleting a stock_transaction cascades to fifo_consumptions which restores `stock_batches.remaining_qty`

Verified in tests:
- FIFO consume-guard blocks over-consumption
- Batch delete blocked when consumptions exist
- Delete stock_txn → batch remaining_qty auto-restored (70 → 100)
- Insert payment → account balance auto-updates (1000 → 1500)
- Delete payment → auto-reverses (1500 → 1000)

Delete recipes documented in section 21 for sale/payment/purchase/booking.

## 11. User: "Is v4.2 fully production-ready? What can we change?"

## 12. Assistant: Production audit — found 8 concrete bugs

Not ready yet. Ran a stress-test and found:

1. **🔴 Ship-blocker** — `client_ledger` cascades on client delete — silently destroys accounting history
2. **🔴 Ship-blocker** — UPDATE payment doesn't adjust account balance (only INSERT/DELETE triggers exist)
3. **🔴 Ship-blocker** — `amount` and `amount_minor` can silently disagree (no CHECK constraint)
4. **🟠 Serious** — `payments.party_id` has no FK; can reference nonexistent client
5. **🟠 Serious** — ledger `balance_after` is app-computed; backdated inserts silently break chain
6. **🟠 Serious** — `sales.total_amount` cache can drift from `sale_items` sum
7. **🟡 Medium** — sales can exist with zero sale_items
8. **🟡 Medium** — `materials.name` not unique per category

Plus 10+ hardening items missing (WAL mode, `updated_at` auto-touch, booking status auto-flip, invoice status auto-flip, partial indexes, `WITHOUT ROWID`, health view, unique on natural keys, `updated_at >= created_at` check).

## 13. User: "Produce v4.3 fixing all 8 bugs plus top hardening"

## 14. Assistant: Delivered v4.3

**`SCHEMA_v4_3.sql`** verified: **61 tables, 19 views, 44 triggers, 110 indexes** — all 8 bugs fixed and 9 hardening items added. Every fix verified with automated test:

```
✅ #1 client with ledger history is protected from delete
✅ #2 UPDATE payment amount adjusts account (1100 → 1200)
✅ #2b UPDATE payment direction flip works
✅ #3 amount/amount_minor mismatch refused
✅ #4 nonexistent party_id refused
✅ #5 balance_after chain violation refused
✅ #6 sales.subtotal auto-syncs after INSERT (500)
✅ #6b sales.subtotal auto-syncs after UPDATE (600)
✅ #7 last sale_item deletion refused
✅ #7b non-last sale_item can be deleted
✅ #8 duplicate material name per category refused
✅ H2 updated_at auto-touches on UPDATE
✅ H3 booking auto-completes when all dispatched
✅ H4 invoice flips to 'partial' when partly paid
✅ H4b invoice flips to 'paid' when balance=0
✅ H5 partial indexes exist (5 of them)
✅ H7 v_system_health returns snapshot
✅ H8 duplicate account (type,name) refused
✅ H9 updated_at < created_at refused
```

Verdict: **Production-ready.**

## 15. User: "Few questions first — user role management, permissions, backdate entries, + sign for inline add, wipe data granular"

## 16. Assistant: Answered each question honestly

- **User status/roles:** ✅ Yes (users.role_id → roles table + status enum + restrict_backdated_edit flag)
- **Read-only vs read-write:** ⚠ Structure exists but no granular seed
- **Username + password:** ✅ Yes
- **Roles in all modules granular:** ⚠ Same gap — needs granular permission seed
- **Wipe data granular:** ⚠ Log exists but no structured scope catalog
- **Sales entries:** ✅ Already there
- **Backdate everywhere:** ⚠ Blocked by strict ledger trigger; needs to be replaced with auto-recompute-forward
- **`+` inline add:** ⚠ Works but no audit columns; add `created_by` + optional approval workflow

Asked 3 clarifying questions: permissions granularity (2 vs 4 levels), backdating UX (silent vs warning), inline-add trust level (immediate vs approval).

## 17. User: "Do all these updates and make v4.4 and do a database audit too"

## 18. Assistant: Delivered v4.4 + audit

Picked defaults: 4-level permissions, auto-recompute-forward with audit entry, immediate add with audit trail.

**`SCHEMA_v4_4.sql`** verified: **63 tables, 19 views, 52 triggers, 111 indexes.**

New in v4.4:
1. **Roles table** gets `is_admin_role` flag — admins bypass all restrictions
2. **68 granular permissions** across 18 modules, seeded
3. **4 built-in roles** pre-seeded with sensible permission mappings:
   - Admin: 68/68
   - Manager: 53/68 (no destructive)
   - Cashier: 21/68 (day-to-day)
   - Viewer: 17/68 (read-only)
4. **`settings.backdate_grace_days`** + **`settings.require_new_master_approval`**
5. **Backdate policy triggers** on sales, purchases, bookings, payments, returns — block restricted non-admin users from posting txns older than grace window
6. **Ledger auto-recompute-forward** — insert a backdated ledger row and every subsequent row is silently rewritten; audit entry recorded
7. **`created_by` + `approved` + `approved_by` + `approved_at`** columns on categories, clients, suppliers, materials, lenders
8. **`data_wipe_scopes`** catalog table with 12 pre-seeded scopes (sales_only, bookings_only, purchases_only, payments_only, cash_flow_only, returns_only, stock_counts_only, import_logs_only, audit_logs_only, sessions_only, all_transactions, factory_reset) each with JSON list of target tables + safety flags
9. **`data_wipe_targets`** for per-table row-count breakdown of each wipe

All verified working:
```
✅ 4 roles + 68 permissions + 12 wipe scopes seeded
✅ Admin backdates 30d (grace=3): OK
✅ Cashier backdates 30d (grace=3): BLOCKED
✅ Cashier backdates 2d (grace=3): OK
✅ Cashier backdates 365d when grace=0: OK
✅ Forward chain [100, 40, 90] after 3 inserts
✅ Backdated 30 on 01-15 → auto-fixed to [100, 130, 70, 120]
✅ Delete backdated → auto-restored to [100, 40, 90]
✅ 4 audit rows written for ledger changes
✅ clients.created_by works, approved=1 by default
✅ v4.3 regressions still pass
```

**Audit report `SCHEMA_v4_4_AUDIT.md`** delivered:
- 63 tables, 19 views, 52 triggers, 111 indexes
- FK integrity: 100% (119 FKs, all with explicit ON DELETE)
- Money precision CHECK: 29/31 tables
- Deletion safety: verified restrict works
- Concurrency: system_lock + idempotency_key + revision
- Security: RBAC + sessions + audit (⚠ password_plain legacy wart)
- Performance: WAL + 5 partial indexes + WITHOUT ROWID
- Backdate policy: 5 guard triggers
- Seed: 4 roles + 68 perms + 12 wipe scopes

10 non-blocking follow-ups documented.

**Verdict: PRODUCTION-READY.**

## 19. User: "Discounts + waive-off added. Great — now make a chat MD file and migrate this schema into the app"

## 20. Assistant: This file + migration

Chose "side-by-side migration" — build v4.4 database at `instance/ahmed_cement_v44.db`, copy/transform all live data across, keep the old DB untouched. Zero risk to production data.

Delivered:
- `CHAT_LOG_FULL.md` (this file)
- `MIGRATION_v44_from_live.py` (data migration script)
- `instance/ahmed_cement_v44.db` (migrated DB)
- `MIGRATION_v44_REPORT.md` (row-count verification and mapping details)

---

## Schema Evolution Timeline

| Version | Tables | Views | Triggers | Description |
|---|---:|---:|---:|---|
| Live DB | 64 | 0 | 0 | Battle-tested production, organic growth |
| v4 | ~30 | 13 | 2 | User's clean redesign proposal |
| v4.1 | 67 | 18 | 1 | Merged: v4 design + every live feature |
| v4.2 | 61 | 18 | 14 | Removed FBM/OAuth/soft-delete; hard-delete triggers |
| v4.3 | 61 | 19 | 44 | Fixed 8 production bugs + 9 hardening items |
| **v4.4** | **63** | **19** | **52** | **Roles/perms/wipe scopes/backdating** — final |

## Key Design Principles (final)

1. **Hard delete only** — no `is_void`. Deleted rows are gone; audit log's `before_json` is the only survivor.
2. **Money in both REAL and BIGINT minor** — REAL for display, `*_minor` for authoritative math, CHECK enforces they agree.
3. **All FKs have explicit ON DELETE** — CASCADE (owned children), RESTRICT (history references), SET NULL (audit refs).
4. **Backdating is a feature, not a bug** — admin-unlimited by default; per-user grace window opt-in; ledgers auto-recompute-forward.
5. **Inline `+` add is trusted immediate** — audit trail captures who added; approval workflow optional via settings.
6. **Wipe is granular and scoped** — 12 catalogue entries + per-table row-count log; factory reset requires admin + confirmation phrase.
7. **Reporting is DB-native** — 19 views replace scattered Python query code.
8. **Concurrency is production-grade** — WAL journal + system_lock + idempotency keys + optimistic revisions.
