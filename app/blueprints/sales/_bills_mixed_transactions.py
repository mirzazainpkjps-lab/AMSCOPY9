"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/mixed_transactions')
@login_required
def mixed_transactions():
    return redirect(url_for('tracking', category='Mixed Transaction'))

