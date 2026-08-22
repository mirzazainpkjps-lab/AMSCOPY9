# Suppliers / GRN / GRN-Edit / Data-Viewing — Real Problems Found & Fixed

**Date:** 2026-08-22 · **Scope:** full read of the Supplier + GRN code paths, plus a direct inspection of the real database snapshot `instance/ahmed_cement.db`.
**Method:** every claim below was verified either by (a) reading the exact code path, (b) running reproduction tests through the app's real routes, or (c) querying your actual data. Nothing here is a guess.

---

## 1. Problems FIXED in this branch (all proven by reproduction tests, now `tests/test_grn_bugcheck.py`)

| # | What you were experiencing | Root cause found | Fix applied |
|---|---|---|---|
| F1 | **You edit a GRN, change Bill Date / Due Date, press Save — it says "GRN updated successfully" but the dates never change.** | `edit_grn` (`app/blueprints/misc/pending.py`) never read `bill_date` / `due_date` from the form. Add reads them; Edit silently ignored them. | Edit now saves both dates. Empty field = intentionally cleared; a form that doesn't send them keeps old values. |
| F2 | **Editing a GRN wipes its photo link (`photo_url`).** | The edit route did `grn_obj.photo_url = request.form.get('photo_url','')` — but the wizard form has NO `photo_url` input, so every edit wrote `''`. | Only overwrite when the form actually sends a value. |
| F3 | **Typing a number with a comma (e.g. `1,500` in Load/Freight/Other Expense, Paid Amount, Tax) crashes the whole save with an error page (HTTP 500).** | `float(request.form.get('loading_cost'))` — a raw Python `float()` on user input. No try/except. Crashed at `app/blueprints/ops/grn.py:38`. | All GRN money fields (add **and** edit) go through a parser that accepts commas and, for real garbage, shows a readable flash message ("'Paid Amount' has an invalid number…") instead of crashing. Nothing is half-saved. |
| F4 | **Item lines with qty 0 (or negative) silently disappear from the saved GRN.** You enter items, save, get "success!" — later some items are missing and you don't know why. | Server-side guard skips `qty <= 0` lines (correct for stock safety) but flashed NOTHING. You never knew. | A warning now appears: "GRN saved, but these item lines were NOT saved because quantity was 0 or negative: …". Same on edit (where it also voids the old line — so this warning is your signal the line was dropped). |
| F5 | **Two suppliers can end up with the same name.** When that happens, ledgers that join GRNs **by name** can mix one supplier's GRNs into the other supplier's ledger, and "pay supplier" can post to the wrong party. | `add_supplier` checks duplicates, but `edit_supplier` **didn't** — renaming supplier A to the same name as supplier B was allowed (reproduction test created 2× "OTHER SUPPLIER"). | Edit now blocks a rename to an existing name (case-insensitive) with a clear message. |
| F6 | **After renaming a supplier, old GRNs still show the OLD name.** This is real in your data: supplier was renamed to **"Faizan Fecto"**, but GRNs **#1, #2, #3 still say "Faizan Facto"** — so the GRN list, GRN search by supplier, and exports show a supplier that "doesn't exist". | Renaming only updated the `supplier` table. The copied name strings on `GRN.supplier` and `Entry.client` were never touched. | `edit_supplier` now syncs `GRN.supplier` + the GRN's IN-entry labels on rename (including legacy GRNs with no `supplier_id`). **Repair script for the 3 existing rows:** `tools/inventory/fix_stale_grn_supplier_names.py` (dry-run by default, `--apply` to write — verified on a copy of your DB: fixes 3 GRNs + 3 entries). |
| F7 | **Suppliers page gets slower as data grows** (and can show opening balance instead of the real balance if the ledger build fails — it silently swallows errors). | The page built a **full financial ledger per supplier** — each ledger runs one payment-lookup query **per GRN**. With S suppliers × G GRNs that's S + S×G queries. | The page now uses `build_supplier_payable_summaries` (the bounded projection that already existed for dashboards): 2 bulk queries + in-memory grouping. Same numbers, fraction of the cost. |

**Test evidence:** `tests/test_grn_bugcheck.py` (8 new regression tests — every one FAILED against the old code and PASSES now). Full suite: **203 passed** (`inventory_flows`, `grn_fifo_costing`, `sales_roundtrip`, `unified_financial_ledgers`, `role_permissions`, and the rest).

---

## 1-bis. Supplier-ledger viewing bugs reported 2026-08-22 (session 2) — FIXED

**Reported:** In the Zia Traders supplier ledger, (1) every payment shows a fake `PAY-##` number with no tracking code, and clicking it opens a **random client's bill**; (2) no Action buttons (View/Edit/Print/Delete/Download) on any row.

