"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/tenant_db_export')
@login_required
def tenant_db_export():
    return "Not Found", 404

