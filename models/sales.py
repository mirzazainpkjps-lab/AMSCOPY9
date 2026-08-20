from .__base import *  # noqa
from .helpers import *  # noqa

class PendingBill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_code = db.Column(db.String(50))
    client_name = db.Column(db.String(100))
    bill_no = db.Column(db.String(50))
    bill_kind = db.Column(db.String(10), default='UNKNOWN', index=True)
    nimbus_no = db.Column(db.String(50))
    amount = db.Column(db.Float, default=0)
    reason = db.Column(db.String(200))
    photo_url = db.Column(db.String(200))
    photo_path = db.Column(db.String(200))
    is_paid = db.Column(db.Boolean, default=False)
    is_cash = db.Column(db.Boolean, default=False)
    is_manual = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.String(50))
    created_by = db.Column(db.String(80))
    is_void = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(500))
    risk_override = db.Column(db.String(20))
    source_module = db.Column(db.String(50), index=True)
    source_table = db.Column(db.String(50), index=True)
    source_id = db.Column(db.Integer, index=True)
    source_bill_no = db.Column(db.String(50), index=True)
    transaction_type = db.Column(db.String(50), index=True)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(100))
    amount = db.Column(db.Float, default=0)
    paid_amount = db.Column(db.Float, default=0)
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    photo_path = db.Column(db.String(200))
    photo_url = db.Column(db.String(500))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    items = db.relationship('BookingItem', backref='booking', lazy=True, cascade='all, delete-orphan')
    is_void = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(500))
    discount = db.Column(db.Float, default=0)
    discount_reason = db.Column(db.String(200))
    receive_in_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    receive_in_account = db.relationship('Account', foreign_keys=[receive_in_account_id], backref='booking_advances')


class BookingItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False)
    material_name = db.Column(db.String(100))
    qty = db.Column(db.Float, default=0)
    price_at_time = db.Column(db.Float, default=0)


class BookingAllocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('direct_sale.id'), nullable=False, index=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('direct_sale_item.id'), nullable=False, index=True)
    booking_item_id = db.Column(db.Integer, db.ForeignKey('booking_item.id'), nullable=False, index=True)
    qty = db.Column(db.Float, default=0)
    is_void = db.Column(db.Boolean, default=False)

    sale = db.relationship('DirectSale', backref=db.backref('booking_allocations', lazy=True))
    sale_item = db.relationship('DirectSaleItem', backref=db.backref('booking_allocations', lazy=True))
    booking_item = db.relationship('BookingItem', backref=db.backref('allocations', lazy=True))


class BookingAllocationRepairArchive(db.Model):
    """Immutable evidence retained when a derived booking allocation is removed.

    This intentionally has no foreign keys: its purpose is to preserve the
    original identifiers and available parent snapshots even when a referenced
    legacy parent no longer exists.
    """
    id = db.Column(db.Integer, primary_key=True)
    original_allocation_id = db.Column(db.Integer, nullable=False, index=True)
    sale_id = db.Column(db.Integer, nullable=False)
    sale_item_id = db.Column(db.Integer, nullable=False)
    booking_item_id = db.Column(db.Integer, nullable=False)
    qty = db.Column(db.Float, default=0)
    was_void = db.Column(db.Boolean, default=False)
    violations = db.Column(db.String(200), nullable=False)
    repair_reason = db.Column(db.String(500), nullable=False)
    repair_run_id = db.Column(db.String(64), nullable=False, index=True)
    source_row_json = db.Column(db.Text, nullable=False)
    sale_snapshot_json = db.Column(db.Text)
    sale_item_snapshot_json = db.Column(db.Text)
    booking_item_snapshot_json = db.Column(db.Text)
    booking_snapshot_json = db.Column(db.Text)
    archived_at = db.Column(db.DateTime, default=pk_model_now, nullable=False, index=True)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # ``client_name`` is retained as an immutable historical display snapshot;
    # ``client_id`` supplies stable identity for new and backfilled records.
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True, index=True)
    client_name = db.Column(db.String(100))
    amount = db.Column(db.Float, default=0)
    amount_minor = db.Column(db.BigInteger, nullable=True)  # authoritative paisa/cents
    method = db.Column(db.String(50))
    payment_type = db.Column(db.String(30), default='Receipt', index=True)  # Receipt | Refund | Material Return | Waive-Off
    source_type = db.Column(db.String(50), nullable=True, index=True)
    source_id = db.Column(db.Integer, nullable=True, index=True)
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    photo_path = db.Column(db.String(200))
    photo_url = db.Column(db.String(500))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    is_void = db.Column(db.Boolean, default=False, index=True)
    note = db.Column(db.String(500))
    discount = db.Column(db.Float, default=0)
    discount_minor = db.Column(db.BigInteger, nullable=True)
    discount_reason = db.Column(db.String(200))
    bank_name = db.Column(db.String(100))
    account_name = db.Column(db.String(100))
    account_no = db.Column(db.String(50))
    payment_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    idempotency_key = db.Column(db.String(64), nullable=True, unique=True, index=True)
    revision = db.Column(db.Integer, default=1, nullable=True)
    created_by = db.Column(db.String(80))
    updated_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now, index=True)

    client = db.relationship('Client', foreign_keys=[client_id], backref='payment_records')
    payment_account = db.relationship('Account', foreign_keys=[payment_account_id], backref='client_payments')
    __mapper_args__ = {'version_id_col': revision}


