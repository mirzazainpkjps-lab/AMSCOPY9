"""tenants — split from system.py."""
from ._common import *  # noqa

@bp.route('/tenants')
@login_required
def tenants_dashboard():
    require_root()
    q = (request.args.get('q') or '').strip()
    status = (request.args.get('status') or '').strip().lower()

    tenants_query = Tenant.query.filter(Tenant.name != DEFAULT_TENANT_NAME)
    if q:
        tenants_query = tenants_query.filter(func.lower(func.trim(Tenant.name)).like(f"%{q.lower()}%"))
    if status in ('active', 'suspended'):
        tenants_query = tenants_query.filter(Tenant.status == status)

    tenants = tenants_query.order_by(Tenant.created_at.desc()).all()
    tenant_ids = [t.id for t in tenants]
    tenant_users_map = {}
    if tenant_ids:
        tenant_users = User.query.filter(User.tenant_id.in_(tenant_ids)).order_by(User.username.asc()).all()
        for u in tenant_users:
            tenant_users_map.setdefault(u.tenant_id, []).append(u)
    total = Tenant.query.filter(Tenant.name != DEFAULT_TENANT_NAME).count()
    active = Tenant.query.filter(Tenant.name != DEFAULT_TENANT_NAME, Tenant.status == 'active').count()
    suspended = Tenant.query.filter(Tenant.name != DEFAULT_TENANT_NAME, Tenant.status == 'suspended').count()
    expiring_soon = Tenant.query.filter(
        Tenant.name != DEFAULT_TENANT_NAME,
        Tenant.expiry_date.isnot(None),
        Tenant.expiry_date <= (pk_today() + timedelta(days=30))
    ).count()
    root_username = os.environ.get('ROOT_USERNAME', 'root')
    root_recovery_unused = RootRecoveryCode.query.filter(
        RootRecoveryCode.username == root_username,
        RootRecoveryCode.used_at.is_(None)
    ).count()

    return render_template(
        'tenants.html',
        tenants=tenants,
        total=total,
        active=active,
        suspended=suspended,
        expiring_soon=expiring_soon,
        root_recovery_unused=root_recovery_unused,
        tenant_users_map=tenant_users_map,
        q=q,
        status_filter=status,
        test_tenant_name=TEST_TENANT_NAME
    )


