"""import helpers."""
from ._common import *  # noqa

def _deploy_paths():
    home = os.path.expanduser('~')
    base_dir = current_app.config.get('DEPLOY_BASE_DIR', os.path.join(home, 'releases'))
    current_path = current_app.config.get('DEPLOY_CURRENT_PATH', os.path.join(home, 'app_current'))
    wsgi_reload_path = current_app.config.get('WSGI_RELOAD_PATH')
    history_path = current_app.config.get(
        'DEPLOY_HISTORY_PATH',
        os.path.join(current_app.instance_path, 'deploy_history.json')
    )
    return base_dir, current_path, wsgi_reload_path, history_path


def _scan_release_zip(zip_path):
    forbidden_prefixes = [
        '.git/', 'instance/', '__pycache__/', '.local/', '.virtualenvs/', '.venv/', 'venv/',
    ]
    forbidden_suffixes = [
        '.db', '.sqlite', '.sqlite3', '.log', '.pyc', '.pyo',
    ]
    forbidden_exact = {
        'errorlog.txt',
    }
    allowed_data_suffixes = ('.xlsx', '.xls')
    blocked = []
    payload_files = []
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for info in zf.infolist():
            name = (info.filename or '').replace('\\', '/').lstrip('./')
            if not name or info.is_dir():
                continue
            low = name.lower()
            if low in forbidden_exact:
                blocked.append(name)
                continue
            if any(low.startswith(p) for p in forbidden_prefixes):
                blocked.append(name)
                continue
            if any(low.endswith(s) for s in forbidden_suffixes):
                blocked.append(name)
                continue
            if low.startswith('data_upgrade/') and low.endswith(allowed_data_suffixes):
                payload_files.append(name)
    return {
        'blocked': sorted(set(blocked)),
        'payload_files': sorted(set(payload_files)),
    }


def _validate_release_zip(zip_path, require_data_upgrade=False):
    scan = _scan_release_zip(zip_path)
    blocked = scan.get('blocked') or []
    if blocked:
        preview = ", ".join(blocked[:8])
        more = f" (+{len(blocked) - 8} more)" if len(blocked) > 8 else ""
        raise ValueError(
            "Release ZIP contains forbidden files/folders "
            f"(e.g., {preview}{more}). Remove DB/log/git/instance/cache artifacts and retry."
        )
    if require_data_upgrade and not (scan.get('payload_files') or []):
        raise ValueError(
            "Release ZIP has no /data_upgrade/*.xlsx payload. "
            "Either include payload files or disable 'require data payload'."
        )
    return scan


def _run_app_migrate_steps():
    from app.services.schema import _bootstrap_database
    _bootstrap_database()
    return _run_sql_migrations()


def _run_post_upgrade_integrity_checks(before_counts):
    row = db.session.execute(text("PRAGMA integrity_check")).fetchone()
    if not row:
        raise ValueError("Integrity check failed: PRAGMA integrity_check returned no result.")
    integrity_text = str(row[0]).strip().lower()
    if integrity_text != 'ok':
        raise ValueError(f"Integrity check failed: {row[0]}")

    fk_rows = db.session.execute(text("PRAGMA foreign_key_check")).fetchall()
    if fk_rows:
        raise ValueError(f"Foreign key check failed with {len(fk_rows)} violation(s).")

    after = _capture_guard_counts()
    must_not_be_zero = ['tenant', 'user']
    for key in must_not_be_zero:
        if after.get(key, 0) <= 0:
            raise ValueError(f"Integrity guard failed: '{key}' table is empty after upgrade.")

    for key, before_val in (before_counts or {}).items():
        if before_val > 0 and after.get(key, 0) == 0:
            raise ValueError(
                f"Integrity guard failed: '{key}' count dropped from {before_val} to 0."
            )

    return {
        'before': before_counts or {},
        'after': after,
        'integrity_check': row[0],
        'foreign_key_violations': len(fk_rows),
    }


def _ensure_data_upgrade_ledger():
    db.session.execute(text(
        "CREATE TABLE IF NOT EXISTS data_upgrade_applied ("
        "file_sha256 TEXT PRIMARY KEY, "
        "file_name TEXT, "
        "source TEXT, "
        "applied_at TEXT, "
        "report_json TEXT"
        ")"
    ))
    db.session.commit()


def _is_data_upgrade_applied(file_sha256):
    q = text("SELECT 1 FROM data_upgrade_applied WHERE file_sha256 = :h LIMIT 1")
    row = db.session.execute(q, {'h': file_sha256}).fetchone()
    return row is not None


