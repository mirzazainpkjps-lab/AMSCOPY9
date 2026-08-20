from .__base import *  # noqa
from .helpers import *  # noqa

class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20))
    time = db.Column(db.String(20))
    type = db.Column(db.String(10))
    material = db.Column(db.String(100))
    client = db.Column(db.String(100))
    client_code = db.Column(db.String(50))
    client_category = db.Column(db.String(50))
    qty = db.Column(db.Float, default=0)
    bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    nimbus_no = db.Column(db.String(50))
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=True)
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    is_void = db.Column(db.Boolean, default=False)
    transaction_category = db.Column(db.String(50))
    driver_name = db.Column(db.String(100))
    note = db.Column(db.String(500))
    booked_material = db.Column(db.String(100))
    is_alternate = db.Column(db.Boolean, default=False)
    source_module = db.Column(db.String(50), index=True)
    source_table = db.Column(db.String(50), index=True)
    source_id = db.Column(db.Integer, index=True)
    source_bill_no = db.Column(db.String(50), index=True)
    transaction_type = db.Column(db.String(50), index=True)


class GRN(db.Model):
    """Goods Receipt Note - for stock receiving"""
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=True)
    supplier = db.Column(db.String(100))
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    photo_path = db.Column(db.String(200))
    photo_url = db.Column(db.String(500))
    loading_cost = db.Column(db.Float, default=0)
    freight_cost = db.Column(db.Float, default=0)
    other_expense = db.Column(db.Float, default=0)
    adjustment_amount = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    paid_amount = db.Column(db.Float, default=0)
    payment_type = db.Column(db.String(50))
    payment_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    tax_percent = db.Column(db.Float, default=0)
    tax_amount = db.Column(db.Float, default=0)
    tax_type = db.Column(db.String(50))
    bank_name = db.Column(db.String(100))
    account_name = db.Column(db.String(100))
    account_no = db.Column(db.String(50))
    supplier_invoice_no = db.Column(db.String(50))
    due_date = db.Column(db.Date)
    bill_date = db.Column(db.Date)
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    items = db.relationship('GRNItem', backref='grn', lazy=True, cascade='all, delete-orphan')
    supplier_rel = db.relationship('Supplier', backref='grns')
    payment_account = db.relationship('Account', foreign_keys=[payment_account_id], backref='grn_payments')
    is_void = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(500))


class GRNItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    grn_id = db.Column(db.Integer, db.ForeignKey('grn.id'), nullable=False)
    mat_name = db.Column(db.String(100))
    qty = db.Column(db.Float, default=0)
    price_at_time = db.Column(db.Float, default=0)
    is_void = db.Column(db.Boolean, default=False, index=True)
    is_locked = db.Column(db.Boolean, default=False, index=True)  # True while consumed by an active sale


class Delivery(db.Model):
    """Delivery records for dispatching"""
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(100))
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    photo_path = db.Column(db.String(200))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    items = db.relationship('DeliveryItem', backref='delivery', lazy=True, cascade='all, delete-orphan')


class DeliveryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.Integer, db.ForeignKey('delivery.id'), nullable=False)
    product = db.Column(db.String(100))
    qty = db.Column(db.Float, default=0)

