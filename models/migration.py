"""Audit-safe records for the controlled legacy-template migration pipeline."""
from .__base import *  # noqa
from .helpers import *  # noqa

class MigrationRun(db.Model):
    __tablename__ = 'migration_run'
    id = db.Column(db.Integer, primary_key=True)
    run_key = db.Column(db.String(36), unique=True, nullable=False, index=True)
    template_type = db.Column(db.String(40), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    source_hash = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default='VALIDATED', index=True)
    mode = db.Column(db.String(20), nullable=False, default='VALIDATION_ONLY')
    summary_json = db.Column(db.Text, default='{}')
    uploaded_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False, index=True)
    imported_at = db.Column(db.DateTime)
    rows = db.relationship('MigrationRow', backref='run', cascade='all, delete-orphan', lazy=True)

class MigrationRow(db.Model):
    __tablename__ = 'migration_row'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('migration_run.id'), nullable=False, index=True)
    source_sheet = db.Column(db.String(80), nullable=False)
    source_row = db.Column(db.Integer, nullable=False)
    legacy_reference = db.Column(db.String(150), index=True)
    row_hash = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default='READY', index=True)
    entity_type = db.Column(db.String(50))
    new_entity_id = db.Column(db.Integer)
    data_json = db.Column(db.Text, nullable=False)
    error_json = db.Column(db.Text, default='[]')
    imported_at = db.Column(db.DateTime)

class MigrationMapping(db.Model):
    __tablename__ = 'migration_mapping'
    id = db.Column(db.Integer, primary_key=True)
    template_type = db.Column(db.String(40), nullable=False, index=True)
    legacy_reference = db.Column(db.String(150), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    run_id = db.Column(db.Integer, db.ForeignKey('migration_run.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    __table_args__ = (db.UniqueConstraint('template_type', 'legacy_reference', name='uq_migration_legacy_mapping'),)
