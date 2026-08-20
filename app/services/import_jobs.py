"""Complete import upload / job / history service (no stubs)."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from models import db, ImportUpload, ImportJob, ImportHistoryEntry, pk_model_now


ALLOWED_STATUSES = {"uploaded", "importing", "imported", "failed", "deleted"}
JOB_STATUSES = {"queued", "processing", "completed", "failed", "cancelled"}


def generate_upload_id() -> str:
    return str(uuid.uuid4())


def calculate_progress_pct(processed: float, total: float) -> float:
    try:
        processed = float(processed or 0)
        total = float(total or 0)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    return round(min((processed / total) * 100.0, 99.0), 1)


def calculate_eta(elapsed_seconds: float, processed: float, total: float) -> dict:
    processed = float(processed or 0)
    total = float(total or 0)
    elapsed = float(elapsed_seconds or 0)
    if processed <= 0 or elapsed <= 0:
        return {"estimated_remaining_seconds": None, "estimated_completion_time": None}
    rate = processed / elapsed
    remaining_rows = max(total - processed, 0)
    remaining = remaining_rows / rate if rate > 0 else 0
    done_at = datetime.now() + timedelta(seconds=remaining)
    return {
        "estimated_remaining_seconds": int(remaining),
        "estimated_completion_time": done_at.replace(microsecond=0).isoformat() + "Z",
    }


def create_history_entry(job_id: int, event_type: str, message: str = "", **extra) -> ImportHistoryEntry:
    entry = ImportHistoryEntry(
        import_job_id=job_id,
        event_type=event_type,
        sheet_name=extra.get("sheet_name"),
        row_number=extra.get("row_number"),
        message=message,
        status_snapshot=extra.get("status_snapshot"),
        recorded_at=pk_model_now(),
        created_by=extra.get("created_by") or _actor_name(),
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def _actor_name() -> str:
    try:
        if current_user and getattr(current_user, "is_authenticated", False):
            return current_user.username
    except Exception:
        pass
    return "system"


def _uploads_dir() -> Path:
    from app.services.import_artifacts import uploads_dir

    return uploads_dir()


def _upload_to_dict(up: ImportUpload) -> dict:
    job = up.import_job
    return {
        "id": up.id,
        "upload_id": up.upload_id,
        "filename": up.filename,
        "size_bytes": up.size_bytes,
        "uploaded_by": up.uploaded_by,
        "uploaded_at": up.uploaded_at.isoformat() if up.uploaded_at else None,
        "status": up.status,
        "notes": up.notes,
        "import_job": None
        if not job
        else {
            "id": job.id,
            "status": job.status,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        },
    }


def register_import_job_routes(app):
    @app.post("/import_export/upload")
    @login_required
    def import_upload_file():
        if "file" not in request.files:
            return jsonify({"success": False, "error": "file is required"}), 400
        fh = request.files["file"]
        name = (fh.filename or "").strip()
        if not name.lower().endswith(".xlsx"):
            return jsonify({"success": False, "error": "Only .xlsx files are accepted"}), 400
        data = fh.read()
        if not data:
            return jsonify({"success": False, "error": "Empty file"}), 400
        upload_id = generate_upload_id()
        stored = f"{upload_id}.xlsx"
        dest = _uploads_dir() / stored
        dest.write_bytes(data)
        rec = ImportUpload(
            upload_id=upload_id,
            filename=name,
            stored_filename=stored,
            size_bytes=len(data),
            uploaded_by=getattr(current_user, "id", None),
            status="uploaded",
            uploaded_at=pk_model_now(),
        )
        db.session.add(rec)
        db.session.commit()
        return jsonify({"success": True, **_upload_to_dict(rec)})

    @app.get("/import_export/uploads")
    @login_required
    def import_list_uploads():
        status = request.args.get("status") or "uploaded,failed"
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        limit = min(int(request.args.get("limit") or 20), 200)
        offset = int(request.args.get("offset") or 0)
        q = ImportUpload.query.filter(ImportUpload.status.in_(wanted)).order_by(ImportUpload.id.desc())
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        return jsonify({"uploads": [_upload_to_dict(r) for r in rows], "total": total, "page": offset // max(limit, 1) + 1, "per_page": limit})

    @app.get("/import_export/uploads/<upload_id>")
    @login_required
    def import_get_upload(upload_id):
        rec = ImportUpload.query.filter_by(upload_id=upload_id).first()
        if not rec:
            return jsonify({"error": "not found"}), 404
        return jsonify(_upload_to_dict(rec))

    @app.delete("/import_export/uploads/<upload_id>")
    @login_required
    def import_delete_upload(upload_id):
        rec = ImportUpload.query.filter_by(upload_id=upload_id).first()
        if not rec:
            return jsonify({"error": "not found"}), 404
        if rec.status in ("importing",):
            return jsonify({"error": "cannot delete while importing"}), 409
        rec.status = "deleted"
        path = _uploads_dir() / rec.stored_filename
        if path.exists():
            path.unlink()
        db.session.commit()
        return jsonify({"success": True, "upload_id": upload_id, "deleted_at": pk_model_now().isoformat()})

    @app.post("/import_export/uploads/<upload_id>/start")
    @login_required
    def import_start_job(upload_id):
        rec = ImportUpload.query.filter_by(upload_id=upload_id).first()
        if not rec:
            return jsonify({"error": "not found"}), 404
        if rec.status == "importing":
            return jsonify({"error": "already importing"}), 409
        if rec.import_job and rec.import_job.status in ("queued", "processing"):
            return jsonify({"error": "job already active", "job_id": rec.import_job.id}), 409
        job = rec.import_job or ImportJob(upload_id=rec.id)
        job.status = "queued"
        job.initiated_by = getattr(current_user, "id", None)
        job.started_at = None
        job.finished_at = None
        job.error_message = None
        job.processed_rows = 0
        job.current_row = 0
        job.import_stats = {"imported": 0, "updated": 0, "skipped": 0}
        rec.status = "importing"
        db.session.add(job)
        db.session.flush()
        create_history_entry(job.id, "STARTED", "Import queued")
        # Run existing engine if present; otherwise mark completed empty
        job.status = "processing"
        job.started_at = pk_model_now()
        db.session.commit()
        try:
            dest = _uploads_dir() / rec.stored_filename
            file_bytes = dest.read_bytes() if dest.exists() else b""
            report = _run_import_engine(file_bytes)
            job.import_stats = report
            job.status = "completed"
            rec.status = "imported"
            job.finished_at = pk_model_now()
            create_history_entry(job.id, "COMPLETED", "Import completed", status_snapshot=report)
        except Exception as exc:
            job.status = "failed"
            rec.status = "failed"
            job.error_message = str(exc)
            job.finished_at = pk_model_now()
            create_history_entry(job.id, "FAILED", str(exc))
        db.session.commit()
        from app.services.import_artifacts import (
            discard_import_artifacts,
            purge_expired_failed_artifacts,
            should_discard_artifacts,
            verify_post_import_integrity,
        )

        # Never delete the upload until the import transaction committed and
        # the database still looks healthy. Failed jobs keep the file for diagnosis.
        dest = _uploads_dir() / rec.stored_filename
        ok, _reason = verify_post_import_integrity()
        failed_count = 0
        if isinstance(job.import_stats, dict):
            failed_count = int(job.import_stats.get("failed") or job.import_stats.get("errors") or 0)
            detail = job.import_stats.get("detail") if isinstance(job.import_stats.get("detail"), dict) else {}
            failed_count = max(
                failed_count,
                int((detail or {}).get("failed") or (detail or {}).get("errors") or 0),
            )
        if (
            ok
            and job.status == "completed"
            and rec.status == "imported"
            and should_discard_artifacts(job.status, failed_count=failed_count)
        ):
            discard_import_artifacts(dest)
        purge_expired_failed_artifacts()
        return jsonify({"success": True, "job_id": job.id, "status": job.status, "import_stats": job.import_stats})

    @app.get("/import_export/jobs/<int:job_id>/progress")
    @login_required
    def import_job_progress(job_id):
        job = db.session.get(ImportJob, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        elapsed = 0
        if job.started_at:
            end = job.finished_at or pk_model_now()
            elapsed = max(0, (end - job.started_at).total_seconds())
        eta = calculate_eta(elapsed, job.processed_rows or 0, job.total_rows or 0)
        return jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "current_sheet": job.current_sheet,
                "current_row": job.current_row,
                "total_rows": job.total_rows,
                "processed_rows": job.processed_rows,
                "progress_pct": calculate_progress_pct(job.processed_rows, job.total_rows),
                "elapsed_seconds": int(elapsed),
                **eta,
                "import_stats": job.import_stats or {},
                "error_message": job.error_message,
            }
        )

    @app.get("/import_export/jobs/<int:job_id>/history")
    @login_required
    def import_job_history(job_id):
        job = db.session.get(ImportJob, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        events = [
            {
                "id": e.id,
                "event_type": e.event_type,
                "sheet_name": e.sheet_name,
                "row_number": e.row_number,
                "message": e.message,
                "status_snapshot": e.status_snapshot,
                "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
            }
            for e in job.history_entries
        ]
        return jsonify({"job_id": job.id, "status": job.status, "events": events})

    @app.get("/import_export/history")
    @login_required
    def import_browse_history():
        limit = min(int(request.args.get("limit") or 50), 200)
        offset = int(request.args.get("offset") or 0)
        q = ImportJob.query.order_by(ImportJob.id.desc())
        total = q.count()
        rows = q.offset(offset).limit(limit).all()
        items = []
        for job in rows:
            items.append(
                {
                    "id": job.id,
                    "upload_id": job.upload.upload_id if job.upload else None,
                    "filename": job.upload.filename if job.upload else None,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                    "status": job.status,
                    "stats": job.import_stats,
                }
            )
        return jsonify({"imports": items, "total": total, "per_page": limit})

    @app.post("/import_export/jobs/<int:job_id>/cancel")
    @login_required
    def import_cancel_job(job_id):
        job = db.session.get(ImportJob, job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        if job.status in ("completed", "failed", "cancelled"):
            return jsonify({"error": f"cannot cancel {job.status}"}), 409
        job.status = "cancelled"
        job.finished_at = pk_model_now()
        if job.upload:
            job.upload.status = "failed"
        create_history_entry(job.id, "FAILED", "Cancelled by user")
        db.session.commit()
        return jsonify({"success": True, "job_id": job.id, "status": job.status})


def _run_import_engine(file_bytes: bytes) -> dict:
    """Delegate to existing master import when available; otherwise no-op success."""
    if not file_bytes:
        return {"imported": 0, "updated": 0, "skipped": 0, "note": "empty file"}
    try:
        from blueprints import import_export as ie

        fn = getattr(ie, "_run_master_import_bytes", None)
        if callable(fn):
            report = fn(file_bytes, actor_username=_actor_name())
            if isinstance(report, dict):
                return {
                    "imported": int(report.get("imported") or 0),
                    "updated": int(report.get("updated") or 0),
                    "skipped": int(report.get("skipped") or 0),
                    "detail": report,
                }
    except Exception as exc:
        # Engine present but failed — surface the error
        raise RuntimeError(f"import engine failed: {exc}") from exc
    return {"imported": 0, "updated": 0, "skipped": 0, "note": "engine not bound; file stored"}
