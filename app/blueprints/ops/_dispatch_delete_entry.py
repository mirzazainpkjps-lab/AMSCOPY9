"""dispatch — split from ops.py."""
from ._common import *  # noqa

@bp.route('/delete_entry/<int:id>', methods=['POST'])
@login_required
def delete_entry(id):
    e = db.session.get(Entry, id)
    if not e:
        return redirect(url_for('index'))

    today_str = pk_today().strftime('%Y-%m-%d')
    if current_user.role != 'admin' and e.date != today_str:
        flash('Permission Denied: Only Admins can delete back-dated records.', 'danger')
        return redirect(url_for('index'))

    changed = _set_entry_void_state(e, True)
    if changed:
        db.session.commit()
        flash('Transaction deleted', 'warning')
    else:
        flash('Transaction already deleted', 'info')
    return redirect(url_for('index'))

