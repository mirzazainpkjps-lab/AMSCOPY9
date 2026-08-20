"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/direct_sales/hold/<int:draft_id>/delete', methods=['POST'])
@login_required
def delete_direct_sale_draft(draft_id):
    row = DirectSaleDraft.query.get(draft_id)
    if row:
        db.session.delete(row)
        db.session.commit()
        flash('Draft deleted', 'success')
    else:
        flash('Draft not found', 'warning')
    return redirect(url_for('direct_sales_page'))

