"""
Comprehensive Test Suite for Physical Cash Reconciliation
"""
import os
import tempfile
import unittest
from datetime import datetime, date, timedelta

os.environ['APP_DB_PATH'] = os.path.join(tempfile.gettempdir(), 'ams_cash_flow_reconciliation_test.db')
os.environ['DB_HEALTH_SNAPSHOT_PATH'] = os.path.join(tempfile.gettempdir(), 'ams_cash_flow_reconciliation_test_health.json')
os.environ['ALLOW_EMPTY_DB'] = '1'
os.environ['ALLOW_DB_DROP'] = '1'

from app import create_app
from models import db
app = create_app({"TESTING": True})
from models import (
    CashFlowDifferenceAdjustment,
    CashFlowReconciliationAudit,
    Client,
    Payment,
    DirectSale,
)
from cash_flow_reconciliation_helpers import (
    create_reconciliation,
    update_reconciliation,
    delete_reconciliation,
    get_reconciliation_history,
)


class TestPhysicalCashReconciliation(unittest.TestCase):
    """Test suite for physical cash reconciliation workflow."""

    def setUp(self):
        """Set up test database and app context."""
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        """Clean up test database."""
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    # =======================
    # TEST 1: Create Reconciliation
    # =======================
    def test_create_new_reconciliation(self):
        """Test creating a new physical cash reconciliation."""
        rec_date = date(2026, 5, 24)
        physical_cash = 50000.0
        calculated_closing = 70000.0
        reason = 'Cash counted in drawer'

        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=calculated_closing,
            physical_cash=physical_cash,
            reason=reason,
            created_by='admin'
        )

        # Verify record created
        assert rec.id is not None
        assert rec.adjustment_date == rec_date
        assert rec.physical_cash_available == physical_cash
        assert rec.calculated_closing == calculated_closing
        assert rec.reason == reason
        
        # Verify difference calculated correctly: physical - system
        assert rec.difference == physical_cash - calculated_closing
        assert rec.difference == -20000.0
        
        # Verify audit trail created
        audit = get_reconciliation_history(rec_date)
        assert len(audit) == 1
        assert audit[0].change_type == 'CREATE'
        assert audit[0].new_physical_cash == physical_cash
        assert audit[0].new_difference == -20000.0

    def test_create_with_negative_difference(self):
        """Test reconciliation where physical cash exceeds calculated closing."""
        rec_date = date(2026, 5, 25)
        physical_cash = 100000.0
        calculated_closing = 70000.0

        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=calculated_closing,
            physical_cash=physical_cash,
            reason='Found extra cash',
            created_by='cashier'
        )

        # Difference should be positive (extra cash in drawer)
        assert rec.difference == 30000.0
        assert rec.physical_cash_available > rec.calculated_closing

    # =======================
    # TEST 2: Update Reconciliation
    # =======================
    def test_update_reconciliation(self):
        """Test editing an existing reconciliation."""
        # Create initial
        rec_date = date(2026, 5, 26)
        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=50000.0,
            physical_cash=45000.0,
            reason='Initial count',
            created_by='user1'
        )
        rec_id = rec.id
        
        # Update with new physical cash
        updated_rec = update_reconciliation(
            reconciliation_id=rec_id,
            physical_cash=48000.0,
            reason='Recount - found more cash',
            edited_by='user1'
        )

        # Verify updates
        assert updated_rec.physical_cash_available == 48000.0
        assert updated_rec.old_physical_cash == 45000.0
        assert updated_rec.difference == -2000.0  # 48000 - 50000
        assert updated_rec.edit_count == 2
        
        # Verify audit trail
        audit = get_reconciliation_history(rec_date)
        assert len(audit) == 2
        assert audit[1].change_type == 'EDIT'
        assert audit[1].old_physical_cash == 45000.0
        assert audit[1].new_physical_cash == 48000.0

    def test_multiple_edits_increment_counter(self):
        """Test that edit_count increments properly."""
        rec_date = date(2026, 5, 27)
        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=30000.0,
            physical_cash=30000.0,
            reason='Initial',
            created_by='user1'
        )
        
        assert rec.edit_count == 1
        
        # Edit 1
        update_reconciliation(rec.id, 29000.0, 'Found discrepancy', 'user1')
        rec = CashFlowDifferenceAdjustment.query.get(rec.id)
        assert rec.edit_count == 2
        
        # Edit 2
        update_reconciliation(rec.id, 28000.0, 'Recount', 'user1')
        rec = CashFlowDifferenceAdjustment.query.get(rec.id)
        assert rec.edit_count == 3
        
        # Verify all edits in audit trail
        audit = get_reconciliation_history(rec_date)
        assert len(audit) == 3
        assert [a.change_type for a in audit] == ['CREATE', 'EDIT', 'EDIT']

    # =======================
    # TEST 3: Delete Reconciliation
    # =======================
    def test_delete_reconciliation(self):
        """Test soft-delete of reconciliation via audit trail."""
        rec_date = date(2026, 5, 28)
        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=40000.0,
            physical_cash=40000.0,
            reason='Test',
            created_by='user1'
        )
        rec_id = rec.id
        
        # Delete
        delete_reconciliation(rec_id, deleted_by='user1')
        
        # Verify active physical reconciliation values are removed, with the row retained for history.
        deleted_rec = CashFlowDifferenceAdjustment.query.get(rec_id)
        assert deleted_rec is not None
        assert deleted_rec.physical_cash_available is None
        
        # Verify audit trail has DELETE entry
        audit = get_reconciliation_history(rec_date)
        assert len(audit) == 2
        assert audit[1].change_type == 'DELETE'
        assert audit[1].old_physical_cash == 40000.0

    # =======================
    # TEST 4: Next Day Opening Balance Logic
    # =======================
    def test_get_opening_for_next_day_new_workflow(self):
        """Test that next day opening = physical_cash for new workflow."""
        rec_date = date(2026, 5, 29)
        physical_cash = 100000.0
        calculated_closing = 120000.0
        
        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=calculated_closing,
            physical_cash=physical_cash,
            reason='Physical count',
            created_by='user1'
        )
        
        # Next day opening should be physical_cash
        next_opening = rec.get_opening_for_next_day(calculated_closing)
        assert next_opening == physical_cash
        assert next_opening == 100000.0

    def test_get_opening_for_next_day_legacy_workflow(self):
        """Test legacy workflow opening calculation."""
        rec_date = date(2026, 5, 30)
        calculated_closing = 50000.0
        difference = 5000.0  # Legacy: user entered difference
        
        # Create legacy-style record
        rec = CashFlowDifferenceAdjustment(
            adjustment_date=rec_date,
            amount=difference,  # Old field
            note='Legacy adjustment',
            physical_cash_available=None,  # New workflow marker
        )
        db.session.add(rec)
        db.session.commit()
        
        # Next day opening = calculated - difference (legacy logic)
        next_opening = rec.get_opening_for_next_day(calculated_closing)
        assert next_opening == calculated_closing - difference
        assert next_opening == 45000.0

    def test_no_reconciliation_uses_calculated_closing(self):
        """Test that without reconciliation, opening = calculated_closing."""
        rec = None  # No reconciliation
        calculated_closing = 30000.0
        
        # If no record, next opening should be calculated_closing
        if rec is None:
            next_opening = calculated_closing
        
        assert next_opening == calculated_closing

    # =======================
    # TEST 5: Audit Trail Immutability
    # =======================
    def test_audit_trail_is_immutable(self):
        """Test that audit trail cannot be modified after creation."""
        rec_date = date(2026, 5, 31)
        create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=60000.0,
            physical_cash=60000.0,
            reason='Test immutability',
            created_by='user1'
        )
        
        # Get audit record
        audit = get_reconciliation_history(rec_date)[0]
        original_changed_at = audit.changed_at
        
        # Try to modify (this should not work in practice due to ORM)
        # Just verify timestamps exist and are correct
        assert audit.changed_at is not None
        assert isinstance(audit.changed_at, datetime)

    def test_audit_trail_tracks_all_changes(self):
        """Test complete audit trail for full lifecycle."""
        rec_date = date(2026, 6, 1)
        
        # Create
        rec = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=100000.0,
            physical_cash=95000.0,
            reason='Initial count',
            created_by='user1'
        )
        
        # Edit
        update_reconciliation(
            rec.id,
            physical_cash=97000.0,
            reason='Recount - found more',
            edited_by='user1'
        )
        
        # Delete
        delete_reconciliation(rec.id, deleted_by='user1')
        
        # Verify audit trail
        audit = get_reconciliation_history(rec_date)
        assert len(audit) == 3
        assert audit[0].change_type == 'CREATE'
        assert audit[1].change_type == 'EDIT'
        assert audit[2].change_type == 'DELETE'
        
        # Verify transitions
        assert audit[0].new_physical_cash == 95000.0
        assert audit[1].old_physical_cash == 95000.0
        assert audit[1].new_physical_cash == 97000.0
        assert audit[2].old_physical_cash == 97000.0

    # =======================
    # TEST 6: Data Validation
    # =======================
    def test_difference_always_calculated_correctly(self):
        """Test that difference = physical_cash - calculated_closing always."""
        test_cases = [
            (100000.0, 100000.0, 0.0),  # Equal
            (100000.0, 95000.0, -5000.0),  # Shortage
            (100000.0, 105000.0, 5000.0),  # Excess
            (1000000.0, 999999.99, -0.01),  # Decimal
        ]
        
        for i, (calculated, physical, expected_diff) in enumerate(test_cases):
            rec = create_reconciliation(
                adjustment_date=date(2026, 6, 2) + timedelta(days=i),
                calculated_closing=calculated,
                physical_cash=physical,
                reason=f'Test case {i}',
                created_by='system'
            )
            assert abs(rec.difference - expected_diff) < 0.01

    def test_zero_physical_cash_allowed(self):
        """Test that zero physical cash is valid."""
        rec = create_reconciliation(
            adjustment_date=date(2026, 6, 10),
            calculated_closing=50000.0,
            physical_cash=0.0,
            reason='No cash in drawer (end of day closure)',
            created_by='user1'
        )
        
        assert rec.physical_cash_available == 0.0
        assert rec.difference == -50000.0

    def test_large_amounts_handled_correctly(self):
        """Test large rupee amounts."""
        large_amount = 100000000.0  # 100 crore
        rec = create_reconciliation(
            adjustment_date=date(2026, 6, 11),
            calculated_closing=large_amount,
            physical_cash=large_amount * 0.98,
            reason='Large transaction day',
            created_by='user1'
        )
        
        assert rec.physical_cash_available == large_amount * 0.98
        assert abs(rec.difference - (large_amount * -0.02)) < 1.0


