"""deploy — split from import_export.py."""
from ._common import *  # noqa

def _safe_extract_zip(zip_path, extract_dir, progress_cb=None, percent_start=35, percent_end=52):
    with zipfile.ZipFile(zip_path, 'r') as zf:
        infos = zf.infolist()
        for info in infos:
            name = info.filename
            if name.startswith('/') or name.startswith('\\') or '..' in name.replace('\\', '/').split('/'):
                raise ValueError("Unsafe path in zip file.")

        file_infos = [i for i in infos if not i.is_dir()]
        total = len(file_infos) or 1
        span = max(1, int(percent_end) - int(percent_start))

        for i, info in enumerate(file_infos, start=1):
            zf.extract(info, extract_dir)
            if progress_cb:
                pct = int(percent_start) + int((i / total) * span)
                display_name = os.path.basename(info.filename.rstrip('/\\')) or info.filename
                progress_cb(pct, f"Copying file {i}/{len(file_infos)}: {display_name}")

        if not file_infos:
            zf.extractall(extract_dir)
            if progress_cb:
                progress_cb(percent_end, "ZIP contains no regular files.")


def _deploy_release_bytes(
    file_bytes,
    run_migrate=False,
    require_data_upgrade=False,
    progress_cb=None,
    actor_username=None,
    job_id=None,
):
    base_dir, current_path, wsgi_reload_path, history_path = _deploy_paths()
    os.makedirs(base_dir, exist_ok=True)
    stamp = pk_now().strftime('%Y%m%d_%H%M%S')
    job_token = (job_id or uuid.uuid4().hex)[:8]
    job_root = os.path.join(current_app.instance_path, 'upgrade_jobs', f"{stamp}_{job_token}")
    incoming_dir = os.path.join(job_root, 'incoming')
    backup_dir = os.path.join(job_root, 'backup')
    report_dir = os.path.join(job_root, 'report')
    for folder in [incoming_dir, backup_dir, report_dir]:
        os.makedirs(folder, exist_ok=True)

    zip_name = f"app_release_{stamp}.zip"
    zip_path = os.path.join(incoming_dir, zip_name)

    release_dir = os.path.join(base_dir, stamp)
    os.makedirs(release_dir, exist_ok=True)

    switched = False
    previous_target = None
    db_snapshot = None
    zip_scan = {}
    integrity_report = None
    try:
        if progress_cb:
            progress_cb(6, 'Capturing baseline integrity state...')
        pre_counts = _capture_guard_counts()

        if progress_cb:
            progress_cb(12, 'Saving uploaded release...')
        with open(zip_path, 'wb') as f:
            f.write(file_bytes)

        if progress_cb:
            progress_cb(18, 'Validating release ZIP...')
        zip_scan = _validate_release_zip(zip_path, require_data_upgrade=require_data_upgrade)

        if progress_cb:
            progress_cb(24, 'Creating automatic backups...')
        db_snapshot = _snapshot_sqlite_db(stamp, backup_dir=backup_dir)
        if not db_snapshot:
            raise ValueError("Failed to create pre-upgrade DB snapshot.")
        backup_name = _create_full_raw_backup(backup_dir=backup_dir)

        if progress_cb:
            progress_cb(34, 'Extracting release files...')
        _safe_extract_zip(zip_path, release_dir, progress_cb=progress_cb, percent_start=34, percent_end=50)

        if progress_cb:
            progress_cb(50, 'Normalizing release structure...')
        _flatten_single_root_folder(release_dir)
        if not os.path.exists(os.path.join(release_dir, 'main.py')):
            raise ValueError("Release missing main.py at root.")

        sql_report = None
        if run_migrate:
            if progress_cb:
                progress_cb(62, 'Running migration...')
            sql_report = _run_app_migrate_steps()

        # Data-upgrade payload sources:
        # 1) release ZIP /data_upgrade/*.xlsx
        # 2) persistent queue folder in instance path
        data_upgrade_reports = []
        queue_dir, processed_dir = _get_data_upgrade_queue_dir()
        release_data_upgrade_dir = os.path.join(release_dir, 'data_upgrade')
        release_candidates = _list_data_upgrade_excels(release_data_upgrade_dir)
        queue_candidates = _list_data_upgrade_excels(queue_dir)
        # Never recurse processed history as pending queue.
        queue_candidates = [p for p in queue_candidates if not p.startswith(processed_dir + os.sep)]
        candidates = [('release', p) for p in release_candidates] + [('queue', p) for p in queue_candidates]
        if require_data_upgrade and not candidates:
            raise ValueError(
                "No data upgrade payload found in release ZIP or queue folder while 'require data payload' is enabled."
            )

        _ensure_data_upgrade_ledger()
        queued_processed = []

        if candidates:
            total_data = len(candidates)
            for i, (source, data_file) in enumerate(candidates, start=1):
                if progress_cb:
                    pct = 68 + int((i / total_data) * 12)
                    progress_cb(
                        pct,
                        f"Applying data upgrade {i}/{total_data} ({source}): {os.path.basename(data_file)}"
                    )
                with open(data_file, 'rb') as f:
                    data_blob = f.read()
                data_hash = _hash_bytes(data_blob)
                if _is_data_upgrade_applied(data_hash):
                    data_upgrade_reports.append({
                        'file': os.path.basename(data_file),
                        'source': source,
                        'sha256': data_hash,
                        'report': {'skipped': 1, 'reason': 'already_applied'},
                    })
                    if source == 'queue':
                        queued_processed.append(data_file)
                    continue
                report = _run_master_import_bytes(
                    file_bytes=data_blob,
                    actor_username=actor_username or 'system',
                    progress_cb=None,
                )
                _mark_data_upgrade_applied(
                    file_sha256=data_hash,
                    file_name=os.path.basename(data_file),
                    source=source,
                    report=report,
                )
                data_upgrade_reports.append({
                    'file': os.path.basename(data_file),
                    'source': source,
                    'sha256': data_hash,
                    'report': report,
                })
                if source == 'queue':
                    queued_processed.append(data_file)

        if progress_cb:
            progress_cb(82, 'Running integrity verification...')
        integrity_report = _run_post_upgrade_integrity_checks(pre_counts)

        if progress_cb:
            progress_cb(88, 'Switching current release...')
        if os.path.islink(current_path):
            previous_target = os.readlink(current_path)
        if os.path.islink(current_path) or not os.path.exists(current_path):
            if os.path.islink(current_path):
                os.unlink(current_path)
            os.symlink(release_dir, current_path)
            switched = True
        else:
            raise ValueError("Current path is not a symlink. Configure WSGI to use DEPLOY_CURRENT_PATH.")

        if progress_cb:
            progress_cb(95, 'Reloading app...')
        if wsgi_reload_path and os.path.exists(wsgi_reload_path):
            os.utime(wsgi_reload_path, None)
            reloaded = True
        else:
            reloaded = False

        # Archive queue files only after a fully successful deploy.
        if queued_processed:
            archived_at = pk_now().strftime('%Y%m%d_%H%M%S')
            for src in queued_processed:
                try:
                    target = os.path.join(processed_dir, f"{archived_at}_{os.path.basename(src)}")
                    shutil.move(src, target)
                except Exception:
                    pass

        entry = {
            'timestamp': stamp,
            'action': 'deploy',
            'zip_name': zip_name,
            'zip_sha256': _hash_file(zip_path),
            'release_dir': release_dir,
            'current_path': current_path,
            'reloaded': reloaded,
            'user': actor_username,
            'backup_name': backup_name,
            'run_migrate': bool(run_migrate),
            'sql_migrations': sql_report,
            'data_upgrade_reports': data_upgrade_reports,
            'require_data_upgrade': bool(require_data_upgrade),
            'zip_scan': {
                'payload_files': zip_scan.get('payload_files', []),
                'blocked_count': len(zip_scan.get('blocked', [])),
            },
            'integrity_report': integrity_report,
            'job_root': job_root,
        }
        _append_deploy_history(history_path, entry)
        return {
            'ok': True,
            'reloaded': reloaded,
            'sql_report': sql_report,
            'data_upgrade_reports': data_upgrade_reports,
            'integrity_report': integrity_report,
        }
    except Exception as e:
        if switched:
            try:
                if os.path.islink(current_path):
                    os.unlink(current_path)
                if previous_target:
                    os.symlink(previous_target, current_path)
            except Exception:
                pass
        restore_ok = False
        if db_snapshot:
            try:
                _restore_sqlite_snapshot(db_snapshot)
                restore_ok = True
            except Exception:
                restore_ok = False
        entry = {
            'timestamp': stamp,
            'action': 'deploy_failed',
            'zip_name': zip_name,
            'release_dir': release_dir,
            'error': str(e),
            'user': actor_username,
            'restored_snapshot': restore_ok,
            'require_data_upgrade': bool(require_data_upgrade),
            'job_root': job_root,
        }
        _append_deploy_history(history_path, entry)
        return {'ok': False, 'error': str(e)}


