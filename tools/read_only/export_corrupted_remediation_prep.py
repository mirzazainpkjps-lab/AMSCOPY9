#!/usr/bin/env python3
"""
Export corrupted negative SB-CP Payment rows and their linked AccountTransactions.

Purpose:
  - Export all corrupted negative SB-CP payments to CSV/JSON
  - Include linked AccountTransaction rows
  - Suggest remediation actions
  - Preserve for safe remediation (phase 2)
  - NO database writes; read-only export.

Output:
  - instance/exports/corrupted_payments_<timestamp>.json
  - instance/exports/corrupted_payments_<timestamp>.csv
  - instance/exports/remediation_suggestions_<timestamp>.json
  - instance/exports/export_manifest_<timestamp>.txt

Safety:
  - Read-only operation
  - All original data exported for audit trail
  - Timestamped for traceability
"""

import os
import sys
import json
import csv
from datetime import datetime
from pathlib import Path

# Add workspace root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import db, app
from models import Payment, AccountTransaction, Client, Account

def ensure_export_dir():
    """Create instance/exports/ directory if needed."""
    export_dir = Path('instance/exports')
    export_dir.mkdir(parents=True, exist_ok=True)
    return export_dir

def get_timestamp():
    """Return ISO format timestamp for file naming."""
    return datetime.now().strftime('%Y%m%d_%H%M%S')

def query_corrupted_payments():
    """
    Query all Payment rows where:
      - amount < 0 (negative)
      - auto_bill_no starts with 'SB-CP-'
    
    Returns list of Payment objects.
    """
    with app.app_context():
        corrupted = db.session.query(Payment).filter(
            Payment.amount < 0,
            Payment.auto_bill_no.like('SB-CP-%')
        ).order_by(Payment.date_posted).all()
        return corrupted

def get_linked_transactions(payment_id):
    """
    Find AccountTransaction rows linked to this Payment via note field.
    
    Looks for patterns like:
      - [SRC:Payment:N]
      - [SRC:ClientRefund:N]
    
    Returns list of AccountTransaction rows.
    """
    with app.app_context():
        # Search for transactions that mention this payment id in notes
        pattern = f'Payment:{payment_id}'
        linked_via_payment = db.session.query(AccountTransaction).filter(
            AccountTransaction.note.contains(pattern)
        ).all()
        
        pattern_refund = f'ClientRefund:{payment_id}'
        linked_via_refund = db.session.query(AccountTransaction).filter(
            AccountTransaction.note.contains(pattern_refund)
        ).all()
        
        return linked_via_payment + linked_via_refund

def payment_to_dict(payment):
    """Convert Payment ORM object to serializable dict."""
    return {
        'payment_id': payment.id,
        'client_name': payment.client_name,
        'amount': float(payment.amount),
        'method': payment.method or '',
        'auto_bill_no': payment.auto_bill_no or '',
        'date_posted': payment.date_posted.isoformat() if payment.date_posted else None,
        'note': payment.note or '',
        'payment_account_id': payment.payment_account_id,
    }

def tx_to_dict(tx):
    """Convert AccountTransaction ORM object to serializable dict."""
    return {
        'tx_id': tx.id,
        'from_account_id': tx.from_account_id,
        'to_account_id': tx.to_account_id,
        'amount': float(tx.amount),
        'transaction_type': tx.transaction_type,
        'description': tx.description or '',
        'date_posted': tx.date_posted.isoformat() if tx.date_posted else None,
        'note': tx.note or '',
    }

