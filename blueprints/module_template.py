"""
Template for creating a new module in the application.
Copy this file and customize it for your feature.

Usage:
1. Copy this file to blueprints/ directory with a descriptive name
2. Rename the blueprint variable from 'template_bp' to 'your_name_bp'
3. Update MODULE_CONFIG with your module details
4. Define your routes using @your_name_bp.route() decorators
5. The module will auto-register on app startup
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from models import db  # Add your models here
from datetime import datetime

# ============================================================================
# MODULE CONFIGURATION
# ============================================================================
MODULE_CONFIG = {
    'name': 'Module Display Name',
    'description': 'Brief description of what this module does',
    'url_prefix': '/module_name',  # Change this to your URL prefix
    'enabled': True,
    'version': '1.0.0',
    'author': 'Your Name',
    'requires_login': True,
    'allowed_roles': ['admin', 'user']  # Optional: restrict by role
}

# ============================================================================
# BLUEPRINT DEFINITION
# ============================================================================
template_bp = Blueprint('template', __name__, template_folder='../templates')

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
def log_action(action, details=''):
    """Log user actions for audit trail."""
    print(f"[{datetime.now()}] User: {current_user.username}, Action: {action}, Details: {details}")


def check_permission(required_permission):
    """Check if current user has required permission."""
    if not current_user.is_authenticated:
        return False
    # Add your permission logic here
    return True


# ============================================================================
# ROUTES - Main Pages
# ============================================================================
@template_bp.route('/', methods=['GET'])
@login_required
def dashboard():
    """Main dashboard for this module."""
    # Add your logic here
    return render_template('template_dashboard.html')


@template_bp.route('/view/<int:item_id>', methods=['GET'])
@login_required
def view_item(item_id):
    """View individual item details."""
    # Fetch item from database
    # item = YourModel.query.get_or_404(item_id)
    return render_template('template_view.html')


# ============================================================================
# ROUTES - Forms & Actions
# ============================================================================
@template_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create_item():
    """Create new item."""
    if request.method == 'POST':
        # Handle form submission
        # db.session.add(new_item)
        # db.session.commit()
        # flash('Item created successfully', 'success')
        log_action('CREATE_ITEM', f'Created item: {request.form.get("name")}')
        return redirect(url_for('template.dashboard'))
    
    return render_template('template_form.html', action='Create')


@template_bp.route('/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    """Edit existing item."""
    # item = YourModel.query.get_or_404(item_id)
    
    if request.method == 'POST':
        # Handle form submission
        # item.name = request.form.get('name')
        # db.session.commit()
        # flash('Item updated successfully', 'success')
        log_action('EDIT_ITEM', f'Edited item: {item_id}')
        return redirect(url_for('template.view_item', item_id=item_id))
    
    return render_template('template_form.html', action='Edit')


@template_bp.route('/delete/<int:item_id>', methods=['POST'])
@login_required
def delete_item(item_id):
    """Delete an item."""
    # item = YourModel.query.get_or_404(item_id)
    # db.session.delete(item)
    # db.session.commit()
    # flash('Item deleted successfully', 'success')
    log_action('DELETE_ITEM', f'Deleted item: {item_id}')
    return redirect(url_for('template.dashboard'))


# ============================================================================
# ROUTES - API Endpoints
# ============================================================================
@template_bp.route('/api/data', methods=['GET'])
@login_required
def api_get_data():
    """API endpoint to fetch data."""
    # fetch data from database
    # data = [{'id': 1, 'name': 'Item 1'}]
    return jsonify({'success': True, 'data': []})


@template_bp.route('/api/search', methods=['GET'])
@login_required
def api_search():
    """API endpoint for searching."""
    query = request.args.get('q', '')
    # Search logic here
    results = []
    return jsonify({'results': results})


# ============================================================================
# ROUTES - File Operations
# ============================================================================
@template_bp.route('/export', methods=['GET'])
@login_required
def export_data():
    """Export data to CSV/Excel."""
    # import pandas as pd
    # df = pd.DataFrame(...)
    # return send_file(...)
    flash('Export functionality not implemented', 'warning')
    return redirect(url_for('template.dashboard'))


@template_bp.route('/import', methods=['GET', 'POST'])
@login_required
def import_data():
    """Import data from file."""
    if request.method == 'POST':
        file = request.files.get('file')
        if file:
            # Process file
            log_action('IMPORT_DATA', f'Imported from file: {file.filename}')
            flash('Data imported successfully', 'success')
        return redirect(url_for('template.dashboard'))
    
    return render_template('template_import.html')


# ============================================================================
# ERROR HANDLERS (Module-specific)
# ============================================================================
@template_bp.errorhandler(404)
def not_found(error):
    """Handle 404 errors in this module."""
    return render_template('template_404.html'), 404


@template_bp.errorhandler(403)
def forbidden(error):
    """Handle 403 errors in this module."""
    flash('You do not have permission to access this page', 'error')
    return redirect(url_for('index')), 403


# ============================================================================
# CONTEXT PROCESSORS
# ============================================================================
@template_bp.context_processor
def inject_module_context():
    """Inject module-specific data into all templates."""
    return {
        'module_name': MODULE_CONFIG['name'],
        'module_version': MODULE_CONFIG['version']
    }


# ============================================================================
# NOTES FOR DEVELOPERS
# ============================================================================
"""
IMPORTANT REMINDERS:
1. Blueprint name must end with '_bp' (e.g., 'my_module_bp')
2. Update MODULE_CONFIG with accurate information
3. Use @login_required for protected routes
4. Use url_for() for cross-module navigation: url_for('template.dashboard')
5. Templates should be in templates/ folder
6. Log important actions for audit trail
7. Always handle database errors gracefully
8. Return proper HTTP status codes
9. Use flash messages for user feedback
10. Test thoroughly before deploying

URL ACCESS:
Your routes will be accessible at:
  http://localhost:5000/<url_prefix>/route
Example:
  http://localhost:5000/module_name/
  http://localhost:5000/module_name/create
  http://localhost:5000/module_name/view/1
  http://localhost:5000/module_name/api/data
"""
