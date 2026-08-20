# AMS Inventory / GRN / Stock Accuracy & Performance Audit Report

**Date:** 2026-08-17 · **Scope:** full inventory/GRN/stock transaction flow · **Status:** completed with fixes + verification

Priority order applied: **Data Integrity → Transactional Consistency → Correct Business Logic → Traceability → Performance → UI**.

---

## A. ROOT CAUSES

### A1. PROBLEM 1 — "Adding supplier from GRN is extremely slow" + "GRN saving takes too long"

| Field | Finding |
|---|---|
| Problem | Supplier add from the GRN screen and GRN save feel slow |
| Root cause 1 | **N+1 GRN-items loading on the GRN page.** `GET /grn` loaded every GRN row and the template iterated `grn.items` per row → **59 queries** for 48 GRNs (one items query per GRN). Every request to the GRN page (including the screen the supplier modal lives on) paid this cost. |
| Root cause 2 | **Full-table Python scan for bill-counter sync on every GRN save and page load.** `get_next_bill_no('GRN')` / `peek_next_bill_no('GRN')` → `_max_used_auto_bill_seq` loaded **every** `auto_bill_no` row of the namespace table into Python and regex-parsed each one, instead of filtering in SQL. `peek_next_bill_no` runs on every GRN page render; `get_next_bill_no` on every save. |
| Root cause 3 | **Missing indexes on bill-number columns.** `find_bill_conflict` (called by every GRN save via `get_next_bill_no`, and by manual-bill validation) searches `direct_sale`, `booking`, `payment`, `material_return`, `grn`, `entry`, `pending_bill` on `manual_bill_no`/`auto_bill_no`/`bill_no` — none of those columns were indexed → full table scans. Harmless at 24k rows, quadratic-feeling on the old production data. |
| Root cause 4 | **Duplicate full-table loads per authenticated request.** The `inject_dropdown_data` context processor loads all clients + materials + delivery persons on every template render, and the dashboard route loaded the same clients + materials again → 2 redundant scans per dashboard hit. |
| Root cause 5 (secondary) | `/api/supplier_balance/<id>` (auto-called after selecting the new supplier) builds a full supplier ledger with an N+1 on `grn.items` (`_supplier_rows`). |
| Affected files | `blueprints/import_export/engine.py` (no), `app/blueprints/ops/grn.py` (page GET + add), `app/blueprints/misc/pending.py` (`edit_grn` GET), `app/services/billing.py` (`_max_used_auto_bill_seq`, `get_next_bill_no`), `app/hooks.py` (`inject_dropdown_data`), `app/blueprints/core.py` (index), `app/services/financial_ledgers.py` (`_supplier_rows`) |
| Why it happens | The routes were written when data was small; every added feature (consistency auto-repair in `find_bill_conflict`, counter sync, dropdown injection) added queries on the same hot path without indexes or eager loading. |

### A2. PROBLEM 2 — "Current Stock by Brand is empty"

| Field | Finding |
|---|---|
| Problem | Dashboard "Current Stock by Brand" table shows no rows even though materials exist |
| Root cause | **The dashboard's stock query is entry-driven (INNER side only).** `stats` was built purely from `entry` rows grouped by `material`. A material that exists in the master but has **no non-void entry movements** (freshly created, or legacy rows all voided) never appears — the table showed the empty-state message "No brand stock yet". In this application the *brand* is the material name (the business names materials by brand — PIONEER, ISM 12MM STEEL, …); there is no separate brand entity, and the dashboard was silently dropping materials with zero movement. |
| Affected files | `app/blueprints/core.py` (`index`), `templates/index.html` |
| Why it happens | The aggregation came from the movement ledger only; the Material master (the list "materials created in the Materials section") was never merged in, so zero-movement materials were invisible. |

### A3. PROBLEM 3 — "Stock Summary: GRN received stock not represented / not traceable"

| Field | Finding |
|---|---|
| Problem | Stock Summary shows "Received" as an anonymous added quantity without the actual GRN history |
| Root cause | The summary aggregates `entry` IN/OUT correctly (single source of truth — verified `material.total == entry net` for all 66 materials), but it presented only aggregates; the **GRN document (bill + supplier) behind each incoming movement was not shown**, so "Received" was not auditable to a GRN. |
| Affected files | `blueprints/inventory/stock.py` (`stock_summary`), `templates/stock_summary.html` |
| Why it happens | Aggregation existed; traceability to the source document did not. |

### A4. Additional real defects found during the audit (same failure class)

| Field | Finding |
|---|---|
| Defect 1 | **GRN add accepted zero/negative quantities.** `POST /grn` with `qty=-50` created a GRN item, an IN entry of −50 and *reduced* `material.total` from 100 → 50. The template has `min="0"` but there was no server-side guard (the direct-sale flow has one; GRN did not). |
| Defect 2 | **Duplicate GRN submission was not DB-enforced.** Two identical POSTs with the same manual bill were blocked only by the app-level `find_bill_conflict`; a double-click race or refresh could slip a second GRN in. No unique constraint existed. |
| Defect 3 (verified OK) | GRN **edit** already reverses (voids old entries, subtracts old qty) then re-applies the new qty — delta math is correct (500→700 = +200; 700→400 = −300). Verified by tests. |
| Defect 4 (verified OK) | GRN **delete/cancel** reverses stock via `_set_grn_void_state` + `_rebuild_material_totals` — exact reversal, verified by test. |
| Defect 5 (verified OK) | **Booking is reservation-only** — booking creation writes no entry and does not touch `material.total`; only dispatch/sale creates the OUT movement. Cancellation writes `type='CANCEL'` entries that never affect physical stock. |

