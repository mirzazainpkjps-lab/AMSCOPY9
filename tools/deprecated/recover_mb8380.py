#!/usr/bin/env python3
"""
EMERGENCY BILL RECOVERY SCRIPT FOR MB NO.8380

This script:
1. Analyzes the current state of MB NO.8380
2. Detects orphaned/inconsistent records
3. Auto-repairs orphaned entries
4. Validates the recovery
5. Generates a detailed recovery report

Usage:
    python recover_mb8380.py
    
Then check the console output and recovery_report.json for details.
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import (
    app, db, DirectSale, Entry, DeliveryRent, SaleDeliveryPerson,
    _bill_no_variants, _not_void, _get_bill_consistency_status,
    _detect_orphaned_void_entries, _auto_repair_orphaned_entries,
    _direct_sale_default_bill_ref
)

def analyze_bill(bill_no='MB NO.8380'):
    """Comprehensive analysis of bill state."""
    print(f"\n{'='*70}")
    print(f"BILL RECOVERY ANALYSIS FOR: {bill_no}")
    print(f"{'='*70}")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    report = {
        'bill_no': bill_no,
        'timestamp': datetime.now().isoformat(),
        'analysis': {},
        'repairs': {},
        'validation': {},
        'errors': []
    }
    
    candidates = _bill_no_variants(bill_no)
    print(f"[*] Bill variants to check: {candidates}")
    
    with app.app_context():
        # STEP 1: Find all DirectSale records
        print(f"\n[STEP 1] Searching DirectSale records...")
        direct_sales = DirectSale.query.filter(
            or_(
                DirectSale.manual_bill_no.in_(candidates) if candidates else False,
                DirectSale.auto_bill_no.in_(candidates) if candidates else False
            )
        ).all()
        
        print(f"    Found {len(direct_sales)} DirectSale record(s)")
        for sale in direct_sales:
            print(f"      - ID={sale.id}, is_void={sale.is_void}, client={sale.client_name}")
            report['analysis'][f'direct_sale_{sale.id}'] = {
                'id': sale.id,
                'is_void': bool(sale.is_void),
                'client': sale.client_name,
                'amount': float(sale.amount or 0),
                'paid_amount': float(sale.paid_amount or 0)
            }
        
        # STEP 2: Find Entry records
        print(f"\n[STEP 2] Searching Entry records...")
        all_entries = Entry.query.filter(
            or_(
                Entry.bill_no.in_(candidates) if candidates else False,
                Entry.auto_bill_no.in_(candidates) if candidates else False
            )
        ).all()
        
        print(f"    Found {len(all_entries)} Entry record(s)")
        active_entries = [e for e in all_entries if not e.is_void]
        void_entries = [e for e in all_entries if e.is_void]
        
        print(f"      - Active: {len(active_entries)}, Voided: {len(void_entries)}")
        for entry in all_entries:
            print(f"        Entry ID={entry.id}: type={entry.type}, is_void={entry.is_void}, "
                  f"qty={entry.qty}, material={entry.material or entry.booked_material}")
            report['analysis'][f'entry_{entry.id}'] = {
                'id': entry.id,
                'is_void': bool(entry.is_void),
                'type': entry.type,
                'qty': float(entry.qty or 0),
                'material': entry.material or entry.booked_material,
                'source_id': entry.source_id,
                'source_table': entry.source_table
            }
        
        # STEP 3: Detect orphaned entries
        print(f"\n[STEP 3] Detecting orphaned Entry records...")
        orphaned = _detect_orphaned_void_entries(bill_no)
        print(f"    Found {len(orphaned)} orphaned Entry record(s)")
        for entry in orphaned:
            print(f"      - Entry ID={entry.id} (void but parent DirectSale is not)")
        
        report['analysis']['orphaned_count'] = len(orphaned)
        
        # STEP 4: Check consistency
        print(f"\n[STEP 4] Checking overall consistency...")
        consistency = _get_bill_consistency_status(bill_no)
        if consistency:
            print(f"    Is consistent: {consistency['is_consistent']}")
            if consistency['issues']:
                print(f"    Issues found:")
                for issue in consistency['issues']:
                    print(f"      - {issue}")
                report['analysis']['consistency_issues'] = consistency['issues']
        
        # STEP 5: Auto-repair orphaned entries
        print(f"\n[STEP 5] Attempting auto-repair of orphaned entries...")
        repaired_count = _auto_repair_orphaned_entries(bill_no)
        print(f"    Repaired {repaired_count} Entry record(s)")
        db.session.flush()
        report['repairs']['orphan_entries_repaired'] = repaired_count
        
        # STEP 6: Re-check consistency after repair
        print(f"\n[STEP 6] Re-checking consistency after repair...")
        consistency_after = _get_bill_consistency_status(bill_no)
        if consistency_after:
            print(f"    Is consistent NOW: {consistency_after['is_consistent']}")
            if consistency_after['issues']:
                print(f"    Remaining issues:")
                for issue in consistency_after['issues']:
                    print(f"      - {issue}")
                report['validation']['remaining_issues'] = consistency_after['issues']
            else:
                print(f"    ✓ NO ISSUES REMAINING!")
                report['validation']['status'] = 'FULLY_RECOVERED'
        
        # STEP 7: Check for duplicate validation
        print(f"\n[STEP 7] Testing duplicate validation...")
        from main import find_bill_conflict
        conflict = find_bill_conflict(bill_no)
        if conflict:
            print(f"    ⚠ CONFLICT STILL EXISTS: {conflict}")
            report['validation']['conflict'] = conflict
        else:
            print(f"    ✓ No conflict found (bill can be reused)")
            report['validation']['conflict'] = None
        
        db.session.commit()
    
    # STEP 8: Generate summary
    print(f"\n{'='*70}")
    print("RECOVERY SUMMARY")
    print(f"{'='*70}")
    print(f"DirectSale records: {len(direct_sales)}")
    print(f"Total Entry records: {len(all_entries)}")
    print(f"Orphaned entries repaired: {repaired_count}")
    print(f"Final status: {'✓ CONSISTENT' if (consistency_after and consistency_after['is_consistent']) else '⚠ ISSUES REMAIN'}")
    print(f"\nFull report saved to: recovery_report.json")
    
    # Save report
    with open('recovery_report.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, default=str)
    
    return report

if __name__ == '__main__':
    from sqlalchemy import or_
    try:
        report = analyze_bill('MB NO.8380')
        print(f"\n✓ Recovery analysis completed successfully")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Recovery analysis failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
