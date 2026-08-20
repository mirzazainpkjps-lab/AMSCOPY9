import json
import sqlite3
import sys


bill = sys.argv[1] if len(sys.argv) > 1 else "7783"
con = sqlite3.connect("instance/ahmed_cement.db")
con.row_factory = sqlite3.Row
tables = ["direct_sale", "booking", "payment", "pending_bill", "entry", "invoice"]
out = {}
for table in tables:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    clauses = []
    params = []
    for col in ["manual_bill_no", "auto_bill_no", "bill_no", "invoice_no", "nimbus_no"]:
        if col in cols:
            clauses.append(f"COALESCE({col}, '') LIKE ?")
            params.append(f"%{bill}%")
    if not clauses:
        continue
    out[table] = [
        dict(r)
        for r in con.execute(
            f"SELECT * FROM {table} WHERE {' OR '.join(clauses)} ORDER BY id LIMIT 50",
            params,
        ).fetchall()
    ]
print(json.dumps(out, indent=2, sort_keys=True, default=str))
