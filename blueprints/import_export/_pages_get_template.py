"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/template/<dataset>')
@login_required
def get_template(dataset):
    """Generate empty templates for manual entry."""
    if not ensure_pandas_installed():
        return redirect(url_for('import_export.import_export_page'))

    fmt = (request.args.get('format') or 'excel').lower()

    if dataset == 'clients':
        df = pd.DataFrame(columns=CLIENT_SCHEMA)
        if fmt == 'csv':
            return Response(
                df.to_csv(index=False),
                mimetype="text/csv",
                headers={"Content-disposition": f"attachment; filename={_download_filename('TEMPLATECLIENTS', 'csv')}"}
            )
    elif dataset == 'dispatch':
        df = pd.DataFrame(columns=DISPATCH_SCHEMA)
        if fmt == 'csv':
            return Response(
                df.to_csv(index=False),
                mimetype="text/csv",
                headers={"Content-disposition": f"attachment; filename={_download_filename('TEMPLATEDISPATCH', 'csv')}"}
            )
    elif dataset == 'pending_bills':
        df = pd.DataFrame(columns=PENDING_BILL_SCHEMA)
        if fmt == 'csv':
            return Response(
                df.to_csv(index=False),
                mimetype="text/csv",
                headers={"Content-disposition": f"attachment; filename={_download_filename('TEMPLATEPENDINGBILLS', 'csv')}"}
            )
    elif dataset == 'client_full':
        if fmt == 'csv':
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('clients.csv', pd.DataFrame(columns=CLIENT_SCHEMA).to_csv(index=False))
                zf.writestr('bookings.csv', pd.DataFrame(columns=BOOKING_SCHEMA).to_csv(index=False))
                zf.writestr('booking_items.csv', pd.DataFrame(columns=BOOKING_ITEM_SCHEMA).to_csv(index=False))
                zf.writestr('dispatch.csv', pd.DataFrame(columns=DISPATCH_SCHEMA).to_csv(index=False))
                zf.writestr('payments.csv', pd.DataFrame(columns=PAYMENT_SCHEMA).to_csv(index=False))
                zf.writestr('sales.csv', pd.DataFrame(columns=SALE_SCHEMA).to_csv(index=False))
                zf.writestr('sale_items.csv', pd.DataFrame(columns=SALE_ITEM_SCHEMA).to_csv(index=False))
                zf.writestr('pending_bills.csv', pd.DataFrame(columns=PENDING_BILL_SCHEMA).to_csv(index=False))
            output.seek(0)
            return send_file(
                output,
                as_attachment=True,
                download_name=_download_filename('TEMPLATECLIENTFULL', 'zip'),
                mimetype='application/zip'
            )
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            pd.DataFrame(columns=CLIENT_SCHEMA).to_excel(writer, sheet_name='Clients', index=False)
            pd.DataFrame(columns=BOOKING_SCHEMA).to_excel(writer, sheet_name='Bookings', index=False)
            pd.DataFrame(columns=BOOKING_ITEM_SCHEMA).to_excel(writer, sheet_name='BookingItems', index=False)
            pd.DataFrame(columns=DISPATCH_SCHEMA).to_excel(writer, sheet_name='Dispatch', index=False)
            pd.DataFrame(columns=PAYMENT_SCHEMA).to_excel(writer, sheet_name='Payments', index=False)
            pd.DataFrame(columns=SALE_SCHEMA).to_excel(writer, sheet_name='Sales', index=False)
            pd.DataFrame(columns=SALE_ITEM_SCHEMA).to_excel(writer, sheet_name='SaleItems', index=False)
            pd.DataFrame(columns=PENDING_BILL_SCHEMA).to_excel(writer, sheet_name='PendingBills', index=False)
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=_download_filename('TEMPLATECLIENTFULL', 'xlsx'))
    else:
        return "Invalid dataset", 400
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    
    return send_file(output, as_attachment=True, download_name=_download_filename(f"TEMPLATE{dataset}", 'xlsx'))

