# Why schema fails on import AND when you add a material yourself

**Date:** 2026-08-25  
**Branch:** `arena/01a0385d-amscopy9`  
**What was checked:** live code, `instance/logs/errorlog.txt`, the cleaned workbook `instance/migration/ALLEXPORT-CLEAN-17-08-2026.xlsx`, ORM models, import engine, material create path, and a reproduced import.

---

## What you are seeing

Two different writes hit the **same SQLite schema rules** that were tightened recently (foreign keys ON, unique bill-number indexes, CSRF on every POST):

1. **Importing data** (full XLSX / module restore / legacy template)
2. **Typing a new material yourself** on Material Brands

The failure is not “the Excel is corrupt” and not “the material form is empty”. It is the **database refusing a row** because the current schema is stricter than the file or the form was built for.

---

## Root causes (in the order they actually fire)

### 1. SQLite foreign keys are now ON (this is the big one)

Every new connection runs `PRAGMA foreign_keys=ON`.

That means:

| Write | What SQLite now rejects |
|---|---|
| Import `material` rows whose `category_id` is not in `material_category` yet | `FOREIGN KEY constraint failed` |
| Overwrite-import that **deletes** `material_category` while `material` rows still exist | same error, whole import aborted |
| Import `direct_sale_item` before `direct_sale` / `grn_item` | same error |
| Add a material with a category id that no longer exists | same error on `POST /add_material` |

The importer used to follow **Excel sheet order**, not parent→child order. A workbook that lists `material` before `material_category` (or a module restore of Materials) therefore dies with a schema/FK error even when the data is valid.

**Fix applied:** import now always inserts in SQLAlchemy FK order (parents first) and deletes in reverse (children first). Overwrite failures that still happen because *other* modules reference the rows get a plain-language message instead of a SQL dump.

### 2. Unique indexes on bill numbers treat blank as a real value

On a **fresh** database the app creates:

```sql
CREATE UNIQUE INDEX uq_direct_sale_auto_bill_no
  ON direct_sale(auto_bill_no)
  WHERE auto_bill_no IS NOT NULL
```

Empty string `''` is NOT NULL. Historical exports often have hundreds of blank auto-bill cells. The first blank row inserts; every later blank row fails unique constraint. Same for booking/payment/GRN/entry.

That is why an import of “your own” file can fail after the first few tables, and why later screens look empty or half-loaded.

**Fix applied:** unique indexes now ignore blank strings (`TRIM(auto_bill_no) <> ''`), matching the existing GRN manual-bill index.

### 3. The current schema is newer than the Aug-17 export

`ALLEXPORT-CLEAN-17-08-2026.xlsx` has **51 sheets**. The live ORM has **67 tables**.

Columns the file does **not** have (left NULL on import — usually OK):

- `direct_sale.idempotency_key`, `idempotency_payload_hash`, `client_code`
- `payment` / `supplier_payment` minor-unit + idempotency columns
- `account` classification columns (`class_category`, channel, linked party, …)
- `grn_item.is_locked`

Sheets the file does **not** have (import warns, does not load):

`accounting_audit_log`, `grn_allocation`, `cash_flow_*`, `account_reconciliation`, `import_job`, `migration_*`, …

This is expected drift, not corruption. Using **Legacy Data Migration** on that full export will also fail, because that path requires the official template headers (`Legacy Reference*`, `Material Name*`, …) — not physical table sheets named `material`.

### 4. Manual “Add Brand” had no safety net

`POST /add_material` did:

```python
db.session.add(new_mat)
db.session.commit()   # no try/except
```

If the FK/unique check fired, Flask returned HTTP 500 (or a JSON CSRF 400 if the page JS did not attach `_csrf_token`). Either way it looks like “saving a material is broken”.

**Fix applied:** the save is wrapped; a failed write rolls back and flashes a clean message. Missing categories are created (`General`) instead of inserting a dangling `category_id`.

### 5. v4.4 SQL file is missing (warning, not the write failure)

Log line:

```
v4.4 schema file not found at .../v44/SCHEMA_v4_4.sql; falling back to the ORM schema bootstrap.
```

The `v44/` directory is not in the repo. Startup **falls back to ORM `db.create_all()`**, which does work. An older 0-byte DB plus this warning produced:

```
DATABASE BOOTSTRAP FAILED — unable to open database file
```

When bootstrap fails, **every** page that touches the database (import, materials, sales) returns HTTP 500. That is the “everything is schema-failing” state from `instance/logs/errorlog.txt` (2026-08-25 04:14).

A missing DB is now a valid first run (`ALLOW_EMPTY_DB=1` by default). After this session the instance DB bootstraps and the cleaned workbook loads (~2,410 sales, 66 materials, 0 FK violations).

### 6. CSRF now gates every POST

Since PRED-005, a material form or import form submitted **without** `_csrf_token` returns:

```
400 Invalid or expired form token. Reload the page and try again.
```

Layout JS injects the token into every mutating form. If JS is blocked, the save never reaches the schema — it dies at the CSRF gate. Reload the page and retry.

---

## What I reproduced

| Step | Result |
|---|---|
| Fresh bootstrap | 67 tables, default Admin, FK=ON, integrity_check=ok |
| `POST` add material `TEST CEMENT XYZ` | **OK** (id=1, code `FBMGEN-000001`) |
| Full-raw import of `ALLEXPORT-CLEAN-17-08-2026.xlsx` | **Data committed** (309 clients, 66 materials, 2,410 sales, 0 FK violations, 0 duplicate auto bills) |
| Same import **outside** an HTTP request | Previously crashed on `session.pop` *after* commit. That crash is fixed. |
| Workbook with `material` sheet **before** `material_category` | Previously `FOREIGN KEY constraint failed`. Now inserts in parent-first order. |

---

## What to do when importing

1. Use **Import & Export → Full XLSX restore**, not Legacy Data Migration, for `ALLEXPORT-*.xlsx`.
2. Prefer **append** if the live DB already has sales that point at GRN lots / materials you are replacing.
3. If overwrite still fails, the message will now say other records still reference those rows — import the parent module too (e.g. Materials + Sales, or a full file).
4. After import, add materials from Material Brands as usual (Auto Code button, or leave code blank).

---

## Files changed in this fix

| File | Change |
|---|---|
| `blueprints/import_export/engine.py` | FK-safe insert/delete order; no crash without HTTP session; clearer overwrite error |
| `blueprints/import_export/_common.py` | Materials module table order is parent then child |
| `app/services/schema.py` | Unique auto-bill indexes ignore blanks; `BigInteger` columns added as BIGINT |
| `app/blueprints/misc/materials.py` | Add-material rolls back and flashes instead of 500 |
| `app/services/legacy_migration.py` | Missing material categories are created instead of inserting a dangling FK |
| `tests/test_schema_import_and_materials.py` | Regression coverage for all three failures |

---

*No production rows were deleted to produce this report. The instance database was empty at the start of the session and was bootstrapped only so the failures could be reproduced.*
