#!/usr/bin/env python3
"""
Deep dive analysis of MB NO.8380 blocking issue
"""
import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import app, db, DirectSale, Invoice, Entry, PendingBill, Booking, Payment, find_bill_conflict
from sqlalchemy import or_

def detailed_state_check():
    """Check exact state that's preventing MB NO.8380 reuse."""
    print(f"\n{'='*70}")
    print(f"DETAILED STATE ANALYSIS FOR MB NO.8380")
    print(f"{'='*70}\n")
    
    report = {
        'timestamp': datetime.now().isoformat(),
        'all_records': {},
        'conflict_check': None,
        'invoice_state': {},
        'pending_bill_state': {},
        'entry_state': {},
        'root_cause': None
    }
    
    with app.app_context():
        # Check EVERYTHING with this bill number
        print("[1] Searching all tables for 'MB NO.8380' or '8380'...")
        
        # DirectSale
        ds_manual = DirectSale.query.filter_by(manual_bill_no='MB NO.8380').all()
        ds_auto = DirectSale.query.filter_by(auto_bill_no='MB NO.8380').all()
        all_ds = ds_manual + ds_auto
        print(f"\n    DirectSale records: {len(all_ds)}")
        for ds in all_ds:
            state = f"void={ds.is_void}" if ds.is_void else "ACTIVE"
            print(f"      ID={ds.id}: {state}, client={ds.client_name}, invoice_id={ds.invoice_id}")
            report['all_records'][f'DirectSale_{ds.id}'] = {
                'id': ds.id,
                'is_void': bool(ds.is_void),
                'client': ds.client_name,
                'invoice_id': ds.invoice_id
            }
        
        # Invoice
        if all_ds and all_ds[0].invoice_id:
            inv = Invoice.query.get(all_ds[0].invoice_id)
            if inv:
                state = f"void={inv.is_void}" if inv.is_void else "ACTIVE"
                print(f"\n    Linked Invoice: ID={inv.id}, {state}, invoice_no={inv.invoice_no}")
                report['invoice_state'] = {
                    'id': inv.id,
                    'is_void': bool(inv.is_void),
                    'invoice_no': inv.invoice_no,
                    'status': inv.status
                }
        
        # PendingBill with this bill_no
        pb_records = PendingBill.query.filter_by(bill_no='MB NO.8380').all()
        print(f"\n    PendingBill records: {len(pb_records)}")
        for pb in pb_records:
            state = f"void={pb.is_void}" if pb.is_void else "ACTIVE"
            print(f"      ID={pb.id}: {state}, client={pb.client_name}, paid={pb.is_paid}")
            report['pending_bill_state'][f'PendingBill_{pb.id}'] = {
                'id': pb.id,
                'is_void': bool(pb.is_void),
                'is_paid': bool(pb.is_paid),
                'client': pb.client_name
            }
        
        # Entry records
        entry_manual = Entry.query.filter_by(bill_no='MB NO.8380').all()
        entry_auto = Entry.query.filter_by(auto_bill_no='MB NO.8380').all()
        all_entries = entry_manual + entry_auto
        print(f"\n    Entry records: {len(all_entries)}")
        for e in all_entries[:5]:  # Show first 5
            state = f"void={e.is_void}" if e.is_void else "ACTIVE"
            print(f"      ID={e.id}: {state}, type={e.type}, qty={e.qty}")
        if len(all_entries) > 5:
            print(f"      ... and {len(all_entries) - 5} more Entry records")
        
        # Booking
        bk_records = Booking.query.filter(or_(
            Booking.manual_bill_no == 'MB NO.8380',
            Booking.auto_bill_no == 'MB NO.8380'
        )).all()
        print(f"\n    Booking records: {len(bk_records)}")
        for bk in bk_records:
            state = f"void={bk.is_void}" if bk.is_void else "ACTIVE"
            print(f"      ID={bk.id}: {state}, client={bk.client_name}")
        
        # Payment
        pay_records = Payment.query.filter(or_(
            Payment.manual_bill_no == 'MB NO.8380',
            Payment.auto_bill_no == 'MB NO.8380'
        )).all()
        print(f"\n    Payment records: {len(pay_records)}")
        for p in pay_records:
            state = f"void={p.is_void}" if p.is_void else "ACTIVE"
            print(f"      ID={p.id}: {state}, client={p.client_name}")
        
        # NOW: Test duplicate validation
        print(f"\n[2] Testing find_bill_conflict('MB NO.8380')...")
        conflict = find_bill_conflict('MB NO.8380')
        print(f"    Result: {conflict}")
        report['conflict_check'] = conflict
        
        if conflict:
            report['root_cause'] = f"Conflict with {conflict[0]} ID={conflict[1]}"
            conflict_type, conflict_id = conflict
            print(f"\n[3] Investigating conflict source...")
            
            if conflict_type == 'DirectSale':
                ds_conflict = DirectSale.query.get(conflict_id)
                if ds_conflict:
                    print(f"    DirectSale #{ds_conflict.id}:")
                    print(f"      - is_void: {ds_conflict.is_void}")
                    print(f"      - client: {ds_conflict.client_name}")
                    print(f"      - manual_bill_no: {ds_conflict.manual_bill_no}")
                    print(f"      - auto_bill_no: {ds_conflict.auto_bill_no}")
                    print(f"      - amount: {ds_conflict.amount}")
                    print(f"      - THIS IS THE BLOCKING RECORD - It shows as non-void!")
            
            elif conflict_type == 'Invoice':
                inv_conflict = Invoice.query.get(conflict_id)
                if inv_conflict:
                    print(f"    Invoice #{inv_conflict.id}:")
                    print(f"      - is_void: {inv_conflict.is_void}")
                    print(f"      - invoice_no: {inv_conflict.invoice_no}")
                    print(f"      - status: {inv_conflict.status}")
        else:
            report['root_cause'] = "NO CONFLICT - bill should be reusable now!"
            print(f"\n    ✓ NO CONFLICT FOUND - bill can be reused")
    
    # Save detailed report
    with open('state_check_report.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, default=str)
    
    print(f"\n[SAVED] Full report: state_check_report.json")
    return report

if __name__ == '__main__':
    try:
        report = detailed_state_check()
        if report.get('conflict_check'):
            print(f"\n⚠ BLOCKING CONFLICT: {report['conflict_check']}")
            sys.exit(1)
        else:
            print(f"\n✓ Bill is now clean - no blocking conflicts!")
            sys.exit(0)
    except Exception as e:
        print(f"\n✗ Analysis failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
