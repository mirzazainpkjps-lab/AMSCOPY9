"""import helpers."""
from ._common import *  # noqa

@import_export_bp.before_request
def _import_export_access_guard():
    # Single-store mode: admin-only.
    if not current_user.is_authenticated:
        return None
    role = getattr(current_user, 'role', None)
    endpoint = (request.endpoint or '')

    root_only = {
        'import_export.app_upgrade',
        'import_export.app_upgrade_start',
        'import_export.app_upgrade_status',
        'import_export.app_upgrade_rollback',
        'import_export.app_upgrade_migrate',
    }
    if endpoint in root_only:
        flash('App Upgrade operations are disabled in single-store mode.', 'warning')
        return redirect(url_for('index'))

    if role != 'admin':
        flash('Only admin can access Import/Export operations.', 'danger')
        return redirect(url_for('index'))
    return None


def _tenant_release_dir(kind='artifacts'):
    """
    Store import/export snapshots in release folders under DEPLOY_BASE_DIR.
    Layout:
      <DEPLOY_BASE_DIR>/data/<kind>/
    """
    base_dir = current_app.config.get('DEPLOY_BASE_DIR') or os.path.join(os.path.expanduser('~'), 'releases')
    username = _actor_username()
    path = os.path.join(base_dir, 'data', _safe_name(username, 'admin'), _safe_name(kind, 'artifacts'))
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        fallback = os.path.join(current_app.instance_path, 'releases', _safe_name(username, 'admin'), _safe_name(kind, 'artifacts'))
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _set_import_actor_context(username=None, tenant_id=None, role=None):
    _IMPORT_ACTOR_CTX.username = username
    _IMPORT_ACTOR_CTX.tenant_id = tenant_id
    _IMPORT_ACTOR_CTX.role = role


def _clear_import_actor_context():
    _IMPORT_ACTOR_CTX.username = None
    _IMPORT_ACTOR_CTX.tenant_id = None
    _IMPORT_ACTOR_CTX.role = None


def _actor_username():
    try:
        if getattr(current_user, 'is_authenticated', False):
            return current_user.username
    except Exception:
        pass
    return getattr(_IMPORT_ACTOR_CTX, 'username', None) or 'system'


def _actor_tenant_id():
    # Single-store mode: no tenant scoping
    return None


def _actor_role():
    try:
        if getattr(current_user, 'is_authenticated', False):
            return getattr(current_user, 'role', None)
    except Exception:
        pass
    try:
        if bool(getattr(g, 'is_root', False)):
            return 'root'
    except Exception:
        pass
    return getattr(_IMPORT_ACTOR_CTX, 'role', None)


def _resolve_scope_context(scope_raw=None, tenant_id_raw=None):
    """Single-store: always operate on full dataset."""
    role = _actor_role()
    return {
        'scope': 'single_store',
        'target_tenant_id': None,
        'target_tenant_name': None,
        'role': role,
    }


def _default_scope_context():
    return _resolve_scope_context(scope_raw=None, tenant_id_raw=None)


def _full_raw_tables_for_scope(scope_ctx):
    return [t for t in db.metadata.sorted_tables if t.name not in FULL_RAW_EXCLUDE_TABLES]


def _scope_table_select(table, scope_ctx):
    return table.select()


def _scoped_model_query(model, scope_ctx):
    q = model.query
    return q


def _capture_guard_counts():
    return {
        'user': int(User.query.count()),
        'client': int(Client.query.count()),
        'material': int(Material.query.count()),
        'entry': int(Entry.query.count()),
        'booking': int(Booking.query.count()),
        'payment': int(Payment.query.count()),
        'direct_sale': int(DirectSale.query.count()),
    }


@import_export_bp.route('/tenant_db_restore', methods=['POST'])
@login_required
def tenant_db_restore():
    return "Not Found", 404


