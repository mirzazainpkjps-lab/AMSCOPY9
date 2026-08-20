"""profit — split from reports.py."""
from ._common import *  # noqa

@bp.route('/daily_transactions')
@login_required
def daily_transactions_redirect():
    return redirect(url_for('inventory.daily_transactions', **request.args))

