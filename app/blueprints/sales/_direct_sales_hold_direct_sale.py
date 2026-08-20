"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/direct_sales/hold', methods=['POST'])
@login_required
def hold_direct_sale():
    draft_payload = _collect_direct_sale_form_draft(request.form, mode='add')
    summary = _summarize_direct_sale_draft(draft_payload)
    draft_id = _safe_int(request.form.get('draft_id'))
    now = pk_now()

    if draft_id:
        row = DirectSaleDraft.query.get(draft_id)
    else:
        row = None

    if not row:
        row = DirectSaleDraft(created_at=now, created_by=current_user.username)
        db.session.add(row)

    row.client_code = draft_payload.get('client_code') or None
    row.client_name = draft_payload.get('client_name') or None
    row.manual_client_name = draft_payload.get('manual_client_name') or None
    row.category = draft_payload.get('category') or None
    row.driver_name = draft_payload.get('driver_name') or None
    row.manual_bill_no = draft_payload.get('manual_bill_no') or None
    row.item_count = summary['item_count']
    row.total_qty = summary['total_qty']
    row.total_amount = summary['total_amount']
    row.payload = json.dumps(draft_payload, ensure_ascii=True)
    row.updated_at = now

    db.session.commit()
    flash('Draft held successfully', 'success')
    return redirect(url_for('direct_sales_page'))

