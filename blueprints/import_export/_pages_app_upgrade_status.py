"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/app_upgrade/status/<job_id>', methods=['GET'])
@login_required
def app_upgrade_status(job_id):
    if not APP_UPGRADE_ENABLED:
        return jsonify({'error': 'Not Found'}), 404
    if current_user.role not in ['admin', 'root']:
        return jsonify({'error': 'Forbidden'}), 403
    state = _get_deploy_progress(job_id)
    if not state:
        return jsonify({'error': 'Unknown job id'}), 404
    return jsonify(state)

