# QA FULL AUDIT — PREDATOR MODE DEEP ADVERSARIAL AUDIT

**Application:** AMS (FAZAL BUILDING MATERIALS) — Flask/SQLAlchemy/SQLite ERP
**Repo:** `rehmanahmedca-source/AMSCOPY9` @ `df99d0c` (branch `arena/01a0379a-amscopy9`)
**Auditor stance:** adversarial — every result was verified against raw SQLite ground truth before being accepted.
**Audit dated:** 2026-08-25 (PKT)
**Baseline:** existing suite `pytest` = **64 passed**. That suite is NOT accepted as proof (see §G).

---

## 0. INDEPENDENT TRUTH SOURCES BUILT FOR THIS AUDIT

| Tool | Location | Purpose |
| --- | --- | --- |
| Predator Truth Engine | `tools/predator_truth_engine.py` | Raw-SQL only (no ORM / no app services). Recomputes sales, account balances (minor units + running balance), stock, receivables, payables, FK orphans, duplicates, soft-delete consistency. Exits non-zero on divergence (`--check`). |
| Predator Route Map | `tools/route_predator_map.py` | Boots the real app and resolves every `(method, URL)` pair through the actual router; lists "all registered endpoints / actual matched / reachable / shadowed". |
| QA harness (scratch, git-ignored) | `.qa/predator_harness.py` | Drives the real HTTP layer; verifies each result against raw sqlite + the truth engine. Evidence in `.qa/results/`. |

**Truth-engine business-rule assumptions (challengeable, documented in the tool):**
* Stock is a pure derivation of `entry` rows (`IN − OUT`, non-void); the `material` master has no opening-stock column → an **auditability gap** (no independent opening-baseline column for stock).
* Account `expected = opening_minor + Σcredits − Σdebits` using `COALESCE(amount_minor, ROUND(amount*100))`.
* Client receivable = opening + sales + bookings + manual pendings − (sale paid/discount, booking paid/discount, receipts, waive-offs, booking-cancel credits).
* Duplicate manual-bill uniqueness is checked only inside each table + GRN↔DirectSale cross-table (application semantics).

**Every finding below is labelled `PROVEN` only when it carries reproduction steps, observed output, expected state, raw state evidence and repeatability. `SUSPECTED` is used only where noted.**

---

# A. DEFECT KILL LIST

### PRED-001 — Concurrent sales allocate the SAME auto bill number  (Critical / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Sales → `app/services/billing.py::get_next_bill_no`, `_sync_bill_counter_with_db`, `find_bill_conflict` |
| **Trigger Sequence** | 1. Fresh DB. 2. Create client `C1`, material `M1` (1000 in stock), delivery person `D`. 3. Fire **8 simultaneous** `POST /add_direct_sale` (qty 1 @ 100, different manual bills, same client). 4. Inspect `direct_sale.auto_bill_no`. |
| **Expected State** | 8 sales, 8 unique `SB-SL-####` auto bill numbers. |
| **Actual State** | 8 sales, **only 4 unique auto bill numbers**; `SB-SL-1001` was assigned to ids `2,3,5,6,8` (5 sales). |
| **Invariant Broken** | `get_next_bill_no()` must be atomic; unique bill identity per document. |
| **Independent Evidence** | raw sqlite: `SELECT auto_bill_no, COUNT(*) FROM direct_sale GROUP BY auto_bill_no HAVING COUNT(*)>1` → `[{'auto_bill_no': 'SB-SL-1001', 'n': 5, 'ids': '2,3,5,6,8'}]`; `view_bill/SB-SL-1001` renders **only** `MB NO.R-4` (the other four are unreachable); `/api/check_bill/SB-SL-1001` → `{"exists":false}`. |
| **Reproduction Command** | `.qa/predator_harness.py::scenario_concurrency` (8-thread variant in `.qa/dbg_race.py`). |
| **Literal Output** | `DUPLICATE AUTO BILLS: [{'auto_bill_no': 'SB-SL-1001', 'n': 5, 'ids': '2,3,5,6,8'}]`; `bill page rendered sale refs: ['MB NO.R-4']` |
| **Root Cause** | `get_next_bill_no` reads `_sync_bill_counter_with_db(ns)` (SELECT MAX + 1) and increments a shared `BillCounter` row; two SQLite writers can read the same MAX before either commits. The `while find_bill_conflict(bill_no)` loop cannot see the other request's uncommitted row. |
| **Recommended Fix** | Serialise bill-number allocation on one `BEGIN IMMEDIATE` transaction per namespace (or use a `BillCounter` row with `UPDATE ... RETURNING`/conditional update), and add a DB `UNIQUE` constraint on `(auto_bill_no)` for bill-bearing tables + a unique retry path on conflict. |
| **Regression Test Required** | `test_concurrent_sales_unique_auto_bill` (≥8 threads, assert unique auto bills and no collision in `view_bill`). |

