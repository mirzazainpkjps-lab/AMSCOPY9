"""wipe — split from misc.py."""
from ._common import *  # noqa

@bp.route('/admin/rebuild_erp_consistency', methods=['POST'])
@login_required
def admin_rebuild_erp_consistency():
    if current_user.role != 'admin' and not _user_can('can_access_settings'):
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))
    client_id = _safe_int(request.form.get('client_id'))
    try:
        stats = rebuild_all_erp_consistency(client_id=client_id)
        db.session.add(AuditLog(
            user_id=getattr(current_user, 'id', None),
            action='erp.rebuild_consistency',
            details=json.dumps({'client_id': client_id, 'stats': stats}, default=str)[:1000]
        ))
        db.session.commit()
        flash('ERP consistency rebuild completed.', 'success')
    except Exception as exc:
        db.session.rollback()
        logging.exception('ERP consistency rebuild failed')
        flash(f'ERP consistency rebuild failed: {exc}', 'danger')
    return redirect(request.referrer or url_for('void_audit_page'))

