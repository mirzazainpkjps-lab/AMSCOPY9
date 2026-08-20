from .__base import *  # noqa
from .helpers import *  # noqa

class UserLoginSession(db.Model):
    """One browser/IP login. Many rows per user — concurrent LAN managers are allowed."""
    __tablename__ = 'user_login_session'
    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(40), unique=True, index=True, nullable=False)
    user_id = db.Column(db.Integer, index=True, nullable=False)
    username = db.Column(db.String(80), index=True)
    role = db.Column(db.String(20))
    ip = db.Column(db.String(80), index=True)
    user_agent = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    last_seen_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    ended_at = db.Column(db.DateTime, nullable=True, index=True)


class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer)
    username = db.Column(db.String(80), index=True)
    action = db.Column(db.String(200), nullable=False)
    details = db.Column(db.String(1000))
    timestamp = db.Column(db.DateTime, default=pk_model_now, index=True)


class AccountingAuditLog(db.Model):
    """Structured, append-only audit event committed with a financial mutation."""
    __tablename__ = 'accounting_audit_log'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    module = db.Column(db.String(50), nullable=False, index=True)
    action = db.Column(db.String(30), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=True, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    username = db.Column(db.String(80), index=True)
    ip_address = db.Column(db.String(80))
    session_id = db.Column(db.String(80))
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    amount_before_minor = db.Column(db.BigInteger, nullable=True)
    amount_after_minor = db.Column(db.BigInteger, nullable=True)
    account_before_id = db.Column(db.Integer, nullable=True)
    account_after_id = db.Column(db.Integer, nullable=True)
    party_before_id = db.Column(db.Integer, nullable=True)
    party_after_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True, nullable=False)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(200))
    password_plain = db.Column(db.String(200))
    role = db.Column(db.String(20), default='user')
    status = db.Column(db.String(20), default='active')
    can_view_stock = db.Column(db.Boolean, default=True)
    can_view_daily = db.Column(db.Boolean, default=True)
    can_view_history = db.Column(db.Boolean, default=True)
    can_import_export = db.Column(db.Boolean, default=False)
    can_manage_directory = db.Column(db.Boolean, default=False)
    can_view_dashboard = db.Column(db.Boolean, default=True)
    can_manage_grn = db.Column(db.Boolean, default=True)
    can_manage_bookings = db.Column(db.Boolean, default=True)
    can_manage_payments = db.Column(db.Boolean, default=True)
    can_manage_sales = db.Column(db.Boolean, default=True)
    can_view_delivery_rent = db.Column(db.Boolean, default=True)
    can_manage_pending_bills = db.Column(db.Boolean, default=True)
    can_view_reports = db.Column(db.Boolean, default=True)
    can_manage_notifications = db.Column(db.Boolean, default=True)
    can_view_client_ledger = db.Column(db.Boolean, default=True)
    can_view_supplier_ledger = db.Column(db.Boolean, default=True)
    can_view_decision_ledger = db.Column(db.Boolean, default=True)
    can_manage_clients = db.Column(db.Boolean, default=False)
    can_manage_suppliers = db.Column(db.Boolean, default=False)
    can_manage_materials = db.Column(db.Boolean, default=False)
    can_manage_delivery_persons = db.Column(db.Boolean, default=False)
    can_access_settings = db.Column(db.Boolean, default=False)
    can_manage_accounts = db.Column(db.Boolean, default=False)
    can_view_cash_flow = db.Column(db.Boolean, default=False)
    restrict_backdated_edit = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=pk_model_now)


class Settings(db.Model):
    """Application settings"""
    id = db.Column(db.Integer, primary_key=True)
    currency = db.Column(db.String(10), default='PKR')
    company_name = db.Column(db.String(100), default='FAZAL BUILDING MATERIALS')
    company_address = db.Column(db.String(200), default='JALAL PUR SOBTIAN')
    company_phone = db.Column(db.String(50), default='+92302-0000993 +92331-0000993')
    company_email = db.Column(db.String(100))
    tax_rate = db.Column(db.Float, default=0)
    invoice_prefix = db.Column(db.String(10), default='INV-')
    bill_prefix = db.Column(db.String(10), default='#')
    ui_theme = db.Column(db.String(20), default='dark_navy')
    allow_global_negative_stock = db.Column(db.Boolean, default=False, nullable=False)
    google_client_id = db.Column(db.String(500))
    google_client_secret = db.Column(db.String(500))
    google_refresh_token = db.Column(db.String(1000))
    google_access_token = db.Column(db.String(1000))
    google_token_expiry = db.Column(db.String(50))
    google_sender_email = db.Column(db.String(200))


class SchemaVersion(db.Model):
    """Tracks database schema version for upgrades."""
    id = db.Column(db.Integer, primary_key=True)
    version = db.Column(db.Integer, default=1)
    applied_at = db.Column(db.DateTime, default=pk_model_now)


class RootRecoveryCode(db.Model):
    __tablename__ = 'root_recovery_code'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, index=True, default='root')
    code_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    used_at = db.Column(db.DateTime, nullable=True, index=True)
    generated_by = db.Column(db.String(80))
    note = db.Column(db.String(300))


class FutureAccountAuditLog(db.Model):
    """Placeholder model to test registry extensibility."""
    __tablename__ = 'future_account_audit_log'
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class SystemLock(db.Model):
    """System-wide mutex for critical operations (e.g., domain wipes).
    
    Ensures only one process can execute a given critical operation at a time.
    Uses atomic DB constraints and TTL for crash safety.
    """
    __tablename__ = 'system_lock'
    id = db.Column(db.Integer, primary_key=True)
    # Lock name, e.g., 'accounts_domain_wipe'
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    # Lock status: 'locked' or 'unlocked'
    status = db.Column(db.String(20), nullable=False, default='unlocked', index=True)
    # Owner/request ID (for diagnostics and debugging)
    owner = db.Column(db.String(100), nullable=True)
    # Timestamp when lock was acquired (for TTL calculation)
    acquired_at = db.Column(db.DateTime, nullable=True, index=True)
    # TTL in seconds; if now() - acquired_at > ttl_seconds, lock can be reclaimed
    ttl_seconds = db.Column(db.Integer, default=3600)  # 1 hour default
    # Notes for diagnostics
    note = db.Column(db.String(500), nullable=True)