### PRED-002 — Account reconciliation double-counts future-dated receipts  (Critical / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Accounts → `app/services/payments_crud.py::reconcile_account` + `_transaction_sums` |
| **Trigger Sequence** | 1. Create cash account, opening 100000. 2. `POST /add_payment` 11.00 dated **2026-09-01** (future date; accepted by `resolve_posted_datetime`). Account balance becomes 100011. 3. `POST /accounts/1/reconcile` with `actual_balance=100011.00`, `reconciliation_date=today (2026-08-25)`. 4. Read `account_reconciliation` and `account`. |
| **Expected State** | `expected_balance=100011`, `difference=0`, account stays 100011 (physically counted cash INCLUDES the future-dated receipt, since it is already in the account). |
| **Actual State** | `expected_balance=100000` (future receipt excluded from the movement window `through=period_end=now`), `difference=+11`, an 11.00 "Reconciliation Excess" **is credited into** the account → account = **100022**, while `final_reconciled_balance=100011`. |
| **Invariant Broken** | `final_reconciled_balance == Account.balance` after reconciliation; the reconcile adjustment must not create value. |
| **Independent Evidence** | raw sqlite before/after: `ACCOUNT BEFORE RECON {'balance': 100011.0}` → `REC {'expected_balance': 100000.0, 'actual_balance': 100011.0, 'difference': 11.0, ...}` → `ACCOUNT AFTER RECON {'balance': 100022.0}`; truth engine: `ACCOUNT.DIVERGENCE ... latest reconciliation final=10001100 != expected=10002200`. |
| **Reproduction Command** | `.qa/dbg_recon3.py` (deterministic) |
| **Literal Output** | see above |
| **Root Cause** | (a) future-dated payment dates are allowed and applied to the live account balance immediately; (b) `reconcile_account` computes movements with `through=period_end` (`now` for today) only, while the account balance has already absorbed the future row → the reconciliation treats counted cash as "excess/loss". |
| **Recommended Fix** | Block future-dated money movements (or require them to be excluded from the live balance until their date), and define reconciliation expected = `ledger_balance()` (which the GUI already displays) rather than a windowed recomputation. |
| **Regression Test Required** | `test_reconcile_with_future_dated_receipt_keeps_balance` |

### PRED-003 — Open-Khata receivables invisible in current payables & exports  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Receivables → `app/services/financial_ledgers.py::build_current_payables` / `_client_snapshot` |
| **Trigger Sequence** | 1. `POST /add_direct_sale` with `category=Open Khata`, `manual_client_name=Walk-in Customer 1`, 25 bags @110 (no client master row — the app does not create one). 2. `GET /api/current_payables`, `GET /current_payables`, `GET /export_current_payables`. 3. Raw `pending_bill` query. |
| **Expected State** | A 2,750.00 receivable should appear in the receivables report/API/CSV. |
| **Actual State** | The receivable exists in raw DB (`pending_bill` amount 2750, `direct_sale` row) but is absent from all three outputs; the API `total_outstanding` does not include it. Even `status=all` emits nothing for the "unresolved" bucket (the loop body is a `continue`). |
| **Invariant Broken** | Every active receivable must be visible in the receivables surface. |
| **Independent Evidence** | raw DB pending=2750; API rows contain no walk-in; truth engine: `RECEIVABLES.UNRESOLVED 2750.00 ... invisible to client-keyed reports`; note also that API, ORM ledger and the truth engine all agree (1000067417.97) — agreement is meaningless here because all three exclude the same un-keyed rows. |
| **Reproduction Command** | `.qa/predator_harness.py::scenario_open_khata_invisible` |
| **Literal Output** | `pending=[2750.0] api_total=20300.0` (no walk-in row) |
| **Root Cause** | Open Khata sales are written with `client_code='OPEN-KHATA'` + free-text name and **no Client master row**; `build_current_payables` only iterates Client rows, and the unresolved branch deliberately emits nothing. |
| **Recommended Fix** | Materialise an `OPEN-KHATA` client (or include unresolved sources keyed by `client_code='OPEN-KHATA'` in the projection with a distinct label). |
| **Regression Test Required** | `test_open_khata_sale_visible_in_payables` |

### PRED-004 — Open-Khata receivable cannot be settled  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Payments → `app/services/payments_crud.py::_resolve_client/save_client_payment` |
| **Trigger Sequence** | After PRED-003: `POST /add_payment` with `client_name='Walk-in Customer 1'`, `client_code='OPEN-KHATA'`, amount 1000, valid cash account. |
| **Expected State** | Payment accepted; receivable reduced. |
| **Actual State** | Redirected back with "Client not found" — no `payment` row; the system offers no legitimate UI path to settle an Open-Khata bill (and it cannot appear in receivables either, so it can be neither seen nor paid). |
| **Invariant Broken** | Every receivable the system creates must be settleable. |
| **Independent Evidence** | raw `payment` table empty after POST; flash from the response page. |
| **Reproduction Command** | `POST /add_payment client_name=Walk-in Customer 1 client_code=OPEN-KHATA` |
| **Root Cause** | `save_client_payment` requires a real Client master row; Open Khata has none (PRED-003). |
| **Recommended Fix** | Same as PRED-003 (create the master row), or allow `client_code='OPEN-KHATA'` settlement. |
| **Regression Test Required** | `test_open_khata_settlement` |

