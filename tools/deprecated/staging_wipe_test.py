import shutil
import pathlib
import sqlite3
import datetime

base = pathlib.Path('d:/locked app/2062026')
src = base / 'instance' / 'ahmed_cement.db'
stamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
dst = base / 'instance' / f'ahmed_cement.testcopy.{stamp}.db'
shutil.copy2(src, dst)
print('Copied DB to:', dst)

def sums(conn):
    cur = conn.cursor()
    def safe(q):
        try:
            return cur.execute(q).fetchone()[0]
        except Exception as e:
            return f'ERR:{e}'
    print('SUM balances:', safe('SELECT SUM(balance) FROM account'))
    print('AccountTransaction SUM:', safe('SELECT SUM(amount) FROM account_transaction WHERE is_void=0'))
    print('FbmCashDrawerEntry SUM:', safe("SELECT SUM(amount) FROM fbm_cash_drawer_entry WHERE is_void=0"))
    print('Payment SUM:', safe('SELECT SUM(amount) FROM payment WHERE is_void=0'))
    print('SupplierPayment SUM:', safe('SELECT SUM(amount) FROM supplier_payment WHERE is_void=0'))

conn = sqlite3.connect(str(dst))
print('\nBefore simulated accounts-domain wipe:')
sums(conn)

print('\nApplying simulated accounts-domain wipe on the copy...')
cur = conn.cursor()
queries = [
    "DELETE FROM account_transaction",
    "DELETE FROM fbm_cash_drawer_entry",
    "DELETE FROM fbm_cash_drawer_category",
    "DELETE FROM cash_flow_difference_adjustment",
    "DELETE FROM cash_flow_reconciliation_audit",
    "UPDATE payment SET payment_account_id = NULL",
    "UPDATE supplier_payment SET payment_account_id = NULL",
    "UPDATE account SET balance = 0",
]
for q in queries:
    try:
        cur.execute(q)
        print('OK:', q.split()[0])
    except Exception as e:
        print('ERR:', q.split()[0], e)

conn.commit()
print('\nAfter simulated accounts-domain wipe:')
sums(conn)
conn.close()
print('\nTest DB left at:', dst)
