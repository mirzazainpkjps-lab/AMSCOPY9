# Why Data Disappears / Reappears — Investigation Report

**App:** AMS ERP (Ahmed Cement & building-materials management)
**Date:** 2026-08-21
**Database examined:** `instance/ahmed_cement.db` (2,451 sales, 4,641 stock entries, 2,268 invoices, 721 payments)

---

## 1. What this app is

A single-server Flask + SQLAlchemy + SQLite ERP for a cement/building-materials
business:

| Module | Purpose |
|---|---|
| **Direct Sales** | "Saved Sales" — cash/credit/open-khata/booking-delivery/mixed sales, invoices, manual bill numbers |
| **Bookings** | Client bookings + allocations against dispatches |
| **Stock / GRN** | Materials, GRN receiving, material returns, FIFO cost |
| **Ledgers** | Client financial ledgers, financial accounts, payments, waive-offs |
| **Pending Bills** | Unpaid dues per client/bill |
| **Driver Payments / Rentals (FBM)** | Delivery-person rent, rental inventory |
| **Reports / Cash Flow** | Profit reports, cash-flow reconciliation |
| **Import & Export** | Full XLSX export and **full replace import** |
| **Settings / Data Wipe** | Granular wipe of selected datasets |
| **Auto-deploy** | GitHub webhook → `git reset --hard` on PythonAnywhere |

~43,000 lines of Python, 64 database tables, deployed on PythonAnywhere with
auto-deploy from GitHub `main`.

---

## 2. Why data "sometimes disappears and sometimes shows" (root cause)

### ROOT CAUSE #1 — the live database is tracked in Git and every deploy runs
### `git reset --hard` over it  ← THE BIG ONE

`instance/ahmed_cement.db`, **`instance/ahmed_cement.db-wal`**,
**`instance/ahmed_cement.db-shm`**, `instance/secret_key`, logs and the
migration XLSX are all **committed to the repository** (verified with
`git ls-files`).

The GitHub auto-pull webhook (`main.py → /git-auto-pull → deploy()`) runs on
the production server on **every push to `main`**:

```
git fetch --prune origin main
git checkout -B main origin/main
git reset --hard origin/main      ← overwrites instance/ahmed_cement.db*
pip install -r requirements.txt
touch WSGI file (reload)
```

Consequences, which match the reported symptoms exactly:

* Any sale saved in production **after** the last commit is **erased** the
  next time a push happens → "saved sales disappear".
* When a new commit containing a database snapshot is pushed, the server
  jumps forward again → "the data shows up again".
* Workers that were still holding the old database file keep writing to an
  orphaned inode while new requests read the restored file → the same list
  can differ between tabs, between refreshes and after restarts → "sometimes
  shows, sometimes not".
* Because the `-wal` and the main file can come from **different points in
  time**, replaying the committed pair can produce a **Frankenstein database**
  (pages of one table new, pages of another old).

**Proven with the current database:**

* Sales **2530–2541** (2026-08-18, 16:03–16:30, batch of 12) exist as
  `direct_sale` rows **with invoices (2263–2268)**, but have **zero
  `direct_sale_item` rows and zero stock `entry` rows** — in the main file
  *and* in the WAL. That combination is impossible for a normally committed
  transaction; it is only possible if table pages from two different moments
  were merged (WAL/commit race + snapshot overwrite).
* Sale **2255** (`MB NO.11169`, RAJA MUDASIR SB JPS) was **hard-deleted on
  2026-08-19 12:49** by Shujaat Muzaffar (audit_log
  `http.post.sales.delete_transaction … /delete_transaction/DirectSale/2255`),
  yet it **still exists today with all its items and entries** — the delete
  was rolled back by a later snapshot restore. This is the smoking gun for
  "deleted / lost data reappears".
* Material returns 83 and 86 (deleted 2026-08-20 10:26/11:44) are gone, so
  the rollback window that restored 2255 is between **2026-08-19 12:49 and
  2026-08-20 11:44**.

### ROOT CAUSE #2 — SQLite on PythonAnywhere's shared filesystem

From `instance/logs/errorlog.txt` (production):

* `sqlite3.OperationalError: unable to open database file` (2026-08-17) —
  WAL journal mode does not work on PythonAnywhere's network filesystem
  (no POSIX shared memory for the `-shm` file).
* `sqlalchemy.OperationalError: database is locked` **six times**
  (2026-08-19/20), including at `PRAGMA journal_mode=DELETE`.
* `OSError: write error` from the rotating log handler (2026-08-20) —
  the log file/rotation itself failed once (disk pressure or file
  replacement).

The code responds by switching journal mode to `DELETE` (whole-file
lock) and applying `PRAGMA busy_timeout=8000` **once, in a
`before_request` hook** (`app/__init__.py → _sqlite_wal_once`). If that one
shot fails (as the logs show it did), **no busy timeout is applied at all**
and the failure is silently swallowed (`app.config['_sqlite_wal_ready'] =
True` anyway). With several gunicorn workers + the auto-reconcile thread +
the backup threads all writing, plain "database is locked" rollbacks happen:

