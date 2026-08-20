"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/app_upgrade/start', methods=['POST'])
@login_required
def app_upgrade_start():
    if not APP_UPGRADE_ENABLED:
        return jsonify({'error': 'Not Found'}), 404
    if current_user.role not in ['admin', 'root']:
        return jsonify({'error': 'Forbidden'}), 403
    file = request.files.get('file')
    if not file or not file.filename.lower().endswith('.zip'):
        return jsonify({'error': 'Please upload a .zip file.'}), 400

    run_migrate = str(request.form.get('run_migrate', '')).lower() in ['1', 'true', 'on', 'yes']
    require_data_upgrade = str(request.form.get('require_data_upgrade', '')).lower() in ['1', 'true', 'on', 'yes']
    job_id = uuid.uuid4().hex
    username = getattr(current_user, 'username', None)
    tenant_id = getattr(current_user, 'tenant_id', None)
    role = getattr(current_user, 'role', None)
    _set_deploy_progress(job_id, percent=0, message='Queued...', done=False, success=False, user=username)
    flask_app = current_app._get_current_object()
    t = threading.Thread(
        target=_deploy_release_worker,
        args=(flask_app, job_id, file.read(), run_migrate, require_data_upgrade, username, tenant_id, role),
        daemon=True,
        name=f"deploy-worker-{job_id[:8]}",
    )
    t.start()
    return jsonify({'job_id': job_id})

