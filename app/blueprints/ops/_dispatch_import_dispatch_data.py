"""dispatch — split from ops.py."""
from ._common import *  # noqa

@bp.route('/import_dispatch_data', methods=['POST'])
@login_required
def import_dispatch_data():
    if current_user.role not in ['admin', 'root']:
        flash('Only tenant admin or root can run import/export operations.', 'danger')
        return redirect(url_for('index'))
    import pandas as pd
    from datetime import datetime
    file = request.files.get('file')
    if not file or not file.filename:
        flash('No file selected', 'danger')
        return redirect(url_for('import_export.import_export_page'))

    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file, low_memory=False)
        else:
            df = pd.read_excel(file)

        count = 0
        for _, row in df.iterrows():
            code = str(row.get('CLIENT_CODE', '')).strip()
            name = str(row.get('CLIENT_NAME', '')).strip()
            c_cat = str(row.get('CLIENT_CATEGORY', '')).strip()
            t_cat = str(row.get('TRANSACTION_CATEGORY', '')).strip()
            bill_no = str(row.get('BILL_NO', '')).strip()
            b_date = str(row.get('BILL_DATE', '')).strip()
            brand = str(row.get('CEMENT_BRAND', '')).strip()
            qty_val = row.get('QTY', 0)
            try:
                qty = float(qty_val)
            except:
                qty = 0
            nimbus = str(row.get('NIMBUS', '')).strip()
            notes = str(row.get('NOTES', '')).strip()

            if not brand or qty <= 0: continue

            # Date conversion
            final_date = None
            date_formats = ['%m/%d/%Y', '%Y-%m-%d', '%d/%m/%Y']
            for fmt in date_formats:
                try:
                    dt_obj = datetime.strptime(b_date, fmt)
                    final_date = dt_obj.strftime('%Y-%m-%d')
                    break
                except:
                    continue

            if not final_date:
                final_date = pk_now().strftime('%Y-%m-%d')

            # Ensure client exists
            if code and name:
                client = Client.query.filter_by(code=code).first()
                if not client:
                    client = Client(code=code, name=name, category=(c_cat or 'General'))
                    db.session.add(client)
                elif c_cat:
                    client.category = c_cat

            # Ensure material exists
            mat = Material.query.filter_by(name=brand).first()
            if not mat:
                mat = Material(name=brand, code=f"MAT-{brand[:3].upper()}", category_id=_get_default_material_category_id())
                db.session.add(mat)

            # Create entry
            entry = Entry(
                date=final_date,
                time="00:00:00",
                type='OUT',
                material=brand,
                client=name,
                client_code=code,
                qty=qty,
                bill_no=bill_no if bill_no != 'UNBILLED' else None,
                nimbus_no=nimbus,
                client_category=c_cat,
                transaction_category=t_cat,
                created_by=current_user.username
            )
            db.session.add(entry)

            # If billed and has bill_no, add to PendingBill if not exists
            if t_cat == 'BILLED' and bill_no and bill_no != 'UNBILLED':
                existing_bill = PendingBill.query.filter_by(bill_no=bill_no, client_code=code).first()
                if not existing_bill:
                    pb = PendingBill(
                        client_code=code,
                        client_name=name,
                        bill_no=bill_no,
                        bill_kind=parse_bill_kind(bill_no),
                        nimbus_no=nimbus,
                        amount=0,
                        reason=notes,
                        created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                        created_by=current_user.username
                    )
                    db.session.add(pb)

            count += 1

        db.session.commit()
        flash(f'Imported {count} dispatching entries successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Import failed: {str(e)}', 'danger')

    return redirect(url_for('import_export.import_export_page'))

