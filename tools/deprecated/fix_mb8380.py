#!/usr/bin/env python3
"""
FIX FOR MB NO.8380 CORRUPTION

Root Cause: DirectSale voided, but orphaned Invoice left active
Solution: Void the orphaned Invoice and sync all related records
"""

import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import (
    app, db, DirectSale, Invoice, Entry, PendingBill, AuditLog,
    find_bill_conflict, _log_bill_repair, current_user
)
from flask_login import LoginManager

def fix_mb8380():
    """
    Fix the MB NO.8380 corruption by:
    1. Finding the orphaned Invoice
    2. Voiding it atomically
    3. Voiding related PendingBills
    4. Validating the fix
    """
    print(f"\n{'='*70}")
    print(f"CRITICAL FIX FOR MB NO.8380 ORPHANED INVOICE")
    print(f"{'='*70}\n")
    
    fix_report = {
        'timestamp': datetime.now().isoformat(),
        'bill_no': 'MB NO.8380',
        'actions': [],
        'validation': {},
        'status': 'PENDING'
    }
    
    with app.app_context():
        # Step 1: Find the orphaned Invoice
        print("[STEP 1] Locating orphaned Invoice...")
        orphaned_invoice = Invoice.query.filter_by(invoice_no='MB NO.8380', is_void=False).first()
        
        if not orphaned_invoice:
            print("    ✗ No orphaned active Invoice found")
            print("    (Maybe it was already voided?)")
            fix_report['status'] = 'NOT_NEEDED'
            return fix_report
        
        print(f"    ✓ Found Invoice ID={orphaned_invoice.id}")
        print(f"      invoice_no: {orphaned_invoice.invoice_no}")
        print(f"      status: {orphaned_invoice.status}")
        print(f"      is_void: {orphaned_invoice.is_void}")
        
        fix_report['actions'].append({
            'action': 'FOUND_ORPHANED_INVOICE',
            'invoice_id': orphaned_invoice.id,
            'invoice_no': orphaned_invoice.invoice_no
        })
        
        # Step 2: Get related DirectSale
        print("\n[STEP 2] Finding related DirectSale...")
        related_sales = DirectSale.query.filter_by(invoice_id=orphaned_invoice.id).all()
        print(f"    Found {len(related_sales)} DirectSale record(s)")
        for sale in related_sales:
            print(f"      - ID={sale.id}, is_void={sale.is_void}")
        
        # Step 3: Void the Invoice
        print(f"\n[STEP 3] Voiding orphaned Invoice {orphaned_invoice.id}...")
        try:
            old_status = orphaned_invoice.is_void
            orphaned_invoice.is_void = True
            
            print(f"    ✓ Invoice marked as void")
            
            fix_report['actions'].append({
                'action': 'VOID_INVOICE',
                'invoice_id': orphaned_invoice.id,
                'previous_state': bool(old_status),
                'new_state': True
            })
        except Exception as e:
            print(f"    ✗ Failed to void Invoice: {e}")
            db.session.rollback()
            fix_report['status'] = 'FAILED'
            fix_report['errors'] = [str(e)]
            return fix_report
        
        # Step 4: Find and void related PendingBills
        print(f"\n[STEP 4] Finding related PendingBill records...")
        related_pending_bills = PendingBill.query.filter_by(bill_no='MB NO.8380', is_void=False).all()
        print(f"    Found {len(related_pending_bills)} active PendingBill record(s)")
        
        for pb in related_pending_bills:
            print(f"      Voiding PendingBill ID={pb.id}")
            pb.is_void = True
            fix_report['actions'].append({
                'action': 'VOID_PENDING_BILL',
                'pending_bill_id': pb.id,
                'client': pb.client_name
            })
        
        # Step 5: Find and void related Entry records that reference this invoice
        print(f"\n[STEP 5] Finding Entry records linked to Invoice...")
        related_entries = Entry.query.filter_by(bill_no='MB NO.8380', is_void=False).all()
        print(f"    Found {len(related_entries)} active Entry record(s)")
        
        void_count = 0
        for entry in related_entries:
            entry.is_void = True
            void_count += 1
        
        if void_count > 0:
            fix_report['actions'].append({
                'action': 'VOID_ENTRIES',
                'entry_count': void_count
            })
        
        # Step 6: Commit all changes
        print(f"\n[STEP 6] Committing changes to database...")
        try:
            db.session.add(AuditLog(
                user_id=None,  # System action
                action='bill.recovery.mb8380',
                details=f'Voided orphaned Invoice #{orphaned_invoice.id} and related records'
            ))
            db.session.commit()
            print(f"    ✓ All changes committed")
        except Exception as e:
            print(f"    ✗ Commit failed: {e}")
            db.session.rollback()
            fix_report['status'] = 'FAILED'
            fix_report['errors'] = [str(e)]
            return fix_report
        
        # Step 7: Validate the fix
        print(f"\n[STEP 7] Validating the fix...")
        conflict = find_bill_conflict('MB NO.8380')
        
        if conflict:
            print(f"    ⚠ CONFLICT STILL EXISTS: {conflict}")
            fix_report['validation']['conflict'] = conflict
            fix_report['validation']['status'] = 'PARTIALLY_FIXED'
        else:
            print(f"    ✓ NO CONFLICT - MB NO.8380 is now CLEAN")
            fix_report['validation']['conflict'] = None
            fix_report['validation']['status'] = 'FULLY_FIXED'
        
        # Step 8: Final state check
        print(f"\n[STEP 8] Checking final state...")
        invoice_final = Invoice.query.filter_by(invoice_no='MB NO.8380').first()
        if invoice_final:
            print(f"    Invoice final state: is_void={invoice_final.is_void}")
        
        pending_final = PendingBill.query.filter_by(bill_no='MB NO.8380', is_void=False).count()
        print(f"    Active PendingBill records: {pending_final}")
        
        entries_final = Entry.query.filter_by(bill_no='MB NO.8380', is_void=False).count()
        print(f"    Active Entry records: {entries_final}")
    
    # Save fix report
    with open('mb8380_fix_report.json', 'w', encoding='utf-8') as f:
        json.dump(fix_report, f, indent=2, default=str)
    
    fix_report['status'] = 'SUCCESS' if not conflict else 'PARTIALLY_SUCCESS'
    
    print(f"\n[SAVED] Detailed fix report: mb8380_fix_report.json")
    return fix_report

if __name__ == '__main__':
    try:
        report = fix_mb8380()
        
        print(f"\n{'='*70}")
        if report['status'] == 'SUCCESS':
            print(f"✓ MB NO.8380 FULLY RECOVERED")
            print(f"  The bill can now be edited and reused")
            sys.exit(0)
        elif report['status'] in ('PARTIALLY_SUCCESS', 'PARTIALLY_FIXED'):
            print(f"⚠ MB NO.8380 PARTIALLY FIXED")
            print(f"  Manual intervention may be needed")
            sys.exit(1)
        else:
            print(f"✗ MB NO.8380 FIX FAILED")
            print(f"  See mb8380_fix_report.json for details")
            sys.exit(1)
    except Exception as e:
        print(f"\n✗ Fix process failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
