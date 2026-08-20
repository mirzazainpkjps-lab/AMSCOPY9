-- ============================================================================
-- AMS POST-MIGRATION AUDIT QUERIES (SQLite)
-- ----------------------------------------------------------------------------
-- Run these against the NEW application database after the cleaned export has
-- been imported (Import & Export -> Full Raw Import).  Every query returns the
-- rows that VIOLATE the migration contract.  A healthy migrated database
-- returns ZERO rows for every "LEAK / ORPHAN / MISMATCH" query below.
--
-- Expected-values query 10 (money totals) returns the single "clean" total for
-- each ledger and should match the values printed by 03_verify_clean_export.py.
--
-- Usage:
--   sqlite3 instance/ahmed_cement.db < tools/migrate/post_import_audit.sql
-- or:
--   python tools/migrate/04_run_post_import_audit.py --db instance/ahmed_cement.db
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. VOIDED-ROW LEAK  (must be empty)
--    Any row with is_void != 0 in the migrated DB means a voided record leaked.
-- ---------------------------------------------------------------------------
SELECT 'VOID LEAK' AS check_name, 'booking' AS tbl, COUNT(*) AS violations FROM booking        WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'pending_bill', COUNT(*) FROM pending_bill       WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'account_transaction', COUNT(*) FROM account_transaction WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'direct_sale', COUNT(*) FROM direct_sale        WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'entry', COUNT(*) FROM entry                    WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'grn', COUNT(*) FROM grn                        WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'payment', COUNT(*) FROM payment                WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'supplier_payment', COUNT(*) FROM supplier_payment WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'delivery_rent', COUNT(*) FROM delivery_rent    WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'material_return', COUNT(*) FROM material_return WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'sale_delivery_persons', COUNT(*) FROM sale_delivery_persons WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'waive_off', COUNT(*) FROM waive_off            WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'grn_item', COUNT(*) FROM grn_item              WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'booking_allocation', COUNT(*) FROM booking_allocation WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'delivery_person_payment', COUNT(*) FROM delivery_person_payment WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'fbm_cash_drawer_entry', COUNT(*) FROM fbm_cash_drawer_entry WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'invoice', COUNT(*) FROM invoice                WHERE COALESCE(is_void,0) != 0
UNION ALL SELECT 'VOID LEAK', 'fbm_rental_item', COUNT(*) FROM fbm_rental_item WHERE COALESCE(is_void,0) != 0;

-- ---------------------------------------------------------------------------
-- 2. CANCELLED-ENTRY LEAK  (must be empty)
--    Cancelled entries are excluded even when is_void = 0.
-- ---------------------------------------------------------------------------
SELECT 'CANCEL LEAK' AS check_name, 'entry' AS tbl, COUNT(*) AS violations
FROM entry
WHERE COALESCE(is_void,0) = 0
  AND ( UPPER(COALESCE(type,'')) = 'CANCEL'
     OR UPPER(COALESCE(transaction_category,'')) = 'CANCEL' );

-- ---------------------------------------------------------------------------
-- 3. ORPHANED FOREIGN KEYS  (must be empty)
--    Every child row must reference an existing parent row.
-- ---------------------------------------------------------------------------
SELECT 'FK ORPHAN' AS check_name, 'booking_item.booking_id' AS ref, COUNT(*) AS violations
FROM booking_item b WHERE NOT EXISTS (SELECT 1 FROM booking p WHERE p.id = b.booking_id)
UNION ALL SELECT 'FK ORPHAN', 'direct_sale_item.sale_id', COUNT(*) FROM direct_sale_item d
    WHERE NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = d.sale_id)
UNION ALL SELECT 'FK ORPHAN', 'booking_allocation.sale_id', COUNT(*) FROM booking_allocation b
    WHERE NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = b.sale_id)
UNION ALL SELECT 'FK ORPHAN', 'booking_allocation.sale_item_id', COUNT(*) FROM booking_allocation b
    WHERE NOT EXISTS (SELECT 1 FROM direct_sale_item p WHERE p.id = b.sale_item_id)
UNION ALL SELECT 'FK ORPHAN', 'booking_allocation.booking_item_id', COUNT(*) FROM booking_allocation b
    WHERE NOT EXISTS (SELECT 1 FROM booking_item p WHERE p.id = b.booking_item_id)
UNION ALL SELECT 'FK ORPHAN', 'entry.source_id (direct_sale)', COUNT(*) FROM entry e
    WHERE e.source_table = 'direct_sale' AND e.source_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = e.source_id)
UNION ALL SELECT 'FK ORPHAN', 'entry.invoice_id', COUNT(*) FROM entry e
    WHERE e.invoice_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM invoice p WHERE p.id = e.invoice_id)
