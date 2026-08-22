# v4.4 Bundle

Everything from the v3 → v4.4 schema redesign, migrated database, and full design conversation. Nothing here is wired into the app yet — this folder is a staging area for you to review and decide when to switch over.

## Files

| File | What it is |
|---|---|
| `SCHEMA_v4_4.sql` | Final production schema — 63 tables, 19 views, 52 triggers, 111 indexes. Loads cleanly into SQLite. |
| `SCHEMA_v4_4_AUDIT.md` | Pre-migration audit report — FK integrity, money precision, trigger coverage, deletion safety, seed data verification. |
| `MIGRATION_v44_from_live.py` | Re-runnable migration script. Reads `../instance/ahmed_cement.db` (live, read-only) and writes a fresh v4.4 DB. |
| `MIGRATION_v44_REPORT.md` | Human-readable migration report: row counts per table, mapping decisions, dropped items, next steps. |
| `MIGRATION_v44_REPORT.json` | Machine-readable version of the same report. |
| `ahmed_cement_v44.db` | The migrated v4.4 database. 3.7 MB, 63 tables populated with your live data. |
| `CHAT_LOG_FULL.md` | Full verbatim transcript of the v3 → v4.4 design conversation, all questions asked and decisions made. |

## Live DB status

`../instance/ahmed_cement.db` — **untouched.** The app is still running against it.

## What the v4.4 DB contains (from live data)

- 7 users (Admin/Cashier roles preserved)
- 4 built-in roles + 68 granular permissions + 12 wipe scopes (seeded)
- 396 clients (313 live + 83 orphan placeholders for legacy sales/bookings)
- 6 suppliers, 66 materials, 14 delivery persons, 12 accounts
- 43 categories (consolidated from 5 live category tables)
- 2,459 sales + 4,508 sale items + 2,710 driver splits
- 399 bookings + 908 items + 1,075 allocations
- 48 purchases + 48 purchase items + 48 FIFO stock batches
- 716 payments (unified from 4 live payment tables)
- 382 waive-offs, 972 delivery rents, 102 returns
- 2,271 invoices, 1,565 pending bills
- 902 audit log rows preserved

Zero FK integrity errors. 6 booking allocations refused (over-allocated in live — legitimate bad data that v4.4 CHECK correctly rejects).

## When you're ready to switch

The Flask app in `../models/` and `../blueprints/` was written for the live schema. Switching to v4.4 requires either:

1. **Rewrite ORM models** to match v4.4 tables/columns (proper path, 3–5 days).
2. **Build a compatibility view layer** — SQL views on v4.4 that expose old table names to old code (quicker, harder to maintain).

Ping me when you decide and I'll build it.