---

## B. FIXES

| # | Old behavior | New behavior | Files modified | DB changes | Business rule |
|---|---|---|---|---|---|
| B1 | GRN page ran 59 queries (N+1 items) | GRN + edit-GRN queries eager-load `GRN.items` (**59 → 11 queries**) | `app/blueprints/ops/grn.py`, `app/blueprints/misc/pending.py` | none | Items shown per GRN unchanged; fewer queries, same data |
| B2 | `_max_used_auto_bill_seq` loaded the whole table into Python | SQL `LIKE 'SB-<NS>-%'` filter (uses new index) — only matching rows scanned | `app/services/billing.py` | none | Counter sync logic unchanged; formats verified all `SB-NS-####`; `find_bill_conflict` loop retained as safety net |
| B3 | Bill-number conflict queries full-scanned 7 tables | 17 additive indexes on `grn/direct_sale/booking/payment/supplier_payment/material_return(manual/auto_bill_no)`, `entry(bill_no, material, type)`, `pending_bill(bill_no, client_code)` | `app/services/schema.py` (`_ensure_performance_indexes`, runs at bootstrap) | additive `CREATE INDEX IF NOT EXISTS` (reversible, no data change) | duplicate validation semantics identical |
| B4 | Dashboard stats entry-driven → zero-movement materials invisible; clients+materials loaded twice per request | stats merged onto the **Material master**: every material renders (0/0/0 when no movements), entry-only materials still appear; removed the duplicate clients/materials loads (context processor supplies them) | `app/blueprints/core.py` | none | "Brand" = material name; stock value still from authoritative entry ledger; nothing hard-coded |
| B5 | Stock Summary "Received" anonymous | New **GRN Receipts (Incoming Stock History)** block: every incoming movement traced to its GRN (date, bill, supplier, material, qty) | `blueprints/inventory/stock.py`, `templates/stock_summary.html` | none | Incoming = entry IN (authoritative); GRN is the source document shown |
| B6 | GRN add accepted qty ≤ 0 (negative reduced stock!) | Server-side guard: zero/negative lines are skipped with `GRN_ITEM_SKIPPED_INVALID_QTY` warning log (mirrors the direct-sale rule) | `app/blueprints/ops/grn.py` | none | "No invalid inventory movement" |
| B7 | Duplicate manual-bill GRN only app-level guarded | **Unique partial index** `uq_grn_manual_bill_no` + `IntegrityError` handled gracefully ("duplicate submission ignored") | `app/services/schema.py`, `app/blueprints/ops/grn.py` | additive unique index (verified no duplicate bills in data) | one GRN per manual bill; one stock movement per GRN |
| B8 | No timing visibility | `GRN_CREATE_START/COMMIT/COMPLETE` and `SUPPLIER_CREATE_START/VALIDATION/INSERT/COMMIT/COMPLETE` duration logs (no secrets) | `app/blueprints/ops/grn.py`, `app/blueprints/masters/add_supplier.py` | none | traceability of slow saves |

---

## C. DATA INTEGRITY

Verified against the migrated application DB (`instance/ahmed_cement.db`) with `tools/inventory/reconcile_stock.py` and the new test suite:

| Entity | Count checked | Mismatches | Status |
|---|---|---|---|
| Materials (stored `material.total` vs movement-ledger `entry` IN−OUT, non-void) | 66 | **0** | ✅ reconciled |
| GRNs (each creates exactly one IN movement; join entry↔GRN by auto bill) | 48 | 0 | ✅ |
| Stock movements (entries) | 4,481 | — | ✅ |
| Sales (OUT once per sale item) | tested | 0 | ✅ |
| Returns (customer return → stock IN once) | tested | 0 | ✅ |
| Bookings (reservation only — no physical deduction) | tested | 0 | ✅ |
| Cancellations (CANCEL entries never touch physical stock) | audited | 0 | ✅ |
| GRN edit deltas (+200 / −300) | tested | 0 | ✅ |
| GRN delete reversal (exact) | tested | 0 | ✅ |
| Duplicate submission (one GRN / one movement) | tested | 0 | ✅ |

**Mismatches found:** 0 · **Mismatches repaired:** 0 (no silent repair performed) · **Requiring manual review:** none currently; the pre-existing legacy watch items (86 orphaned invoices, 186 bookings without pending bill) are unchanged and reported by `tools/health/preflight_check.py`.

### Authoritative stock equation (implemented everywhere in this app)