UNION ALL SELECT 'FK ORPHAN', 'pending_bill.source_id (direct_sale)', COUNT(*) FROM pending_bill pb
    WHERE pb.source_table = 'direct_sale' AND pb.source_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = pb.source_id)
UNION ALL SELECT 'FK ORPHAN', 'pending_bill.source_id (booking)', COUNT(*) FROM pending_bill pb
    WHERE pb.source_table = 'booking' AND pb.source_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM booking p WHERE p.id = pb.source_id)
UNION ALL SELECT 'FK ORPHAN', 'delivery_rent.sale_id', COUNT(*) FROM delivery_rent d
    WHERE d.sale_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = d.sale_id)
UNION ALL SELECT 'FK ORPHAN', 'sale_delivery_persons.sale_id', COUNT(*) FROM sale_delivery_persons s
    WHERE NOT EXISTS (SELECT 1 FROM direct_sale p WHERE p.id = s.sale_id)
UNION ALL SELECT 'FK ORPHAN', 'sale_delivery_persons.delivery_person_id', COUNT(*) FROM sale_delivery_persons s
    WHERE NOT EXISTS (SELECT 1 FROM delivery_person p WHERE p.id = s.delivery_person_id)
UNION ALL SELECT 'FK ORPHAN', 'waive_off.payment_id', COUNT(*) FROM waive_off w
    WHERE w.payment_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.id = w.payment_id)
UNION ALL SELECT 'FK ORPHAN', 'material_return.payment_id', COUNT(*) FROM material_return m
    WHERE m.payment_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM payment p WHERE p.id = m.payment_id)
UNION ALL SELECT 'FK ORPHAN', 'material_return_item.material_return_id', COUNT(*) FROM material_return_item m
    WHERE NOT EXISTS (SELECT 1 FROM material_return p WHERE p.id = m.material_return_id)
UNION ALL SELECT 'FK ORPHAN', 'grn_item.grn_id', COUNT(*) FROM grn_item g
    WHERE NOT EXISTS (SELECT 1 FROM grn p WHERE p.id = g.grn_id)
UNION ALL SELECT 'FK ORPHAN', 'direct_sale_item.grn_item_id', COUNT(*) FROM direct_sale_item d
    WHERE d.grn_item_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM grn_item p WHERE p.id = d.grn_item_id)
UNION ALL SELECT 'FK ORPHAN', 'follow_up_reminder.pending_bill_id', COUNT(*) FROM follow_up_reminder f
    WHERE NOT EXISTS (SELECT 1 FROM pending_bill p WHERE p.id = f.pending_bill_id)
UNION ALL SELECT 'FK ORPHAN', 'follow_up_contact.pending_bill_id', COUNT(*) FROM follow_up_contact f
    WHERE NOT EXISTS (SELECT 1 FROM pending_bill p WHERE p.id = f.pending_bill_id)
UNION ALL SELECT 'FK ORPHAN', 'follow_up_contact.reminder_id', COUNT(*) FROM follow_up_contact f
    WHERE f.reminder_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM follow_up_reminder p WHERE p.id = f.reminder_id)
UNION ALL SELECT 'FK ORPHAN', 'direct_sale.invoice_id', COUNT(*) FROM direct_sale d
    WHERE d.invoice_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM invoice p WHERE p.id = d.invoice_id)
UNION ALL SELECT 'FK ORPHAN', 'account_transaction.from_account_id', COUNT(*) FROM account_transaction a
    WHERE a.from_account_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM account p WHERE p.id = a.from_account_id)
UNION ALL SELECT 'FK ORPHAN', 'account_transaction.to_account_id', COUNT(*) FROM account_transaction a
    WHERE a.to_account_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM account p WHERE p.id = a.to_account_id)
UNION ALL SELECT 'FK ORPHAN', 'payment.payment_account_id', COUNT(*) FROM payment p
    WHERE p.payment_account_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM account a WHERE a.id = p.payment_account_id)
UNION ALL SELECT 'FK ORPHAN', 'supplier_payment.supplier_id', COUNT(*) FROM supplier_payment sp
    WHERE NOT EXISTS (SELECT 1 FROM supplier p WHERE p.id = sp.supplier_id)
UNION ALL SELECT 'FK ORPHAN', 'supplier_payment.payment_account_id', COUNT(*) FROM supplier_payment sp
    WHERE sp.payment_account_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM account a WHERE a.id = sp.payment_account_id)
UNION ALL SELECT 'FK ORPHAN', 'grn.supplier_id', COUNT(*) FROM grn g
    WHERE g.supplier_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM supplier p WHERE p.id = g.supplier_id);

