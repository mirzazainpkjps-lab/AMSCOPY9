#!/usr/bin/env python
import sqlite3

print("=== CHECKING sale_delivery_persons TABLE ===\n")

# Check source
print("SOURCE DATABASE:")
conn = sqlite3.connect("DATA TO COPY\\SQLITEBACKUP-06-05-2026_08-40AM.db")
cursor = conn.cursor()
try:
    cursor.execute("SELECT COUNT(*) FROM sale_delivery_persons")
    count = cursor.fetchone()[0]
    print(f"  sale_delivery_persons: {count} rows")
    
    cursor.execute("PRAGMA table_info(sale_delivery_persons)")
    cols = cursor.fetchall()
    print(f"  Columns: {[c[1] for c in cols[:5]]}...")
except Exception as e:
    print(f"  Error: {e}")
conn.close()

# Check target
print("\nTARGET DATABASE:")
conn = sqlite3.connect("instance\\ahmed_cement.db")
cursor = conn.cursor()

cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%delivery%'")
tables = cursor.fetchall()
print(f"  Delivery-related tables: {[t[0] for t in tables]}")

try:
    cursor.execute("SELECT COUNT(*) FROM sale_delivery_persons")
    count = cursor.fetchone()[0]
    print(f"  sale_delivery_persons: {count} rows")
except Exception as e:
    print(f"  sale_delivery_persons not found or error")

conn.close()

print("\n=== ACTION REQUIRED ===")
print("The table 'sale_delivery_persons' has 361 rows in source but doesn't exist in target.")
print("This table may need to be created and imported separately.")