| # | Root cause found | Fix applied |
|---|---|---|
| F8 | **All 78 payments in the DB have NO bill number at all** (`auto_bill_no` and `manual_bill_no` both empty — they predate the auto-numbering that `save_supplier_payment` now does). The ledger therefore displayed an invented fallback label `PAY-<id>` — and the template linked that label to the generic bill lookup `/view_bill/PAY-22`, which resolves nothing and falls through to an unrelated client bill. | Ledger rows for supplier payments now link directly to the **supplier payment receipt** (`/download_supplier_payment/<id>`), and GRN rows link to `/view_bill/<ref>?src=grn&src_id=<id>` so a GRN's manual bill number can never collide with a client's bill. |
| F9 | Same cause — no tracking numbers on existing payments. (New payments already get `SB-SP-####` automatically; the old ones never did.) | **Backfill tool:** `python3 tools/inventory/backfill_supplier_payment_bill_nos.py --apply` gives every existing payment a unique `SB-SP-####` in chronological order (verified on a copy of the live DB: all 78 numbered). Dry-run by default. |
| F10 | The shared ledger template rendered Actions only for driver settlements; supplier rows showed "—". | Supplier-ledger rows now have actions (supplier ledger ONLY — client & driver ledgers untouched, guarded by a regression test): **GRN rows:** View · Print · Edit · Delete (identical endpoints to the GRN page). **Payment rows:** View · Download (PDF/HTML receipt) · Edit (jumps to Accounts → Supplier Payments, pre-filtered to this supplier, where the shared edit modal is one click away) · Delete (reverses accounting, keeps history). GRN-controlled auto-payments show "Edit GRN" instead, so they're managed at the source. |

Tests: `tests/test_supplier_ledger_actions.py` (3 new — links, tracking numbers, and a guard that client ledgers got none of the new buttons). Full suite: **206 passed**.



These are not code bugs — they are real inconsistencies sitting in your database right now. They are what you SEE when viewing stock/supplier data.

### 2.1 Stock is deeply negative for ~50 materials
Examples from your data: `12MM STEEL −81,195`, `ISM 12MM STEEL −63,876`, `RENT-STEEL −47,364`, `RENT-CEMENT −23,112`, `CHAUGATH −12,300`, `20MM STEEL −11,680`.

Why (three stacked causes):
1. **Sales history was imported, purchases were not.** There are **4,504 OUT (sale) entries** vs only **150 IN entries** (48 of them from GRNs; 102 are opening/import rows). The old app's sales came in — the old purchases/GRNs mostly didn't.
2. **The same material exists under two spellings**, so purchases land on one name and sales on the other. Concrete pairs found in your material master:
   - `PIONEER` (−4,507) vs `PIONER` (−35)
   - `10MM STEEL` (−2,298) vs `STEEL 10MM` (−155.6)
   - `RENT STEEL` (−18,299) vs `RENT-STEEL` (−47,364)
   - `RING 7X7` (−20.5) vs `RINGS 7X7` (−614)
   - `FAUJI` (−3,505) vs `FT-FAUJI` (−773); `DG` (−8,795) vs `FT-DG` (−317); `KOHAT` (−7,439) vs `FT-KOHAT` (−965)
3. **10 GRNs were hard-deleted** (missing ids: 7, 11, 15, 18, 19, 20, 23, 36, 40, 46) — their stock reversal came out, but sales made against those materials stayed in.

**Knock-on effect that disturbs daily work:** negative-stock protection is ON, so selling these materials gets **rejected** — and the yard works around it by picking a *different* (wrong) material name, which makes the data worse every day.

### 2.2 Stored stock (`material.total`) ≠ entry ledger for 4 materials
`DG` differs by **+20**, `ISM 12MM STEEL` by **+184.8**, `20MM STEEL` by **+1.0**, `6MM STEEL` by **+0.3**.
Root cause class: paths that hand-adjust `material.total` by *name lookup* — if the material was renamed/missing at that moment, the reversal misses and the total drifts from the movement ledger. (The 2026-08-17 audit had this at zero drift; it came back — edits happened after that audit.)
**Repair that already exists:** `python3 tools/inventory/reconcile_stock.py` recalculates every `material.total` from the entry ledger. Run it after taking a backup.

### 2.3 One renamed supplier (historical)
Covered in F6 above — repair script provided.

---

## 2-bis. Settings → User Roles: READ-ONLY / READ & WRITE access mode — ADDED (session 3)

**Request:** a per-user setting in Settings → User Permissions to make an account **Read Only** (view everything, change nothing) or **Read & Write** (normal).