-- ---------------------------------------------------------------------------
-- 4. ACCOUNT LEDGER IDENTITY  (must be empty)
--    stored account.balance must equal the non-void account_transaction net.
-- ---------------------------------------------------------------------------
SELECT * FROM (
    SELECT 'LEDGER ACCOUNT' AS check_name, a.name AS ref,
           ROUND(ABS(a.balance - (
               SELECT COALESCE(SUM(CASE WHEN t.to_account_id = a.id THEN t.amount ELSE 0 END),0)
                    - COALESCE(SUM(CASE WHEN t.from_account_id = a.id THEN t.amount ELSE 0 END),0)
               FROM account_transaction t WHERE COALESCE(t.is_void,0) = 0
           )), 2) AS violations
    FROM account a
) WHERE violations > 0.01;

-- ---------------------------------------------------------------------------
-- 5. MATERIAL / STOCK LEDGER IDENTITY  (must be empty)
--    stored material.total must equal non-void entry IN minus OUT net.
-- ---------------------------------------------------------------------------
SELECT * FROM (
    SELECT 'LEDGER MATERIAL' AS check_name, m.name AS ref,
           ROUND(ABS(m.total - (
               SELECT COALESCE(SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN e.qty
                                        WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -e.qty ELSE 0 END),0)
               FROM entry e WHERE COALESCE(e.is_void,0)=0 AND TRIM(COALESCE(e.material,'')) = TRIM(m.name)
           )), 2) AS violations
    FROM material m
) WHERE violations > 0.01;

-- ---------------------------------------------------------------------------
-- 6. CLIENT LEDGER SUMMARY  (informational)
--    outstanding balance per client using the app's own identity:
--    opening + booking.amount + direct_sale.amount - booking.paid - ds.paid
--    - payments(>=0) + payments(<0) - booking.discount - ds.discount - waive_off
-- ---------------------------------------------------------------------------
SELECT 'CLIENT LEDGER' AS check_name, c.id, c.name AS client, c.opening_balance,
       COALESCE((SELECT SUM(b.amount) FROM booking b WHERE COALESCE(b.is_void,0)=0
                  AND LOWER(TRIM(b.client_name)) = LOWER(TRIM(c.name))),0) AS booking_debit,
       COALESCE((SELECT SUM(ds.amount) FROM direct_sale ds WHERE COALESCE(ds.is_void,0)=0
                  AND LOWER(TRIM(ds.client_name)) = LOWER(TRIM(c.name))),0) AS sale_debit,
       COALESCE((SELECT SUM(b.paid_amount) FROM booking b WHERE COALESCE(b.is_void,0)=0
                  AND LOWER(TRIM(b.client_name)) = LOWER(TRIM(c.name))),0) AS booking_credit,
       COALESCE((SELECT SUM(ds.paid_amount) FROM direct_sale ds WHERE COALESCE(ds.is_void,0)=0
                  AND LOWER(TRIM(ds.client_name)) = LOWER(TRIM(c.name))),0) AS sale_credit,
       COALESCE((SELECT SUM(p.amount) FROM payment p WHERE COALESCE(p.is_void,0)=0 AND p.amount >= 0
                  AND (p.client_id = c.id OR (p.client_id IS NULL AND LOWER(TRIM(p.client_name)) = LOWER(TRIM(c.name))))),0) AS payment_credit,
       COALESCE((SELECT SUM(-p.amount) FROM payment p WHERE COALESCE(p.is_void,0)=0 AND p.amount < 0
                  AND (p.client_id = c.id OR (p.client_id IS NULL AND LOWER(TRIM(p.client_name)) = LOWER(TRIM(c.name))))),0) AS payment_debit,
       COALESCE((SELECT SUM(w.amount) FROM waive_off w WHERE COALESCE(w.is_void,0)=0
                  AND LOWER(TRIM(w.client_name)) = LOWER(TRIM(c.name))),0) AS waive_off_total
