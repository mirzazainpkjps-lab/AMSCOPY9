# USER ROLE / MODULE PERMISSION AUDIT — 19 Aug 2026

Re-audit of **every module** against the permission system, because many new
functions were added. This document is the current source of truth for
**what features exist, and who has access to what**.

---

## 1. How the audit was done

A re-runnable audit tool was added:

```
.venv/bin/python tools/audit_permissions.py
```

It boots the app, lists **all 281 registered views** (473 URL endpoints incl.
legacy aliases), and classifies every one as:

| Classification | Meaning | Count |
|---|---|---|
| **MAPPED** | protected by `ENDPOINT_PERMISSION_MAP` or blueprint prefix | **215** |
| **INLINE** | protected by an explicit check inside the view (`_user_can` / role / 403) | 25 |
| **ROLE-GATED** | blueprint-level admin-only gate (`/admin/*`) | 3 |
| **ROOT-ONLY** | `require_root()` — disabled in single-store mode (404) | 17 |
| **PUBLIC-OK** | login page / logout / dashboard | 3 |
| **REMAINING** | login-only by design (see §5) | 18 |

**Stale map keys: 0** — no permission entry points to a dead endpoint.

---

## 2. What was found BEFORE this update (the gaps)

### 🔴 Whole new modules with NO permission at all (every logged-in user could open them)

| Module | URL | Endpoints | Now protected by |
|---|---|---|---|
| **Financial Accounts (Khata)** | `/accounts/…` | ~40 (dashboard, accounts, categories, transactions, transfers, receipts, expenditures, client/supplier payments, KPIs, reconciliations, audit trail) | **NEW** `can_manage_accounts` (OFF by default) — legacy payment managers keep entry (see §4) |
| **Cash Flow** | `/cash_flow`, `/cash_flow_differences`, reconciliation detail, meta APIs, `/api/current_payables*` | 9 | **NEW** `can_view_cash_flow` (OFF by default) |

### 🟠 Endpoints of existing modules that slipped past the permission map

| Area | Endpoints | Now protected by |
|---|---|---|
| Import/Export async job engine | upload / list / get / delete / start / progress / history / browse-history / cancel job (`/import_export/uploads`, `/import_export/jobs/…`) | `can_import_export` |
| Import/Export extended pages | full raw import history + report, tenant DB export/restore, transfer export/import | `can_import_export` |
| Supplier ledger | `download_supplier_ledger`, `download_supplier_payment`, `pay_supplier` page | `can_view_supplier_ledger` / `can_manage_suppliers` |
| Driver (delivery person) ledger | page, search API, ledger API, PDF download | `can_view_delivery_rent` **OR** `can_view_client_ledger` (new OR-semantics in the map) |
| Driver ledger money ops | settle / edit / void / restore driver payment, opening balance | `can_manage_sales` / `can_manage_delivery_persons` |
| Sales drafts & on-demand edit modals | hold / resume / delete direct-sale draft, direct-sale & booking edit modals, pending-bill modals, client modals | `can_manage_sales` / `can_manage_bookings` / `can_manage_pending_bills` / `can_manage_clients` |
| Ledgers & exports | material ledger page, client clearance PDF, supplier/client opening-balance, stock/daily redirect aliases, unpaid-CSV export | `can_view_history` / `can_view_client_ledger` / `can_manage_*` / `can_view_stock` / `can_view_daily` / `can_view_reports` |
| Admin diagnostics | `/api/audit/financial-integrity` | `can_access_settings` |

### 🟡 Mechanics added to close the gaps safely

* **New OR-semantics**: a map entry can be a tuple = *any of* the listed
  permissions (used where two modules legitimately share a page, e.g. the
  driver ledger).
* **Blueprint-prefix fallback**: `accounts` →
  `('can_manage_accounts', 'can_manage_payments')` protects the whole module
  *and any future route added to it* without listing 40 entries.
* **New User columns** `can_manage_accounts`, `can_view_cash_flow` —
  auto-migrated on startup (`_ensure_model_columns`), NULL → OFF default.

---

## 3. New permission flags (added this update)

| Flag | Feature | Default | Notes |
|---|---|---|---|
| `can_manage_accounts` | Financial Accounts module (Khata: accounts, transactions, transfers, receipts/expenditures, reconciliations) | **OFF** | Admin must tick it per user. Account **master** add/edit/delete/toggle stays **admin/root only** even with this flag. |
| `can_view_cash_flow` | Cash Flow module (cash flow, differences, reconciliation detail, current-payables API) | **OFF** | Admin must tick it per user. |

Legacy behaviour preserved: **payment managers** (`can_manage_payments`) can
still enter the Accounts module and transact there (that rule was encoded in
`tests/test_accounts_integrity_upgrade.py` and kept).

---

## 4. FINAL STATE — module × permission matrix (what we have, and who can use it)

`Admin` / `Root` always has **ALL**. Everything else:

