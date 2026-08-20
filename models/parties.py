from .__base import *  # noqa
from .helpers import *  # noqa

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    address = db.Column(db.String(200))
    category = db.Column(db.String(50), default='General')
    opening_balance = db.Column(db.Float, default=0)
    opening_balance_date = db.Column(db.DateTime, default=pk_model_now, index=True)
    is_active = db.Column(db.Boolean, default=True)
    transferred_to_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    require_manual_invoice = db.Column(db.Boolean, default=False)
    book_no = db.Column(db.String(50))
    financial_page = db.Column(db.String(50))
    cement_page = db.Column(db.String(50))
    steel_page = db.Column(db.String(50))
    financial_book_no = db.Column(db.String(50))
    cement_book_no = db.Column(db.String(50))
    steel_book_no = db.Column(db.String(50))
    location_url = db.Column(db.String(500))
    page_notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    address = db.Column(db.String(200))
    opening_balance = db.Column(db.Float, default=0)
    opening_balance_date = db.Column(db.DateTime, default=pk_model_now, index=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class SupplierPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False, index=True)
    amount = db.Column(db.Float, default=0)
    amount_minor = db.Column(db.BigInteger, nullable=True)  # authoritative paisa/cents
    method = db.Column(db.String(50))
    payment_type = db.Column(db.String(30), default='Payment', index=True)
    source_type = db.Column(db.String(50), nullable=True, index=True)
    source_id = db.Column(db.Integer, nullable=True, index=True)
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    note = db.Column(db.String(500))
    is_void = db.Column(db.Boolean, default=False, index=True)
    bank_name = db.Column(db.String(100))
    account_name = db.Column(db.String(100))
    account_no = db.Column(db.String(50))
    payment_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    idempotency_key = db.Column(db.String(64), nullable=True, unique=True, index=True)
    revision = db.Column(db.Integer, default=1, nullable=True)
    created_by = db.Column(db.String(80))
    updated_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now, index=True)

    supplier = db.relationship('Supplier', backref='payments')
    payment_account = db.relationship('Account', foreign_keys=[payment_account_id], backref='supplier_payments')
    __mapper_args__ = {'version_id_col': revision}


class DeliveryPerson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(30))
    # Opening balance is additive and optional.  Positive means the business
    # owes the delivery person; negative means an advance/prepayment.
    opening_balance = db.Column(db.Float, default=0)
    opening_balance_date = db.Column(db.DateTime, default=pk_model_now, index=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

