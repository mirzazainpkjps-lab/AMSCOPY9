#!/usr/bin/env python
"""
Import missing data for sale_delivery_persons table
"""

import sqlite3

def import_missing_table():
    source_db = "DATA TO COPY\\SQLITEBACKUP-06-05-2026_08-40AM.db"
    target_db = "instance\\ahmed_cement.db"
    
    print("=== IMPORTING sale_delivery_persons ===\n")
    
    source_conn = sqlite3.connect(source_db)
    target_conn = sqlite3.connect(target_db)
    
    source_cursor = source_conn.cursor()
    target_cursor = target_conn.cursor()
    
    # Get source data
    source_cursor.execute("SELECT * FROM sale_delivery_persons")
    columns = [desc[0] for desc in source_cursor.description]
    rows = source_cursor.fetchall()
    
    print(f"Found {len(rows)} rows to import")
    print(f"Columns: {columns}")
    
    # Import each row
    inserted = 0
    errors = 0
    
    for row in rows:
        placeholders = ",".join(["?"] * len(columns))
        insert_sql = f"INSERT INTO sale_delivery_persons ({','.join(columns)}) VALUES ({placeholders})"
        
        try:
            target_cursor.execute(insert_sql, row)
            inserted += 1
        except Exception as e:
            errors += 1
            if errors <= 3:  # Show first 3 errors only
                print(f"Error on row {row[0]}: {str(e)[:80]}")
    
    # Commit
    target_conn.commit()
    
    # Verify
    target_cursor.execute("SELECT COUNT(*) FROM sale_delivery_persons")
    final_count = target_cursor.fetchone()[0]
    
    print(f"\n✓ Inserted: {inserted}")
    print(f"✗ Errors:  {errors}")
    print(f"Final count in target: {final_count}")
    
    source_conn.close()
    target_conn.close()
    
    if inserted == len(rows):
        print("\n✅ SUCCESS - All rows imported!")
        return True
    else:
        print(f"\n⚠ PARTIAL - {inserted}/{len(rows)} rows imported")
        return False

if __name__ == "__main__":
    import_missing_table()
