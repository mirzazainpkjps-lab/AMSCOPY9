"""Comprehensive post-migration audit. Looks for:
  1. NULL/empty values in columns that have a default and the app depends on
  2. Inconsistent booleans / is_void
  3. Cancellation rows that survived the CANCEL filter
  4. Money column issues (NaN, wrong precision)
  5. Return type mismatches
  6. Booking cancellation specifics
  7. FK orphans that survived
  8. Strange source_type/source_id combos
"""
import sqlite3
import sys
from collections import defaultdict

con = sqlite3.connect('instance/ahmed_cement.db')
con.row_factory = sqlite3.Row
cur = con.cursor()

def section(title):
    print()
    print('=' * 80)
    print(f'  {title}')
    print('=' * 80)

def show(rows, n=15):
    rows = list(rows)
    if not rows:
        print('   (none)')
        return
    cols = rows[0].keys()
    widths = {c: max(len(c), max(len(str(r[c] or '')) for r in rows[:n])) for c in cols}
    print('   ' + ' | '.join(c.ljust(widths[c]) for c in cols))
    for r in rows[:n]:
        print('   ' + ' | '.join(str(r[c] or '').ljust(widths[c]) for c in cols))
    if len(rows) > n:
        print(f'   ... {len(rows) - n} more rows (total {len(rows)})')

# ---------------------------------------------------------------------------
section('1. material_return.return_type NULLs')
# ---------------------------------------------------------------------------
rows = cur.execute("SELECT * FROM material_return WHERE return_type IS NULL OR TRIM(return_type) = '' ORDER BY id")
show(rows, 20)
total = cur.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM material_return WHERE return_type IS NULL OR TRIM(return_type)=''").fetchone()
print(f'   TOTAL: count={total[0]} amount={total[1]}')

# ---------------------------------------------------------------------------
section('2. material_return.return_type vs entry.transaction_category mismatch')
# ---------------------------------------------------------------------------
# A material_return with return_type='booked' should have entries tagged
# transaction_category='Booked Return'. A return_type='normal' should have
# transaction_category='Return'.
sql = """
SELECT mr.id, mr.client_name, mr.return_type AS mr_type,
       mr.manual_bill_no, mr.auto_bill_no,
       e.transaction_category, e.client_category, e.type, e.material, e.qty
FROM material_return mr
LEFT JOIN entry e ON (e.bill_no = mr.manual_bill_no OR e.bill_no = mr.auto_bill_no)
                 AND e.nimbus_no = 'Material Return'
                 AND e.is_void = 0
WHERE mr.is_void = 0
  AND ((mr.return_type = 'booked' AND e.transaction_category != 'Booked Return')
       OR (mr.return_type = 'normal' AND e.transaction_category = 'Booked Return')
       OR (mr.return_type IS NULL))
ORDER BY mr.id, e.id
"""
rows = list(cur.execute(sql))
print(f'   Mismatches: {len(rows)}')
show(rows, 30)

# ---------------------------------------------------------------------------
section('3. Cancelled entries (type=CANCEL) that survived is_void=0')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT id, date, time, type, transaction_category, material, client, client_code,
       bill_no, qty, nimbus_no, is_void
FROM entry
WHERE is_void = 0
  AND (UPPER(COALESCE(type,'')) = 'CANCEL'
    OR UPPER(COALESCE(transaction_category,'')) = 'CANCEL')
"""))
print(f'   Survived cancels: {len(rows)}')
show(rows, 10)

# ---------------------------------------------------------------------------
section('4. Booking / booking_item integrity')
# ---------------------------------------------------------------------------
print('   voided bookings with non-voided items:')
rows = list(cur.execute("""
SELECT b.id, b.manual_bill_no, b.auto_bill_no, b.is_void,
       (SELECT COUNT(*) FROM booking_item bi WHERE bi.booking_id = b.id) AS item_count
FROM booking b
WHERE b.is_void = 1
  AND EXISTS (SELECT 1 FROM booking_item bi WHERE bi.booking_id = b.id)
