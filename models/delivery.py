from .__base import *  # noqa
from .helpers import *  # noqa

class DeliveryRent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('direct_sale.id'), nullable=True, index=True)
    delivery_person_name = db.Column(db.String(100), nullable=False, index=True)
    bill_no = db.Column(db.String(50), index=True)
    amount = db.Column(db.Float, default=0)
    note = db.Column(db.String(500))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    created_by = db.Column(db.String(80))
    is_void = db.Column(db.Boolean, default=False, index=True)

    sale = db.relationship('DirectSale', backref=db.backref('delivery_rents', lazy=True))


class SaleDeliveryPerson(db.Model):
    __tablename__ = 'sale_delivery_persons'
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('direct_sale.id'), nullable=False, index=True)
    delivery_person_id = db.Column(db.Integer, db.ForeignKey('delivery_person.id'), nullable=False, index=True)
    bags_delivered = db.Column(db.Float, default=0)
    rent_amount = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    is_void = db.Column(db.Boolean, default=False, index=True)

    sale = db.relationship('DirectSale', backref=db.backref('delivery_person_allocations', lazy=True))
    delivery_person = db.relationship('DeliveryPerson')


class DeliveryPersonPayment(db.Model):
    """Source document for a payment/waive-off to a delivery person (driver).

    This row records *who / why / against which rent allocation*.  The single
    authoritative financial movement for the cash part lives in
    ``AccountTransaction`` (``transaction_type='Driver Payment'``) and is kept
    in sync 1:1 by ``_sync_delivery_person_payment_accounting``.  The driver
    ledger and the account ledger therefore project the same event instead of
    maintaining two independent balances.
    """
    id = db.Column(db.Integer, primary_key=True)
    delivery_person_id = db.Column(db.Integer, db.ForeignKey('delivery_person.id'), nullable=False, index=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('direct_sale.id'), nullable=True, index=True)
    allocation_id = db.Column(db.Integer, db.ForeignKey('sale_delivery_persons.id'), nullable=True, index=True)
    amount_paid = db.Column(db.Float, default=0)
    amount_paid_minor = db.Column(db.BigInteger, nullable=True)  # authoritative paisa/cents
    waive_off_amount = db.Column(db.Float, default=0)
    waive_off_minor = db.Column(db.BigInteger, nullable=True)  # authoritative paisa/cents
    # Explicit source of funds.  Never assume cash: the selected account is the
    # account that receives the MONEY OUT effect.  Nullable so that legacy rows
    # and waive-off-only settlements (no cash movement) remain valid.
    payment_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True, index=True)
    method = db.Column(db.String(50))
    reference = db.Column(db.String(50), index=True)
    note = db.Column(db.String(500))
    date_posted = db.Column(db.DateTime, default=pk_model_now, index=True)
    idempotency_key = db.Column(db.String(64), nullable=True, unique=True, index=True)
    revision = db.Column(db.Integer, default=1, nullable=True)
    created_by = db.Column(db.String(80))
    updated_by = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now, index=True)
    is_void = db.Column(db.Boolean, default=False, index=True)

    delivery_person = db.relationship('DeliveryPerson')
    sale = db.relationship('DirectSale')
    allocation = db.relationship('SaleDeliveryPerson')
    payment_account = db.relationship('Account', foreign_keys=[payment_account_id],
                                      backref='delivery_person_payments')

