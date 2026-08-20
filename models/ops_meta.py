from .__base import *  # noqa
from .helpers import *  # noqa

class TenantWipeBackupHistory(db.Model):
    """
    Legacy multi-tenant table retained in some historical DB backups.
    In single-store mode, the application does not use tenant wipe history.
    """
    __tablename__ = 'tenant_wipe_backup_history'
    id = db.Column(db.Integer, primary_key=True)
    tenant_name = db.Column(db.String(120), nullable=True)
    performed_by = db.Column(db.String(80), nullable=True)
    targets = db.Column(db.String(1000), nullable=True)
    backup_filename = db.Column(db.String(255), nullable=True)
    backup_path = db.Column(db.String(1000), nullable=True)
    wipe_status = db.Column(db.String(20), default='pending', index=True)
    note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class RootBackupSettings(db.Model):
    __tablename__ = 'root_backup_settings'
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    frequency = db.Column(db.String(20), default='hourly', nullable=False)
    recipient_emails = db.Column(db.String(1000), nullable=True)
    include_full_raw_xlsx = db.Column(db.Boolean, default=True, nullable=False)
    include_sqlite_db = db.Column(db.Boolean, default=True, nullable=False)
    subject_prefix = db.Column(db.String(120), default='PWARE Root Backup', nullable=False)
    keep_history_count = db.Column(db.Integer, default=200, nullable=False)
    last_sent_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(20), nullable=True)
    last_message = db.Column(db.String(500), nullable=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now, index=True)


class RootBackupEmailHistory(db.Model):
    __tablename__ = 'root_backup_email_history'
    id = db.Column(db.Integer, primary_key=True)
    trigger_type = db.Column(db.String(30), default='auto', nullable=False, index=True)
    status = db.Column(db.String(20), default='failed', nullable=False, index=True)
    recipient_emails = db.Column(db.String(1000), nullable=True)
    subject = db.Column(db.String(300), nullable=True)
    attachment_name = db.Column(db.String(255), nullable=True)
    attachment_size_kb = db.Column(db.Integer, nullable=True)
    backup_path = db.Column(db.String(1000), nullable=True)
    message = db.Column(db.String(1000), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class StaffEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(200), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class FollowUpReminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pending_bill_id = db.Column(db.Integer, db.ForeignKey('pending_bill.id'), nullable=False)
    remind_at = db.Column(db.DateTime, nullable=False)
    note = db.Column(db.String(500))
    is_done = db.Column(db.Boolean, default=False)
    alerted_at = db.Column(db.DateTime)
    acknowledged_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    pending_bill = db.relationship('PendingBill', backref=db.backref('reminders', lazy=True, cascade='all, delete-orphan'))


class FollowUpContact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pending_bill_id = db.Column(db.Integer, db.ForeignKey('pending_bill.id'), nullable=False)
    reminder_id = db.Column(db.Integer, db.ForeignKey('follow_up_reminder.id'))
    contacted_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    channel = db.Column(db.String(30), default='Call')
    response = db.Column(db.String(200))
    note = db.Column(db.String(500))
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    pending_bill = db.relationship('PendingBill', backref=db.backref('contact_logs', lazy=True, cascade='all, delete-orphan'))
    reminder = db.relationship('FollowUpReminder', backref=db.backref('closure_contact_logs', lazy=True))


class ReconBasket(db.Model):
    """Reconciliation Basket for Data Lab"""
    id = db.Column(db.Integer, primary_key=True)
    bill_no = db.Column(db.String(50))
    inv_date = db.Column(db.Date)
    inv_client = db.Column(db.String(100))
    fin_client = db.Column(db.String(100))
    inv_material = db.Column(db.String(100))
    inv_qty = db.Column(db.Float, default=0)
    status = db.Column(db.String(20))
    match_score = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