* a save that hit the lock **rolls back** → the sale never made it to disk,
  even though the user saw the form process;
* or the retry/double-submit creates a **duplicate** (the app has an
  idempotency-key guard for direct sales, which helps, but not all flows
  have it).

### CONTRIBUTING FACTOR #3 — background "auto-reconcile" thread mutates data

`utils/reconciliation.py → run_auto_reconcile` runs in a daemon thread every
10 minutes (started on the first request, `app/hooks.py`) and **writes**:

* renames `DirectSaleItem.product_name` to match entries (can silently change
  what a saved sale shows),
* migrates `entry.bill_no` values (can move a sale's stock entries to a
  different bill reference),
* overwrites `Material.total` from net entry quantities,
* inserts `[RECON:ACCOUNT:…]` adjustment transactions.

It also takes **raw `shutil.copy2` file copies of the live DB** into
`instance/reconcile_backups/` — a plain file copy of a WAL-mode database is
not a consistent backup (the maintenance module already has a correct
`sqlite3` backup API implementation that is not used here).

### CONTRIBUTING FACTOR #4 — full-replace import + wipe workflows

* 2026-08-17: a **full raw import in `replace_tenant_data` mode** deleted
  every row and re-inserted 24,584 rows from an Excel export
  (`instance/import_reports/full_raw_import_report_20260817_140353_480002.*`).
* 2026-08-19 16:38: `Admin` ran **Data Wipe → delete selected data**
  (`/delete_selected_data`), right after a wipe preview.
* `_WIPE_BACKUP_ENABLED = False` and `_AUTO_BACKUP_ENABLED = False`
  (`app/services/constants.py`) — **no wipe safety backup and no automatic
  database backup are produced by the running app**, so after any of these
  operations the only "backup" is whatever database copy happens to be
  committed to GitHub.

Any one of these operations combined with Root Cause #1 can produce
"mixed-time" databases exactly like the one examined.

---

## 3. Data inconsistencies found in the current database

### A. Lost data (irreversible without re-entry)

| # | Finding | Detail |
|---|---|---|
| A1 | **12 phantom sales (2530–2541)** | 2026-08-18 16:03–16:30: headers + invoices exist, **all line items and stock entries are missing** (e.g. `MB NO.11684` SHEHZAD KHAN SB DHINDA, 163,461.40, invoice OPEN). Stock, client ledgers and reports are wrong for these. |
| A2 | **Deleted sale 2255 resurrected** | Hard-deleted 2026-08-19 12:49, still present (duplicate bill `MB NO.11169` with sale 2508). |
| A3 | **4 active `CANCEL` entry rows** | ids 10029, 10030 (MB NO.10659), 10049 (MB NO.11420), 10090 (MB NO.2405) — booking cancellations never voided; they distort stock. |

### B. Financial inconsistencies (receivables wrong)

| # | Finding | Detail |
|---|---|---|
| B1 | **₹1,574,769.05 of dues under-reported** | 25 active pending bills show a smaller unpaid amount than the invoice/sale due (worst: `MB NO.11684` shows 4,494 due vs 163,461 actual; `MB NO.7096` 402,993 vs 555,707). |
| B2 | **Sale 2533 marked PAID** | `MB NO.11673` SHAMRAIZ SB JAGUWAN: pending bill amount 0 + is_paid=1, but the sale/invoice due is 39,635. |
| B3 | 87 orphan invoices | Invoices with no linked sale (legacy + wipe residue). |
| B4 | 190 bookings with no pending bill | Booked dues not tracked. |

### C. Stock inconsistencies

| # | Finding | Detail |
|---|---|---|
| C1 | **51 materials at negative stock** | e.g. `12MM STEEL` −78,752.90, `ISM 12MM STEEL` −61,962.20, `RENT-STEEL` −47,360.25. `allow_global_negative_stock` is OFF (settings row even missing), so the app **rejects new sales of these materials** — the yard may be entering workarounds (wrong material names, manual adjustments). |
| C2 | Stock vs ledger drift | `ISM 12MM STEEL` stored −61,962.20 vs net entries −62,147.00 (184.80 gap = one lost line, matching the CANCEL qty on 08-19), `DG` −20.00, `20MM STEEL` −1.00, `6MM STEEL` −0.30. |
| C3 | 8 entry rows cross-linked to the wrong sale | entries of `MB NO.11169` carry the other sale's `source_id` (2255 ↔ 2508). |

### D. Master-data / process

| # | Finding | Detail |
|---|---|---|
| D1 | **1,405 manual bill numbers used more than once** across sales/bookings/pending bills (legacy register overlap; active duplicate: `MB NO.11169`). |
| D2 | 2 client names shared by multiple records → name-based lookup can post to the wrong ledger. |
| D3 | `settings` table empty (0 rows) — all settings fall back to code defaults. |
| D4 | 3 inactive clients + 1 inactive material kept for FK integrity. |

