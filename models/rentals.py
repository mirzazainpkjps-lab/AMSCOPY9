from .__base import *  # noqa
from .helpers import *  # noqa

class FBMRentalItem(db.Model):
    __tablename__ = 'fbm_rental_item'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    opening_qty = db.Column(db.Integer, default=0)
    available_qty = db.Column(db.Integer, default=0)
    rent_per_day = db.Column(db.Float, default=0)
    is_void = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now)

    rentals = db.relationship('FBMRental', backref='item', lazy=True)

    @property
    def rented_qty(self):
        return sum(r.qty for r in self.rentals if r.status == 'active')

    @property
    def status(self):
        if self.is_void:
            return 'void'
        if self.available_qty <= 0:
            return 'out of stock'
        return 'active'


class FBMClient(db.Model):
    __tablename__ = 'fbm_client'
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    address = db.Column(db.String(250))
    phone = db.Column(db.String(50))
    identity_card = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now)

    rentals = db.relationship('FBMRental', backref='client', lazy=True)


class FBMRental(db.Model):
    __tablename__ = 'fbm_rental'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('fbm_client.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('fbm_rental_item.id'), nullable=False)
    qty = db.Column(db.Integer, default=1)
    rent_per_unit = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, default=0)
    qty_returned = db.Column(db.Integer, default=0)
    paid_amount = db.Column(db.Float, default=0)
    discount_amount = db.Column(db.Float, default=0)
    start_datetime = db.Column(db.DateTime, default=pk_model_now, index=True)
    return_datetime = db.Column(db.DateTime, nullable=True, index=True)
    status = db.Column(db.String(20), default='active')
    payment_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now)

    payment_account = db.relationship('Account', foreign_keys=[payment_account_id], backref='fbm_payments')

    @property
    def remaining_qty(self):
        return max(0, (self.qty or 0) - (self.qty_returned or 0))

    @property
    def balance_due(self):
        due = (self.total_amount or 0) - (self.paid_amount or 0) - (self.discount_amount or 0)
        return round(max(0.0, due), 2)

    def days_used(self, at_time=None):
        if not self.start_datetime:
            return 0
        end = at_time or datetime.now()
        total_seconds = max(0.0, (end - self.start_datetime).total_seconds())
        days = math.ceil(total_seconds / 86400)
        return max(1, days)

    def charge_for_qty(self, qty, at_time=None):
        return round(self.days_used(at_time) * (self.rent_per_unit or 0) * (qty or 0), 2)

    def current_estimated_amount(self):
        if self.status != 'active':
            return round(self.total_amount or 0, 2)
        return round(self.days_used() * (self.rent_per_unit or 0) * (self.remaining_qty or 0), 2)

