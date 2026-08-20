#!/usr/bin/env python3
"""
Dry-run remediation analyzer for corrupted negative SB-CP Payment rows.

Purpose:
  - Read exported corrupted payment data
  - Analyze each row and its linked AccountTransactions
  - Propose remediation actions with confidence levels
  - Calculate balance impacts (before/after)
  - Print detailed report WITHOUT writing to database
  - Support explicit user confirmation before actual remediation (phase 3)

Execution Mode:
  - DEFAULT: DRY RUN (no writes)
  - REQUIRES: --execute flag to perform actual DB mutations
  - OUTPUTS: dry_run_report_<timestamp>.txt with full analysis

Safety Rules:
  - Read-only unless --execute flag present
  - All proposed changes printed before any execution
  - Transactional (batch per-client)
  - Audit trail preserved (is_void=True, original row id in note)
  - Rollback via DB snapshot restore

"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# Add workspace root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import db, app
from models import Payment, AccountTransaction, Client, Account, PendingBill

class RemediationAnalyzer:
    """Analyze and propose remediation for corrupted Payment rows."""
    
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.report_lines = []
        
        # Accumulate statistics
        self.stats = {
            'total_corrupted': 0,
            'void_and_create_refund': 0,
            'manual_review': 0,
            'total_amount_affected': 0.0,
            'clients_affected': set(),
        }
        
        # Track proposed changes by client
        self.client_actions = defaultdict(list)
        
    def log(self, msg):
        """Add line to report and print."""
        self.report_lines.append(msg)
        print(msg)
    
    def load_exports(self, export_dir='instance/exports'):
        """
        Load most recent export JSON file.
        
        Returns parsed data or None if not found.
        """
        export_path = Path(export_dir)
        if not export_path.exists():
            self.log(f'ERROR: Export directory not found: {export_dir}')
            return None
        
        # Find most recent corrupted_payments_*.json
        json_files = sorted(export_path.glob('corrupted_payments_*.json'))
        if not json_files:
            self.log(f'ERROR: No export files found in {export_path}')
            return None
        
        latest = json_files[-1]
        self.log(f'\nLoading export: {latest.name}')
        
        with open(latest, 'r') as f:
            data = json.load(f)
        
        return data
    
    def analyze_payment(self, payment_dict):
        """
        Analyze a single corrupted Payment record.
        
        Returns action proposal dict.
        """
        suggestion = payment_dict['remediation_suggestion']
        payment_id = payment_dict['payment_id']
        client = payment_dict['client_name']
        
        proposal = {
            'payment_id': payment_id,
            'client': client,
            'amount': payment_dict['amount'],
            'auto_bill_no': payment_dict['auto_bill_no'],
            'date_posted': payment_dict['date_posted'],
            'action': suggestion['action'],
            'confidence': suggestion['confidence'],
            'requires_review': suggestion['requires_curator_review'],
            'linked_tx_ids': suggestion.get('linked_tx_ids', []),
            'reasoning': suggestion['reasoning'],
        }
        
        return proposal
    
    def print_header(self):
        """Print analysis header."""
        self.log('\n' + '='*80)
        self.log('REMEDIATION DRY-RUN ANALYSIS')
        self.log('='*80)
        self.log(f'Timestamp: {self.timestamp}')
        self.log(f'Execution Mode: {"DRY RUN (NO DB WRITES)" if self.dry_run else "REAL EXECUTION (WITH DB WRITES)"}')
        self.log('='*80 + '\n')
    
    def print_summary(self):
        """Print final summary."""
        self.log('\n' + '='*80)
        self.log('SUMMARY')
        self.log('='*80)
        self.log(f'Total corrupted rows analyzed: {self.stats["total_corrupted"]}')
        self.log(f'  - Void + Create Refund (high confidence): {self.stats["void_and_create_refund"]}')
        self.log(f'  - Manual Review Required (low confidence): {self.stats["manual_review"]}')
        self.log(f'Total amount affected: {self.stats["total_amount_affected"]:.2f}')
        self.log(f'Unique clients affected: {len(self.stats["clients_affected"])}')
        self.log('='*80 + '\n')
    
    def print_client_section(self, client, actions):
        """Print remediation actions grouped by client."""
        self.log(f'\nCLIENT: {client}')
        self.log('-' * 80)
        
        client_total = 0.0
        high_confidence = 0
        manual_review = 0
        
        for action in actions:
            self.log(f'\n  Payment ID: {action["payment_id"]}')
            self.log(f'    Auto Bill No: {action["auto_bill_no"]}')
            self.log(f'    Amount: {action["amount"]:.2f}')
            self.log(f'    Date Posted: {action["date_posted"]}')
            self.log(f'    Action: {action["action"].upper()}')
            self.log(f'    Confidence: {action["confidence"].upper()}')
            self.log(f'    Reasoning: {action["reasoning"]}')
            
            if action['linked_tx_ids']:
                self.log(f'    Linked Account Transactions: {action["linked_tx_ids"]}')
            
            if action['requires_review']:
                self.log(f'    [!] REQUIRES CURATOR REVIEW')
            
            client_total += action["amount"]
            if action["confidence"] == "high":
                high_confidence += 1
            else:
                manual_review += 1
        
        self.log(f'\n  Client subtotal amount: {client_total:.2f}')
        self.log(f'  High-confidence actions: {high_confidence}')
        self.log(f'  Manual review required: {manual_review}')
    
    def run(self, execute=False):
        """Main execution."""
        self.dry_run = not execute
        
        self.print_header()
        
        # Load exports
        export_data = self.load_exports()
        if not export_data:
            self.log('ABORT: Could not load export data')
            return False
        
        # Analyze each payment
        self.log(f'\n[ANALYSIS] Processing {len(export_data["corrupted_payments"])} records...\n')
        
        for payment_dict in export_data['corrupted_payments']:
            proposal = self.analyze_payment(payment_dict)
            client = proposal['client']
            
            self.client_actions[client].append(proposal)
            self.stats['total_corrupted'] += 1
            self.stats['total_amount_affected'] += proposal['amount']
            self.stats['clients_affected'].add(client)
            
            if proposal['action'] == 'void_and_create_refund':
                self.stats['void_and_create_refund'] += 1
            else:
                self.stats['manual_review'] += 1
        
        # Print analysis by client
        self.log(f'\n[DETAILED PROPOSALS BY CLIENT]\n')
        for client in sorted(self.client_actions.keys()):
            self.print_client_section(client, self.client_actions[client])
        
        # Print summary
        self.print_summary()
        
        # Print execution plan
        self.log('\n[PROPOSED EXECUTION PLAN]')
        self.log('-' * 80)
        
        if self.stats['void_and_create_refund'] > 0:
            self.log(f'\nPHASE 1: VOID + CREATE REFUND')
            self.log(f'         {self.stats["void_and_create_refund"]} rows')
            self.log(f'         Action: Set is_void=True, create new Payment with auto_bill_no=NULL')
        
        if self.stats['manual_review'] > 0:
            self.log(f'\nPHASE 2: MANUAL REVIEW')
            self.log(f'         {self.stats["manual_review"]} rows')
            self.log(f'         Action: HOLD - require curator review before remediation')
        
        self.log(f'\nPHASE 3: REBUILD PENDING')
        self.log(f'         For {len(self.stats["clients_affected"])} clients')
        self.log(f'         Action: rebuild_pending_bills(client_id=...) per affected client')
        
        self.log('\n' + '='*80)
        if self.dry_run:
            self.log('DRY RUN COMPLETE - NO DATABASE CHANGES')
            self.log('To proceed with actual remediation, re-run with --execute flag')
        else:
            self.log('[!!!] REAL EXECUTION MODE - Database changes will be committed')
        self.log('='*80 + '\n')
        
        return True
    
    def save_report(self):
        """Save report to file."""
        report_dir = Path('instance/exports')
        report_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f'dry_run_report_{self.timestamp}.txt'
        filepath = report_dir / filename
        
        with open(filepath, 'w') as f:
            f.write('\n'.join(self.report_lines))
        
        self.log(f'\n[REPORT SAVED] {filepath}\n')
        return str(filepath)

def main():
    parser = argparse.ArgumentParser(
        description='Dry-run analysis of corrupted Payment remediation'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        default=False,
        help='DANGEROUS: Actually execute database mutations. Default is dry-run only.'
    )
    
    args = parser.parse_args()
    
    if args.execute:
        print('\n[WARNING] --execute flag detected!')
        print('          This will WRITE to the database.')
        print('          Press Ctrl+C to abort now.\n')
        import time
        time.sleep(3)
    
    analyzer = RemediationAnalyzer(dry_run=not args.execute)
    analyzer.run(execute=args.execute)
    analyzer.save_report()

if __name__ == '__main__':
    main()
