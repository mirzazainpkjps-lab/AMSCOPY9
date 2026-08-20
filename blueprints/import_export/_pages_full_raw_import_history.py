"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/full_raw_import_history', methods=['GET', 'POST'])
@login_required
def full_raw_import_history():
    if not current_app.config.get('LOGIN_DISABLED') and getattr(current_user, 'role', None) not in ['admin', 'root']:
        return "Forbidden", 403
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        selected = request.form.getlist('report')
        report_dir = _get_full_raw_report_dir()
        if action == 'delete_selected':
            removed = 0
            for name in selected:
                safe_name = os.path.basename(name)
                if safe_name != name or not safe_name.endswith('.csv'):
                    continue
                path = os.path.join(report_dir, safe_name)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        meta_path = path.replace('.csv', '.meta.json')
                        if os.path.exists(meta_path):
                            os.remove(meta_path)
                        removed += 1
                    except Exception:
                        pass
            flash(f"Removed {removed} report(s).", 'info')
        elif action == 'delete_all':
            removed = 0
            if os.path.exists(report_dir):
                for name in os.listdir(report_dir):
                    if not name.endswith('.csv'):
                        continue
                    path = os.path.join(report_dir, name)
                    try:
                        os.remove(path)
                        meta_path = path.replace('.csv', '.meta.json')
                        if os.path.exists(meta_path):
                            os.remove(meta_path)
                        removed += 1
                    except Exception:
                        pass
            flash(f"Removed {removed} report(s).", 'info')
        return redirect(url_for('import_export.full_raw_import_history'))
    reports = _list_full_raw_reports()
    return render_template('full_raw_import_history.html', reports=reports)

