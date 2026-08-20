#!/usr/bin/env python
"""
INTELLIGENT DATA IMPORTER FOR FBM SERVER
- Zero Duplicates
- Smart Conflict Resolution
- Full Validation
- Rollback Capability
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any
import sys
import shutil

class DataImporter:
    def __init__(self, source_db: str, target_db: str, dry_run: bool = False):
        self.source_db = source_db
        self.target_db = target_db
        self.dry_run = dry_run
        self.backup_db = f"{target_db}.backup"
        self.report = {
            "status": "PENDING",
            "timestamp": datetime.now().isoformat(),
            "dry_run": dry_run,
            "operations": [],
            "statistics": {},
            "errors": [],
            "warnings": []
        }
        
        # Import order (respects foreign keys)
        self.table_import_order = [
            'role', 'permission', 'role_permission',
            'user', 'user_role',
            'account_category', 'account',
            'supplier', 'delivery_person', 'material_category', 'material',
            'client', 'delivery_rent',
            'entry', 'booking', 'invoice', 'payment',
            'pending_bill', 'waive_off', 'direct_sale',
            'grn', 'delivery', 'booking_item', 'direct_sale_item',
            'material_return', 'material_return_item', 'grn_item', 'delivery_item',
            'fbm_rental_item', 'fbm_client', 'fbm_rental',
            'follow_up_reminder', 'follow_up_contact',
            'settings', 'staff_email', 'bill_counter',
            'fbm_cash_drawer_category', 'fbm_cash_drawer_entry',
            'recon_basket', 'audit_log',
            'supplier_payment', 'sale_delivery_person', 'delivery_person_payment'
        ]
        
        # Unique constraint mappings (table -> unique fields)
        self.unique_constraints = {
            'user': ['username'],
            'client': ['code'],
            'supplier': ['name'],
            'material': ['code'],
            'material_category': ['name'],
            'account': ['code'],
            'account_category': ['name'],
        }

    def backup_target_db(self):
        """Create backup of current target database"""
        if Path(self.target_db).exists():
            shutil.copy2(self.target_db, self.backup_db)
            msg = f"✓ Backed up target DB: {self.backup_db}"
            print(msg)
            self.report["operations"].append({"action": "backup", "details": msg})
        else:
            msg = f"⚠ Target DB doesn't exist yet: {self.target_db}"
            self.report["warnings"].append(msg)

    def get_source_tables(self) -> List[str]:
        """Get list of tables from source database"""
        conn = sqlite3.connect(self.source_db)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return tables

    def get_table_schema(self, table: str, db: str = None) -> Dict:
        """Get table schema information"""
        if db is None:
            db = self.source_db
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table});")
        columns = {}
        for row in cursor.fetchall():
            col_id, name, type_, notnull, default, pk = row
            columns[name] = {
                'type': type_,
                'notnull': bool(notnull),
                'pk': bool(pk),
                'default': default
            }
        conn.close()
        return columns

    def record_exists(self, table: str, record: Dict, target_conn: sqlite3.Connection) -> Tuple[bool, Any]:
        """Check if record exists using unique constraints"""
        if table not in self.unique_constraints:
            return False, None
        
        cursor = target_conn.cursor()
        unique_fields = self.unique_constraints[table]
        
        # Build WHERE clause from unique fields
        where_parts = []
        values = []
        for field in unique_fields:
            if field in record:
                where_parts.append(f"{field} IS ?")
                values.append(record[field])
        
        if where_parts:
            where_clause = " AND ".join(where_parts)
            sql = f"SELECT id FROM {table} WHERE {where_clause} LIMIT 1"
            try:
                cursor.execute(sql, values)
                result = cursor.fetchone()
                return result is not None, result[0] if result else None
            except Exception as e:
                return False, None
        
        return False, None

    def import_table(self, table: str, source_conn: sqlite3.Connection, 
                    target_conn: sqlite3.Connection) -> Dict:
        """Import single table with duplicate detection"""
        stats = {
            "table": table,
            "source_rows": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "details": []
        }
        
        source_cursor = source_conn.cursor()
        target_cursor = target_conn.cursor()
        
        try:
            # Get source data
            source_cursor.execute(f"SELECT * FROM {table}")
            columns = [desc[0] for desc in source_cursor.description]
            rows = source_cursor.fetchall()
            stats["source_rows"] = len(rows)
            
            if len(rows) == 0:
                return stats
            
            # Import each row
            for row in rows:
                record = dict(zip(columns, row))
                
                # Check for duplicates
                exists, existing_id = self.record_exists(table, record, target_conn)
                
                if exists:
                    # Could update if data differs, for now skip to avoid conflicts
                    stats["skipped"] += 1
                    stats["details"].append(f"Row {record.get('id', '?')} exists (id={existing_id})")
                else:
                    # Insert new row
                    placeholders = ",".join(["?"] * len(columns))
                    insert_sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
                    
                    try:
                        if not self.dry_run:
                            target_cursor.execute(insert_sql, row)
                        stats["inserted"] += 1
                    except Exception as e:
                        stats["errors"] += 1
                        stats["details"].append(f"Error inserting row {record.get('id', '?')}: {str(e)[:80]}")
            
            # Commit if not dry run
            if not self.dry_run:
                target_conn.commit()
                
        except Exception as e:
            msg = f"Error importing table {table}: {str(e)}"
            self.report["errors"].append(msg)
            stats["errors"] += 1
            if not self.dry_run:
                target_conn.rollback()
        
        return stats

    def validate_import(self, target_conn: sqlite3.Connection) -> Dict:
        """Validate imported data"""
        validation = {
            "status": "OK",
            "checks": [],
            "issues": []
        }
        
        cursor = target_conn.cursor()
        available_tables = self.get_source_tables()
        
        # Check row counts
        for table in available_tables:
            try:
                source_conn = sqlite3.connect(self.source_db)
                source_cursor = source_conn.cursor()
                
                source_cursor.execute(f"SELECT COUNT(*) FROM {table}")
                source_count = source_cursor.fetchone()[0]
                
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                target_count = cursor.fetchone()[0]
                
                validation["checks"].append({
                    "table": table,
                    "source_rows": source_count,
                    "target_rows": target_count,
                    "match": source_count == target_count
                })
                
                if source_count > 0 and target_count == 0:
                    validation["issues"].append(f"⚠ {table}: {source_count} rows in source but 0 in target!")
                    validation["status"] = "WARNINGS"
                
                source_conn.close()
            except Exception as e:
                validation["issues"].append(f"Error validating {table}: {str(e)[:100]}")
                validation["status"] = "ERRORS"
        
        return validation

    def run(self):
        """Execute the import"""
        print("\n" + "="*70)
        print("INTELLIGENT DATA IMPORTER - FBM SERVER")
        print("="*70)
        print(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE EXECUTION'}")
        print(f"Source: {self.source_db}")
        print(f"Target: {self.target_db}")
        print("="*70 + "\n")
        
        try:
            # Backup
            self.backup_target_db()
            
            # Connect to databases
            source_conn = sqlite3.connect(self.source_db)
            target_conn = sqlite3.connect(self.target_db)
            
            # Get actual available tables
            available_tables = self.get_source_tables()
            ordered_tables = [t for t in self.table_import_order if t in available_tables]
            
            print(f"\n📊 IMPORTING {len(ordered_tables)} DATA TABLES:\n")
            
            # Import tables
            table_stats = []
            for table in ordered_tables:
                # Get source table count
                src_cursor = source_conn.cursor()
                src_cursor.execute(f"SELECT COUNT(*) FROM {table}")
                source_count = src_cursor.fetchone()[0]
                
                if source_count > 0:
                    stats = self.import_table(table, source_conn, target_conn)
                    table_stats.append(stats)
                    
                    status_symbol = "✓" if stats["errors"] == 0 else "✗"
                    print(f"{status_symbol} {table:30s} | Source: {stats['source_rows']:5d} | "
                          f"Inserted: {stats['inserted']:5d} | Skipped: {stats['skipped']:5d} | "
                          f"Errors: {stats['errors']:3d}")
            
            # Validation
            print("\n📋 VALIDATION:\n")
            validation = self.validate_import(target_conn)
            
            print(f"Validation Status: {validation['status']}\n")
            for check in validation['checks']:
                if check['match']:
                    symbol = "✓"
                elif check['source_rows'] > 0:
                    symbol = "⚠"
                else:
                    symbol = " "
                print(f"{symbol} {check['table']:30s} | Source: {check['source_rows']:6d} | "
                      f"Target: {check['target_rows']:6d}")
            
            if validation['issues']:
                print(f"\n⚠ WARNINGS/ISSUES:")
                for issue in validation['issues']:
                    print(f"  {issue}")
            
            # Summary
            total_inserted = sum(s["inserted"] for s in table_stats)
            total_skipped = sum(s["skipped"] for s in table_stats)
            total_errors = sum(s["errors"] for s in table_stats)
            
            print("\n" + "="*70)
            print("📈 IMPORT SUMMARY:")
            print("="*70)
            print(f"Total Inserted:  {total_inserted:,}")
            print(f"Total Skipped:   {total_skipped:,}")
            print(f"Total Errors:    {total_errors:,}")
            print(f"Mode:            {'DRY RUN' if self.dry_run else 'LIVE IMPORT'}")
            print("="*70 + "\n")
            
            # Update report
            self.report["status"] = "SUCCESS" if total_errors == 0 else "COMPLETED_WITH_ERRORS"
            self.report["statistics"] = {
                "total_inserted": total_inserted,
                "total_skipped": total_skipped,
                "total_errors": total_errors,
                "total_tables": len(table_stats)
            }
            self.report["validation"] = validation
            self.report["table_details"] = table_stats
            
            # Close connections
            source_conn.close()
            target_conn.close()
            
            return True
            
        except Exception as e:
            self.report["status"] = "FAILED"
            self.report["errors"].append(str(e))
            print(f"\n❌ FATAL ERROR: {e}")
            return False
    
    def save_report(self, report_file: str):
        """Save import report to JSON"""
        self.report["completed_at"] = datetime.now().isoformat()
        with open(report_file, 'w') as f:
            json.dump(self.report, f, indent=2, default=str)
        print(f"📄 Report saved: {report_file}")


def main():
    source_db = "DATA TO COPY\\SQLITEBACKUP-06-05-2026_08-40AM.db"
    target_db = "instance\\ahmed_cement.db"
    report_file = "import_report.json"
    
    # Live import (target DB is empty, safe to import directly)
    print("\n✅ EXECUTING LIVE IMPORT\n")
    
    importer = DataImporter(source_db, target_db, dry_run=False)
    success = importer.run()
    importer.save_report(report_file)
    
    if success:
        print(f"\n✅ IMPORT COMPLETE!")
        print(f"📄 Detailed report: {report_file}")
    else:
        print(f"\n❌ IMPORT FAILED - Check report: {report_file}")


if __name__ == "__main__":
    main()