"""))
print(f'   count={len(rows)}')
show(rows, 10)

print()
print('   booking_item with missing booking (FK orphan):')
rows = list(cur.execute("""
SELECT bi.id, bi.booking_id, bi.material_name, bi.qty
FROM booking_item bi
LEFT JOIN booking b ON b.id = bi.booking_id
WHERE b.id IS NULL
"""))
print(f'   count={len(rows)}')
show(rows, 10)

# ---------------------------------------------------------------------------
section('5. DirectSale / DirectSaleItem integrity')
# ---------------------------------------------------------------------------
print('   direct_sale_item with missing direct_sale:')
rows = list(cur.execute("""
SELECT dsi.id, dsi.sale_id, dsi.product_name, dsi.qty
FROM direct_sale_item dsi
LEFT JOIN direct_sale ds ON ds.id = dsi.sale_id
WHERE ds.id IS NULL
"""))
print(f'   count={len(rows)}')

print()
print('   voided direct_sale with non-voided items:')
rows = list(cur.execute("""
SELECT ds.id, ds.manual_bill_no, ds.auto_bill_no, ds.is_void,
       (SELECT COUNT(*) FROM direct_sale_item dsi WHERE dsi.sale_id = ds.id) AS items
FROM direct_sale ds
WHERE ds.is_void = 1
  AND EXISTS (SELECT 1 FROM direct_sale_item dsi WHERE dsi.sale_id = ds.id)
"""))
print(f'   count={len(rows)}')

# ---------------------------------------------------------------------------
section('6. payment client_id / payment_type / source_type orphans')
# ---------------------------------------------------------------------------
print('   payment with client_id but no client:')
rows = list(cur.execute("""
SELECT p.id, p.client_id, p.client_name, p.amount, p.payment_type
FROM payment p
LEFT JOIN client c ON c.id = p.client_id
WHERE p.client_id IS NOT NULL AND c.id IS NULL
"""))
print(f'   count={len(rows)}')
show(rows, 10)

print()
print('   payment with source_type set but source_id NULL or pointing nowhere:')
rows = list(cur.execute("""
SELECT p.id, p.client_name, p.amount, p.payment_type, p.source_type, p.source_id
FROM payment p
WHERE p.source_type IS NOT NULL
  AND (p.source_id IS NULL
       OR (p.source_type = 'MaterialReturn' AND NOT EXISTS (SELECT 1 FROM material_return mr WHERE mr.id = p.source_id))
       OR (p.source_type = 'DirectSale' AND NOT EXISTS (SELECT 1 FROM direct_sale ds WHERE ds.id = p.source_id))
       OR (p.source_type = 'Booking' AND NOT EXISTS (SELECT 1 FROM booking b WHERE b.id = p.source_id))
      )
"""))
print(f'   count={len(rows)}')
show(rows, 10)

print()
print('   MaterialReturn payments with payment_type != Material Return:')
rows = list(cur.execute("""
SELECT p.id, p.client_name, p.amount, p.payment_type, p.source_type, p.source_id
FROM payment p
WHERE p.source_type = 'MaterialReturn'
  AND p.payment_type != 'Material Return'
"""))
print(f'   count={len(rows)}')

print()
print('   payment_type=Material Return with no MaterialReturn source:')
rows = list(cur.execute("""
SELECT p.id, p.client_name, p.amount, p.payment_type, p.source_type, p.source_id
FROM payment p
WHERE p.payment_type = 'Material Return'
  AND (p.source_type IS NULL OR p.source_type != 'MaterialReturn')
"""))
print(f'   count={len(rows)}')
show(rows, 10)

# ---------------------------------------------------------------------------
section('7. material_return with missing payment')
# ---------------------------------------------------------------------------
print('   material_return with payment_id NULL or pointing nowhere:')
rows = list(cur.execute("""
SELECT mr.id, mr.client_name, mr.amount, mr.payment_id, mr.is_void
FROM material_return mr
LEFT JOIN payment p ON p.id = mr.payment_id
WHERE mr.payment_id IS NULL OR p.id IS NULL
"""))
print(f'   count={len(rows)}')
show(rows, 10)

print()
print('   material_return with voided payment:')
rows = list(cur.execute("""
SELECT mr.id, mr.client_name, mr.amount, mr.payment_id, p.is_void
FROM material_return mr
JOIN payment p ON p.id = mr.payment_id
WHERE mr.is_void = 0 AND p.is_void = 1
"""))
print(f'   count={len(rows)}')

# ---------------------------------------------------------------------------
section('8. Entry points to source that no longer exists')
# ---------------------------------------------------------------------------
print('   entry with source_table set but source_id not found:')
for src_table, label in [('direct_sale', 'direct_sale'), ('booking', 'booking')]:
    rows = list(cur.execute(f"""
    SELECT e.id, e.source_table, e.source_id, e.bill_no, e.material, e.client, e.type
    FROM entry e
    WHERE e.source_table = '{src_table}' AND e.source_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM {src_table} t WHERE t.id = e.source_id)
    """))
    print(f'   -> {label}: count={len(rows)}')
    show(rows, 5)

# ---------------------------------------------------------------------------
section('9. Booking allocations pointing at voided parents')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT ba.id, ba.sale_id, ba.sale_item_id, ba.booking_item_id, ba.qty, ba.is_void,
       ds.is_void AS ds_void, bi.id IS NULL AS bi_missing
FROM booking_allocation ba
LEFT JOIN direct_sale ds ON ds.id = ba.sale_id
LEFT JOIN booking_item bi ON bi.id = ba.booking_item_id
WHERE ba.is_void = 0
  AND (ds.is_void = 1 OR bi.id IS NULL)
"""))
print(f'   count={len(rows)}')
show(rows, 10)