class WaiveOff(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('payment.id'), nullable=True, index=True)
    client_code = db.Column(db.String(50), index=True)
    client_name = db.Column(db.String(100), index=True)
    bill_no = db.Column(db.String(50), index=True)
    amount = db.Column(db.Float, default=0)
    reason = db.Column(db.String(300))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    created_by = db.Column(db.String(80))
    note = db.Column(db.String(500))
    is_void = db.Column(db.Boolean, default=False, index=True)

    payment = db.relationship('Payment', backref=db.backref('waive_off_rows', lazy=True))


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_code = db.Column(db.String(50))
    client_name = db.Column(db.String(100))
    invoice_no = db.Column(db.String(50))
    is_manual = db.Column(db.Boolean, default=False)
    date = db.Column(db.Date)
    total_amount = db.Column(db.Float, default=0)
    balance = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='OPEN')
    is_cash = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.String(50))
    created_by = db.Column(db.String(80))
    is_void = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(500))

    entries = db.relationship('Entry', backref='invoice', lazy=True)
    direct_sales = db.relationship('DirectSale', backref='invoice', lazy=True)


class BillCounter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    namespace = db.Column(db.String(12), default='GEN', index=True, nullable=False)
    count = db.Column(db.Integer, default=1000)


class DirectSale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    idempotency_key = db.Column(db.String(64), nullable=True, index=True)
    client_name = db.Column(db.String(100))
    client_code = db.Column(db.String(50), index=True)
    category = db.Column(db.String(50))
    amount = db.Column(db.Float, default=0)
    paid_amount = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    discount_reason = db.Column(db.String(200))
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    photo_path = db.Column(db.String(200))
    photo_url = db.Column(db.String(500))
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=True)
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    items = db.relationship('DirectSaleItem', backref='direct_sale', lazy=True, cascade='all, delete-orphan')
    is_void = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(500))
    driver_name = db.Column(db.String(100))
    rent_item_revenue = db.Column(db.Float, default=0)
    delivery_rent_cost = db.Column(db.Float, default=0)
    rent_variance_loss = db.Column(db.Float, default=0)
    # Payment method fields
    payment_method = db.Column(db.String(50))
    payment_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    bank_name = db.Column(db.String(100))
    account_name = db.Column(db.String(100))
    account_no = db.Column(db.String(50))

    payment_account = db.relationship('Account', foreign_keys=[payment_account_id], backref='direct_sale_payments')


class DirectSaleDraft(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_code = db.Column(db.String(50))
    client_name = db.Column(db.String(100))
    manual_client_name = db.Column(db.String(100))
    category = db.Column(db.String(50))
    driver_name = db.Column(db.String(100))
    manual_bill_no = db.Column(db.String(50))
    item_count = db.Column(db.Integer, default=0)
    total_qty = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    payload = db.Column(db.Text)
    created_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, index=True)


class DirectSaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('direct_sale.id'), nullable=False)
    product_name = db.Column(db.String(100))
    qty = db.Column(db.Float, default=0)
    price_at_time = db.Column(db.Float, default=0)
    grn_item_id = db.Column(db.Integer, db.ForeignKey('grn_item.id'), nullable=True)  # Link to specific GRN item
    cost_rate_at_sale = db.Column(db.Float, nullable=True)  # Frozen FIFO cost at post time

    grn_item = db.relationship('GRNItem', backref='sale_items')


class GRNAllocation(db.Model):
    """FIFO lot consumption: cash/credit sale line -> oldest remaining GRN item."""
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('direct_sale.id'), nullable=False, index=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('direct_sale_item.id'), nullable=False, index=True)
    grn_item_id = db.Column(db.Integer, db.ForeignKey('grn_item.id'), nullable=False, index=True)
    qty = db.Column(db.Float, default=0)
    cost_rate = db.Column(db.Float, default=0)
    is_void = db.Column(db.Boolean, default=False, index=True)

    sale = db.relationship('DirectSale', backref=db.backref('grn_allocations', lazy=True))
    sale_item = db.relationship('DirectSaleItem', backref=db.backref('grn_allocations', lazy=True))
    grn_item = db.relationship('GRNItem', backref=db.backref('allocations', lazy=True))


class MaterialReturn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(100))
    return_type = db.Column(db.String(20), default='normal')  # normal | booked
    amount = db.Column(db.Float, default=0)
    manual_bill_no = db.Column(db.String(50))
    auto_bill_no = db.Column(db.String(50))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    note = db.Column(db.String(500))
    payment_id = db.Column(db.Integer, db.ForeignKey('payment.id'), nullable=True, index=True)
    is_void = db.Column(db.Boolean, default=False, index=True)
    items = db.relationship('MaterialReturnItem', backref='material_return', lazy=True, cascade='all, delete-orphan')

    payment = db.relationship('Payment')


class MaterialReturnItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    material_return_id = db.Column(db.Integer, db.ForeignKey('material_return.id'), nullable=False, index=True)
    material_name = db.Column(db.String(100))
    qty = db.Column(db.Float, default=0)
    unit_rate = db.Column(db.Float, default=0)
    rent_rate = db.Column(db.Float, default=0)
    price_at_time = db.Column(db.Float, default=0)