def suggest_remediation(payment, linked_txs):
    """
    Generate remediation suggestion for a corrupted Payment row.
    
    Rules:
      - If linked AccountTransaction exists, suggest void+create-refund (high confidence)
      - If no linked tx, suggest manual review (flag for curator)
    
    Returns dict with suggestion details.
    """
    suggestion = {
        'payment_id': payment.id,
        'client_name': payment.client_name,
        'amount': float(payment.amount),
        'auto_bill_no': payment.auto_bill_no,
        'action': None,
        'confidence': None,
        'reasoning': '',
        'requires_curator_review': False,
        'linked_tx_count': len(linked_txs),
    }
    
    if linked_txs:
        suggestion['action'] = 'void_and_create_refund'
        suggestion['confidence'] = 'high'
        suggestion['reasoning'] = (
            f'{len(linked_txs)} AccountTransaction(s) found linked to this Payment. '
            'Suggests legitimate refund settlement. Void corrupted row and create '
            'new Payment with auto_bill_no=NULL, method=Refund, preserving linked tx reference.'
        )
        suggestion['linked_tx_ids'] = [tx.id for tx in linked_txs]
    else:
        suggestion['action'] = 'manual_review'
        suggestion['confidence'] = 'low'
        suggestion['reasoning'] = (
            'No linked AccountTransaction found. Manually verify if this is a legitimate '
            'refund (check note field) or orphaned entry. Requires curator review before remediation.'
        )
        suggestion['requires_curator_review'] = True
    
    return suggestion

def export_corrupted_to_json(corrupted_payments, export_dir, timestamp):
    """Export corrupted payments as JSON with full details and linked transactions."""
    export_data = {
        'export_metadata': {
            'timestamp': timestamp,
            'export_type': 'corrupted_negative_scp_payments',
            'total_count': len(corrupted_payments),
            'query': 'Payment.amount < 0 AND Payment.auto_bill_no LIKE "SB-CP-%"',
        },
        'corrupted_payments': [],
    }
    
    for payment in corrupted_payments:
        linked_txs = get_linked_transactions(payment.id)
        payment_dict = payment_to_dict(payment)
        payment_dict['linked_transactions'] = [tx_to_dict(tx) for tx in linked_txs]
        
        suggestion = suggest_remediation(payment, linked_txs)
        payment_dict['remediation_suggestion'] = suggestion
        
        export_data['corrupted_payments'].append(payment_dict)
    
    filename = f'corrupted_payments_{timestamp}.json'
    filepath = export_dir / filename
    
    with open(filepath, 'w') as f:
        json.dump(export_data, f, indent=2, default=str)
    
    return str(filepath)

