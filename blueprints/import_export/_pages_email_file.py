"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/email_file', methods=['POST'])
@login_required
def email_file():
    kind = (request.form.get('kind') or '').strip().lower()
    dataset = (request.form.get('dataset') or '').strip()
    fmt = (request.form.get('format') or 'excel').strip().lower()

    filename = None
    mime = None
    content = None

    if kind == 'template':
        filename, mime, content = _build_template_attachment(dataset, fmt)
        if not filename:
            flash('Invalid template selection for email.', 'warning')
            return redirect(url_for('import_export.import_export_page'))
    elif kind == 'master':
        try:
            scope_ctx = _resolve_scope_context(
                scope_raw=request.form.get('scope'),
                tenant_id_raw=request.form.get('tenant_id'),
            )
        except ValueError as e:
            flash(str(e), 'danger')
            return redirect(url_for('import_export.import_export_page'))
        content = _build_master_export_bytes(scope_ctx=scope_ctx)
        filename = f"master_backup_{pk_today()}.xlsx"
        mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    elif kind == 'export':
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        if dataset == 'clients':
            clients = Client.query.all()
            data = [{k: getattr(x, k) for k in CLIENT_SCHEMA if hasattr(x, k)} for x in clients]
            for d, x in zip(data, clients):
                d['status'] = 'ACTIVE' if x.is_active else 'INACTIVE'
        elif dataset == 'dispatch':
            query = Entry.query.filter_by(type='OUT')
            if start_date:
                query = query.filter(Entry.date >= start_date)
            if end_date:
                query = query.filter(Entry.date <= end_date)
            entries = query.all()
            data = []
            for x in entries:
                match_status = "UNMATCHED"
                if x.bill_no and PendingBill.query.filter_by(bill_no=x.bill_no, client_code=x.client_code).first():
                    match_status = "MATCHED"
                data.append({
                    'CLIENT_CODE': x.client_code, 'CLIENT_NAME': x.client, 'CLIENT_CATEGORY': x.client_category,
                    'TRANSACTION_CATEGORY': 'CEMENT+BILL' if x.bill_no else 'CEMENT', 'BILL_NO': x.bill_no,
                    'BILL_DATE': x.date, 'CEMENT_BRAND': x.material, 'QTY': x.qty, 'NIMBUS': x.nimbus_no,
                    'NOTES': '', 'SOURCE': 'CEMENT', 'MATCH_STATUS': match_status
                })
        elif dataset == 'pending_bills':
            query = PendingBill.query.filter(PendingBill.is_void == False)
            if start_date:
                query = query.filter(PendingBill.created_at >= start_date)
            if end_date:
                query = query.filter(PendingBill.created_at <= f"{end_date} 23:59:59")
            bills = query.order_by(PendingBill.id.desc()).all()
            data = [{
                'client_code': x.client_code, 'client_name': x.client_name, 'bill_no': x.bill_no,
                'bill_kind': x.bill_kind,
                'amount': x.amount, 'reason': x.reason, 'nimbus': x.nimbus_no,
                'is_paid': x.is_paid, 'created_at': x.created_at
            } for x in bills]
        else:
            flash('Invalid export dataset for email.', 'warning')
            return redirect(url_for('import_export.import_export_page'))

        df = pd.DataFrame(data)
        base = f"{dataset}_export_{pk_today()}"
        if fmt == 'csv':
            filename = f"{base}.csv"
            mime = 'text/csv'
            content = df.to_csv(index=False).encode('utf-8')
        else:
            filename = f"{base}.xlsx"
            mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            out = io.BytesIO()
            with pd.ExcelWriter(out, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            content = out.getvalue()
    else:
        flash('Invalid email request.', 'warning')
        return redirect(url_for('import_export.import_export_page'))

    flash('Email delivery has been removed from this build. Use Download instead.', 'warning')
    return redirect(url_for('import_export.import_export_page'))

