"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/export', methods=['GET'])
@login_required
def export_data():
    dataset = request.args.get('dataset')
    fmt = request.args.get('format', 'excel')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if dataset == 'clients':
        query = Client.query.all()
        data = [{k: getattr(x, k) for k in CLIENT_SCHEMA if hasattr(x, k)} for x in query]
        # Map status
        for d, x in zip(data, query):
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
            if x.bill_no:
                if PendingBill.query.filter_by(bill_no=x.bill_no, client_code=x.client_code).first():
                    match_status = "MATCHED"
            
            data.append({
                'CLIENT_CODE': x.client_code,
                'CLIENT_NAME': x.client,
                'CLIENT_CATEGORY': x.client_category,
                'TRANSACTION_CATEGORY': 'CEMENT+BILL' if x.bill_no else 'CEMENT',
                'BILL_NO': x.bill_no,
                'BILL_DATE': x.date,
                'CEMENT_BRAND': x.material,
                'QTY': x.qty,
                'NIMBUS': x.nimbus_no,
                'NOTES': '',
                'SOURCE': 'CEMENT',
                'MATCH_STATUS': match_status
            })
        
    elif dataset == 'pending_bills':
        query = PendingBill.query.filter(PendingBill.is_void == False)

        # Support filters from pending_bills page
        start_date = start_date or request.args.get('bill_from')
        end_date = end_date or request.args.get('bill_to')
        client_code = request.args.get('client_code')
        bill_no = request.args.get('bill_no')
        category = request.args.get('category')
        bill_kind = (request.args.get('bill_kind') or '').strip().upper()
        is_cash = request.args.get('is_cash')
        is_manual = request.args.get('is_manual')

        if start_date:
            query = query.filter(PendingBill.created_at >= start_date)
        if end_date:
            query = query.filter(PendingBill.created_at <= f"{end_date} 23:59:59")
        if client_code:
            query = query.filter(PendingBill.client_code == client_code)
        if bill_no:
            query = query.filter(PendingBill.bill_no.ilike(f"%{bill_no}%"))
        if bill_kind in ['SB', 'MB']:
            query = query.filter(PendingBill.bill_kind == bill_kind)
        if is_cash is not None and is_cash != '':
            query = query.filter(PendingBill.is_cash == (is_cash == '1'))
        if is_manual is not None and is_manual != '':
            query = query.filter(PendingBill.is_manual == (is_manual == '1'))
        if category:
            if category == 'Unbilled Cash' or category == 'Cash':
                query = query.filter(PendingBill.is_cash == True)
            elif category == 'Cash Paid':
                query = query.filter(
                    PendingBill.is_paid == True,
                    or_(
                        PendingBill.client_code == 'OPEN-KHATA',
                        func.upper(PendingBill.client_name) == 'OPEN KHATA'
                    )
                )
            elif category == 'Open Khata':
                query = query.filter(
                    or_(
                        PendingBill.client_code == 'OPEN-KHATA',
                        func.upper(PendingBill.client_name) == 'OPEN KHATA'
                    )
                )
            else:
                query = query.join(Client, PendingBill.client_code == Client.code).filter(
                    func.lower(func.trim(Client.category)) == category.lower().strip()
                )
            
        bills = query.order_by(PendingBill.id.desc()).all()
        data = [{
            'client_code': x.client_code,
            'client_name': x.client_name,
            'bill_no': x.bill_no,
            'bill_kind': x.bill_kind,
            'amount': x.amount,
            'reason': x.reason,
            'nimbus': x.nimbus_no,
            'is_paid': x.is_paid,
            'created_at': x.created_at
        } for x in bills]
    elif dataset == 'unpaid_transactions':
        query = PendingBill.query.filter(PendingBill.is_void == False)
        
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        material = request.args.get('material')
        bill_no = request.args.get('bill_no')
        status = request.args.get('status', 'unpaid')
        include_booking = request.args.get('include_booking', '0')

        if start_date:
            query = query.filter(PendingBill.created_at >= start_date)
        if end_date:
            query = query.filter(PendingBill.created_at <= f"{end_date} 23:59:59")
        if material:
            query = query.filter(PendingBill.reason.ilike(f'%{material}%'))
        if bill_no:
            query = query.filter((PendingBill.bill_no.ilike(f'%{bill_no}%')) | (PendingBill.nimbus_no.ilike(f'%{bill_no}%')))
        
        if status == 'paid':
            query = query.filter(PendingBill.is_paid == True)
        elif status == 'unpaid':
            query = query.filter(PendingBill.is_paid == False)
            
        query = query.filter(or_(PendingBill.amount > 0, PendingBill.is_paid == True))

        if include_booking not in ['1', 'true', 'on', 'yes']:
            booked_names = [r[0] for r in db.session.query(Booking.client_name).filter(Booking.is_void == False).distinct().all() if r[0]]
            booked_codes = set()
            if booked_names:
                booked_codes = {c.code for c in Client.query.filter(Client.name.in_(booked_names)).all()}
            if booked_codes:
                query = query.filter(~PendingBill.client_code.in_(booked_codes))
            if booked_names:
                query = query.filter(~PendingBill.client_name.in_(booked_names))
        
        bills = query.order_by(PendingBill.id.desc()).all()
        data = [{
            'client_code': x.client_code,
            'client_name': x.client_name,
            'bill_no': x.bill_no,
            'amount': x.amount,
            'reason': x.reason,
            'nimbus': x.nimbus_no,
            'is_paid': x.is_paid,
            'created_at': x.created_at
        } for x in bills]
    else:
        return "Invalid dataset", 400

    try:
        audit_log(current_user, 'export.data', f'dataset={dataset} format={fmt} rows={len(data)}')
    except Exception:
        pass
        
    df = pd.DataFrame(data)
    section_map = {
        'clients': 'CLIENTLIST',
        'dispatch': 'DISPATCH',
        'pending_bills': 'PENDINGBILLS',
        'unpaid_transactions': 'UNPAIDTRANSACTIONS'
    }
    section = section_map.get(dataset, 'DATAEXPORT')
    
    if fmt == 'csv':
        csv_text = df.to_csv(index=False)
        _archive_artifact_bytes(csv_text, _download_filename(section, 'csv'), kind='exports')
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename={_download_filename(section, 'csv')}"}
        )
    elif fmt == 'pdf':
        # Basic HTML to PDF using weasyprint if available
        html = f"""
        <html><head><style>
            @page {{ size: 14.8cm 21cm; margin: 1cm; }}
            body {{ font-family: sans-serif; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 10px; }}
            th, td {{ border: 1px solid #ddd; padding: 4px; }}
            th {{ background: #f2f2f2; }}
        </style></head><body>
        <h2>{dataset.upper()} EXPORT</h2>
        <p>Generated: {pk_now()}</p>
        {df.to_html(index=False)}
        </body></html>
        """
        try:
            from flask_weasyprint import HTML, render_pdf
            return render_pdf(HTML(string=html), download_name=_download_filename(section, 'pdf'))
        except:
            return "PDF generation not available", 500
    else:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        _archive_artifact_bytes(output.getvalue(), _download_filename(section, 'xlsx'), kind='exports')
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=_download_filename(section, 'xlsx'))

