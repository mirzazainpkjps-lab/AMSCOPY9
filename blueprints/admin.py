"""
Admin module for monitoring and managing the application modules.
Provides dashboard to view loaded modules and their configuration.
"""

from flask import Blueprint, render_template, jsonify
from flask_login import login_required, current_user
from utils.module_loader import get_modules_info
from datetime import datetime
from zoneinfo import ZoneInfo

# Module configuration
MODULE_CONFIG = {
    'name': 'Admin Module',
    'description': 'System administration and module management',
    'url_prefix': '/admin',
    'enabled': True,
    'requires_login': True,
    'allowed_roles': ['admin']
}

admin_bp = Blueprint('admin', __name__)
PK_TZ = ZoneInfo('Asia/Karachi')


def pk_now():
    return datetime.now(PK_TZ).replace(tzinfo=None)


def is_admin():
    """Check if current user is admin."""
    return current_user.is_authenticated and current_user.role == 'admin'


@admin_bp.before_request
def check_admin():
    """Ensure only admins can access admin routes."""
    if not is_admin():
        from flask import abort
        abort(403)


@admin_bp.route('/')
@login_required
def dashboard():
    """Admin dashboard with system overview."""
    modules = get_modules_info('blueprints')
    module_count = len(modules)
    blueprint_count = sum(len(bps) for _, bps in modules)
    
    context = {
        'module_count': module_count,
        'blueprint_count': blueprint_count,
        'system_time': pk_now(),
        'modules': modules
    }
    
    return render_template('admin_dashboard.html', **context)


@admin_bp.route('/modules')
@login_required
def modules_list():
    """List all loaded modules with details."""
    modules = get_modules_info('blueprints')
    
    modules_data = []
    for module_name, blueprints in modules:
        modules_data.append({
            'name': module_name,
            'blueprints': blueprints,
            'blueprint_count': len(blueprints),
            'status': 'Loaded'
        })
    
    return render_template('admin_modules.html', modules=modules_data)


@admin_bp.route('/api/modules')
@login_required
def api_get_modules():
    """API endpoint to get all modules as JSON."""
    modules = get_modules_info('blueprints')
    
    result = {
        'success': True,
        'timestamp': pk_now().isoformat(),
        'total_modules': len(modules),
        'modules': [
            {
                'name': module_name,
                'blueprints': blueprints,
                'status': 'active'
            }
            for module_name, blueprints in modules
        ]
    }
    
    return jsonify(result)


@admin_bp.route('/api/health')
@login_required
def api_health_check():
    """API endpoint for system health check."""
    return jsonify({
        'status': 'healthy',
        'timestamp': pk_now().isoformat(),
        'modules_loaded': len(get_modules_info('blueprints'))
    })


@admin_bp.context_processor
def inject_admin_context():
    """Inject admin-specific data into all admin templates."""
    return {
        'admin_section': True,
        'current_user_role': current_user.role if current_user.is_authenticated else 'guest'
    }
