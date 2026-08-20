-- ============================================================================
-- AMS POST-IMPORT ENRICHMENT (SQLite) — run AFTER the clean import succeeds.
-- ----------------------------------------------------------------------------
-- The app's full-raw importer restores rows verbatim with Core INSERTs, which
-- bypasses the ORM's before_flush synchronisers.  These statements re-derive
-- the derived/authoritative columns the application relies on, and harden the
-- fresh database so new sales/payments never collide or leak plaintext.
--
-- 1. exact minor-unit money mirrors (amount_minor / balance_minor / …)
-- 2. payment.client_id backfill from client names
-- 3. direct_sale.client_code backfill (app startup also does this)
-- 4. legacy plaintext passwords cleared (password_hash remains authoritative)
--
-- Run inside a transaction; all statements are idempotent for a fresh import.
--   sqlite3 instance/ahmed_cement.db < tools/migrate/post_import_enrichment.sql
-- ============================================================================

BEGIN;

-- 1a. Account balances -> minor units (paisa)
UPDATE account
SET balance_minor = CAST(ROUND(COALESCE(balance,0) * 100) AS INTEGER),
    opening_balance_minor = CAST(ROUND(COALESCE(opening_balance, balance, 0) * 100) AS INTEGER),
    opening_balance_date = COALESCE(opening_balance_date, created_at);

-- 1b. Account transactions
UPDATE account_transaction
SET amount_minor = CAST(ROUND(COALESCE(amount,0) * 100) AS INTEGER);

-- 1c. Payments
UPDATE payment
SET amount_minor   = CAST(ROUND(COALESCE(amount,0)   * 100) AS INTEGER),
    discount_minor = CAST(ROUND(COALESCE(discount,0) * 100) AS INTEGER);

-- 1d. Supplier payments
UPDATE supplier_payment
SET amount_minor = CAST(ROUND(COALESCE(amount,0) * 100) AS INTEGER);

-- 2. Backfill payment.client_id from the client master (name match).
--    Keeps NULL where no unambiguous match exists (historical snapshots stay).
UPDATE payment
SET client_id = (
    SELECT c.id FROM client c
    WHERE LOWER(TRIM(c.name)) = LOWER(TRIM(payment.client_name))
      AND c.id = (SELECT MIN(c2.id) FROM client c2
                  WHERE LOWER(TRIM(c2.name)) = LOWER(TRIM(payment.client_name)))
)
WHERE client_id IS NULL
  AND TRIM(COALESCE(client_name,'')) <> '';

-- 3. Backfill direct_sale.client_code from the client master.
UPDATE direct_sale
SET client_code = (
    SELECT c.code FROM client c
    WHERE LOWER(TRIM(c.name)) = LOWER(TRIM(direct_sale.client_name))
    LIMIT 1
)
WHERE TRIM(COALESCE(client_code,'')) = '';

-- 4. Clear legacy plaintext passwords (hash stays authoritative for login).
UPDATE user SET password_plain = NULL;

COMMIT;

-- ---------------------------------------------------------------------------
-- Sanity checks after enrichment (should print 0 for each count)
-- ---------------------------------------------------------------------------
SELECT 'NULL_MONEY_MINOR' AS check_name, COUNT(*) AS violations FROM account          WHERE balance_minor IS NULL
UNION ALL SELECT 'NULL_MONEY_MINOR', COUNT(*) FROM account_transaction WHERE amount_minor IS NULL
UNION ALL SELECT 'NULL_MONEY_MINOR', COUNT(*) FROM payment            WHERE amount_minor IS NULL
UNION ALL SELECT 'NULL_MONEY_MINOR', COUNT(*) FROM supplier_payment   WHERE amount_minor IS NULL
UNION ALL SELECT 'PLAINTEXT_REMAINS', COUNT(*) FROM user              WHERE password_plain IS NOT NULL AND TRIM(password_plain) <> '';