### PRED-005 — CSRF protection is limited to `accounts.*` endpoints  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Security → `app/hooks.py::_protect_against_csrf` |
| **Trigger Sequence** | Logged-in session. `POST /add_payment` (or `/add_direct_sale`, `/add_material_return`, `/add_booking`, `/void_transaction`, `/delete_transaction`, `/delete_selected_data`, …) **without** `_csrf_token`/`X-CSRF-Token`. |
| **Expected State** | 400 rejected (all state-changing routes carry session-bound CSRF). |
| **Actual State** | `status=302`, and the state changed: `['payment','account_transaction','account','pending_bill']` (receipt fully posted). |
| **Invariant Broken** | A cross-site/forum/form post must not be able to mutate money/stock state. (Mitigated in modern browsers by `SameSite=Lax`, but state integrity must not rely on browser defaults; also any subdomain/XSS context defeats it.) |
| **Independent Evidence** | table diff before/after POST without token; response status 302 (not 400). |
| **Reproduction Command** | `curl -X POST /add_payment` with session cookies but no CSRF token |
| **Root Cause** | `_protect_against_csrf` returns early for every endpoint that is not `accounts`/`accounts.*`. |
| **Recommended Fix** | Enforce the CSRF gate on every mutating route (belongs in a generic before_request), or at minimum all money/stock mutation endpoints. |
| **Regression Test Required** | `test_csrf_required_for_sales_payment_grn_posts` |

### PRED-006 — Un-keyed POST replay duplicates sales and stock movements  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Sales → `app/blueprints/sales/_direct_sales_add_direct_sale.py` |
| **Trigger Sequence** | Same sale payload (no `manual_bill_no`, no `idempotency_key`) posted twice in a row. |
| **Expected State** | Backend rejects the duplicate (idempotency must not depend on the JS-minted key). |
| **Actual State** | 2 sales, 2 `entry` OUT rows, stock −20 on a 10-bag sale; 2 pending bills. The only guard is an optional form field. |
| **Invariant Broken** | State-changing POSTs are repeatable without side effects; "expected rows == actual rows". |
| **Independent Evidence** | raw sqlite: 2 `direct_sale` rows with same client/qty/rate and NULL manual bill; `material.total` delta −20; `entry` rows 2. |
| **Reproduction Command** | `.qa/predator_harness.py::scenario_double_submission` |
| **Literal Output** | `actual: 2 sales, stock 300.0 -> 270.0 (delta 30.0)` (3 duplicates incl. keyed test) |
| **Root Cause** | Frontend mints `idempotency_key` per page open; backend accepts keyless submits and has no payload-level uniqueness constraint. |
| **Recommended Fix** | Server-minted/session-bound key or `(client, date, items-fingerprint)` uniqueness; add `UNIQUE`-enforced idempotency key at DB level (>0-length). |
| **Regression Test Required** | `test_backend_rejects_unkeyed_replay` |

### PRED-007 — Idempotency key reused with a different payload silently loses data  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Sales → `add_direct_sale` idempotency pre-check |
| **Trigger Sequence** | 1. Sale Alpha with `idempotency_key=K`. 2. Completely different sale (client Beta, qty 77, bill B) re-submitted with **the same key K**. |
| **Expected State** | Rejected with an error ("key already used") or stored as a new sale. |
| **Actual State** | Silently treated as replay: flash "already saved (duplicate submission ignored)" and redirect to the FIRST sale; **no row for Beta** — the second transaction is lost without any record. |
| **Invariant Broken** | Idempotency keys must be bound to the full request payload (client, items, bill). |
| **Independent Evidence** | raw sqlite has zero sales for Client Beta; response redirect carries the first sale's bill. |
| **Reproduction Command** | POST two different sale payloads with the same `idempotency_key` |
| **Root Cause** | Pre-check: `DirectSale.query.filter_by(idempotency_key=idem_key).first()` — no payload comparison. |
| **Recommended Fix** | Store a payload hash; on key match verify hash — mismatch ⇒ 400 with explanation. |
| **Regression Test Required** | `test_idem_key_payload_binding` |

### PRED-008 — Reconcile period guard bypassed on payment CREATE  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Accounts → `save_client_payment` (create path) |
| **Trigger Sequence** | 1. Reconcile cash account today (`period_end = today 12:00:15`). 2. `POST /accounts/payments/clients/save` with `date=2026-08-25 10:00:00` (inside the closed period). |
| **Expected State** | Rejected: "This transaction is in a reconciled period …". State unchanged. |
| **Actual State** | Accepted. State changed: `['payment','account_transaction','account','pending_bill']`. |
| **Invariant Broken** | Finalised periods must be immutable; post-close receipt rewrites the closed reconciliation. |
| **Independent Evidence** | table diff before/after the POST. `_assert_period_open` is called only in the **edit** branch of `save_client_payment`, never on create. |
| **Reproduction Command** | `.qa/predator_harness.py::scenario_account_adjustment` step 4 |
| **Root Cause** | Missing `_assert_period_open(...)` on the create path (also missing on SupplierPayment create, GRN payments, etc.). |
| **Recommended Fix** | Assert periods open on every money-movement create (Payment, SupplierPayment, GRN payment, refund). |
| **Regression Test Required** | `test_payment_create_in_reconciled_period_rejected` |

