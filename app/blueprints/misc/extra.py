"""extra — split from misc.py."""
from ._common import *  # noqa

@bp.route('/toggle_bill_paid/<int:id>', methods=['POST'])
@login_required
def toggle_bill_paid(id):
    bill = db.session.get(PendingBill, id)
    if bill:
        bill.is_paid = not bill.is_paid
        is_open_khata_bill = (
            bill.client_code == OPEN_KHATA_CODE or
            (bill.client_name or '').strip().upper() == OPEN_KHATA_NAME
        )
        if bill.is_paid and is_open_khata_bill:
            bill.is_cash = True
        elif (not bill.is_paid) and is_open_khata_bill:
            bill.is_cash = False
        db.session.commit()
        return jsonify({'success': True, 'is_paid': bill.is_paid})
    return jsonify({'success': False}), 404


@bp.route('/export_unpaid_transactions')
@login_required
def export_unpaid_transactions():
    if current_user.role not in ['admin', 'root']:
        flash('Only tenant admin or root can run import/export operations.', 'danger')
        return redirect(url_for('index'))
    """Redirects to the generic export function for unpaid transactions."""
    args = request.args.to_dict()
    args['dataset'] = 'unpaid_transactions'
    return redirect(url_for('import_export.export_data', **args))


@bp.route('/fix_system_issues')
@login_required
def fix_system_issues():
    """Auto-fix common sync issues"""
    if current_user.role != 'admin': return redirect(url_for('index'))

    _rebuild_direct_sale_pending_bills()
    _rebuild_material_totals()
    db.session.commit()

    flash('System issues fixed: direct sale pending bills rebuilt and material stock totals refreshed.', 'success')
    return redirect(url_for('system_report'))


@bp.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    upload_dir = os.path.join(basedir, 'static', 'uploads')
    return send_from_directory(upload_dir, filename)


@bp.route('/change_password', methods=['POST'])
@login_required
def change_password():
    raw_pw = str(request.form.get('password') or '').strip()
    if not raw_pw:
        flash('Password is required', 'danger')
        return redirect(url_for('settings'))
    current_user.password_hash = generate_password_hash(raw_pw)
    current_user.password_plain = None
    db.session.commit()
    flash('Password Updated', 'success')
    return redirect(url_for('settings'))


@bp.route('/delete_all_data', methods=['POST'])
@login_required
def delete_all_data():
    return redirect(url_for('settings'))


@bp.route('/generate_dummy_data')
@login_required
def generate_dummy_data():
    flash('This legacy test-data feature has been permanently removed.', 'warning')
    return redirect(url_for('settings'))


