"""client — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/financial_ledger/<int:client_id>')
@login_required
def financial_ledger_details(client_id):
    return redirect(url_for('financial_ledger', client_id=client_id))