### PRED-009 — Domain wipe is unusable (FK ordering) and leaks SQL to the user  (High / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Wipe → `app/blueprints/misc/_wipe_delete_selected_data.py` (full-wipe branch) |
| **Trigger Sequence** | 1. Create GRN(s) + sales that reference GRN lots (or simply any DB with GRN + DirectSaleItem rows). 2. `POST /delete_selected_data` with `confirm_text='DELETE ALL DATA'`, `hard_delete_override=1`, `delete_targets=['direct_sales','payments','accounts']`. |
| **Expected State** | Selected datasets deleted; FK-clean DB; friendly outcome. |
| **Actual State** | `Wipe failed: (sqlite3.IntegrityError) FOREIGN KEY constraint failed [SQL: DELETE FROM grn_item] (Background on this error at: https://sqlalche.me/e/20/gkpj)` — transaction fully rolled back (counts unchanged: 113 sales / 2 accounts / 10 payments), but **the wipe can never complete** when `direct_sale_item.grn_item_id` references `grn_item`, because `GRNItem` is deleted before `DirectSaleItem`. |
| **Invariant Broken** | Wipe (a) must work on a supported dataset, (b) must not expose SQL internals in user-facing flashes. |
| **Independent Evidence** | before/after counts identical (full rollback = good atomicity), plus the flash text containing SQL + SQLAlchemy URL + parameter bindings. |
| **Reproduction Command** | `.qa/dbg_wipe2.py` |
| **Literal Output** | `FLASH: Wipe failed: (sqlite3.IntegrityError) FOREIGN KEY constraint failed [SQL: DELETE FROM grn_item] (Background on this error at: https://sqlalche.me/e/20/gkpj)` |
| **Root Cause** | Delete order ignores the FK chain `direct_sale_item.grn_item_id → grn_item.id`; and exception messages are surfaced verbatim. |
| **Recommended Fix** | Delete/void `DirectSaleItem` (or set `grn_item_id=NULL`) before `GRNItem`; map exceptions to a clean message; keep the rollback. |
| **Regression Test Required** | `test_full_wipe_with_grn_linked_sales_succeeds` + `test_wipe_error_does_not_leak_sql` |

### PRED-010 — User-visible errors expose SQL / internals  (Medium / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Sales / GRN / wipe error handlers (`flash(f'Error processing sale: {str(e)}')`, `flash(f'Unable to ...: {exc}')`) |
| **Trigger Sequence** | Trigger a DB constraint violation through a normal flow — e.g. two concurrent first-time sales both auto-creating driver `D` (unique constraint), or the wipe above; read the flashed message. |
| **Expected State** | Clean validation message ("Delivery person could not be created; retry"). |
| **Actual State** | `Error processing sale: (sqlite3.IntegrityError) (sqlite3.IntegrityError) UNIQUE constraint failed: delivery_person.name [SQL: INSERT INTO delivery_person (name, phone, opening_balance, opening_balance_date, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?)] [parameters: ('D', None, 0.0, ...)] (Background on this error at: https://sqlalche.me/e/20/gkpj)` — SQL, table names, parameter values and library URLs are all user-visible. |
| **Invariant Broken** | Errors must never leak internals; every failure must be *clean validation error / safe server error / unsafe leak* — this is an unsafe leak. |
| **Independent Evidence** | flash content captured from the rendered page + app log. |
| **Reproduction Command** | `.qa/dbg_race.py` (first variant, concurrent driver creation) |
| **Root Cause** | `str(exc)`/`logging.exception`-style messages printed into flash across many legacy routes. |
| **Recommended Fix** | Log full exceptions server-side; flash a friendly message; add a global exception filter for `IntegrityError`. |
| **Regression Test Required** | `test_duplicate_driver_race_shows_clean_error` |

### PRED-011 — Route shadow: `/export_unpaid_transactions` serves the WRONG handler  (Low / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | Routing → `tools/route_predator_map.py` output; `misc.export_unpaid_transactions` vs `reports.export_current_payables` |
| **Trigger Sequence** | `GET /export_unpaid_transactions` (or `url_for('export_unpaid_transactions')`). |
| **Expected State** | The admin-only generic exporter (`dataset='unpaid_transactions'`) runs. |
| **Actual State** | The router matches `reports.export_current_payables` (registered first). The `misc` handler (which enforces `role in ('admin','root')` and redirects into the import/export engine) is **dead code**: unreachable. Side effect: the intended role guard on that URL is disabled — any authenticated user gets the current-payables CSV. |
| **Invariant Broken** | One URL ⇒ one handler; permissions must not be attached to shadowed handlers. |
| **Independent Evidence** | route map: `{'method':'GET','url':'/export_unpaid_transactions','all_registered_endpoints':['reports.export_current_payables','misc.export_unpaid_transactions','export_current_payables','export_unpaid_transactions'], 'actual_matched_endpoint':'reports.export_current_payables', 'shadowed_endpoints':['misc.export_unpaid_transactions','export_unpaid_transactions'], 'behaviour_divergent':true}`. |
| **Reproduction Command** | `python tools/route_predator_map.py --db <db>` |
| **Root Cause** | Same URL registered by two blueprints; alias mechanism duplicates the rule; Flask resolves by registration order. |
| **Recommended Fix** | Remove the duplicate route (or give it a distinct path) and centralise the export permission check in the surviving handler. |
| **Regression Test Required** | `test_export_unpaid_transactions_runs_documented_handler` |