| What | Detail |
|---|---|
| Where | Settings → User Permissions → **Access Mode** selector in both the *Create User* modal and the *Edit Permissions* modal; the users table shows a **READ ONLY** / **READ & WRITE** badge per user. |
| Behaviour | `Read Only` keeps every ticked module for **viewing** (GET) but blocks **every** save / edit / delete (POST/PUT/PATCH/DELETE) with a clear message: *"Your account is READ-ONLY — viewing is allowed, saving/changing/deleting is blocked."* AJAX calls get a JSON 403. The user's own light/dark theme preference still works. `Read & Write` = exactly the old behaviour. Administrators are never restricted. |
| Enforcement | One new `before_request` hook (`_enforce_read_only_access_mode` in `app/hooks.py`). Existing permission checkboxes, endpoint map and all module code are untouched — the hook only acts when a non-admin user's `access_mode` is `read_only`. |
| Storage | New `user.access_mode` column (`read_write` default / `read_only`), auto-added to the DB at startup by the existing `_ensure_model_columns` migration — no manual step. Existing users default to Read & Write. |
| Bonus bug fixed | `/add_user` (Create User in Settings) was **crashing with a NameError** on the original code — `generate_password_hash` was missing from the imports. Fixed and covered by tests. |

Tests: `tests/test_read_only_access_mode.py` (5 tests: view allowed / write blocked, default = read & write, admin switch via Settings on & off, admins unaffected, theme exemption, Settings UI shows the selector). Full suite: **211 passed**.

---

## 3. Remaining code-level problems (NOT yet changed — need your decision)

1. **THE BIG ONE — your live database is still tracked in Git and deploys run `git reset --hard`.** Already proven in `DATA_LOSS_INVESTIGATION.md`: this is why data "disappears and reappears". `main.py` now preserves `instance/` around the reset, but `git ls-files` still shows `instance/ahmed_cement.db`, `-wal`, `-shm`, `secret_key` tracked, and `.gitignore` does not exclude them. **Recommended next step (say the word and I'll do it):** `git rm --cached instance/*` + `.gitignore` entries, deployed together with the existing preserve/restore mechanism.
2. **Any GRN whose lots were consumed by a sale cannot be edited AT ALL** — not even its note, date, or price. `_grn_has_locked_lots` blocks the whole edit. Protects FIFO costing, but very blunt; a finer rule (allow edits that don't reduce locked qty / allow header-only edits) needs a business decision.
3. **GRN list has no pagination** — every visit loads ALL GRNs (and the edit page renders the full list again below the form). Fine at 48 GRNs; painful at a few thousand.
4. **Typing a new supplier name straight into the GRN form silently creates a new Supplier master record.** Every typo becomes a supplier. Consider requiring selection from the dropdown, or an "add new supplier?" confirmation.
5. **`add_supplier` from the GRN modal skips phone/address validation and permission is enforced only via the endpoint map** — minor, but it is how junk suppliers get created.
6. **SQLite locking on PythonAnywhere + the background auto-reconcile thread that WRITES data** (renames items, moves bill refs, overwrites `Material.total`). Already documented in `DATA_LOSS_INVESTIGATION.md` §2–3; still true.
7. **`settings` table is empty** in your DB (0 rows) — every setting falls back to code defaults, including the negative-stock policy. Worth setting explicitly in the UI so behaviour is predictable.

---

## 4. What I changed (files)

| File | Change |
|---|---|
| `app/blueprints/ops/grn.py` | `_parse_grn_money()` parser used for all money fields; skip-warning flash for zero-qty lines |
| `app/blueprints/misc/pending.py` (`edit_grn`) | money parser; `bill_date`/`due_date` saved; `photo_url` preserved; skip-warning flash |
| `app/blueprints/masters/edit_supplier.py` | duplicate-name guard; rename syncs `GRN.supplier` + IN-entry client labels |
| `app/blueprints/masters/suppliers.py` | page uses `build_supplier_payable_summaries` (bounded) instead of per-supplier full ledgers |
| `app/services/api.py` | re-export `build_supplier_payable_summaries` |
| `tools/inventory/fix_stale_grn_supplier_names.py` | NEW — one-shot repair for stale GRN supplier names (dry-run default) |
| `tools/inventory/backfill_supplier_payment_bill_nos.py` | NEW — assigns SB-SP-#### tracking numbers to all existing supplier payments (dry-run default) |
| `templates/financial_ledger.html` | supplier-ledger reference links point at payment receipts / GRN hints; supplier-only action buttons |
| `tests/test_grn_bugcheck.py` | NEW — 8 regression tests covering F1–F7 |
| `tests/test_supplier_ledger_actions.py` | NEW — 3 regression tests covering F8–F10 |

## 5. Suggested order of operations for you

1. Merge/deploy this branch (fixes stop the bleeding for GRN edit/save crashes, silent drops, and supplier rename problems).
2. On the server, take a backup, then run:
   - `python3 tools/inventory/fix_stale_grn_supplier_names.py --apply`
   - `python3 tools/inventory/reconcile_stock.py`
3. Decide the duplicate-material merges (PIONEER/PIONER, RENT STEEL/RENT-STEEL, …) — `rename_material_label.py` exists for this; merging moves both sides onto one name and the negatives mostly cancel out.
4. Decide how to bring in the missing purchase history (old-app GRNs / opening balances) — that is the true fix for the giant negatives.
5. Then tackle the Git-tracked-database issue (item 3.1) — I can prepare it as a separate, carefully-ordered change.
