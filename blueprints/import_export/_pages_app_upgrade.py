"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/app_upgrade', methods=['GET', 'POST'])
@login_required
def app_upgrade():
    if not APP_UPGRADE_ENABLED:
        return "Not Found", 404
    if current_user.role not in ['admin', 'root']:
        return "Forbidden", 403
    base_dir, current_path, wsgi_reload_path, history_path = _deploy_paths()
    os.makedirs(base_dir, exist_ok=True)

    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename.lower().endswith('.zip'):
            flash('Please upload a .zip file.', 'danger')
            return redirect(url_for('import_export.app_upgrade'))
        run_migrate = str(request.form.get('run_migrate', '')).lower() in ['1', 'true', 'on', 'yes']
        require_data_upgrade = str(request.form.get('require_data_upgrade', '')).lower() in ['1', 'true', 'on', 'yes']
        result = _deploy_release_bytes(
            file.read(),
            run_migrate=run_migrate,
            require_data_upgrade=require_data_upgrade,
            actor_username=getattr(current_user, 'username', None),
        )
        if result.get('ok'):
            sql_report = result.get('sql_report')
            if sql_report:
                flash(
                    f"Upgrade complete. SQL applied: {sql_report.get('applied', 0)} / {sql_report.get('files', 0)}.",
                    'success'
                )
            else:
                flash('Upgrade complete. Reloaded app.' if result.get('reloaded') else 'Upgrade complete. Please reload the app.', 'success')
        else:
            flash(f"Upgrade failed: {result.get('error')}", 'danger')
        return redirect(url_for('import_export.app_upgrade'))

    history = _load_deploy_history(history_path)
    releases = []
    try:
        for name in sorted(os.listdir(base_dir), reverse=True):
            path = os.path.join(base_dir, name)
            if os.path.isdir(path):
                releases.append({'name': name, 'path': path})
    except Exception:
        releases = []

    current_target = None
    if os.path.islink(current_path):
        try:
            current_target = os.readlink(current_path)
        except Exception:
            current_target = None

    migrations_dir = _get_migrations_dir()
    migration_files = _list_sql_migrations(migrations_dir)
    data_upgrade_queue_dir, _ = _get_data_upgrade_queue_dir()
    queue_files = _list_data_upgrade_excels(data_upgrade_queue_dir)
    queue_files = [p for p in queue_files if '\\processed\\' not in p.replace('/', '\\')]
    return render_template(
        'app_upgrade.html',
        base_dir=base_dir,
        current_path=current_path,
        current_target=current_target,
        wsgi_reload_path=wsgi_reload_path,
        history=history,
        releases=releases,
        migrations_dir=migrations_dir,
        migration_files=migration_files,
        data_upgrade_queue_dir=data_upgrade_queue_dir,
        data_upgrade_queue_count=len(queue_files),
    )