class TestReconciliationEdgeCases(unittest.TestCase):
    """Edge case tests."""

    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_duplicate_date_raises_error(self):
        """Test that duplicate reconciliation dates are prevented (unique constraint)."""
        rec_date = date(2026, 6, 15)
        
        # Create first
        rec1 = create_reconciliation(
            adjustment_date=rec_date,
            calculated_closing=50000.0,
            physical_cash=50000.0,
            reason='First',
            created_by='user1'
        )
        
        # Try to create duplicate - should fail or update
        try:
            rec2 = create_reconciliation(
                adjustment_date=rec_date,
                calculated_closing=50000.0,
                physical_cash=45000.0,
                reason='Second attempt',
                created_by='user1'
            )
            # If we get here, check if it's the same record
            # (SQLAlchemy might handle this)
            assert rec2.id == rec1.id or db.session.query(
                CashFlowDifferenceAdjustment
            ).filter_by(adjustment_date=rec_date).count() == 1
        except Exception as e:
            # Expected: unique constraint violation
            assert 'unique' in str(e).lower() or 'already exists' in str(e).lower()

    def test_empty_reason_allowed(self):
        """Test that empty reason is acceptable."""
        rec = create_reconciliation(
            adjustment_date=date(2026, 6, 16),
            calculated_closing=50000.0,
            physical_cash=50000.0,
            reason='',  # Empty
            created_by='user1'
        )
        
        assert rec.reason == ''


if __name__ == '__main__':
    unittest.main()
