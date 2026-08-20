"""profit — split from reports.py."""
from ._common import *  # noqa

@bp.route('/stock_summary')
@login_required
def stock_summary_redirect():
    return redirect(url_for('inventory.stock_summary'))

