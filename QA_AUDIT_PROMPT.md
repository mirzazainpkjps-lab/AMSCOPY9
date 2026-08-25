# FULL-STACK HUMAN-SIMULATION QA PROMPT — AMS ERP (`AMSCOPY9`)

> Paste everything below the line into an AI coding agent that has shell access to this repo.
> It is written against the **verified** structure of this codebase (Flask 3 + SQLAlchemy 2 + SQLite,
> 487 registered routes, 109 Jinja templates, 67 ORM model classes across `models/*.py`,
> 64 existing pytest cases).

---

## ROLE

You are a senior QA engineer + full-stack auditor. You will test this application the way a **real
human operator** would — typing data into forms, clicking through pages, scrolling reports, and
comparing what came out against what went in. You will then audit the frontend, backend, API layer
and database for correctness, data-integrity and scale problems.

You are **not** doing a code read-through. Reading code is only allowed to explain a defect you
already reproduced by running the app. Every defect in your report must come with a **reproduction
command and the actual output you observed**.

---

## 0. HARD RULES — READ FIRST

These rules exist because a previous audit pass produced false findings by pattern-matching HTML.

1. **Never infer "data is present" from a substring match on a rendered page.**
   Naive checks like `"30,000" in html` produce false positives. Confirmed example from this repo:
   the string `30000` appears in `templates/layout.html` inside `setTimeout(hideLoading, 30000)`,
   so a substring check reported a sale as "visible in reports" when **no sale row existed at all**.
   To assert a figure is present you must:
   - match the app's real money format, e.g. regex `\b30,000\.00\b` (2 decimals, comma thousands), **and**
   - confirm the row context (client code / bill no / date) is in the same rendered row, **and**
   - cross-check against the JSON API or a direct DB query.

2. **Never infer "the save failed" from an HTTP status alone.**
   Confirmed example: `POST /add_direct_sale` returns **302** on success *and* **302** on
   validation failure. The only reliable signals are (a) the redirect `Location`, and (b) the flash
   message. A failure redirects to `/direct_sales?resume=add`; a success redirects to
   `/direct_sales?download_bill=<BILL>&download_src=direct_sale&...`.

3. **Read flash messages from the session, not by regexing HTML.**
   Regexing the body for `alert-danger` grabs CSS rules first. Use the session snapshot (see §2).

4. **A clean exit code is not a pass.** If a count is off by one or a total reads `0.0`, that is the
   result. Report it.

5. **State what you did not check.** An honest gap is acceptable. A guess written in the same voice
   as a verified fact is not.

6. **Do not modify `instance/*.db` on the real deployment.** Every write test must run against a
   throwaway `APP_DB_PATH` (see §1).

---

## 1. BOOTSTRAP THE ENVIRONMENT (verified working)

The system Python is PEP 668 managed — `pip install` fails without a venv.

```bash
cd /home/user/AMSCOPY9
python3 -m venv .venv
.venv/bin/pip install -q "flask>=3.1.2" "flask-login>=0.6.3" "flask-sqlalchemy>=3.1.1" \
  "sqlalchemy>=2.0.46" "werkzeug>=3.1.5" "pytest>=8.0.0" \
  "openpyxl>=3.1.5" "numpy<2" "reportlab>=4.0.7" "pandas>=2.2.0" "pypdf>=4.0.0"
```

`pypdf` is required or `tests/test_notes_visibility_and_pdf.py` fails at **collection** and the
whole suite aborts.

**Establish the baseline before changing anything:**

```bash
.venv/bin/python -m pytest tests/ -q
# Verified baseline on this branch: 64 passed in ~147s
```

**Dump the real route inventory** (do not trust documentation):

```bash
PYTHONPATH=. .venv/bin/python -c "
from app import create_app
app = create_app()
for r in sorted(app.url_map.iter_rules(), key=str):
    print(','.join(sorted(m for m in r.methods if m not in {'HEAD','OPTIONS'})), r, r.endpoint)
" > routes.txt
wc -l routes.txt   # Verified: 487 rules, 291 unique (method,url) pairs
```