FROM client c
ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 7. SEQUENCE SAFETY  (rows show counters that would collide with data)
--    New records must never reuse a bill number that already exists.
-- ---------------------------------------------------------------------------
WITH seqs AS (
    SELECT 'BK' AS ns, MAX(seq) AS mx FROM (
        SELECT CAST(REPLACE(auto_bill_no,'SB-BK-','') AS INTEGER) AS seq
        FROM booking WHERE auto_bill_no LIKE 'SB-BK-%'
        UNION SELECT CAST(REPLACE(bill_no,'SB-BK-','') AS INTEGER) FROM pending_bill
              WHERE bill_no LIKE 'SB-BK-%')
    UNION ALL SELECT 'SL', MAX(seq) FROM (
        SELECT CAST(REPLACE(auto_bill_no,'SB-SL-','') AS INTEGER) AS seq
        FROM direct_sale WHERE auto_bill_no LIKE 'SB-SL-%')
    UNION ALL SELECT 'CP', MAX(seq) FROM (
        SELECT CAST(REPLACE(auto_bill_no,'SB-CP-','') AS INTEGER) AS seq
        FROM payment WHERE auto_bill_no LIKE 'SB-CP-%')
    UNION ALL SELECT 'RTN', MAX(seq) FROM (
        SELECT CAST(REPLACE(auto_bill_no,'SB-RTN-','') AS INTEGER) AS seq
        FROM material_return WHERE auto_bill_no LIKE 'SB-RTN-%')
    UNION ALL SELECT 'GRN', MAX(seq) FROM (
        SELECT CAST(REPLACE(auto_bill_no,'SB-GRN-','') AS INTEGER) AS seq
        FROM grn WHERE auto_bill_no LIKE 'SB-GRN-%')
)
SELECT 'SEQ CHECK' AS check_name, s.ns AS ref, bc.count AS counter,
       s.mx AS max_sequence_in_data,
       (bc.count <= s.mx) AS violations
FROM seqs s JOIN bill_counter bc ON bc.namespace = s.ns
WHERE bc.count <= s.mx;

-- ---------------------------------------------------------------------------
-- 8. DUPLICATE NATURAL KEYS  (must be empty)
-- ---------------------------------------------------------------------------
SELECT 'DUP KEY' AS check_name, 'client.code' AS ref, COUNT(*) AS violations FROM (
    SELECT code FROM client GROUP BY UPPER(TRIM(code)) HAVING COUNT(*) > 1)
UNION ALL SELECT 'DUP KEY', 'material.name', COUNT(*) FROM (
    SELECT name FROM material GROUP BY UPPER(TRIM(name)) HAVING COUNT(*) > 1)
UNION ALL SELECT 'DUP KEY', 'invoice.invoice_no', COUNT(*) FROM (
    SELECT invoice_no FROM invoice GROUP BY UPPER(TRIM(invoice_no)) HAVING COUNT(*) > 1)
UNION ALL SELECT 'DUP KEY', 'delivery_person.name', COUNT(*) FROM (
    SELECT name FROM delivery_person GROUP BY UPPER(TRIM(name)) HAVING COUNT(*) > 1)
UNION ALL SELECT 'DUP KEY', 'supplier.name', COUNT(*) FROM (
    SELECT name FROM supplier GROUP BY UPPER(TRIM(name)) HAVING COUNT(*) > 1)
UNION ALL SELECT 'DUP KEY', 'staff_email.email', COUNT(*) FROM (
    SELECT email FROM staff_email GROUP BY UPPER(TRIM(email)) HAVING COUNT(*) > 1);

-- ---------------------------------------------------------------------------
-- 9. ORPHANED / INACTIVE CUSTOMERS & INVENTORY (informational)
--    Master rows with is_active=0 are preserved for FK integrity.  The counts
--    below are expected (legacy: 3 inactive clients, 1 inactive material) and
--    should simply be reviewed by the business.
-- ---------------------------------------------------------------------------
SELECT 'INACTIVE MASTER' AS check_name, 'client' AS tbl, COUNT(*) AS violations
FROM client WHERE COALESCE(is_active,1) = 0
UNION ALL SELECT 'INACTIVE MASTER', 'material', COUNT(*) FROM material WHERE COALESCE(is_active,1) = 0;

-- ---------------------------------------------------------------------------
-- 10. EXPECTED MONEY TOTALS (should equal 03_verify_clean_export.py output)
--     All sums below exclude voided rows.  These are the ledger totals the
--     business must confirm match the cleaned legacy dataset.
-- ---------------------------------------------------------------------------
SELECT 'TOTAL' AS check_name, 'direct_sale.amount' AS ref,
       ROUND(COALESCE(SUM(amount),0),2) AS violations FROM direct_sale WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'direct_sale.paid_amount', ROUND(COALESCE(SUM(paid_amount),0),2)
       FROM direct_sale WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'payment.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM payment WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'pending_bill.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM pending_bill WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'invoice.total_amount', ROUND(COALESCE(SUM(total_amount),0),2)
       FROM invoice WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'invoice.balance', ROUND(COALESCE(SUM(balance),0),2)
       FROM invoice WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'account_transaction.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM account_transaction WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'booking.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM booking WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'booking.paid_amount', ROUND(COALESCE(SUM(paid_amount),0),2)
       FROM booking WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'waive_off.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM waive_off WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'material_return.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM material_return WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'supplier_payment.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM supplier_payment WHERE COALESCE(is_void,0)=0
UNION ALL SELECT 'TOTAL', 'delivery_rent.amount', ROUND(COALESCE(SUM(amount),0),2)
       FROM delivery_rent WHERE COALESCE(is_void,0)=0;