def _mark_data_upgrade_applied(file_sha256, file_name, source, report):
    db.session.execute(
        text(
            "INSERT OR IGNORE INTO data_upgrade_applied "
            "(file_sha256, file_name, source, applied_at, report_json) "
            "VALUES (:h, :n, :s, :t, :r)"
        ),
        {
            'h': file_sha256,
            'n': file_name,
            's': source,
            't': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
            'r': json.dumps(report or {}, ensure_ascii=True),
        },
    )
    db.session.commit()


def _get_migrations_dir():
    return current_app.config.get('MIGRATIONS_DIR', os.path.join(current_app.root_path, 'migrations'))


def _list_sql_migrations(migrations_dir):
    if not os.path.isdir(migrations_dir):
        return []
    files = [f for f in os.listdir(migrations_dir) if f.lower().endswith('.sql')]
    files.sort()
    return files


def _get_applied_migrations(conn):
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS migration_history (filename TEXT PRIMARY KEY, applied_at TEXT)"
    )
    cur.execute("SELECT filename FROM migration_history")
    rows = cur.fetchall()
    return {r[0] for r in rows}


@import_export_bp.route('/app_upgrade/rollback', methods=['POST'])
@login_required
def app_upgrade_rollback():
    if not APP_UPGRADE_ENABLED:
        return "Not Found", 404
    if current_user.role not in ['admin', 'root']:
        return "Forbidden", 403
    base_dir, current_path, wsgi_reload_path, history_path = _deploy_paths()
    target = request.form.get('release')
    if not target:
        flash('Select a release to rollback.', 'warning')
        return redirect(url_for('import_export.app_upgrade'))

    release_dir = os.path.join(base_dir, target)
    if not os.path.isdir(release_dir):
        flash('Invalid release selected.', 'danger')
        return redirect(url_for('import_export.app_upgrade'))

    if not (os.path.islink(current_path) or not os.path.exists(current_path)):
        flash('Current path is not a symlink. Configure WSGI to use DEPLOY_CURRENT_PATH.', 'danger')
        return redirect(url_for('import_export.app_upgrade'))

    backup_name = _create_full_raw_backup()
    if os.path.islink(current_path):
        os.unlink(current_path)
    os.symlink(release_dir, current_path)

    if wsgi_reload_path and os.path.exists(wsgi_reload_path):
        os.utime(wsgi_reload_path, None)
        reloaded = True
    else:
        reloaded = False

    entry = {
        'timestamp': pk_now().strftime('%Y%m%d_%H%M%S'),
        'action': 'rollback',
        'release_dir': release_dir,
        'current_path': current_path,
        'reloaded': reloaded,
        'user': getattr(current_user, 'username', None),
        'backup_name': backup_name,
    }
    _append_deploy_history(history_path, entry)

    flash('Rollback complete. Reloaded app.' if reloaded else 'Rollback complete. Please reload the app.', 'success')
    return redirect(url_for('import_export.app_upgrade'))


@import_export_bp.route('/app_upgrade/migrate', methods=['POST'])
@login_required
def app_upgrade_migrate():
    if not APP_UPGRADE_ENABLED:
        return "Not Found", 404
    if current_user.role not in ['admin', 'root']:
        return "Forbidden", 403
    _, _, wsgi_reload_path, history_path = _deploy_paths()
    stamp = pk_now().strftime('%Y%m%d_%H%M%S')
    try:
        backup_name = _create_full_raw_backup()
        sql_report = _run_app_migrate_steps()
        if wsgi_reload_path and os.path.exists(wsgi_reload_path):
            os.utime(wsgi_reload_path, None)
            reloaded = True
        else:
            reloaded = False

        entry = {
            'timestamp': stamp,
            'action': 'migrate',
            'reloaded': reloaded,
            'user': getattr(current_user, 'username', None),
            'bootstrap_ok': True,
            'sql_migrations': sql_report,
            'backup_name': backup_name,
        }
        _append_deploy_history(history_path, entry)

        flash(
            f"Migrate complete. SQL applied: {sql_report['applied']} / {sql_report['files']}.",
            'success'
        )
    except Exception as e:
        entry = {
            'timestamp': stamp,
            'action': 'migrate_failed',
            'error': str(e),
            'user': getattr(current_user, 'username', None),
        }
        _append_deploy_history(history_path, entry)
        flash(f"Migrate failed: {e}", 'danger')
    return redirect(url_for('import_export.app_upgrade'))


