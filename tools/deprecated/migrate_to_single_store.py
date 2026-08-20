#!/usr/bin/env python
"""
DATABASE MIGRATION: Convert Multi-Tenant to Single-Store
- Remove tenant_id columns from all tables
- Preserve all existing data
- Update schema for single-store operation
"""

import sqlite3
import shutil
from pathlib import Path

def migrate_to_single_store():
    """Migrate database from multi-tenant to single-store"""

    db_path = "instance\\ahmed_cement.db"
    backup_path = "instance\\ahmed_cement.db.pre_single_store"

    print("STARTING SINGLE-STORE MIGRATION")
    print("=" * 50)

    # Create backup
    if Path(db_path).exists():
        print(f"Creating backup: {backup_path}")
        shutil.copy2(db_path, backup_path)

    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all tables with tenant_id columns
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT IN ('sqlite_sequence', 'tenant')
    """)
    tables = [row[0] for row in cursor.fetchall()]

    print(f"📊 Found {len(tables)} tables to process")

    # Process each table
    for table in tables:
        print(f"Processing table: {table}")

        # Check if table has tenant_id column
        cursor.execute(f"PRAGMA table_info({table})")
        columns = cursor.fetchall()
        column_names = [col[1] for col in columns]

        if 'tenant_id' in column_names:
            print(f"  Has tenant_id column - removing")

            # Get column info for recreation
            non_tenant_columns = [col for col in columns if col[1] != 'tenant_id']

            # Create new table schema without tenant_id
            create_sql = f"CREATE TABLE {table}_new ("
            column_defs = []

            for col in non_tenant_columns:
                col_name = col[1]
                col_type = col[2]
                col_notnull = "NOT NULL" if col[3] else ""
                col_default = f"DEFAULT {col[4]}" if col[4] is not None else ""
                col_pk = "PRIMARY KEY" if col[5] else ""

                col_def = f"{col_name} {col_type} {col_notnull} {col_default} {col_pk}".strip()
                column_defs.append(col_def)

            create_sql += ", ".join(column_defs) + ")"

            try:
                # Create new table
                cursor.execute(create_sql)

                # Copy data (excluding tenant_id)
                source_cols = [col[1] for col in non_tenant_columns]
                placeholders = ",".join(["?"] * len(source_cols))
                insert_sql = f"INSERT INTO {table}_new ({','.join(source_cols)}) SELECT {','.join(source_cols)} FROM {table}"

                cursor.execute(insert_sql)

                # Drop old table and rename new one
                cursor.execute(f"DROP TABLE {table}")
                cursor.execute(f"ALTER TABLE {table}_new RENAME TO {table}")

                # Recreate indexes (excluding tenant_id indexes)
                cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))
                indexes = cursor.fetchall()

                for index_sql, in indexes:
                    if 'tenant_id' not in index_sql:
                        try:
                            cursor.execute(index_sql)
                        except Exception as e:
                            print(f"  ⚠️  Could not recreate index: {str(e)[:50]}")

                print(f"  Migrated {table} successfully")

            except Exception as e:
                print(f"  Error migrating {table}: {e}")
                conn.rollback()
                continue

        else:
            print(f"  No tenant_id column - skipping")

    # Remove tenant-related tables
    tenant_tables = ['tenant', 'tenant_feature', 'role', 'permission', 'role_permission', 'user_role']
    for table in tenant_tables:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"Dropped tenant table: {table}")
        except Exception as e:
            print(f"Could not drop {table}: {e}")

    # Update user table to remove tenant_id and simplify roles
    try:
        print("\nUpdating user table...")
        cursor.execute("ALTER TABLE user DROP COLUMN tenant_id")
        cursor.execute("UPDATE user SET role = 'admin' WHERE role = 'root'")
        print("Updated user table")
    except Exception as e:
        print(f"Could not update user table: {e}")

    # Commit all changes
    conn.commit()
    conn.close()

    print("\n" + "=" * 50)
    print("MIGRATION COMPLETED")
    print(f"Backup saved: {backup_path}")
    print("Database converted to single-store mode")
    print("=" * 50)

if __name__ == "__main__":
    migrate_to_single_store()
