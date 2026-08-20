"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/full_raw_import', methods=['POST'])
@login_required
def full_raw_import():
    from blueprints.import_export._pages_transfer_import import _wants_import_json, _import_result_payload

    def _json_error(msg, status=400):
        return jsonify(_import_result_payload(False, msg, {})), status

    # ===== PANDAS DEPENDENCY CHECK =====
    if not _validate_pandas_installed():
        msg = 'CRITICAL: pandas library is not installed. Run: pip install pandas>=2.3.3'
        if _wants_import_json():
            return _json_error(msg, 500)
        flash(msg, 'danger')
        return redirect(url_for('import_export.import_export_page'))
    # ===== END DEPENDENCY CHECK =====
    
    if not _full_raw_import_enabled():
        msg = 'Full raw import is disabled by safety toggle.'
        if _wants_import_json():
            return _json_error(msg, 400)
        flash(msg, 'warning')
        return redirect(url_for('import_export.import_export_page'))

    if current_user.role not in ['admin', 'root']:
        msg = 'Only admin or root can run full raw import.'
        if _wants_import_json():
            return _json_error(msg, 403)
        flash(msg, 'danger')
        return redirect(url_for('import_export.import_export_page'))

    file = request.files.get('file')
    if not file:
        msg = 'No file uploaded for full raw import.'
        if _wants_import_json():
            return _json_error(msg, 400)
        flash(msg, 'danger')
        return redirect(url_for('import_export.import_export_page'))

    mode = (request.form.get('mode') or 'append').strip().lower()
    if mode not in ['append', 'replace_tenant_data']:
        mode = 'append'
    try:
        scope_ctx = _resolve_scope_context(
            scope_raw=request.form.get('scope'),
            tenant_id_raw=request.form.get('tenant_id'),
        )
    except ValueError as e:
        msg = str(e)
        if _wants_import_json():
            return _json_error(msg, 400)
        flash(msg, 'danger')
        return redirect(url_for('import_export.import_export_page'))
    if scope_ctx.get('scope') == 'all_tenants' and mode == 'replace_tenant_data':
        msg = 'Replace mode is blocked for all-tenants scope. Use append mode.'
        if _wants_import_json():
            return _json_error(msg, 400)
        flash(msg, 'danger')
        return redirect(url_for('import_export.import_export_page'))

    try:
        file_bytes = file.read()
        if hasattr(file, 'stream'):
            file.stream.seek(0)
        _archive_artifact_bytes(file_bytes, f"full_raw_import_{file.filename}", kind='imports')
    except Exception as e:
        msg = f'Invalid Excel file: {e}'
        if _wants_import_json():
            return _json_error(msg, 400)
        flash(msg, 'danger')
        return redirect(url_for('import_export.import_export_page'))

    report_name = None
    try:
        report, report_name = _run_full_raw_import_bytes(
            file_bytes=file_bytes,
            scope_ctx=scope_ctx,
            mode=mode,
            source_file_name=file.filename,
        )
        try:
            audit_log(
                current_user,
                'import.full_raw',
                f'mode={mode} inserted={report.get("inserted")} skipped={report.get("skipped")} tables={report.get("tables")}',
            )
        except Exception:
            pass
        failed = int(report.get('failed') or report.get('errors') or 0)
        warnings = int(report.get('warnings') or 0)
        outcome = 'complete with row-level problems' if failed else ('complete with warnings' if warnings else 'complete')
        msg = (
            f"Full raw import {outcome} ({mode}). Inserted: {report.get('inserted', 0)}, "
            f"Updated: {report.get('updated', 0)}, Skipped: {report.get('skipped', 0)}, "
            f"Failed: {failed}, Warnings: {warnings}, Tables: {report.get('tables', 0)}. "
            "Valid rows were saved; rejected or unavailable data is listed in the report."
        )
        if _wants_import_json():
            return jsonify(_import_result_payload(failed == 0, msg, report, {'report_name': report_name}))
        flash(msg, 'warning' if (failed or warnings) else 'success')
    except Exception as e:
        db.session.rollback()
        msg = f'Full raw import failed: {e}'
        if _wants_import_json():
            return jsonify(_import_result_payload(False, msg, {})), 400
        flash(msg, 'danger')

    return redirect(url_for('import_export.import_export_page', full_raw_import_report=report_name))

