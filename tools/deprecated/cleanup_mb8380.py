#!/usr/bin/env python3
"""
COMPLETELY REMOVE Invoice ID=738 to free up bill number MB NO.8380
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import app, db, Invoice, DirectSale

with app.app_context():
    print("\n" + "="*70)
    print("CLEANUP: DELETE Invoice ID=738 (MB NO.8380)")
    print("="*70 + "\n")
    
    # Check if any DirectSale references this Invoice
    print("[1] Checking for DirectSale records referencing Invoice ID=738...")
    sales = DirectSale.query.filter_by(invoice_id=738).all()
    
    if sales:
        print(f"    Found {len(sales)} DirectSale record(s):")
        for sale in sales:
            print(f"      - DirectSale ID={sale.id}, is_void={sale.is_void}")
        
        print(f"\n    Clearing invoice_id references...")
        for sale in sales:
            sale.invoice_id = None
        
        print(f"    ✓ References cleared")
    else:
        print(f"    ✓ No DirectSale references found")
    
    # Find and delete the Invoice
    print(f"\n[2] Deleting Invoice ID=738...")
    invoice = Invoice.query.get(738)
    
    if invoice:
        print(f"    Found Invoice: invoice_no={invoice.invoice_no}")
        print(f"    Status: {invoice.status}")
        print(f"    is_void: {invoice.is_void}")
        
        try:
            db.session.delete(invoice)
            db.session.commit()
            print(f"    ✓ Invoice DELETED from database")
        except Exception as e:
            print(f"    ✗ Failed to delete: {e}")
            db.session.rollback()
            sys.exit(1)
    else:
        print(f"    ✗ Invoice ID=738 not found")
        sys.exit(1)
    
    # Verify deletion
    print(f"\n[3] Verifying deletion...")
    remaining = Invoice.query.filter_by(invoice_no='MB NO.8380').all()
    
    if remaining:
        print(f"    ✗ STILL FOUND {len(remaining)} Invoice record(s) with MB NO.8380")
        sys.exit(1)
    else:
        print(f"    ✓ CONFIRMED: Bill number 'MB NO.8380' is now FREE for reuse")
    
    print(f"\n" + "="*70)
    print(f"✓ CLEANUP COMPLETE - Bill number 'MB NO.8380' can now be created")
    print(f"="*70 + "\n")