# ---------------------------------------------------------------------------
section('10. voided bookings / sales with non-voided pending_bill')
# ---------------------------------------------------------------------------
for src_table in ['booking', 'direct_sale']:
    rows = list(cur.execute(f"""
    SELECT pb.id, pb.source_table, pb.source_id, pb.bill_no, pb.amount, pb.is_void,
           t.is_void AS src_void
    FROM pending_bill pb
    JOIN {src_table} t ON t.id = pb.source_id
    WHERE pb.source_table = '{src_table}' AND pb.is_void = 0 AND t.is_void = 1
    """))
    print(f'   {src_table} source: count={len(rows)}')
    show(rows, 5)

# ---------------------------------------------------------------------------
section('11. Account / AccountTransaction integrity')
# ---------------------------------------------------------------------------
print('   account_transaction with from_account_id = to_account_id:')
rows = list(cur.execute("""
SELECT id, from_account_id, to_account_id, amount, transaction_type, is_void
FROM account_transaction
WHERE COALESCE(is_void,0) = 0
  AND from_account_id = to_account_id
  AND from_account_id IS NOT NULL
"""))
print(f'   count={len(rows)}')

print()
print('   account balances vs account_transaction derived balance:')
rows = list(cur.execute("""
SELECT a.id, a.name, a.balance, a.balance_minor,
       ROUND(COALESCE((SELECT SUM(CASE WHEN t.to_account_id = a.id THEN t.amount ELSE 0 END)
                          - SUM(CASE WHEN t.from_account_id = a.id THEN t.amount ELSE 0 END)
                       FROM account_transaction t WHERE COALESCE(t.is_void,0)=0), 0), 2) AS derived,
       ROUND(ABS(a.balance - COALESCE((SELECT SUM(CASE WHEN t.to_account_id = a.id THEN t.amount ELSE 0 END)
                                             - SUM(CASE WHEN t.from_account_id = a.id THEN t.amount ELSE 0 END)
                                        FROM account_transaction t WHERE COALESCE(t.is_void,0)=0), 0)), 2) AS diff
FROM account a
"""))
issues = [r for r in rows if r['diff'] > 0.01]
print(f'   accounts out of balance: {len(issues)} / {len(rows)}')
for r in issues:
    print(f"     acct {r['id']:>3} {r['name']:<30} balance={r['balance']:>14}  derived={r['derived']:>14}  diff={r['diff']}")

# ---------------------------------------------------------------------------
section('12. Stock derived vs material.total')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT m.id, m.name, m.total,
       ROUND(COALESCE((SELECT SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN e.qty
                                       WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -e.qty ELSE 0 END)
                       FROM entry e WHERE COALESCE(e.is_void,0)=0
                                     AND TRIM(COALESCE(e.material,'')) = TRIM(m.name)), 0), 2) AS derived,
       ROUND(ABS(COALESCE(m.total,0) - COALESCE((SELECT SUM(CASE WHEN UPPER(COALESCE(e.type,''))='IN' THEN e.qty
                                                                     WHEN UPPER(COALESCE(e.type,''))='OUT' THEN -e.qty ELSE 0 END)
                                                  FROM entry e WHERE COALESCE(e.is_void,0)=0
                                                                    AND TRIM(COALESCE(e.material,'')) = TRIM(m.name)), 0)), 2) AS diff
