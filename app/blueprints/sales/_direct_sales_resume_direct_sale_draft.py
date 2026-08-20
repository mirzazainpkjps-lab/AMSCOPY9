"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/direct_sales/hold/<int:draft_id>/resume', methods=['POST'])
@login_required
def resume_direct_sale_draft(draft_id):
    row = DirectSaleDraft.query.get_or_404(draft_id)
    try:
        payload = json.loads(row.payload or '{}')
    except Exception:
        payload = {}
    payload['draft_id'] = row.id
    session['direct_sale_form_draft'] = payload
    return redirect(url_for('direct_sales_page', resume='add'))