---

## 2. THE TEST HARNESS YOU MUST USE

Every form POST needs a CSRF token. Confirmed: `app/hooks.py:149` reads
`request.form['_csrf_token']` or the `X-CSRF-Token` header; without it,
`POST /accounts/accounts/add` returns **400** `{"error":"Invalid or expired form token. Reload the page and try again."}`

Use this harness pattern (verified against the live routes):

```python
import os, sys
sys.path.insert(0, "/home/user/AMSCOPY9")
DB = "/tmp/qa.db"
for p in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(p): os.remove(p)
os.environ.update(APP_DB_PATH=DB, ALLOW_EMPTY_DB="1", BACKUP_EMBEDDED_SCHEDULER="0",
                  AMS_SCHEMA_VERSION="v44", DEFAULT_ADMIN_USER="Admin",
                  DEFAULT_ADMIN_PASSWORD="Admin@fbm12345", TESTING="1")
from app import create_app
app = create_app(); c = app.test_client()

def csrf():
    with c.session_transaction() as s:
        t = s.get("_csrf_token")
        if not t:
            import secrets; t = secrets.token_hex(16); s["_csrf_token"] = t
        return t

def post(url, data):
    """POST with CSRF; returns (response, flash_messages)."""
    with c.session_transaction() as s: s["_flashes"] = []
    d = dict(data); d.setdefault("_csrf_token", csrf())
    r = c.post(url, data=d, follow_redirects=False)
    with c.session_transaction() as s:
        return r, list(s.get("_flashes", []))

c.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"})   # -> 302 to /
```

---

## 3. VERIFIED FORM CONTRACTS (use these exact field names)

Discovered from the route handlers — do not guess.

| Purpose | Route | Required fields |
|---|---|---|
| Login | `POST /login` | `username`, `password` |
| Material | `POST /add_material` | `material_name`, `material_unit`, optional `material_code`, `category_id` |
| Client | `POST /add_client` | `name`; optional `code`, `phone`, `address`, `category`, `opening_balance`, `opening_balance_date` |
| Delivery person | `POST /delivery_persons/add` | `name`, `phone` — **note: there is NO `/add_delivery_person` route** |
| Supplier | `POST /add_supplier` | `name`; optional `phone`, `address`, `opening_balance` |
| Cash account | `POST /accounts/accounts/add` | `name`, `class_category`, `class_subcategory`, `class_account_type`, `channel`, `account_status`, `opening_amount` + `_csrf_token` |
| Stock in (GRN) | `POST /grn` | `action=add`, `supplier`, `mat_name[]`, `qty[]`, `price[]`, `date`, `paid_amount` |
| Direct sale | `POST /add_direct_sale` | `client_name`, `driver_name`, `product_name[]`, `material_id[]`, `qty[]`, `unit_rate[]`, `paid_amount`, `payment_method`, `payment_account_id`, `category`, `has_bill` |

Valid account classification triples come from `blueprints/accounts/classification.py::CLASSIFICATION`.
Verified working cash triple: `class_category=Assets`, `class_subcategory=Cash`,
`class_account_type=Main Cash`, `channel=cash`.

`DirectSale` has **no `sale_date` column** — the column is `date_posted` (`models/sales.py:190`).
Seeding with `sale_date=` raises `TypeError: 'sale_date' is an invalid keyword argument`.

---

## 4. PHASE A — END-TO-END HUMAN FLOW (input → output comparison)

Run this exact chain. It is verified to succeed end-to-end. Any step that deviates is a finding.

**Setup order matters** — this is the dependency chain a real user must satisfy:

1. Login as `Admin` / `Admin@fbm12345` → expect `302` → `/`
2. Create material `QA Cement 50kg` → flash `Brand Added — by Admin`
3. Create client `QA Sentinel Client` → flash `Client Registered — by Admin`, code `FBMCL-00001`
4. Create driver `QA Driver Alpha` → flash `Delivery person saved — by Admin`
5. Create cash account `QA Main Cash` → flash `Account added successfully! — by Admin`
6. Create supplier `QA Supplier Beta` → flash `Supplier Added — by Admin`
7. **GRN stock-in 500 bags @ 120** → flash `GRN added successfully!` → verify `Material.total == 500.0`
8. **Direct sale: 200 bags @ 150 = 30,000, paid 10,000** → flash `Direct sale added successfully`

**Now compare input against output. Verified expected values:**

| Assertion | Expected | How to verify |
|---|---|---|
| `DirectSale.amount` | `30000.0` | DB query |
| `DirectSale.paid_amount` | `10000.0` | DB query |
| `DirectSaleItem` count | `1` (`qty=200.0`, `price_at_time=150.0`) | DB query |
| Cash `Account.balance` | `10000.0` | DB query |
| `AccountTransaction` rows | `1` | DB query |
| `Material.total` after sale | `300.0` (500 − 200) | DB query |
| `/api/current_payables` | `outstanding: 20000.0`, `total_outstanding: 20000.0`, `status: "Outstanding"` | JSON |
| `/current_payables` HTML | contains `20,000.00` | regex on 2-decimal money format |
| `/profit_reports` HTML | contains `30,000.00` | regex |
| Bill number | auto-issued `SB-SL-1000` appears in the redirect URL, but `DirectSale.manual_bill_no` stays `NULL` and `auto_bill_no` holds it | DB query — **check both columns** |

**Then run the negative cases.** Each of these is a *confirmed* guard — verify the message is
human-readable and that **no partial row is committed**:

- Sale with `paid_amount > 0` but **no** `payment_account_id` →
  `Select a cash/bank account for the paid amount to post into Accounts. — by Admin`
- Sale of **200 bags with zero stock** →
  `Insufficient stock for QA Cement 50kg. Available: 0, Required: 200.0 (Non-booked). Enable 'Allow Negative Stock' or global setting to bypass.`
- Sale with **no driver** → `Delivery person is required for sale dispatch.`
- Sale with **non-booked item and rate 0** → `Rate is required for non-booked items: <name>`
- Sale with **no valid line items** → `No valid material items were captured. Add at least one item with qty > 0.`
- Sale where **delivery bags > total qty** → `Total delivery bags cannot exceed total material quantity for this sale.`
- Account POST **without CSRF token** → `400 {"error":"Invalid or expired form token..."}`

**Known weak error surfacing to re-test and confirm:** submitting `payment_account_id` as the
literal string `"None"` produces the user-facing flash
`invalid literal for int() with base 10: 'None' — by Admin` — a raw Python exception leaked into
the UI instead of a validation message. Find every route that leaks exception text into a flash.

---

## 5. PHASE B — LARGE DATA ENTRY & SCALE (every module, every page)

Seed a large dataset and re-measure. Verified seed + timings for reference:

```
seeded in 0.7s: clients=1500 materials=200 sales=6000 sale_items=6000
```

```
PAGE                    status  ms       bytes
/direct_sales           200     204      2798778
/current_payables       200     255      334222
/financial_details      200     134      724405
/profit_reports         200     111      830770
/clients                200     246      781779
/materials              200     10       201505
/client_ledger/1        200     26       92537
/stock_summary          200     19       219371
/cash_flow              200     26       175058
/bookings               200     40       1427516
/pending_bills          200     152      1458939
/grn                    200     27       216873
/dispatching            200     27       958510
/ledger                 200     50       1265799
```

Scale up to **25,000 sales / 10,000 clients / 2,000 materials** and record the curve.

### Confirmed scale defect to reproduce and size

`/direct_sales` returns **2,798,678 bytes** at only 1,500 clients, even though the sales table
**is** correctly paginated (`app/blueprints/sales/_direct_sales_direct_sales_page.py:13-15`,
`per_page` clamped to 10–50). The payload is *not* the sales list. It is the client picker:

- Every client is rendered as a `combobox-item` `<div>` with `data-combo-code` / `data-combo-name`
  attributes, **4 times** — once per input: `dsFilterClient`, `addSaleClientCode`,
  `manualClientName`, `addSaleManualClientDisplay`. 1,500 clients → **6,000 DOM divs**.
- Plus `const knownClientNames = new Set([...1500 strings...])` and
  `const knownClientCodes = new Set([...1500 strings...])` (≈ 48 KB combined).
- Measured: `Bulk Client` occurs **12,010 times** in one page; `<option>` tags only **27**.

Payload grows **O(clients)**, not O(page size). Extrapolate and report the payload at 5,000 and
10,000 clients. Note that `GET /api/clients/search` **already exists** and would allow the combobox
to be lazy-loaded instead — check whether it is wired up at all.

### Also measure under load
- Query counts per page (use `tools/profile_requests.py`, already in the repo).
- Page render time on a cold worker vs warm (the app warms the Jinja cache for 109 templates at startup).
- Whether any page loads an unbounded `.all()` with no `.limit()`.
- Memory growth over 500 sequential POSTs.

---

## 6. PHASE C — REPORTING SECTION (real page scroll + data comparison)

The reporting surface (verified routes):

`/profit_reports`, `/financial_details`, `/current_payables`, `/cash_flow`,
`/cash_flow_differences`, `/stock_summary`, `/daily_transactions`, `/ledger`, `/decision_ledger`,
`/client_ledger/<id>`, `/delivery_ledger/<id>`, `/supplier_ledger/<id>`, `/material_ledger/<id>`,
`/accounts/ledger/<id>`, `/accounts/kpi/*`, `/void_audit`, `/system_report`

For **each** report, with the Phase A dataset loaded:

1. Load the page and record status, byte size, and render time.
2. **Scroll the full rendered document** (in a headless browser if available, otherwise parse the
   whole HTML) and confirm:
   - the header/footer render (no truncated template),
   - every table has a header row and at least one data row,
   - **totals rows equal the sum of their own visible line items** — recompute them yourself,
   - no `None`, `nan`, `NaN`, `undefined`, `[object Object]`, or `&#39;` leaking into cells,
   - money is formatted consistently (2 decimals) everywhere,
   - dates are not `1970-01-01` or blank.
3. Cross-check the HTML against the JSON API for the same report (e.g. `/api/current_payables`)
   — **they must agree**.
4. Apply every filter the page offers (date range, client, status, amount operator) and confirm the
   filtered total equals the sum of the filtered rows.
5. Exercise pagination: page 1 → last page → back. Row counts must be stable and no row may
   appear twice or vanish.
6. Test the export paths (`/export_current_payables`, `/export_unpaid_transactions`,
   `/download_client_ledger/<id>`, `/download_supplier_ledger/<id>`) and confirm the exported
   figures match the on-screen figures **exactly**. PDF exports must contain the figures verbatim —
   `tests/test_ledgers_and_pdf_accuracy.py` already does this for some; extend it.
7. **Empty-state and single-row state:** run every report against a **fresh empty database** and
   confirm it renders a proper "no data" state rather than a 500 or a blank page.

**Verified reference values for Phase A data:** `/api/current_payables` returns
`{"outstanding":20000.0,"total_outstanding":20000.0,"total_records":1,"status":"Outstanding",
"client_code":"FBMCL-00001"}`.

---

## 7. PHASE D — DATABASE INTEGRITY & ORPHANS

67 ORM model classes across `models/` (`cash.py`, `catalog.py`, `core.py`, `delivery.py`,
`events.py`, `imports.py`, `migration.py`, `ops_meta.py`, `parties.py`, `rentals.py`, `sales.py`,
`stock.py`).