@bp.route('/tenants/create', methods=['POST'])
@login_required
def tenants_create():
    require_root()
    name = (request.form.get('name') or '').strip()
    status = (request.form.get('status') or 'active').strip()
    subscription_plan = (request.form.get('subscription_plan') or '').strip()
    expiry_date_raw = (request.form.get('expiry_date') or '').strip()
    expiry_date = None
    if expiry_date_raw:
        try:
            expiry_date = datetime.strptime(expiry_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid expiry date. Use YYYY-MM-DD format.', 'danger')
            return redirect(url_for('tenants_dashboard'))
    if not name:
        flash('Tenant name required', 'danger')
        return redirect(url_for('tenants_dashboard'))

    existing = Tenant.query.filter_by(name=name).first()
    if existing:
        flash('Tenant already exists', 'warning')
        return redirect(url_for('tenants_dashboard'))

    tenant = Tenant(
        name=name,
        status=status,
        subscription_plan=subscription_plan,
        expiry_date=expiry_date
    )
    db.session.add(tenant)
    db.session.flush()

    # Create a default tenant admin account
    default_username = os.environ.get('DEFAULT_TENANT_ADMIN_USERNAME', 'admin')
    default_password = os.environ.get('DEFAULT_TENANT_ADMIN_PASSWORD', 'Admin@12345')
    existing_admin = User.query.filter_by(username=default_username, tenant_id=tenant.id).first()
    if not existing_admin:
        db.session.add(User(
            username=default_username,
            password_hash=generate_password_hash(default_password),
            password_plain=None,
            role='admin',
            status='active',
            tenant_id=tenant.id
        ))
    db.session.commit()
    audit_log(current_user, tenant.id, 'tenant.create', f'name={name}')
    flash(f'Tenant created. Default admin login: {default_username} / {default_password}', 'success')
    return redirect(url_for('tenants_dashboard'))


@bp.route('/tenants/<tenant_id>/reset_admin', methods=['POST'])
@login_required
def tenants_reset_admin(tenant_id):
    require_root()
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        flash('Tenant not found', 'danger')
        return redirect(url_for('tenants_dashboard'))

    default_username = os.environ.get('DEFAULT_TENANT_ADMIN_USERNAME', 'admin')
    default_password = os.environ.get('DEFAULT_TENANT_ADMIN_PASSWORD', 'Admin@12345')

    admin_user = User.query.filter_by(username=default_username, tenant_id=tenant.id).first()
    if not admin_user:
        admin_user = User(
            username=default_username,
            password_hash=generate_password_hash(default_password),
            password_plain=None,
            role='admin',
            status='active',
            tenant_id=tenant.id
        )
        db.session.add(admin_user)
    else:
        admin_user.password_hash = generate_password_hash(default_password)
        admin_user.password_plain = None
        admin_user.status = 'active'

    db.session.commit()
    audit_log(current_user, tenant.id, 'tenant.reset_admin', f'username={default_username}')
    flash(f'Reset admin for {tenant.name}. Login: {default_username} / {default_password}', 'success')
    return redirect(url_for('tenants_dashboard'))


@bp.route('/tenants/<tenant_id>/reset_missing_passwords', methods=['POST'])
@login_required
def tenants_reset_missing_passwords(tenant_id):
    require_root()
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        flash('Tenant not found', 'danger')
        return redirect(url_for('tenants_dashboard'))

    default_user_password = os.environ.get('DEFAULT_TENANT_USER_PASSWORD', 'User@12345')
    default_admin_password = os.environ.get('DEFAULT_TENANT_ADMIN_PASSWORD', 'Admin@12345')

    users = User.query.filter_by(tenant_id=tenant.id).all()
    updated = 0
    for u in users:
        if (u.password_hash or '').strip():
            continue
        new_pw = default_admin_password if (u.role or '').strip().lower() == 'admin' else default_user_password
        u.password_hash = generate_password_hash(new_pw)
        u.password_plain = None
        updated += 1

    db.session.commit()
    audit_log(current_user, tenant.id, 'tenant.reset_missing_passwords', f'updated={updated}')
    if updated:
        flash(f'Reset missing password hashes for {updated} user(s) in {tenant.name}. Temporary defaults were not stored.', 'success')
    else:
        flash('All tenant users already have password hashes.', 'info')
    return redirect(url_for('tenants_dashboard'))


@bp.route('/tenants/<tenant_id>/status', methods=['POST'])
@login_required
def tenants_update_status(tenant_id):
    require_root()
    status = (request.form.get('status') or '').strip()
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        flash('Tenant not found', 'danger')
        return redirect(url_for('tenants_dashboard'))
    if status not in ('active', 'suspended'):
        flash('Invalid status', 'danger')
        return redirect(url_for('tenants_dashboard'))
    tenant.status = status
    db.session.commit()
    audit_log(current_user, tenant.id, 'tenant.status', f'status={status}')
    flash('Tenant status updated', 'success')
    return redirect(url_for('tenants_dashboard'))


@bp.route('/tenants/<tenant_id>/update', methods=['POST'])
@login_required
def tenants_update(tenant_id):
    require_root()
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        flash('Tenant not found', 'danger')
        return redirect(url_for('tenants_dashboard'))

    name = (request.form.get('name') or '').strip()
    status = (request.form.get('status') or '').strip().lower()
    subscription_plan = (request.form.get('subscription_plan') or '').strip()
    expiry_date_raw = (request.form.get('expiry_date') or '').strip()

    if not name:
        flash('Tenant name required', 'danger')
        return redirect(url_for('tenants_dashboard'))
    if status not in ('active', 'suspended'):
        flash('Invalid status', 'danger')
        return redirect(url_for('tenants_dashboard'))

    duplicate = Tenant.query.filter(
        Tenant.id != tenant.id,
        func.lower(func.trim(Tenant.name)) == name.lower()
    ).first()
    if duplicate:
        flash('Another tenant already uses this name.', 'danger')
        return redirect(url_for('tenants_dashboard'))

    expiry_date = None
    if expiry_date_raw:
        try:
            expiry_date = datetime.strptime(expiry_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid expiry date. Use YYYY-MM-DD format.', 'danger')
            return redirect(url_for('tenants_dashboard'))

    tenant.name = name
    tenant.status = status
    tenant.subscription_plan = subscription_plan
    tenant.expiry_date = expiry_date
    db.session.commit()
    audit_log(current_user, tenant.id, 'tenant.update', f'name={name}, status={status}, expiry={expiry_date or ""}')
    flash('Tenant details updated', 'success')
    return redirect(url_for('tenants_dashboard'))


@bp.route('/tenants/<tenant_id>/backup_history')
@login_required
def tenants_backup_history(tenant_id):
    require_root()
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        flash('Tenant not found', 'danger')
        return redirect(url_for('tenants_dashboard'))
    rows = TenantWipeBackupHistory.query.filter_by(tenant_id=tenant.id).order_by(TenantWipeBackupHistory.created_at.desc()).all()
    return render_template('tenant_backup_history.html', tenant=tenant, rows=rows)


@bp.route('/tenants/backup_history/download/<int:history_id>')
@login_required
def tenants_backup_history_download(history_id):
    require_root()
    row = db.session.get(TenantWipeBackupHistory, history_id)
    if not row:
        flash('Backup history record not found.', 'danger')
        return redirect(url_for('tenants_dashboard'))
    fpath = (row.backup_path or '').strip()
    if not fpath or not os.path.exists(fpath):
        flash('Backup file no longer exists on disk.', 'danger')
        return redirect(url_for('tenants_backup_history', tenant_id=row.tenant_id))
    ext = _ext_from_name(fpath, 'xlsx')
    return send_file(
        fpath,
        as_attachment=True,
        download_name=_download_filename('TENANTBACKUP', ext),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@bp.route('/tenants/backup_history/restore/<int:history_id>', methods=['POST'])
@login_required
def tenants_backup_history_restore(history_id):
    require_root()
    row = db.session.get(TenantWipeBackupHistory, history_id)
    if not row:
        flash('Backup history record not found.', 'danger')
        return redirect(url_for('tenants_dashboard'))

    tenant_id = row.tenant_id
    tenant_name = row.tenant_name
    backup_filename = row.backup_filename
    fpath = (row.backup_path or '').strip()
    if not fpath or not os.path.exists(fpath):
        flash('Backup file no longer exists on disk.', 'danger')
        return redirect(url_for('tenants_backup_history', tenant_id=tenant_id))

    try:
        from blueprints.import_export import _run_full_raw_import_bytes

        with open(fpath, 'rb') as f:
            file_bytes = f.read()

        scope_ctx = {
            'scope': 'tenant',
            'target_tenant_id': tenant_id,
            'target_tenant_name': tenant_name,
            'role': 'root',
        }
        _run_full_raw_import_bytes(
            file_bytes=file_bytes,
            scope_ctx=scope_ctx,
            mode='replace_tenant_data',
            source_file_name=backup_filename or os.path.basename(fpath)
        )
        flash(f"Backup restored to tenant '{tenant_name}' successfully.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Backup restore failed: {e}', 'danger')

    return redirect(url_for('tenants_backup_history', tenant_id=tenant_id))


@bp.route('/tenants/<tenant_id>/delete', methods=['POST'])
@login_required
def tenants_delete(tenant_id):
    require_root()
    tenant = db.session.get(Tenant, tenant_id)
    if not tenant:
        flash('Tenant not found', 'danger')
        return redirect(url_for('tenants_dashboard'))
    if not can_hard_delete_tenant(tenant):
        flash('Hard delete allowed only for test tenant', 'danger')
        return redirect(url_for('tenants_dashboard'))
    hard_delete_tenant(tenant.id)
    audit_log(current_user, tenant_id, 'tenant.delete', f'name={tenant.name}')
    flash('Test tenant deleted permanently', 'success')
    return redirect(url_for('tenants_dashboard'))


