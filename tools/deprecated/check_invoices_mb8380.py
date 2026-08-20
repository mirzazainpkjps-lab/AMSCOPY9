#!/usr/bin/env python3
"""
Check all Invoice records with MB NO.8380 and prepare for complete cleanup
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import app, db, Invoice

with app.app_context():
    print("\n[CHECKING] All Invoice records with 'MB NO.8380':\n")
    
    invoices = Invoice.query.filter_by(invoice_no='MB NO.8380').all()
    
    print(f"Total records found: {len(invoices)}\n")
    
    for inv in invoices:
        print(f"Invoice ID: {inv.id}")
        print(f"  invoice_no: {inv.invoice_no}")
        print(f"  status: {inv.status}")
        print(f"  is_void: {inv.is_void}")
        print(f"  amount: {inv.amount}")
        print(f"  client_name: {inv.client_name if hasattr(inv, 'client_name') else 'N/A'}")
        print()
    
    if invoices:
        print(f"[ACTION] To completely remove this bill number, we need to DELETE these Invoice records")
        print(f"[WARNING] This is a data cleanup operation - ensure no other references exist")
