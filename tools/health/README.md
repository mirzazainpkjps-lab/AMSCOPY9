# AMS Preflight — Lightweight Transaction Watch

A tiny, read-only health check that watches for the exact conditions that
**stopped new transactions** on the old app when per-client history grew
(bookings, booked sales, dues, cash payments, material returns, booking
cancellations piling up for the same client).

**Philosophy — "no garbage collection":** this tool never rewrites data, never
batch-cleans, and never grows. It is a *detector with a small rolling log*.
When something shows up, you fix the *specific* thing (usually one row or one
setting) with the tools listed below — not a mass cleanup.

```
tools/health/
  preflight_check.py   ← the watcher (read-only, ~2 s, cron-friendly)
  README.md            ← you are here
```

## Run it

```bash
python tools/health/preflight_check.py                 # human-readable
python tools/health/preflight_check.py --json          # for scripts
python tools/health/preflight_check.py --quiet         # problems only
python tools/health/preflight_check.py --db /path/app.db
```

Exit codes: `0` = no blockers, `1` = blockers found, `2` = error.
History is kept in `instance/health/preflight.log` (last 100 runs by default;
`--keep N` to change; `--no-log` to disable). Nothing else is ever written.

## Schedule

```cron
# every morning before work — quiet, so cron stays silent while healthy
0 8 * * *  cd /home/user/ams99 && .venv/bin/python tools/health/preflight_check.py --quiet >/dev/null 2>&1 \
           || echo "AMS PREFLIGHT BLOCKERS" | mail -s "AMS preflight" ops@example.local

# right after every Full Raw Import / restore
# weekly deep check (the app's own report — still read-only)
15 8 * * 1  cd /home/user/ams99 && .venv/bin/python tools/consistency_report.py >> instance/health/weekly.log 2>&1
```

## Current baseline (migrated DB, 2026-08-17)

| Status | Item | Count | Blocks new transactions? |
|---|---|---|---|
| ✅ fixed | negative stock blocked (settings row was missing) | 49 materials | was YES → now allowed |
| 👁 watch | duplicate client names (ambiguous lookup) | 2 pairs | no |
| 👁 watch | duplicate manual bill numbers across modules | 1,355 | no |
| 👁 watch | orphaned invoices (no linked sale) | 86 | no |
| 👁 watch | bookings with no pending bill | 186 | no |
| 👁 watch | inactive clients / materials (kept for FK integrity) | 3 / 1 | no |

Everything else the tool checks (dangling allocations, cancel leaks, counter
collisions, ledger drift, missing stock entries, missing credit pending bills)
is currently **0**.

## Check meanings & how to fix

### BLOCKERS (exit 1 — act on these)

| id | Meaning | Fix |
|---|---|---|
| `b1` | Materials are negative **and** `allow_global_negative_stock` is off → sale screen rejects those items (`_direct_sales_add_direct_sale.py`). This is the most common "can't make a sale" error. | Settings page → enable **Allow global negative stock** (or tick the per-sale "allow negative" box). One settings row; reversible. |
| `b2` | `booking_allocation` rows whose sale / sale-item / booking-item no longer exists. The legacy export had 129 of these; they were purged pre-import. New ones appear only if a booking or sale is deleted without cleanup. | Delete the orphaned allocation rows (they are derived links, not ledgers). |
| `b3` | Active `entry` rows with `type='CANCEL'` but `is_void=0` (booking cancellations that were never voided). They inflate ledgers. | Set `is_void=1` on those entries after confirming the cancellation is genuine. |
| `b4` | `bill_counter` value ≤ the largest `SB-NS-####` already in the data → the next auto bill would be a **duplicate number**. | Bump the counter to `max(sequence)+1` for that namespace. |
| `b5` | Duplicate client codes (master key). | Rename/merge one of the clients. |
| `b6` | Two active pending bills for the same client+bill. | Void the stale one. |

### WATCH (exit 0 — review periodically)

| id | Meaning | When to care |
|---|---|---|
| `w1` | Same client name on 2 records → `get_client_by_input(name)` is ambiguous; payments/dues can land on the wrong ledger. | Before posting for that client; fix by using codes or renaming one record. |
| `w2` | Manual bill numbers reused across sales/bookings/pending bills (legacy book overlap). | Reports that join on bill number can cross-link; auto bills (`SB-…`) are unaffected. |
| `w3`/`w4` | Sales with no stock entry / credit sales with no pending bill. | Ledger/stock views for those bills; rebuild with `tools/repair_controlled/repair_erp_consistency.py --confirm` if the counts grow. |
| `w5`/`w6` | Invoices without a linked sale / bookings without a pending bill. | Pre-existing legacy conditions (86 / 186); verify before any new posting to those bills. |
| `w7`/`w8` | `account.balance` or `material.total` drifted from the transaction sums. | If it grows, run the app's consistency report, then `repair_erp_consistency.py --confirm`. |
| `w9` | Payments with a name but no `client_id` link. | Old data only; new payments always link. |
| `w10` | Inactive masters preserved. | Expected — they exist so historical FKs stay valid. |

## Repair tools (all require `--confirm`, take a backup first)

```bash
python tools/repair_controlled/repair_erp_consistency.py --confirm          # rebuild derived ledgers
python tools/repair_controlled/repair_direct_sale_duplicates.py --confirm   # void duplicate sale/entry/pending rows
python tools/repair_controlled/repair_exact_bill_duplicates.py --confirm    # remove exact duplicate items
python tools/consistency_report.py                                          # read-only deep check (weekly)
```

## Rules of thumb

1. **Detect first, fix the specific thing.** Never run a bulk rewrite "to be safe".
2. **Keep the log small** — the rolling log caps itself; do not archive more.
3. **Run after every import/restore** — most blockers appear at load time
   (settings row missing, counters stale), not while typing sales.
4. **Back up before any repair** (`repair_*` scripts already do this into
   `instance/reconcile_backups/`).