```text
Current Stock (material.total)
  = Σ entry IN (non-void)  −  Σ entry OUT (non-void)
    IN  = GRN received (48) + customer returns (nimbus 'Material Return') + manual/dispatch receiving (IN)
    OUT = sales/dispatches (nimbus 'Direct Sale' / 'Booking Delivery') + manual OUT
```

**Single source of truth:** `entry` is the movement ledger; `material.total` is the stored balance and is kept synchronized by every mutation (`GRN add/edit/delete`, sale add/void/edit, dispatch add, returns). `tools/consistency_report.py` and `tools/inventory/reconcile_stock.py` both verify the identity on demand. There is **no double counting**: a GRN creates exactly one IN entry; a sale creates exactly one OUT entry; booking (reservation) creates none.

---

## D. PERFORMANCE (measured on the migrated DB, local SQLite, same machine)

| Operation | Before | After | Notes |
|---|---|---|---|
| **GET /grn (GRN page + supplier modal host)** | **59 queries / 67 ms** | **11 queries / 52 ms** | −81% queries; on large production GRN tables the win is much bigger (N+1 removed) |
| **POST /grn (add, 1 item)** | 25 queries / 39 ms | 26 queries / 37 ms | same work; bounded counter scan + indexed conflict checks scale on prod |
| **POST /add_supplier (ajax)** | 4 queries / 8 ms | 6 queries / 13 ms | already light; +2 validation/audit queries, timing logs added |
| **GET / (dashboard)** | 29 queries / 460 ms | 28 queries / 495 ms | −2 full-table loads; **54 → 66 brand rows** (all materials now render) |
| **GET /stock_summary (month)** | 12 queries / 20 ms | 14 queries / 22 ms | +GRN receipts traceability block |
| **DB size / indexes** | 0 new indexes | +17 indexes +1 unique | additive, applied to live DB |

The dashboard's remaining weight is `build_current_payables` (bounded 6-query snapshot + in-memory client projection — already N+1-free, business-critical; left untouched rather than optimized at the cost of accuracy).

---

## E. ACCEPTANCE CRITERIA CHECKLIST

- [x] Supplier creation from GRN is responsive (endpoint was already light; host page N+1 removed)
- [x] GRN creation/save is responsive (bounded counter scan, indexed conflict checks, eager items)
- [x] No unnecessary expensive queries run during supplier creation (4-6 queries, logged)
- [x] No unnecessary expensive queries run during GRN creation (single transaction, logged)
- [x] Current Stock by Brand correctly displays **all** existing materials (66/66) with 0s when no movement
- [x] Stock Summary reflects actual GRN receipts **and preserves GRN history** (bill + supplier + date)
- [x] Every valid incoming movement increases stock exactly once (tests 1-3, 6)
- [x] Every valid outgoing movement decreases stock exactly once (tests 4-5, 13)
- [x] Returns affect inventory in the correct direction (customer return → IN)
- [x] Cancellations reverse their original inventory effect (GRN delete exact reversal; booking cancel reservation-only)
- [x] GRN edits apply only the correct quantity delta (+200 / −300)
- [x] Duplicate submissions cannot duplicate stock (app-level + unique partial index + graceful rejection)
- [x] Booking/reservation separated from physical stock (reservation-only, verified)
- [x] Stored stock and calculated stock reconcile (66/66, tool + tests)
- [x] No historical business records silently lost (purge report + audits; only void/cancel/orphan rows excluded pre-load)
- [x] No transaction can leave GRN/inventory/financial partially committed (single commit; IntegrityError rolls back)
- [x] Dashboard totals match authoritative inventory data (entry-ledger aggregation)
- [x] Stock-by-brand totals equal the sum of underlying material stocks (each row is the material)
- [x] Tests cover the critical inventory scenarios (14 new tests, 133 total pass)
- [x] Existing unrelated functionality continues working (full suite 133 passed)

---

## F. Files changed

| File | Change |
|---|---|
| `app/services/schema.py` | `_ensure_performance_indexes()` + `uq_grn_manual_bill_no` (bootstrap, idempotent) |
| `app/services/billing.py` | `_max_used_auto_bill_seq` bounded SQL scan |
| `app/blueprints/ops/grn.py` | eager `GRN.items`, server-side qty>0 guard, duplicate-submission handling, timing logs |
| `app/blueprints/ops/_common.py` | `IntegrityError` import |
| `app/blueprints/misc/pending.py` | eager `GRN.items` on edit-GRN page |
| `app/blueprints/core.py` | dashboard stats merged onto Material master; removed duplicate dropdown loads |
| `app/blueprints/masters/add_supplier.py` (+`_common.py`) | timing logs |
| `blueprints/inventory/stock.py` | `_grn_receipt_history()` + template payload |
| `templates/stock_summary.html` | GRN Receipts (Incoming Stock History) table |
| `tests/test_inventory_flows.py` | 14 scenario tests (all pass) |
| `tools/inventory/reconcile_stock.py` | read-only stock reconciliation tool (0 mismatches) |

No database migration file is required: all schema additions are additive `CREATE INDEX IF NOT EXISTS` executed by the existing bootstrap (`_bootstrap_database`), reversible with `DROP INDEX`, and already applied to the live DB.
