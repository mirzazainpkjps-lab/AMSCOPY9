"""client — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/download_full_client_history/<int:id>')
@login_required
def download_full_client_history(id):
    client = Client.query.get_or_404(id)
    (
        financial_history,
        _pending_bills,
        total_debit,
        total_credit,
        total_balance,
        material_history_grouped
    ) = _build_client_ledger_rows(client)
    active_bills = _client_all_active_bills(client)
    summary = _client_history_summary(client, financial_history, material_history_grouped, active_bills)
    receipt_blocks = _client_history_receipts(client)
    generated_at = pk_now()
    settings_obj = Settings.query.first()
    generated_by = getattr(current_user, 'username', '') or 'System'

    rendered = render_template(
        'client_full_history_pdf.html',
        client=client,
        settings=settings_obj,
        generated_at=generated_at,
        generated_by=generated_by,
        summary=summary,
        financial_history=financial_history,
        pending_bills=active_bills,
        total_debit=total_debit,
        total_credit=total_credit,
        total_balance=total_balance,
        material_history_grouped=material_history_grouped,
        receipt_blocks=receipt_blocks
    )

    filename = f"CLIENT_HISTORY_{re.sub(r'[^A-Za-z0-9]+', '_', client.code or str(client.id)).strip('_') or client.id}.pdf"
    pdf_response = _try_render_weasy_pdf(rendered, filename, disposition='attachment')
    if pdf_response:
        return pdf_response

    response = make_response(rendered)
    response.headers['Content-Disposition'] = f"attachment; filename={filename[:-4]}.html"
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    _disable_response_cache(response)
    return response

