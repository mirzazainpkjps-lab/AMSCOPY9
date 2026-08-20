"""bills — split from sales.py."""
from ._common import *  # noqa
from app.services.void_rebuild import hard_delete_transaction

@bp.route('/void_transaction/<string:type>/<int:id>', methods=['POST'])
@login_required
def void_transaction(type, id):
    # Legacy URL kept so old forms still work; always hard-deletes.
    return delete_transaction(type, id)


@bp.route('/delete_transaction/<string:type>/<int:id>', methods=['POST'])
@login_required
def delete_transaction(type, id):
    if not _user_can('can_manage_sales') and getattr(current_user, 'role', '') != 'admin':
        flash('Permission denied', 'danger')
        return redirect(request.referrer or url_for('index'))

    try:
        ok = hard_delete_transaction(type, id)
        if ok:
            db.session.add(AuditLog(
                user_id=getattr(current_user, 'id', None),
                action=f'transaction.delete.{type}',
                details=f'id={id}, reason={(request.form.get("reason") or "").strip()}'
            ))
            db.session.commit()
            flash(f'{type} deleted', 'success')
        else:
            flash(f'{type} not found', 'warning')
    except Exception as exc:
        db.session.rollback()
        logging.getLogger(__name__).exception('Hard delete failed')
        flash(f'Unable to delete: {exc}', 'danger')
    return redirect(request.referrer or url_for('index'))