def _deploy_release_worker(
    flask_app,
    job_id,
    file_bytes,
    run_migrate,
    require_data_upgrade,
    username,
    tenant_id=None,
    role=None,
):
    with flask_app.app_context():
        with flask_app.test_request_context('/import_export/app_upgrade'):
            g.user = None
            g.is_root = (role == 'root')
            g.tenant_id = tenant_id
            g.enforce_tenant = (not g.is_root) and (tenant_id is not None)
            _set_import_actor_context(username=username, tenant_id=tenant_id, role=role)
            try:
                _set_deploy_progress(job_id, percent=2, message='Started upgrade...', done=False, success=False)
                result = _deploy_release_bytes(
                    file_bytes,
                    run_migrate=run_migrate,
                    require_data_upgrade=require_data_upgrade,
                    actor_username=username,
                    job_id=job_id,
                    progress_cb=lambda p, m: _set_deploy_progress(job_id, percent=p, message=m, done=False),
                )
                if result.get('ok'):
                    sql_report = result.get('sql_report')
                    data_reports = result.get('data_upgrade_reports') or []
                    if sql_report:
                        done_msg = f"Done. SQL applied: {sql_report.get('applied', 0)} / {sql_report.get('files', 0)}."
                    else:
                        done_msg = 'Done. Upgrade completed.'
                    if data_reports:
                        imported = sum((x.get('report') or {}).get('imported', 0) for x in data_reports)
                        updated = sum((x.get('report') or {}).get('updated', 0) for x in data_reports)
                        done_msg += f" Data upgrade files: {len(data_reports)} (Imported {imported}, Updated {updated})."
                    if result.get('reloaded'):
                        done_msg += " Reloaded automatically."
                    else:
                        done_msg += " Reload file not found; using in-browser refresh."
                    _set_deploy_progress(
                        job_id,
                        percent=100,
                        message=done_msg,
                        done=True,
                        success=True,
                        reloaded=result.get('reloaded'),
                        user=username,
                    )
                else:
                    _set_deploy_progress(
                        job_id,
                        percent=100,
                        message='Upgrade failed.',
                        done=True,
                        success=False,
                        error=result.get('error'),
                        user=username,
                    )
            finally:
                _clear_import_actor_context()