def export_corrupted_to_csv(corrupted_payments, export_dir, timestamp):
    """Export corrupted payments as CSV with core fields."""
    filename = f'corrupted_payments_{timestamp}.csv'
    filepath = export_dir / filename
    
    with open(filepath, 'w', newline='') as csvfile:
        fieldnames = [
            'payment_id', 'client_name', 'amount', 'auto_bill_no', 'method',
            'date_posted', 'note', 'linked_tx_count', 'suggested_action',
            'action_confidence', 'requires_curator_review'
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for payment in corrupted_payments:
            linked_txs = get_linked_transactions(payment.id)
            suggestion = suggest_remediation(payment, linked_txs)
            
            row = {
                'payment_id': payment.id,
                'client_name': payment.client_name,
                'amount': float(payment.amount),
                'auto_bill_no': payment.auto_bill_no or '',
                'method': payment.method or '',
                'date_posted': payment.date_posted.isoformat() if payment.date_posted else '',
                'note': (payment.note or '')[:100],  # Truncate for CSV
                'linked_tx_count': len(linked_txs),
                'suggested_action': suggestion['action'],
                'action_confidence': suggestion['confidence'],
                'requires_curator_review': suggestion['requires_curator_review'],
            }
            writer.writerow(row)
    
    return str(filepath)

def export_remediation_suggestions(corrupted_payments, export_dir, timestamp):
    """Export remediation suggestions grouped by action and client."""
    suggestions_data = {
        'export_metadata': {
            'timestamp': timestamp,
            'total_corrupted_rows': len(corrupted_payments),
        },
        'by_action': {
            'void_and_create_refund': [],
            'manual_review': [],
        },
        'by_client': {},
    }
    
    for payment in corrupted_payments:
        linked_txs = get_linked_transactions(payment.id)
        suggestion = suggest_remediation(payment, linked_txs)
        
        # Group by action
        action = suggestion['action']
        suggestions_data['by_action'][action].append(suggestion)
        
        # Group by client
        client = payment.client_name
        if client not in suggestions_data['by_client']:
            suggestions_data['by_client'][client] = []
        suggestions_data['by_client'][client].append(suggestion)
    
    filename = f'remediation_suggestions_{timestamp}.json'
    filepath = export_dir / filename
    
    with open(filepath, 'w') as f:
        json.dump(suggestions_data, f, indent=2, default=str)
    
    return str(filepath)

def export_manifest(export_dir, timestamp, exported_files):
    """Create manifest file listing all exports."""
    manifest_data = {
        'timestamp': timestamp,
        'export_timestamp_iso': datetime.now().isoformat(),
        'files': exported_files,
        'instructions': {
            'phase_1': 'Exports created. Data ready for analysis.',
            'phase_2': 'Run remediation_dryrun_script.py to validate proposed actions.',
            'phase_3': 'Review dry-run report before executing actual remediation.',
            'safety': 'NO database writes during export or dry-run phases.',
        },
    }
    
    filename = f'export_manifest_{timestamp}.txt'
    filepath = export_dir / filename
    
    with open(filepath, 'w') as f:
        f.write('CORRUPTED PAYMENT REMEDIATION EXPORT MANIFEST\n')
        f.write('=' * 70 + '\n\n')
        f.write(f'Export timestamp: {manifest_data["export_timestamp_iso"]}\n')
        f.write(f'Total corrupted rows: {len(exported_files)}\n\n')
        f.write('FILES:\n')
        for file in exported_files:
            f.write(f'  - {Path(file).name}\n')
        f.write('\n\nINSTRUCTIONS:\n')
        for phase, desc in manifest_data['instructions'].items():
            if phase != 'safety':
                f.write(f'  {phase.upper()}: {desc}\n')
        f.write(f'\n  SAFETY: {manifest_data["instructions"]["safety"]}\n')
        f.write('\nAll data read-only. No writes during export/analysis.\n')
    
    return str(filepath)

def main():
    """Main export orchestration."""
    print('\n' + '='*70)
    print('CORRUPTED PAYMENT REMEDIATION PREPARATION - EXPORT PHASE')
    print('='*70 + '\n')
    
    export_dir = ensure_export_dir()
    timestamp = get_timestamp()
    
    print(f'[1/4] Querying corrupted negative SB-CP payments...')
    corrupted = query_corrupted_payments()
    print(f'      Found {len(corrupted)} corrupted rows')
    
    print(f'\n[2/4] Exporting to JSON...')
    json_file = export_corrupted_to_json(corrupted, export_dir, timestamp)
    print(f'      -> {json_file}')
    
    print(f'\n[3/4] Exporting to CSV...')
    csv_file = export_corrupted_to_csv(corrupted, export_dir, timestamp)
    print(f'      -> {csv_file}')
    
    print(f'\n[4/4] Exporting remediation suggestions...')
    suggestions_file = export_remediation_suggestions(corrupted, export_dir, timestamp)
    print(f'      -> {suggestions_file}')
    
    exported = [json_file, csv_file, suggestions_file]
    manifest_file = export_manifest(export_dir, timestamp, exported)
    print(f'      -> {manifest_file}')
    
    print(f'\n' + '='*70)
    print(f'EXPORT COMPLETE - {len(corrupted)} corrupted rows')
    print(f'Location: {export_dir}')
    print('='*70)
    
    print(f'\nNext step: Run scripts/remediation_dryrun_script.py')
    print('           to validate proposed actions before execution.\n')

if __name__ == '__main__':
    main()
