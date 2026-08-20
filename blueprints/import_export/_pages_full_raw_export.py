"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/full_raw_export')
@login_required
def full_raw_export():
    try:
        scope_ctx = _resolve_scope_context(
            scope_raw=request.args.get('scope'),
            tenant_id_raw=request.args.get('tenant_id'),
        )
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('import_export.import_export_page'))
    content = _build_full_raw_export_bytes(scope_ctx=scope_ctx)
    try:
        audit_log(current_user, 'export.full_raw', f'scope={scope_ctx.get("scope")} bytes={len(content or b"")}')
    except Exception:
        pass
    _archive_artifact_bytes(content, _download_filename('ALLEXPORT', 'xlsx'), kind='exports')
    output = io.BytesIO(content)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=_download_filename('ALLEXPORT', 'xlsx'),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

