"""client — clearance / outstanding statement."""
from ._common import *  # noqa

@bp.route('/download_client_clearance/<int:id>')
@login_required
def download_client_clearance(id):
    client = Client.query.get_or_404(id)
    (
        financial_history,
        pending_bills,
        total_debit,
        total_credit,
        total_balance,
        _material_history_grouped
    ) = _build_client_ledger_rows(client)
    unpaid = [b for b in pending_bills if not b.is_paid and not b.is_void and float(b.amount or 0) > 0]
    generated_at = pk_now()
    settings_obj = Settings.query.first()
    rendered = render_template(
        'client_clearance.html',
        client=client,
        settings=settings_obj,
        generated_at=generated_at,
        financial_history=financial_history,
        pending_bills=unpaid,
        total_debit=total_debit,
        total_credit=total_credit,
        total_balance=total_balance,
        auto_print=(request.args.get('action') == 'print'),
    )
    filename = f"CLEARANCE_{re.sub(r'[^A-Za-z0-9]+', '_', client.code or str(client.id)).strip('_') or client.id}.pdf"
    if request.args.get('action') != 'print':
        pdf_response = _try_render_weasy_pdf(rendered, filename, disposition='attachment')
        if pdf_response:
            return pdf_response
    response = make_response(rendered)
    response.headers['Content-Disposition'] = (
        f"{'inline' if request.args.get('action') == 'print' else 'attachment'}; "
        f"filename={filename[:-4]}.html"
    )
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    _disable_response_cache(response)
    return response
