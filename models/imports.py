from .__base import *  # noqa
from .helpers import *  # noqa

class ImportUpload(db.Model):
    __tablename__ = 'import_upload'
    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    status = db.Column(db.String(20), default='uploaded', nullable=False, index=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now, nullable=False)

    import_job = db.relationship('ImportJob', back_populates='upload', uselist=False)


class ImportJob(db.Model):
    __tablename__ = 'import_job'
    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey('import_upload.id'), unique=True, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), default='queued', nullable=False, index=True)
    current_sheet = db.Column(db.String(50))
    current_row = db.Column(db.Integer, default=0)
    total_rows = db.Column(db.Integer, default=0)
    processed_rows = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text)
    import_stats = db.Column(db.JSON)
    initiated_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now, nullable=False)

    upload = db.relationship('ImportUpload', back_populates='import_job')
    history_entries = db.relationship('ImportHistoryEntry', back_populates='job', cascade='all, delete-orphan')


class ImportHistoryEntry(db.Model):
    __tablename__ = 'import_history_entry'
    id = db.Column(db.Integer, primary_key=True)
    import_job_id = db.Column(db.Integer, db.ForeignKey('import_job.id'), nullable=False, index=True)
    event_type = db.Column(db.String(50), nullable=False)
    sheet_name = db.Column(db.String(50))
    row_number = db.Column(db.Integer)
    message = db.Column(db.Text)
    status_snapshot = db.Column(db.JSON)
    recorded_at = db.Column(db.DateTime, default=pk_model_now, nullable=False, index=True)
    created_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=pk_model_now, nullable=False)

    job = db.relationship('ImportJob', back_populates='history_entries')

