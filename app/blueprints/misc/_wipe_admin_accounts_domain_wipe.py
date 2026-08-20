"""wipe — split from misc.py."""
from ._common import *  # noqa

@bp.route('/admin/accounts_domain_wipe', methods=['POST'])
@login_required
def admin_accounts_domain_wipe():
    """Admin-only endpoint to explicitly clear accounting snapshots and reset balances.
    This performs the same actions as the `accounts_domain_wipe` domain but
    exposes it as a guarded admin endpoint for emergency use.
    
    Uses a system-wide mutex lock to prevent concurrent executions.
    """
    if getattr(current_user, 'role', None) not in ['admin', 'root']:
        flash('Unauthorized', 'danger')
        return redirect(url_for('settings'))

    # Require explicit confirmation text to avoid accidental use
    if request.form.get('confirm_text') != 'WIPE_ACCOUNTS':
        flash('Incorrect confirmation text. Type WIPE_ACCOUNTS to proceed.', 'danger')
        return redirect(url_for('settings'))

    # PHASE 1: Attempt to acquire system-wide lock (hard blocking)
    lock_name = 'accounts_domain_wipe'
    owner_id = getattr(current_user, 'username', None) or 'unknown'
    acquired, lock_error = acquire_system_lock(lock_name, ttl_seconds=3600, owner_id=owner_id)
    if not acquired:
        logging.getLogger('app').warning('Failed to acquire lock for accounts_domain_wipe: %s', lock_error)
        flash(lock_error or 'Another wipe operation is in progress. Try again later.', 'danger')
        return redirect(url_for('settings'))

    # Lock acquired; ensure we release it at the end
    try:
        # Perform the accounts-domain reset using the centralized reset function.
        history_row = None
        if _WIPE_BACKUP_ENABLED:
            try:
                backup_info = _create_pre_wipe_safety_backups(['accounts_domain_wipe'])
                history_row = TenantWipeBackupHistory(
                    tenant_name='single_store',
                    performed_by=getattr(current_user, 'username', None),
                    performed_by_role=getattr(current_user, 'role', None),
                    targets='accounts_domain_wipe',
                    backup_filename=(backup_info or {}).get('snap_db'),
                    backup_path=(backup_info or {}).get('backup_dir'),
                    wipe_status='pending',
                    note='Pre-wipe snapshot for accounts domain wipe.'
                )
                db.session.add(history_row)
                db.session.commit()
            except Exception:
                db.session.rollback()
                flash('Pre-wipe backup failed. Operation blocked.', 'danger')
                return redirect(url_for('settings'))

        # Mark operation as in-progress (visible to other processes) to reduce
        # chance of concurrent reads/operations during the atomic wipe+verify.
        if history_row:
            try:
                history_row.wipe_status = 'in_progress'
                history_row.note = 'Wipe started (pre-commit marker, lock acquired)'
                db.session.add(history_row)
                db.session.commit()
            except Exception:
                db.session.rollback()

        try:
            # Perform engine + post-processor inside a single transaction to guarantee atomicity.
            with db.session.begin():
                execute_domain_wipe('accounts_domain', db.session)
                accounts_domain_post_reset(db.session)
                if history_row:
                    history_row.wipe_status = 'completed'
                    history_row.note = 'Completed accounts_domain_wipe via admin endpoint.'
                    db.session.add(history_row)
            # At this point the transaction has committed successfully.
            # Run post-commit integrity verification (read-only).
            try:
                ok, report = verify_accounts_domain_wipe_integrity()
            except Exception:
                ok, report = False, {'error': 'verification_failed'}

            if not ok:
                # Attempt restoration from pre-wipe backup if available.
                try:
                    from blueprints.import_export import _restore_sqlite_snapshot
                    snap = None
                    if backup_info and backup_info.get('db_backup_path'):
                        snap = {'src_db': db_path, 'snap_db': backup_info.get('db_backup_path')}
                    if snap:
                        _restore_sqlite_snapshot(snap)
                        logging.getLogger('app').error('Integrity check failed after wipe; DB restored from backup. Report: %s', report)
                    else:
                        logging.getLogger('app').error('Integrity check failed after wipe; no backup available. Report: %s', report)
                except Exception:
                    logging.getLogger('app').exception('Failed to restore DB from pre-wipe backup after integrity failure')

                if history_row:
                    try:
                        history_row.wipe_status = 'failed'
                        history_row.note = f'Integrity validation failed: {json.dumps(report)}'
                        db.session.add(history_row)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                flash('Accounts domain wipe failed integrity validation and was rolled back/restored. Check logs.', 'danger')
            else:
                # Rebuild health snapshot so the next startup doesn't see a false
                # corruption drop.  Mark it as an intentional reset so the health
                # check knows this was deliberate.
                try:
                    _rebuild_health_snapshot(
                        intentional_operation={
                            'operation': 'accounts_domain_wipe',
                            'intentional': True,
                            'reset_context': 'granular_wipe',
                            'reset_source': 'granular_wipe',
                            'timestamp': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
                            'performed_by': getattr(current_user, 'username', None),
                        },
                        reset_context='granular_wipe'
                    )
                except Exception:
                    logging.getLogger('app').exception('Failed to rebuild health snapshot after accounts_domain_wipe')
                audit_log(current_user, 'data.wipe.accounts_domain', {'performed_by': getattr(current_user, 'username', None)})
                flash('Accounts domain wipe completed (transactions cleared, balances reset).', 'success')
        except Exception as e:
            # Transaction should rollback automatically; record failure separately.
            if history_row:
                try:
                    history_row.wipe_status = 'failed'
                    history_row.note = f'Accounts domain wipe failed: {e}'
                    db.session.add(history_row)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            logging.getLogger('app').exception('Accounts domain wipe failed')
            flash(f'Accounts domain wipe failed: {e}', 'danger')
    finally:
        # PHASE 7: Always release the lock (success or failure)
        released, release_error = release_system_lock(lock_name)
        if not released:
            logging.getLogger('app').warning('Failed to release lock: %s', release_error)

    return redirect(url_for('settings'))

