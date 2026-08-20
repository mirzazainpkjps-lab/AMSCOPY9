import sqlite3

DB='instance/ahmed_cement.db'
con=sqlite3.connect(DB)
cur=con.cursor()
print('DB:', DB)
print('SUM balances:', cur.execute('SELECT SUM(balance) FROM account').fetchone()[0])
print('AccountTransaction SUM:', cur.execute('SELECT SUM(amount) FROM account_transaction WHERE is_void=0').fetchone()[0])
print('FbmCashDrawerEntry SUM:', cur.execute("SELECT SUM(amount) FROM fbm_cash_drawer_entry WHERE is_void=0").fetchone()[0])
print('Payment SUM:', cur.execute('SELECT SUM(amount) FROM payment WHERE is_void=0').fetchone()[0])
print('SupplierPayment SUM:', cur.execute('SELECT SUM(amount) FROM supplier_payment WHERE is_void=0').fetchone()[0])
con.close()