FROM material m
"""))
issues = [r for r in rows if r['diff'] > 0.01]
print(f'   materials out of balance: {len(issues)} / {len(rows)}')
for r in issues[:20]:
    print(f"     {r['id']:>3} {r['name']:<30} total={r['total']!s:>14}  derived={r['derived']!s:>14}  diff={r['diff']}")

# ---------------------------------------------------------------------------
section('13. material_return.amount vs items total')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT mr.id, mr.client_name, mr.amount, mr.return_type,
       (SELECT ROUND(SUM(qty * (COALESCE(unit_rate,0) + COALESCE(rent_rate,0))), 2)
        FROM material_return_item mri WHERE mri.material_return_id = mr.id) AS items_total,
       ROUND(ABS(mr.amount - COALESCE((SELECT SUM(qty * (COALESCE(unit_rate,0) + COALESCE(rent_rate,0)))
                                      FROM material_return_item mri WHERE mri.material_return_id = mr.id), 0)), 2) AS diff
FROM material_return mr
WHERE mr.is_void = 0
"""))
issues = [r for r in rows if r['diff'] > 0.01]
print(f'   material_return amount != items total: {len(issues)} / {len(rows)}')
for r in issues[:20]:
    print(f"     {r['id']:>3} {r['client_name'][:35]:<35} return_type={str(r['return_type']):<8} amount={r['amount']!s:>10} items_total={r['items_total']!s:>10} diff={r['diff']}")

# ---------------------------------------------------------------------------
section('14. payment.amount vs derived (payment + linked waive) for material returns')
# ---------------------------------------------------------------------------
# For Material Return payments the app ties a 1:1 Payment of the same amount.
# Look for any tied MR payment that doesn't match.
rows = list(cur.execute("""
SELECT mr.id, mr.amount AS mr_amount, mr.return_type, p.amount AS pay_amount, p.is_void AS pay_void, mr.is_void AS mr_void
FROM material_return mr
LEFT JOIN payment p ON p.id = mr.payment_id
WHERE ABS(COALESCE(mr.amount,0) - COALESCE(p.amount,0)) > 0.01
   OR (p.is_void = 1 AND mr.is_void = 0)
"""))
print(f'   mismatch count: {len(rows)}')
show(rows, 15)

# ---------------------------------------------------------------------------
section('15. Booking cancellation cross-check')
# ---------------------------------------------------------------------------
# A booking with is_void=1 might still have CANCEL-type entries that survived.
# Or CANCEL entries might point at non-voided bookings (which is fine if the
# booking was a partial cancel).
print('   CANCEL entries that point at non-voided bookings (these are intentional cancels):')
rows = list(cur.execute("""
SELECT e.id, e.bill_no, e.material, e.qty, e.note, b.is_void AS booking_void
FROM entry e
LEFT JOIN booking b ON b.manual_bill_no = e.bill_no OR b.auto_bill_no = e.bill_no
WHERE e.is_void = 0
  AND (UPPER(COALESCE(e.type,'')) = 'CANCEL' OR UPPER(COALESCE(e.transaction_category,'')) = 'CANCEL')
"""))
print(f'   count={len(rows)}')
show(rows, 10)

print()
print('   Legacy: any non-cancel entry with transaction_category containing cancel?')
rows = list(cur.execute("""
SELECT e.id, e.type, e.transaction_category, e.material, e.bill_no
FROM entry e
WHERE e.is_void = 0
  AND e.type != 'CANCEL'
  AND LOWER(COALESCE(e.transaction_category,'')) LIKE '%cancel%'
"""))
print(f'   count={len(rows)}')
show(rows, 5)

# ---------------------------------------------------------------------------
section('16. Bookings with no items (orphan bookings)')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT b.id, b.manual_bill_no, b.auto_bill_no, b.amount, b.is_void
FROM booking b
WHERE NOT EXISTS (SELECT 1 FROM booking_item bi WHERE bi.booking_id = b.id)
"""))
print(f'   count={len(rows)}')
show(rows, 5)

# ---------------------------------------------------------------------------
section('17. Direct sales with no items')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT ds.id, ds.manual_bill_no, ds.auto_bill_no, ds.amount, ds.is_void
FROM direct_sale ds
WHERE NOT EXISTS (SELECT 1 FROM direct_sale_item dsi WHERE dsi.sale_id = ds.id)
"""))
print(f'   count={len(rows)}')
show(rows, 5)