def _run_sql_migrations():
    migrations_dir = _get_migrations_dir()
    files = _list_sql_migrations(migrations_dir)
    if not files:
        return {'applied': 0, 'skipped': 0, 'files': 0, 'dir': migrations_dir}

    conn = db.engine.raw_connection()
    try:
        allow_destructive = str(
            os.environ.get(
                'MIGRATIONS_ALLOW_DESTRUCTIVE',
                current_app.config.get('MIGRATIONS_ALLOW_DESTRUCTIVE', '0')
            )
        ).strip().lower() in ['1', 'true', 'on', 'yes']
        applied = _get_applied_migrations(conn)
        to_apply = [f for f in files if f not in applied]
        cur = conn.cursor()
        applied_count = 0
        for name in to_apply:
            path = os.path.join(migrations_dir, name)
            with open(path, 'r', encoding='utf-8') as f:
                sql = f.read()
            if not sql.strip():
                continue
            if not allow_destructive and re.search(
                r'\b(drop\s+table|truncate\s+table|delete\s+from)\b',
                sql,
                flags=re.IGNORECASE,
            ):
                raise ValueError(
                    f"Destructive SQL blocked in migration '{name}'. "
                    f"Set MIGRATIONS_ALLOW_DESTRUCTIVE=1 to allow it explicitly."
                )
            cur.executescript(sql)
            cur.execute(
                "INSERT OR IGNORE INTO migration_history (filename, applied_at) VALUES (?, ?)",
                (name, pk_now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            applied_count += 1
        conn.commit()
        return {
            'applied': applied_count,
            'skipped': len(files) - applied_count,
            'files': len(files),
            'dir': migrations_dir,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


