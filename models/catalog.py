from .__base import *  # noqa
from .helpers import *  # noqa

class Material(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('material_category.id'), index=True, nullable=True)
    unit_price = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    unit = db.Column(db.String(20), default='Bags')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    category = db.relationship('MaterialCategory', backref=db.backref('materials', lazy=True))


class MaterialCategory(db.Model):
    __tablename__ = 'material_category'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