The repo's own read-only health check
(`python3 tools/health/preflight_check.py`) currently reports **status
BLOCK** with the same two blockers (b1 negative stock, b3 active CANCEL
rows) plus the watch items above.

---

## 4. Security notes (fix these while you are in here)

1. **The live database, secret_key and a 1.7 MB XLSX are committed to
   GitHub** — anyone with read access to the repo has your whole business
   data and your session secret.
2. **The webhook token is committed in `main.py`**
   (`WEBHOOK_TOKEN = "PakistanZindabad1947-2026"`). Anyone with it can
   trigger a full deploy of arbitrary `main` content. The code comment even
   says "Do not use the old token you exposed" — rotate it in GitHub and in
   the file.

---

## 5. What was fixed in this branch

**`main.py` — deployments can no longer overwrite the live database.**
The auto-deploy now:

1. **STEP 0:** copies the whole `instance/` directory (live DB + WAL + SHM +
   snapshot + logs) to `.instance_preserve/` *before* `git fetch/checkout/reset`;
2. runs the code update exactly as before;
3. **finally:** restores every preserved instance file back into place — on
   success *and* after a failed deploy — then releases the lock.

So a push now updates **code only**; the database the yard is writing into
is never rolled back to a committed snapshot. `.instance_preserve/` is in
`.gitignore`.

This is a safe, self-contained change: the database files stay tracked in
Git for now, so this commit alone cannot delete anything on the server.

---

## 6. Recommended follow-ups (in order)

1. **Back up the live DB from PythonAnywhere first** (download
   `instance/ahmed_cement.db` *plus* `-wal`/`-shm`, or use the app's
   Maintenance → Backup which uses the proper SQLite backup API).
2. **After the new deploy code is live**, stop tracking the data in Git:
   `git rm --cached instance/ahmed_cement.db instance/ahmed_cement.db-wal
   instance/ahmed_cement.db-shm instance/secret_key instance/logs/*
   instance/migration/* instance/import_reports/*` and add `instance/` to
   `.gitignore`. (Do this only *after* step 1's deploy is verified, because
   the first reset of an untracked file removes it — the new STEP 0/finally
   restore makes that safe.)
3. **Rotate the webhook token** (GitHub repo → Settings → Webhooks) and the
   `SECRET_KEY`.
4. **Repair the damaged data** (the app ships guarded tools for this; each
   takes its own backup first and requires `--confirm`):
   * Re-enter the 12 phantom sales 2530–2541 from the client's paper bills
     (invoice totals are intact), or hard-delete them and re-save;
   * Run `python tools/repair_controlled/repair_erp_consistency.py --confirm`
     to rebuild pending bills / ledgers / stock from source transactions —
     this corrects the 25 under-reported dues and the B1–B2 gaps;
   * Void the 4 active `CANCEL` entry rows and re-issue the correct
     cancellation; decide on `MB NO.11169` (keep 2255 or 2508, void the
     other).
5. **Enable safety nets:** set `_AUTO_BACKUP_ENABLED`/`BACKUP_EMBEDDED_SCHEDULER`
   and `_WIPE_BACKUP_ENABLED` to `1` (hourly online backup + pre-wipe
   backup + wipe history row), and point backups somewhere off the
   PythonAnywhere home dir if possible.
6. **Reduce SQLite lock pain on PythonAnywhere:** keep `SQLITE_JOURNAL_MODE=DELETE`
   (already forced), but apply `PRAGMA busy_timeout` on every connection via
   the existing `connect` event (not the one-shot `before_request`), and run
   the web app with a **single worker** (gunicorn `--workers 1`) — one
   writer eliminates most "database is locked" rollbacks.
7. **Tame the auto-reconcile thread** (or disable `AUTO_RECONCILE_FIX`):
   it should not rename sale items or rewrite `entry.bill_no` while users
   are working; switch its DB copy to the `sqlite3` backup API.
8. **Stock:** reconcile the negative materials against the physical yard
   (GRN history was rebuilt by the 08-17 import; the large negatives are a
   mix of lost entries and genuine oversells) — see
   `INVENTORY_AUDIT_REPORT.md` for the material-level view.

---

## 7. Evidence index

| Evidence | Location |
|---|---|
| DB/WAL/SHM tracked in git | `git ls-files instance/` |
| Auto-deploy `git reset --hard` | `main.py → deploy()` |
| `database is locked` ×6, `unable to open database file` ×2, log write error | `instance/logs/errorlog.txt` |
| Phantom sales 2530–2541 (headers+invoices, no items/entries) | `direct_sale`, `invoice` 2263–2268, `direct_sale_item`, `entry` |
| Resurrected deleted sale 2255 | `audit_log` 2026-08-19 12:49:28 vs current `direct_sale` |
| 16:38 wipe run | `audit_log` `http.post.misc.delete_selected_data` 2026-08-19 16:38:43 |
| Full replace import 08-17 | `instance/import_reports/full_raw_import_report_20260817_140353_480002.meta.json` |
| Health status BLOCK (2 blockers, 7 watch) | `python3 tools/health/preflight_check.py --json` |