1. **FK integrity:** `PRAGMA foreign_key_check` after every destructive operation.
2. **Orphan sweep** after each delete/void/wipe — a child row whose parent is gone. Known risk
   points documented in `ORPHAN_SCENARIO_AUDIT.md`: `BookingItem`, `DirectSaleItem`, `GRNItem`,
   `MaterialReturnItem`, `DeliveryItem`, and `SaleDeliveryPerson` — verified at
   `models/delivery.py:18-31`, `SaleDeliveryPerson` has FKs to `direct_sale.id` and
   `delivery_person.id` but **no `cascade='all, delete-orphan'`** on either relationship.
   Confirm each by experiment, not by reading the audit doc.
3. **Money invariants** — these must hold after every operation you perform:
   - `sum(DirectSale.amount) - sum(DirectSale.paid_amount)` == reported total outstanding
   - each `Account.balance` == its opening balance + sum of its `AccountTransaction`s
   - `Material.total` == opening + GRN in − sales out + returns in
   - no negative balance unless negative stock is explicitly enabled
4. **Float money:** amounts are stored in `db.Float` alongside `*_minor` integer columns
   (`balance_minor`, `opening_balance_minor`). Verify the float and minor columns never drift apart
   after repeated edits — check `utils/money.py::to_minor` / `from_minor` round-trips.
5. **Idempotency:** `DirectSale.idempotency_key` and `AccountTransaction` idempotency exist.
   Submit the **same form twice** (double-click simulation) and confirm exactly one row is created.
   `tools/roundtrip2_idempotency.py` exists — run it.
6. **Void/restore round-trip:** void a sale, confirm stock and account balance revert exactly,
   restore it, confirm they come back exactly. Then void→edit→restore and re-check.
7. **Wipe engine:** `tests/test_wipe_granular.py` exists. Run it, then test the wipe UI
   (`app/blueprints/misc/_wipe_*.py`) for partial-selection cases where a parent is wiped but a
   child table is not selected.

---

## 8. PHASE E — API, SECURITY & AUTH

1. **Unauthenticated access:** hit every one of the 487 routes with no session. Only `/login` and
   static assets should be reachable. Report anything else.
2. **Authorization:** create a non-admin user and confirm master-data mutation, wipe, admin and
   tenant routes are denied. `tools/audit_permissions.py` exists.
3. **CSRF coverage:** find every state-changing route that does **not** require `_csrf_token`.
   Accounts is protected (`app/hooks.py:136-153`) — verify which other blueprints are not.
4. **Injection & boundary inputs** into every text/number field:
   - SQL: `' OR 1=1 --`, `'; DROP TABLE client; --`
   - XSS: `<script>alert(1)</script>`, `"><img src=x onerror=alert(1)>` — then **read the value back
     on a list page and confirm it is escaped, not executed**
   - Numbers: negative, `0`, `1e309`, `NaN`, `Infinity`, `1,000`, `12.3456789`, empty, whitespace
   - Strings: 10,000-char values, unicode/RTL, emoji, trailing spaces, leading zeros
   - Dates: `2026-02-30`, `31-12-2026`, empty, future-dated
5. **Secrets in the repo** — confirmed present, verify and report current state:
   - `main.py:36-39` — `WEBHOOK_TOKEN` falls back to the literal `"PakistanZindabad1947-2026"`
     (line 38) when `AMS_WEBHOOK_TOKEN` is unset.
   - `main.py:54-56` — `GITHUB_REPO` points at
     `https://github.com/rehmanahmedca-source/ams99.git` (line 55), **not** this repo (`AMSCOPY9`).
   - `main.py` `deploy()` — confirmed `git reset --hard` (see the note at `main.py:135`), with
     `preserve_instance_data()` at `main.py:148`, `restore_instance_data()` at `main.py:182`, and the
     call sites at `main.py:270` / `main.py:413`. Confirm that a `preserve_instance_data()` failure
     cannot destroy `instance/*.db`.
6. **Missing schema bundle:** `app/__init__.py:86` sets
   `AMS_V44_SCHEMA_PATH = <root>/v44/SCHEMA_v4_4.sql`, but **the `v44/` directory does not exist
   in this checkout**. Confirmed boot log:
   `WARNING [app.services.v44_schema]: v4.4 schema file not found ...; falling back to the ORM schema bootstrap.`
   Determine whether the ORM-fallback schema differs from the intended v4.4 schema (missing indexes,
   constraints, defaults) and report the delta.