| Module / Feature | Permission flag | Default for new users |
|---|---|---|
| Dashboard | `can_view_dashboard` | ON |
| GRN (Receiving) | `can_manage_grn` | ON |
| Stock Summary | `can_view_stock` | ON |
| Daily Breakdown (dispatch) | `can_view_daily` | ON |
| History & Bills | `can_view_history` | ON |
| Material Ledger | `can_view_history` | ON |
| Bookings (+ edit modal, cancel/revert) | `can_manage_bookings` | ON |
| Sales: direct sale, returns, void/unvoid, delete bill, hold/resume draft | `can_manage_sales` | ON |
| Payments (+ driver settle/pay) | `can_manage_payments` | ON |
| Pending Bills (+ modals) | `can_manage_pending_bills` | ON |
| Delivery Rent + Driver Ledger (view/PDF/API) | `can_view_delivery_rent` **or** `can_view_client_ledger` | ON (both default ON) |
| Driver payments (edit/void/restore) | `can_manage_sales` | ON |
| Driver opening balance | `can_manage_delivery_persons` | OFF |
| Client Ledger, clearance PDF, client modals view | `can_view_client_ledger` | ON |
| Client add/edit/delete/transfer, opening balance | `can_manage_clients` | OFF |
| Supplier Ledger + downloads + vouchers | `can_view_supplier_ledger` | ON |
| Supplier add/edit/delete + supplier payments, opening balance | `can_manage_suppliers` | OFF |
| Decision Ledger | `can_view_decision_ledger` | ON |
| Reports: profit, unpaid transactions, mixed, financial details, CSV export | `can_view_reports` | ON |
| **Cash Flow + differences/reconciliation** 🆕 | **`can_view_cash_flow`** | **OFF** |
| Notifications / reminders | `can_manage_notifications` | ON |
| Materials + categories (all ops) | `can_manage_materials` | OFF |
| Delivery persons master | `can_manage_delivery_persons` | OFF |
| Import/Export center (all pages + async jobs + full-raw + transfer + app upgrade history) | `can_import_export` | OFF |
| **Financial Accounts (Khata module)** 🆕 | **`can_manage_accounts`** (or legacy `can_manage_payments` for transacting) | **OFF** |
| Settings page, activity log, void audit, restore, password change, sessions, financial-integrity API | `can_access_settings` | OFF |
| Admin panel `/admin/*`, user management, data wipe, rebuild/fix tools, debug | **role = admin/root** (not a flag) | — |
| Root recovery / backup settings / tenants | **root only** (disabled in single-store) | — |

The same table is now rendered live in **Settings → User Permissions →
Feature Access Matrix** (features × every user, with ALL/✓/·), so you can
always see *on what features we gave access to whom* without reading code.

---

## 5. Intentionally NOT flagged (login-only) — reviewed and accepted

| Endpoint | Why login-only is acceptable |
|---|---|
| `/api/clients/search`, `/api/suppliers/search`, `/api/client_next_code`, `/api/client_booking_status/…`, `/api/client_financial_summary/…`, `/api/last_sold_price`, `/api/check_bill/…` | Cross-module lookup helpers used by *several* already-protected pages (sales, payments, pending bills, dashboard, tracking). Gating one API with one flag would break pages for users holding only part of the permissions. |
| `/api/ui/theme` | Personal UI preference. |
| `/uploads/<file>` | Serves the user's own uploaded files (login required). |
| `/delete_all_data`, `/generate_dummy_data` | Removed legacy features — no-op stubs that only flash and redirect. |
| `/module_name/*` (`template` blueprint) | Development scaffold for building future modules — not a live feature. |

---

## 6. Settings screen changes (what you asked to update)

1. **Badges** per user now include `Financial Accounts` and `Cash Flow`.
2. **Add / Edit user modals** — checkboxes are grouped by module
   (Operations / Sales & Billing / Ledgers & Reports / Masters / Finance &
   System) and driven by one `PERMISSION_GROUPS` list, so the UI can never
   drift from the real permissions again.
3. **New Feature Access Matrix** in Settings → User Permissions — every
   feature × every user at a glance (Admin shows ALL).
4. Saving a user now persists the two new flags; un-ticked boxes are stored
   as explicit `False` (not NULL), so the OFF-by-default rule holds.

## 7. Guard rails (tests)

`tests/test_role_permissions.py` (13 tests) locks in:

* no stale/unknown permission references; UI groups == editable fields
* Cash Flow blocked → granted round-trip
* Accounts module blocked → granted round-trip; payment-manager legacy entry kept
* import/export, supplier downloads, driver-ledger OR semantics, sales
  drafts/modals all gated
* Settings page renders the matrix + both new checkboxes
* edit-user POST persists the new flags

Full suite: **192 passed**.

## 8. What you should do now (admin checklist)

1. Open **Settings → User Permissions**.
2. For each staff account, tick **Financial Accounts** and/or **Cash Flow**
   (both start OFF — nobody has them yet).
3. Check the **Feature Access Matrix** to confirm every user has exactly the
   features intended.
4. Re-run the audit any time new features are added:
   `.venv/bin/python tools/audit_permissions.py`