# ---------------------------------------------------------------------------
section('18. Booking allocation: sale_item_id / booking_item_id consistency')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT ba.id, ba.sale_id, ba.sale_item_id, ba.booking_item_id, ba.qty,
       dsi.sale_id AS dsi_sale, dsi.product_name
FROM booking_allocation ba
LEFT JOIN direct_sale_item dsi ON dsi.id = ba.sale_item_id
WHERE ba.is_void = 0
  AND dsi.sale_id != ba.sale_id
"""))
print(f'   mismatched sale_item: {len(rows)}')
show(rows, 5)

# ---------------------------------------------------------------------------
section('19. Out entries that violate the booking allocation (over-delivery)')
# ---------------------------------------------------------------------------
# For each booking_item, the allocated qty should not exceed booked qty.
rows = list(cur.execute("""
SELECT bi.id, bi.booking_id, bi.material_name, bi.qty AS booked_qty,
       COALESCE((SELECT SUM(ba.qty) FROM booking_allocation ba
                 WHERE ba.booking_item_id = bi.id AND ba.is_void = 0), 0) AS allocated_qty,
       ROUND(COALESCE((SELECT SUM(ba.qty) FROM booking_allocation ba
                       WHERE ba.booking_item_id = bi.id AND ba.is_void = 0), 0) - bi.qty, 2) AS over
FROM booking_item bi
WHERE COALESCE((SELECT SUM(ba.qty) FROM booking_allocation ba
                WHERE ba.booking_item_id = bi.id AND ba.is_void = 0), 0) > bi.qty + 0.001
"""))
print(f'   booking items over-allocated: {len(rows)}')
show(rows, 10)

# ---------------------------------------------------------------------------
section('20. Money column NaN/Inf check (REAL columns)')
# ---------------------------------------------------------------------------
for tbl, col in [
    ('payment', 'amount'), ('payment', 'discount'),
    ('material_return', 'amount'),
    ('direct_sale', 'amount'), ('direct_sale', 'paid_amount'), ('direct_sale', 'discount'),
    ('booking', 'amount'), ('booking', 'paid_amount'), ('booking', 'discount'),
    ('pending_bill', 'amount'),
    ('account_transaction', 'amount'),
    ('account', 'balance'), ('account', 'opening_balance'),
]:
    rows = list(cur.execute(f"SELECT id, {col} FROM {tbl} WHERE {col} IS NOT NULL AND (typeof({col}) NOT IN ('integer','real') OR {col} != {col})"))
    if rows:
        print(f'   {tbl}.{col}: {len(rows)} suspicious')
        show(rows, 5)
    else:
        print(f'   {tbl}.{col}: ok')

# ---------------------------------------------------------------------------
section('21. Date sanity (date_posted in future / very old)')
# ---------------------------------------------------------------------------
for tbl, col in [('payment','date_posted'), ('material_return','date_posted'),
                 ('booking','date_posted'), ('direct_sale','date_posted')]:
    rows = list(cur.execute(f"SELECT id, {col} FROM {tbl} WHERE {col} > '2027-01-01' OR {col} < '2024-01-01'"))
    if rows:
        print(f'   {tbl}.{col}: {len(rows)} out-of-range')
        show(rows, 5)
    else:
        print(f'   {tbl}.{col}: ok')

# ---------------------------------------------------------------------------
section('22. Manual bill no duplicates across tables')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT bill_no, GROUP_CONCAT(table_name), COUNT(*) AS c FROM (
  SELECT 'booking' AS table_name, manual_bill_no AS bill_no FROM booking WHERE manual_bill_no IS NOT NULL AND manual_bill_no != ''
  UNION ALL
  SELECT 'direct_sale', manual_bill_no FROM direct_sale WHERE manual_bill_no IS NOT NULL AND manual_bill_no != ''
  UNION ALL
  SELECT 'material_return', manual_bill_no FROM material_return WHERE manual_bill_no IS NOT NULL AND manual_bill_no != ''
  UNION ALL
  SELECT 'payment', manual_bill_no FROM payment WHERE manual_bill_no IS NOT NULL AND manual_bill_no != ''
  UNION ALL
  SELECT 'invoice', invoice_no FROM invoice WHERE invoice_no IS NOT NULL AND invoice_no != ''
  UNION ALL
  SELECT 'grn', manual_bill_no FROM grn WHERE manual_bill_no IS NOT NULL AND manual_bill_no != ''
)
WHERE bill_no IS NOT NULL AND TRIM(bill_no) != ''
GROUP BY bill_no
HAVING COUNT(DISTINCT table_name) > 1
ORDER BY c DESC
"""))
print(f'   shared manual bill nos across tables: {len(rows)}')
show(rows, 25)