---

## 9. PHASE F — ROUTING & FRONTEND STRUCTURE

1. **Duplicate route registrations — confirmed: 194 of 291 unique `(method, url)` pairs are
   registered more than once**, producing 487 rules. Cause:
   `app/__init__.py:407 _alias_unprefixed_endpoints()`, which intentionally registers a bare
   endpoint alias so legacy `url_for('clients')` calls keep working after the blueprint split.
   For each duplicate, determine which view actually wins and whether the shadowed one is dead code.
   Verified with `app.url_map.bind('localhost').match(url)`:
   - `/clients` → `masters.clients` (shadowing bare `clients`)
   - `/current_payables` → `reports.unpaid_transactions_page`
   - `/export_unpaid_transactions` is registered **4 times**
     (`reports.export_current_payables`, `misc.export_unpaid_transactions`,
     `export_current_payables`, `export_unpaid_transactions`) and resolves to
     **`reports.export_current_payables`** — so `misc.export_unpaid_transactions` is unreachable at
     that URL. Editing it has no effect. Flag every such trap.
2. **`url_for` correctness:** render every template and confirm no `BuildError`. `tools/live_smoke.py`
   does the GET sweep — run it:
   ```bash
   APP_DB_PATH=/tmp/qa.db ALLOW_EMPTY_DB=1 .venv/bin/python tools/live_smoke.py
   # Verified: "SMOKE PASS — all pages load, no 500s"
   ```
3. **Dead JS / broken handlers:** for each page, confirm every `onclick`/`submit` handler in the
   rendered HTML points at a route that exists in `routes.txt`, and every `fetch()` URL resolves.
4. **Frontend UX under real use:** tab order, required-field indicators, whether validation errors
   are shown next to the offending field, whether the loading overlay can get stuck (note the
   `setTimeout(hideLoading, 30000)` watchdog in `templates/layout.html`), modal focus traps, and
   behaviour at 1280px and 375px widths.
5. **Pagination & scroll:** on every list page confirm the pager, the row-count label, and
   `per_page` selector agree with the actual rendered rows.

---

## 10. DELIVERABLE

Produce a single `QA_FULL_AUDIT.md` containing:

1. **Executive summary** — defect counts by severity, and a go/no-go verdict.
2. **Defect register.** One row per finding:

   | ID | Severity | Module | Page/Route | What I did | Expected | Actual (verbatim output) | Evidence (file:line) | Fix |

   Severity = `Critical` (data loss / wrong money / security) · `High` (feature broken) ·
   `Medium` (wrong display, poor validation) · `Low` (cosmetic).

   **Every row must contain a command that reproduces it and the literal output you observed.**
   No exceptions.

3. **Money reconciliation table** — for each of the Phase A/B datasets: expected vs actual for sale
   total, paid, outstanding, cash balance, stock, and each report's grand total.
4. **Scale table** — payload bytes and render ms at 1.5k / 5k / 10k / 25k rows per page.
5. **Coverage matrix** — every route from `routes.txt` × {GET smoke, POST tested, auth checked,
   boundary inputs}. Mark untested cells explicitly as `NOT TESTED` and say why.
6. **What I could not verify** — an explicit, honest list.

---

## 11. SUGGESTED EXECUTION ORDER

1. §1 bootstrap + `pytest` baseline (must be 64 passed before you touch anything)
2. §1 route dump → build `routes.txt`
3. §4 Phase A end-to-end, positive then negative
4. §6 Phase C reporting on the Phase A dataset
5. §5 Phase B bulk seed + scale curve
6. §7 Phase D integrity/orphans/idempotency
7. §8 Phase E security sweep
8. §9 Phase F routing + frontend
9. §10 write `QA_FULL_AUDIT.md`

Re-run `pytest tests/ -q` and `tools/live_smoke.py` at the **end** and report both results. If
either regressed against the baseline, that is your top finding.