### PRED-012 — GRN edit/delete fully blocked while any lot is consumed by a sale  (Low / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | GRN → `app/blueprints/misc/pending.py::edit_grn`, `hard_delete_transaction('GRN')` |
| **Trigger Sequence** | 1. GRN 500 @100. 2. Credit sale 200 links GRN lots (`grn_item.is_locked=1`). 3. `POST /edit_grn/<id>` to change **only the freight cost** (or any non-item field); also `action=delete`. |
| **Expected State** | Non-stock fields editable; item qty of locked lots blocked; delete blocked with clear message. |
| **Actual State** | Any edit attempt is fully blocked with "This GRN has locked lots used by cash/credit sales. Delete those sales before changing item qty/rate." even when items are untouched; delete similarly blocked. The block itself is atomic and clean (no partial state). |
| **Invariant Broken** | None (data integrity preserved) — but feature availability: correcting a supplier name / expense on a GRN is impossible after any sale without deleting the sale. Classified Low (usability/operational risk). |
| **Independent Evidence** | raw rows unchanged after POST; `grn_item.is_locked=1`. |
| **Reproduction Command** | `.qa/predator_harness.py::scenario_grn_edit_delete` |
| **Root Cause** | `_grn_has_locked_lots()` gates the entire edit route rather than only qty/rate edits. |
| **Recommended Fix** | Permit non-stock field edits; restrict only locked lines. |
| **Regression Test Required** | `test_grn_nonstock_edit_allowed_when_lots_locked` |

### PRED-013 — `check_bill` API returns `exists:false` for real auto-billed sales  (Medium / PROVEN)

| Field | Value |
| --- | --- |
| **Module** | API → `app/services/api.py::check_bill_api` |
| **Trigger Sequence** | Create a sale with auto bill `SB-SL-1110`; `GET /api/check_bill/SB-SL-1110`. |
| **Expected State** | `{"exists": true}` (source of truth: `direct_sale.auto_bill_no`). |
| **Actual State** | `{"exists": false}` while the row exists (raw DB + page). |
| **Invariant Broken** | API must agree with DB count/report surfaces (hidden-record hunt). |
| **Independent Evidence** | raw sqlite row exists; page renders it; API says false. |
| **Reproduction Command** | `GET /api/check_bill/SB-SL-1110` |
| **Root Cause** | The API probes a subset of bill tables (visible only for pending/manual refs) and misses direct_sale auto bills. |
| **Recommended Fix** | Query all bill-bearing sources (or at least DirectSale auto bills) using the shared `_lookup_bill` matcher. |
| **Regression Test Required** | `test_check_bill_detects_auto_billed_sale` |

### PRED-014 — Test suite blind spots (mutation testing)  (Coverage / PROVEN)

See §G. Three mutations that break money/stock rules kept all 64 tests green.

---

# B. STATE DIVERGENCE TABLE

Every value below is the **same business event** measured across layers. `TE` = independent truth engine (raw SQL). Baseline event set: Chain A (GRN 500 → credit sale 200@110 → payment 5000 → delete+recreate).

| Layer | Sale outstanding (Client Alpha, after 5000 payment) | Stock "Cement 50kg" | Cash account | Matches |
| --- | ---: | ---: | ---: | --- |
| Raw DB (pending_bill sum) | 17 000.00 | 300.00 (pre-repeat) | 105 000.00 * | — |
| Independent Truth Engine | 17 000.00 | 300.00 | 105 000.00 | ✔ |
| ORM ledger (`build_client_financial_ledger`) | 17 000.00 | 300.00 | 105 000.00 | ✔ |
| API (`/api/current_payables`) | 17 000.00 | — | — | ✔ |
| HTML (`/current_payables`) | 17 000.00 | — | — | ✔ |
| CSV export (`/export_current_payables`) | 17 000.00 | — | — | ✔ |
| PDF invoice | 16 967.00 (outstanding after Previous −5 033) | — | — | ✔ (consistent with cutoff) |

**Divergent rows found by the same method (all layers agree but all are wrong):**
| Scenario | Layer | Expected | Actual | Match |
| --- | --- | ---: | ---: | --- |
| Open-Khata 2 750 receivable | Raw DB pending | 2 750.00 | 2 750.00 | — |
| Open-Khata 2 750 receivable | TE | 2 750.00 (unresolved flag) | 2 750.00 | ✔ engine flags |
| Open-Khata 2 750 receivable | ORM ledger | not present | 0 | ✘ |
| Open-Khata 2 750 receivable | API | not present | 0 | ✘ |
| Open-Khata 2 750 receivable | CSV export | not present | 0 | ✘ |
| Reconcile with future-dated receipt | TE | 100 011.00 | 100 022.00 | ✘ |
| Reconcile with future-dated receipt | `account_reconciliation.final` | 100 011.00 | 100 011.00 | ✘ (≠ account) |
| Reconcile with future-dated receipt | `Account.balance` | 100 011.00 | 100 022.00 | ✘ |