# ---------------------------------------------------------------------------
section('23. Material return item integrity')
# ---------------------------------------------------------------------------
print('   material_return_item pointing to non-existent material_return:')
rows = list(cur.execute("""
SELECT mri.id, mri.material_return_id, mri.material_name, mri.qty
FROM material_return_item mri
LEFT JOIN material_return mr ON mr.id = mri.material_return_id
WHERE mr.id IS NULL
"""))
print(f'   count={len(rows)}')

print('   material_return_item with negative qty:')
rows = list(cur.execute("SELECT id, material_return_id, material_name, qty FROM material_return_item WHERE qty < 0"))
print(f'   count={len(rows)}')

# ---------------------------------------------------------------------------
section('24. Entries with nimbus_no=Material Return but no linked material_return')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT e.id, e.date, e.bill_no, e.material, e.qty, e.client_code, e.transaction_category
FROM entry e
WHERE e.nimbus_no = 'Material Return'
  AND e.is_void = 0
  AND NOT EXISTS (
    SELECT 1 FROM material_return mr
    WHERE (mr.manual_bill_no = e.bill_no OR mr.auto_bill_no = e.bill_no)
  )
"""))
print(f'   count={len(rows)}')
show(rows, 10)

# ---------------------------------------------------------------------------
section('25. MaterialReturn with amount != sum of payments (including 0-amount ones)')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT mr.id, mr.client_name, mr.amount, mr.return_type,
       COALESCE(p.amount, 0) AS payment_amount
FROM material_return mr
LEFT JOIN payment p ON p.id = mr.payment_id
WHERE mr.is_void = 0 AND p.is_void = 0
  AND ABS(COALESCE(mr.amount,0) - COALESCE(p.amount,0)) > 0.01
"""))
print(f'   count={len(rows)}')
show(rows, 15)

# ---------------------------------------------------------------------------
section('26. Booking amount vs items total')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT b.id, b.manual_bill_no, b.amount, b.discount, b.paid_amount,
       (SELECT ROUND(SUM(qty * COALESCE(price_at_time,0)), 2) FROM booking_item bi WHERE bi.booking_id = b.id) AS items_total,
       ROUND(ABS(b.amount - COALESCE((SELECT SUM(qty * COALESCE(price_at_time,0)) FROM booking_item bi WHERE bi.booking_id = b.id), 0)), 2) AS diff
FROM booking b
WHERE b.is_void = 0
"""))
issues = [r for r in rows if r['diff'] > 0.01]
print(f'   booking amount != items total: {len(issues)} / {len(rows)}')
for r in issues[:15]:
    print(f"     {r['id']:>3} {r['manual_bill_no'] or '-':<20} amount={r['amount']:>10} items_total={r['items_total']:>10} diff={r['diff']}")

# ---------------------------------------------------------------------------
section('27. DirectSale amount vs items total')
# ---------------------------------------------------------------------------
rows = list(cur.execute("""
SELECT ds.id, ds.manual_bill_no, ds.amount, ds.discount,
       (SELECT ROUND(SUM(qty * COALESCE(price_at_time,0)), 2) FROM direct_sale_item dsi WHERE dsi.sale_id = ds.id) AS items_total,
       ROUND(ABS(ds.amount - COALESCE((SELECT SUM(qty * COALESCE(price_at_time,0)) FROM direct_sale_item dsi WHERE dsi.sale_id = ds.id), 0)), 2) AS diff
FROM direct_sale ds
WHERE ds.is_void = 0
"""))
issues = [r for r in rows if r['diff'] > 0.01]
print(f'   direct_sale amount != items total: {len(issues)} / {len(rows)}')
for r in issues[:15]:
    print(f"     {r['id']:>3} {r['manual_bill_no'] or '-':<20} amount={r['amount']:>10} items_total={r['items_total']:>10} diff={r['diff']}")

print()
print('DONE.')
