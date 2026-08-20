"""
Cash Flow Reconciliation Helpers
Physical Cash Available Workflow
"""
from datetime import datetime, timedelta
from models import (
    db,
    CashFlowDifferenceAdjustment,
    CashFlowReconciliationAudit,
    pk_model_now,
)
import logging

logger = logging.getLogger(__name__)


def create_reconciliation(adjustment_date, calculated_closing, physical_cash, reason='', created_by=''):
    """
    Create a new physical cash reconciliation record.
    
    Args:
        adjustment_date: Date of reconciliation
        calculated_closing: System-calculated closing balance
        physical_cash: Physical cash available (user input)
        reason: Explanation for any discrepancy
        created_by: User creating the record
    
    Returns:
        CashFlowDifferenceAdjustment object
    """
    difference = physical_cash - calculated_closing
    
    reconciliation = CashFlowDifferenceAdjustment(
        adjustment_date=adjustment_date,
        calculated_closing=calculated_closing,
        physical_cash_available=physical_cash,
        difference=difference,
        reason=reason,
        created_by=created_by,
        created_at=pk_model_now(),
        edit_count=1,
    )
    db.session.add(reconciliation)
    db.session.flush()  # Get the ID
    
    # Create initial audit record
    audit = CashFlowReconciliationAudit(
        reconciliation_id=reconciliation.id,
        adjustment_date=adjustment_date,
        change_type='CREATE',
        new_physical_cash=physical_cash,
        new_difference=difference,
        new_reason=reason,
        changed_by=created_by,
        changed_at=pk_model_now(),
    )
    db.session.add(audit)
    db.session.commit()
    
    logger.info(f"Created reconciliation for {adjustment_date}: physical={physical_cash}, diff={difference}, by={created_by}")
    return reconciliation


def update_reconciliation(reconciliation_id, physical_cash, reason='', edited_by=''):
    """
    Edit an existing physical cash reconciliation.
    
    Args:
        reconciliation_id: ID of CashFlowDifferenceAdjustment
        physical_cash: New physical cash value
        reason: Updated reason
        edited_by: User editing the record
    
    Returns:
        Updated CashFlowDifferenceAdjustment object
    """
    reconciliation = CashFlowDifferenceAdjustment.query.get(reconciliation_id)
    if not reconciliation:
        raise ValueError(f"Reconciliation {reconciliation_id} not found")
    
    # Store old values for audit
    old_physical = reconciliation.physical_cash_available
    old_difference = reconciliation.difference
    old_reason = reconciliation.reason
    
    # Update values
    new_difference = physical_cash - reconciliation.calculated_closing
    reconciliation.physical_cash_available = physical_cash
    reconciliation.difference = new_difference
    reconciliation.reason = reason
    reconciliation.old_physical_cash = old_physical
    reconciliation.edited_by = edited_by
    reconciliation.edited_date = pk_model_now()
    reconciliation.edit_count = (reconciliation.edit_count or 0) + 1
    reconciliation.updated_at = pk_model_now()
    
    # Create audit record
    audit = CashFlowReconciliationAudit(
        reconciliation_id=reconciliation_id,
        adjustment_date=reconciliation.adjustment_date,
        change_type='EDIT',
        old_physical_cash=old_physical,
        new_physical_cash=physical_cash,
        old_difference=old_difference,
        new_difference=new_difference,
        old_reason=old_reason,
        new_reason=reason,
        changed_by=edited_by,
        changed_at=pk_model_now(),
    )
    db.session.add(audit)
    db.session.commit()
    
    logger.info(f"Updated reconciliation {reconciliation_id}: old_physical={old_physical}, new_physical={physical_cash}, by={edited_by}")
    return reconciliation


def delete_reconciliation(reconciliation_id, deleted_by=''):
    """
    Remove active reconciliation values while retaining the record and audit history.
    
    Args:
        reconciliation_id: ID of CashFlowDifferenceAdjustment
        deleted_by: User deleting the record
    """
    reconciliation = CashFlowDifferenceAdjustment.query.get(reconciliation_id)
    if not reconciliation:
        raise ValueError(f"Reconciliation {reconciliation_id} not found")
    
    # Create audit record
    audit = CashFlowReconciliationAudit(
        reconciliation_id=reconciliation_id,
        adjustment_date=reconciliation.adjustment_date,
        change_type='DELETE',
        old_physical_cash=reconciliation.physical_cash_available,
        old_difference=reconciliation.difference,
        old_reason=reconciliation.reason,
        changed_by=deleted_by,
        changed_at=pk_model_now(),
    )
    db.session.add(audit)
    
    reconciliation.old_physical_cash = reconciliation.physical_cash_available
    reconciliation.physical_cash_available = None
    reconciliation.calculated_closing = None
    reconciliation.difference = None
    reconciliation.amount = 0
    reconciliation.reason = None
    reconciliation.note = 'Reconciliation removed; audit trail retained.'
    reconciliation.edited_by = deleted_by
    reconciliation.edited_date = pk_model_now()
    reconciliation.edit_count = (reconciliation.edit_count or 0) + 1
    db.session.commit()
    
    logger.info(f"Deleted reconciliation {reconciliation_id}, by={deleted_by}")


def get_reconciliation_history(adjustment_date):
    """
    Get all changes to a reconciliation.
    
    Returns:
        List of CashFlowReconciliationAudit records, ordered by change_at
    """
    return CashFlowReconciliationAudit.query.filter_by(
        adjustment_date=adjustment_date
    ).order_by(CashFlowReconciliationAudit.changed_at.asc()).all()


def migrate_legacy_record(reconciliation):
    """
    Convert a legacy (old workflow) record to new workflow.
    Only works if physical_cash_available is still NULL.
    
    Args:
        reconciliation: CashFlowDifferenceAdjustment with old data
    
    Returns:
        True if migrated, False if not applicable
    """
    if reconciliation.physical_cash_available is not None:
        # Already migrated or created with new workflow
        return False
    
    if reconciliation.amount is None:
        # No difference to work from
        return False
    
    # Create migration audit record (mark as MIGRATE, not CREATE)
    audit = CashFlowReconciliationAudit(
        reconciliation_id=reconciliation.id,
        adjustment_date=reconciliation.adjustment_date,
        change_type='MIGRATE',
        new_difference=reconciliation.amount,
        new_reason=reconciliation.note,
        changed_by='system',
        changed_at=pk_model_now(),
    )
    db.session.add(audit)
    db.session.commit()
    
    logger.info(f"Migrated legacy record {reconciliation.id} for date {reconciliation.adjustment_date}")
    return True