*Cash account shown before the manual-receipt/void/restore sequence; every layer at that instant was 105 000.00.

**Accounts section invariant (full run):**
* `Account.balance == from_minor(balance_minor)` — held on every tested path, including legacy float-only routes, thanks to the `before_flush` minor-unit synchroniser (`models/events.py`). ✔
* Ledger running balance == stored balance — ✔ for all accounts except the reconciled account in PRED-002.

---

# C. HIDDEN RECORD MATRIX

Raw = sqlite count; ORM count identical (SQLAlchemy reads same rows); API/Page/Report/Export measured on the QA data set after the full harness run.

| Entity | DB (raw / active) | ORM | API | Page (first page) | Report | Export | Difference |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| client | 3 / 3 | 3 | 1 row payables | 3 | 1 | 1 | OK (client master isn't a report entity) |
| supplier | 2 / 2 | 2 | 2 | 2 | — | — | OK |
| material | 4 / 4 | 4 | 4 (stock summary 4) | 4 | 4 | 4 | OK |
| direct_sale | 113 / 113 | 113 | n/a | ~20 (paginated) | — | — | OK (paginated) |
| payment | 10 / 10 | 10 | 7 manual refs visible | 10 (per_page 50) | — | — | OK |
| pending_bill | 112 / 112 | 112 | 1 client summary | 15 refs (paginated) | 1 | 1 | **OK but Open-Khata 2 750 is excluded from report/export (see PRED-003)** |
| entry | 123 / 121 | 123 | — | — | — | — | 2 voided (edit rewrite of GRN/Sale) — correct |
| grn | 6 / 6 | 6 | 6 | 6 | 6 | 6 | OK |
| booking | 2 / 2 | 2 | 2 | 2 | 2 | 2 | OK |
| material_return | 2 / 2 | 2 | 2 | 2 | 2 | 2 | OK |
| account_transaction | 14 / 12 | 14 | 12 | 12 | 12 | — | 2 voided — correct |
| **Open-Khata receivable** | **2750.00 in pending_bill** | 2750.00 | **0** | seen in pending_bills list | **0** | **0** | **✘ HIDDEN (PRED-003)** |
| **auto-billed sale w/ duplicated SB-SL-1001** | **5 rows** | 5 | `check_bill → exists:false` | 1 row shown (`R-4`) via bill viewer | — | — | **✘ HIDDEN (PRED-001, PRED-013)** |

Ghost-record sweep (FK orphans, entry→source, pending→source overlaps, waive duplicates): **0 orphans** on the QA dataset. Duplicate scan: 1 (concurrent auto-bill PRED-001). Soft-delete consistency: voided parents have no active children (sale delete rewrites entries; payment void voids waive rows) ✔.

---

# D. TRANSACTION ATOMICITY MATRIX

Full-old-state vs full-new-state checks after injected failures / normal mutations. Results measured with raw row counts + truth engine; a hybrid state is any discrepancy between source rows and derived rows.

| Operation | Failure Point | Expected | Actual DB State | Orphans | PASS/FAIL |
| --- | --- | --- | --- | --- | --- |
| `POST /add_direct_sale` — insufficient stock (natural failure) | stock validation, before insert | full old state | no sale/item/entry/pending rows | 0 | ✅ PASS (atomic) |
| `POST /add_direct_sale` — concurrent duplicate driver creation (IntegrityError) | after parent insert attempt | full old state | rolled back; flash error; no orphan rows | 0 | ✅ PASS atomic, ❌ message leaks SQL (PRED-010) |
| `POST /delete_transaction/DirectSale/<id>` | normal | full new state (sale+children removed, stock restored, account untouched) | sale/items/entries/pending gone; stock back to IN-net | 0 | ✅ PASS |
| `POST /accounts/payments/clients/void/<id>` then restore | normal | exact −/+ reversal once | void −5000; restore +5000; one active tx each time | 0 | ✅ PASS |
| `POST /edit_grn/<id>` (unlocked lots) | normal (void old + create new) | full new state | 1 old entry voided + 1 new entry; material total == entry net | 0 | ✅ PASS |
| `POST /edit_grn/<id>` (locked lots) | guard before change | full old state | completely unchanged | 0 | ✅ PASS (blocked; PRED-012 usability) |
| `POST /accounts/<id>/edit` desired-balance adjustment | normal + double-click (same idempotency key) | exactly one adjustment | 1 `Adjustment` tx; balance == desired | 0 | ✅ PASS |
| `POST /accounts/1/reconcile` (normal) | normal | final == account | 1 REC row; account == final | 0 | ✅ PASS |
| `POST /accounts/1/reconcile` (future-dated receipt present) | normal | final == account | **final 100011 ≠ account 100022** | 0 | ❌ FAIL (PRED-002) |
| `POST /delete_selected_data` full wipe on a copy | FK order | full new state | **full old state (rollback)** — no partial wipe, but operation never succeeds | 0 | ✅ atomic, ❌ functional (PRED-009) |
| `POST /add_booking` + `POST /add_material_return` + booking delivery | cross-module | booked balance math | allocations qty 100, entries 1, stock consistent, alternate-return restores balance | 0 | ✅ PASS |
| 100 × `POST /add_direct_sale` qty 0.1 | repeated writes | no drift | 100 rows, stock == entry net exactly | 0 | ✅ PASS |
| Restart (new process, same DB) | persistence | counts unchanged | unchanged; truth engine runs | 0 | ✅ PASS |

---

# E. TEMPORAL BOUNDARY MATRIX

Payments posted at 2026-07-31 23:59:59 / 08-01 00:00:00 / 08-01 00:00:01 / 08-31 23:59:59 / 09-01 00:00:00, then filtered Aug 1–Aug 31.

| Boundary Record | In Aug filter? | In DB | In page (`/payments?date_from=08-01&date_to=08-31`) | Correct? |
| --- | --- | --- | --- | --- |
| 07-31 23:59:59 | No | Yes | **absent** | ✅ |
| 08-01 00:00:00 | Yes | Yes | present | ✅ |
| 08-01 00:00:01 | Yes | Yes | present | ✅ |
| 08-31 23:59:59 | Yes | Yes | present | ✅ |
| 09-01 00:00:00 | No | Yes | **absent** | ✅ |

* `func.date(date_posted) >= from AND <= to` semantics: correct at all tested boundaries (no previous/next-day leakage).
* **Failures related to time:** future-dated receipts (09-01) are *allowed* and applied to the live account — that permission is what breaks reconciliation (PRED-002) and the period guard (PRED-008). GRN backdate policy blocks non-admin edits only; admin can backdate (tested 08-20 GRN at "current date" = OK).
* Month-end/year-end/leap-day boundaries for the cash-flow daily report were **not** exercised (see §H).

---

# F. CONCURRENCY MATRIX

| Test | Simulated | Successful | Failed | Duplicate rows | Stock drift | Money drift | Lock errors | Recovery |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 concurrent sales, 40+40 bags, same material | 2 threads | 2 | 0 | 0 | 0 (stock == net) | 0 | none | clean |
| 8 concurrent sales (dedicated DB) | 8 threads | 8 | 0 | **5 sales share SB-SL-1001** | 0 | 0 | none | **identity corruption (PRED-001)**; bills unreachable via viewer |
| 8 concurrent first-time sales auto-creating driver `D` | 8 threads | 1 | 7 (IntegrityError flash) | 0 | 0 | 0 | none surfaced | rollback clean, but internal SQL flashed (PRED-010) |
| 2 concurrent payments into the same account | not executed (SQLite serialises writer; see §H) | — | — | — | — | — | — | — |

Observations:
* SQLite `busy_timeout=8000` + the app's single-writer pattern means writers serialise; the demoed races are read-then-write interleavings (bill counter, unique-name insert) that are **not** protected by the lock.
* No `database is locked` errors were exposed to users during testing; exceptions were caught and flashed (with leaky text).
* Negative-stock invariant: stock validation prevents oversell in sequential tests (rejects with clean flash, full rollback). Under concurrency both sales against ample stock succeeded; the oversell race (70+70 vs 100) was not forced because stock validation reads `material.total` after the other transaction in WAL — **this remains SUSPECTED** (see §H — requires a deterministic multi-writer interleave with a busy-wait).

---

# G. TEST BLIND SPOTS (mutation checks)

Method: mutate one critical rule, restore via git immediately after; run full `pytest -q` (64 tests). Any mutation surviving green = blind spot.

| # | Mutation | File/line mutated | Effect | Existing tests | Verdict |
| --- | --- | --- | --- | --- | --- |
| M1 | Stock validation `raise` → `pass` (insufficient stock allowed) | `_direct_sales_add_direct_sale.py` | Sales can go negative stock | **64 passed** | 🔴 **BLIND SPOT** — no test asserts an insufficient-stock sale is rejected |
| M2 | `find_bill_conflict()` returns `None` always | `billing.py` | Duplicate manual bills accepted across all modules | **64 passed** | 🔴 **BLIND SPOT** — no test asserts duplicate-bill rejection |
| M3 | Client receivables projection drops **all** sales (`for sale in []`) | `financial_ledgers.py::_make_client_obligations` | Current payables no longer includes sales | **64 passed** | 🔴 **BLIND SPOT** — no test asserts sales appear in the receivables projection |

Each mutation also silently disables protections that this audit found were the ONLY thing standing between the app and corrupted state (M2 is the only guard against PRED-006-style duplicates for same-bill payloads).

---

# H. UNTESTED TERRITORY

| Area | NOT TESTED | WHY | RISK LEVEL | WHAT IS REQUIRED TO TEST IT |
| --- | --- | --- | --- | --- |
| Deterministic oversell race (70+70 vs stock 100) | Not forced | The app serialises writers; a deliberate lock-stall interleave needs a test hook/monkeypatch of `_rebuild_material_totals` inside the transaction | **High** (money/stock integrity) | A pytest-based two-connection test: begin `IMMEDIATE` on conn A, fire sale B, assert B waits/rolls back and stock never negative; verify `material.total` unchanged |
| Cash-flow daily report boundaries (month/year/leap) | Not exercised | Requires the cash-flow module seed (`cash_flow_difference_adjustment` etc.) and date-parameterised runs | Medium | Seed 00:00/23:59 rows around month/year/leap boundaries and compare daily report rows |
| Import/export engine (Excel full-raw import → full-raw export round trip) | Not exercised | Would need fixture spreadsheets; big surface | **High** (bulk write risk) | Round-trip a synthetic workbook through `full_raw_export` → `full_raw_import` on a clone DB, then run the truth engine + FK sweep + restart |
| Domain wipe granular paths (accounts only, clients only, etc.) | Blocked at `DELETE FROM grn_item` in the full wipe | The full wipe aborts before granular success can be assessed | Medium | Fix PRED-009 first, then per-dataset wipe with truth-engine verification |
| FBM Rentals modules (rentals, rental clients, rent items) | Not exercised | Separate domain; requires rental fixtures | Medium | Create rental lifecycle (create → deliver → return → settle), verify `fbm_rental`/`fbm_rental_item` and account effects |
| Delivery-person ledger settle + waive-off path | Not exercised | Settle flow depends on `delivery_rent` allocations | Medium | Create delivery rent row → `settle_delivery_person` → verify `delivery_person_payment` + account tx + loss row |
| Tenant DB restore / backup history restore | Not exercised | Destructive on the QA DB; requires a fixture DB copy and restore pipeline | **High** | Restore a backed-up DB into a scratch path, boot, run lifecycles, verify restart |
| Stale session / expiry / remember-me behaviour | Not exercised | Session guard only protects login; a stale `ams_session` was not simulated | Medium | Manipulate session cookie sign to invalid → verify no state change on POST (Flask `load_user` error path) |
| WeasyPrint native PDF path | Not exercised | Host lacks pango/cairo; the ReportLab fallback was tested instead | Low | Install system libs and re-run export forensics; assert page count + totals identical |
| PythonAnywhere/NFS deployment path (`DELETE` journal fallback, instance preserve/restore webhook) | Not exercised | Requires that environment | Medium | Deploy in staged env; run the auto-pull webhook with a committed instance snapshot and verify no data loss |
| Performance/volume (>1k bills, pagination at scale) | Not exercised | Time budget; QA set ~120 rows | Medium | Seed 5k sales + 2k payments; run route benchmark `tools/profile_requests.py` and page-walk at per_page 10/25/50 |
| CSRF replay with expired session token | Partially | Token tested missing; expired-token replay not | Low | Acquire token, expire session server-side, replay POST → must be 400 with no state change |
| Edit-payment optimistic-concurrency (`revision`) | Not exercised | Needs two concurrent edits | Low | Two threads editing the same payment row with different `revision`; assert one wins |
| `user`/admin role enforcement (non-admin user with partial permissions) | Not exercised | Only admin tested; role-based `_user_can` branches exist | Medium | Create a non-admin user with `can_manage_sales=0`, attempt sale/payment/void via UI, verify flash + rollback |

---

## 1. VERDICT SUMMARY

| Severity | Count | IDs |
| --- | ---: | --- |
| Critical | 2 | PRED-001, PRED-002 |
| High | 7 | PRED-003, PRED-004, PRED-005, PRED-006, PRED-007, PRED-008, PRED-009 |
| Medium | 3 | PRED-010, PRED-013 (+ informational PRED-014) |
| Low | 2 | PRED-011, PRED-012 |
| Coverage (blind spots) | 3 | M1–M3 |

**Positive (independently verified) behaviour:** Chain-A layer reconciliation (DB/TE/ORM/API/HTML/CSV/PDF) ✔; payment void/restore exact-reversal ✔; GRN full-rewrite edit ✔; booking + alternate-material return chain ✔; account adjustment idempotency ✔; 100× numeric repeat without stock drift ✔; temporal payment boundaries ✔; malformed-input error handling (no traceback leaks across 20 probes) ✔; pagination robustness ✔; restart persistence ✔; unauthenticated mutation = zero state change ✔; FK/orphan integrity on the QA dataset ✔.

**Notable internal controls that ARE present:** session-bound CSRF for `accounts.*`; core audit log; `before_flush` minor-unit harmoniser; FIFO lot locking; idempotency keys on sales/payments (payload-agnostic); transaction-wide rollback everywhere tested; readonly truth-engine tooling.

## 2. HOW TO REPLAY THIS AUDIT

```bash
# 1. independent truth engine (raw SQL, no app code)
python tools/predator_truth_engine.py --db <db.sqlite> --check      # non-zero exit = divergence

# 2. route shadow map
python tools/route_predator_map.py --db <db.sqlite> --json

# 3. full adversarial harness (creates a throw-away QA DB)
python .qa/predator_harness.py            # findings -> .qa/results/findings.json

# 4. scenario-specific reproductions (clean, deterministic)
python .qa/dbg_race.py                    # PRED-001  (8 threads)
python .qa/dbg_recon3.py                  # PRED-002  (future-dated receipt + reconcile)
python .qa/dbg_wipe2.py                   # PRED-009  (domain wipe on a DB copy)
```
