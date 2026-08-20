"""wipe — split from misc.py."""
from ._common import *  # noqa

@bp.route('/data_wipe_preview', methods=['POST'])
@login_required
def data_wipe_preview():
    if current_user.role not in ['admin', 'root']:
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 403
    payload = request.get_json(silent=True) or {}
    targets = payload.get('delete_targets') or request.form.getlist('delete_targets')
    return jsonify({'ok': True, **_wipe_preview_for_targets(targets)})

