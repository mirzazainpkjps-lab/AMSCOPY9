"""client — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/download_client_ledger/<int:id>')
@login_required
def download_client_ledger(id):
    client = Client.query.get_or_404(id)
    action = (request.args.get('action') or 'download').lower()
    disposition = 'inline' if action == 'print' else 'attachment'

    (
        financial_history,
        pending_bills,
        total_debit,
        total_credit,
        total_balance,
        material_history_grouped
    ) = _build_client_ledger_rows(client)

    generated_at = pk_now()

    rendered = render_template(
        'client_ledger_print.html',
        client=client,
        financial_history=financial_history,
        pending_bills=pending_bills,
        total_debit=total_debit,
        total_credit=total_credit,
        total_balance=total_balance,
        material_history_grouped=material_history_grouped,
        generated_at=generated_at,
        auto_print=(action == 'print'),
        pdf_error=False
    )

    if action != 'print':
        pdf_response = _try_render_weasy_pdf(
            rendered,
            _download_filename('CLIENTLEDGER', 'pdf'),
            disposition=disposition
        )
        if pdf_response:
            return pdf_response

        rendered = render_template(
            'client_ledger_print.html',
            client=client,
            financial_history=financial_history,
            pending_bills=pending_bills,
            total_debit=total_debit,
            total_credit=total_credit,
            total_balance=total_balance,
            material_history_grouped=material_history_grouped,
            generated_at=generated_at,
            auto_print=False,
            pdf_error=True
        )

    response = make_response(rendered)
    fallback_name = _download_filename('CLIENTLEDGER', 'html')
    response.headers['Content-Disposition'] = f'{disposition}; filename={fallback_name}'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    _disable_response_cache(response)
    return response

