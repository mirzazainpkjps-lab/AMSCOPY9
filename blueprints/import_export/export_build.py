"""export_build — split from import_export.py."""
from ._common import *  # noqa

def _build_template_attachment(dataset, fmt):
    fmt = (fmt or 'excel').lower()
    df = None

    if dataset == 'clients':
        df = pd.DataFrame(columns=CLIENT_SCHEMA)
    elif dataset == 'dispatch':
        df = pd.DataFrame(columns=DISPATCH_SCHEMA)
    elif dataset == 'pending_bills':
        df = pd.DataFrame(columns=PENDING_BILL_SCHEMA)
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
            return "template_client_full_csv.zip", 'application/zip', output.getvalue()
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
        return "template_client_full.xlsx", 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', output.getvalue()
    else:
        return None, None, None

    if fmt == 'csv':
        return f"template_{dataset}.csv", 'text/csv', df.to_csv(index=False).encode('utf-8')

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    return f"template_{dataset}.xlsx", 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', output.getvalue()


def _build_master_export_bytes(scope_ctx=None):
    if scope_ctx is None:
        scope_ctx = _default_scope_context()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        clients = _scoped_model_query(Client, scope_ctx).order_by(Client.code.asc()).all()
        client_data = [{k: getattr(x, k) for k in CLIENT_SCHEMA if hasattr(x, k)} for x in clients]
        for d, c in zip(client_data, clients):
            d['status'] = 'ACTIVE' if c.is_active else 'INACTIVE'
        pd.DataFrame(client_data or [], columns=CLIENT_SCHEMA).to_excel(writer, sheet_name='Clients', index=False)

        bills = _scoped_model_query(PendingBill, scope_ctx).all()
        bill_data = [{
            'client_code': x.client_code, 'bill_no': x.bill_no, 'name': x.client_name,
            'amount': x.amount, 'reason': x.reason, 'nimbus': x.nimbus_no
        } for x in bills]
        pd.DataFrame(bill_data or [], columns=PENDING_BILL_SCHEMA).to_excel(writer, sheet_name='PendingBills', index=False)

        # Materials
        materials = _scoped_model_query(Material, scope_ctx).outerjoin(MaterialCategory).order_by(Material.code.asc()).all()
        material_data = [{
            'code': m.code,
            'name': m.name,
            'category_name': m.category.name if m.category else '',
            'unit_price': m.unit_price,
            'total': m.total,
            'unit': m.unit
        } for m in materials]
        pd.DataFrame(material_data or [], columns=['code', 'name', 'category_name', 'unit_price', 'total', 'unit']).to_excel(writer, sheet_name='Materials', index=False)

        # Material Categories
        categories = _scoped_model_query(MaterialCategory, scope_ctx).all()
        category_data = [{
            'id': c.id,
            'name': c.name,
            'is_active': c.is_active
        } for c in categories]
        pd.DataFrame(category_data or [], columns=['id', 'name', 'is_active']).to_excel(writer, sheet_name='MaterialCategories', index=False)

        entries = _scoped_model_query(Entry, scope_ctx).filter_by(type='OUT').all()
        dispatch_data = []
        for x in entries:
            match_status = "UNMATCHED"
            pending_match = _scoped_model_query(PendingBill, scope_ctx).filter_by(
                bill_no=x.bill_no,
                client_code=x.client_code
            ).first()
            if x.bill_no and pending_match:
                match_status = "MATCHED"
            dispatch_data.append({
                'CLIENT_CODE': x.client_code, 'CLIENT_NAME': x.client, 'CLIENT_CATEGORY': x.client_category,
                'TRANSACTION_CATEGORY': 'CEMENT+BILL' if x.bill_no else 'CEMENT', 'BILL_NO': x.bill_no,
                'BILL_DATE': x.date, 'CEMENT_BRAND': x.material, 'QTY': x.qty, 'NIMBUS': x.nimbus_no,
                'NOTES': '', 'SOURCE': 'CEMENT', 'MATCH_STATUS': match_status
            })
        pd.DataFrame(dispatch_data or [], columns=DISPATCH_SCHEMA).to_excel(writer, sheet_name='Dispatch', index=False)

        bookings = _scoped_model_query(Booking, scope_ctx).filter(Booking.is_void == False).all()
        booking_data = [{
            'client_name': b.client_name, 'manual_bill_no': b.manual_bill_no, 'amount': b.amount,
            'paid_amount': b.paid_amount, 'date_posted': b.date_posted, 'note': b.note
        } for b in bookings]
        pd.DataFrame(booking_data or [], columns=BOOKING_SCHEMA).to_excel(writer, sheet_name='Bookings', index=False)

        booking_items = []
        for b in bookings:
            for i in b.items:
                booking_items.append({
                    'booking_bill_no': b.manual_bill_no, 'booking_client_name': b.client_name,
                    'material_name': i.material_name, 'qty': i.qty, 'price_at_time': i.price_at_time
                })
        pd.DataFrame(booking_items or [], columns=BOOKING_ITEM_SCHEMA).to_excel(writer, sheet_name='BookingItems', index=False)

        payments = _scoped_model_query(Payment, scope_ctx).filter(Payment.is_void == False).all()
        payment_data = [{
            'client_name': p.client_name, 'manual_bill_no': p.manual_bill_no, 'amount': p.amount,
            'method': p.method, 'date_posted': p.date_posted, 'note': p.note
        } for p in payments]
        pd.DataFrame(payment_data or [], columns=PAYMENT_SCHEMA).to_excel(writer, sheet_name='Payments', index=False)

        drawer_rows = _scoped_model_query(FbmCashDrawerEntry, scope_ctx).all()
        drawer_data = [{
            'id': r.id,
            'entry_type': r.entry_type,
            'amount': r.amount,
            'category': r.category,
            'method': r.method,
            'note': r.note,
            'source': r.source,
            'date_posted': r.date_posted,
            'created_by': r.created_by,
            'is_void': r.is_void,
        } for r in drawer_rows]
        pd.DataFrame(
            drawer_data or [],
            columns=['id', 'entry_type', 'amount', 'category', 'method', 'note', 'source', 'date_posted', 'created_by', 'is_void']
        ).to_excel(writer, sheet_name='FBMCashDrawer', index=False)

        drawer_categories = _scoped_model_query(FbmCashDrawerCategory, scope_ctx).order_by(FbmCashDrawerCategory.name.asc()).all()
        drawer_category_data = [{
            'id': c.id,
            'name': c.name,
            'is_active': c.is_active,
            'created_at': c.created_at,
        } for c in drawer_categories]
        pd.DataFrame(
            drawer_category_data or [],
            columns=['id', 'name', 'is_active', 'created_at']
        ).to_excel(writer, sheet_name='FBMCashDrawerCategories', index=False)

        sales = _scoped_model_query(DirectSale, scope_ctx).filter(DirectSale.is_void == False).all()
        sale_data = [{
            'client_name': s.client_name, 'manual_bill_no': s.manual_bill_no, 'auto_bill_no': s.auto_bill_no,
            'category': s.category, 'amount': s.amount, 'paid_amount': s.paid_amount,
            'date_posted': s.date_posted, 'note': s.note
        } for s in sales]
        pd.DataFrame(sale_data or [], columns=SALE_SCHEMA).to_excel(writer, sheet_name='Sales', index=False)

        sale_items = []
        for s in sales:
            bill_ref = s.manual_bill_no or s.auto_bill_no
            for i in s.items:
                sale_items.append({
                    'sale_bill_no': bill_ref, 'sale_client_name': s.client_name,
                    'product_name': i.product_name, 'qty': i.qty, 'price_at_time': i.price_at_time
                })
        pd.DataFrame(sale_items or [], columns=SALE_ITEM_SCHEMA).to_excel(writer, sheet_name='SaleItems', index=False)

        grns = _scoped_model_query(GRN, scope_ctx).filter(GRN.is_void == False).all()
        grn_data = [{
            'supplier': g.supplier,
            'manual_bill_no': g.manual_bill_no,
            'auto_bill_no': g.auto_bill_no,
            'date_posted': g.date_posted,
            'note': g.note
        } for g in grns]
        pd.DataFrame(grn_data or [], columns=['supplier', 'manual_bill_no', 'auto_bill_no', 'date_posted', 'note']).to_excel(writer, sheet_name='GRN', index=False)

        grn_items = []
        for g in grns:
            for i in g.items:
                grn_items.append({
                    'GRN Manual Bill': g.manual_bill_no,
                    'GRN Auto Bill': g.auto_bill_no,
                    'Material Name': i.mat_name,
                    'Quantity': i.qty,
                    'Rate': i.price_at_time
                })
        pd.DataFrame(grn_items or [], columns=['GRN Manual Bill', 'GRN Auto Bill', 'Material Name', 'Quantity', 'Rate']).to_excel(writer, sheet_name='GRNItems', index=False)

        delivery_persons = _scoped_model_query(DeliveryPerson, scope_ctx).order_by(DeliveryPerson.name.asc()).all()
        delivery_person_data = [{
            'name': d.name,
            'phone': d.phone,
            'is_active': d.is_active,
            'created_at': d.created_at
        } for d in delivery_persons]
        pd.DataFrame(
            delivery_person_data or [],
            columns=['name', 'phone', 'is_active', 'created_at']
        ).to_excel(writer, sheet_name='DeliveryPersons', index=False)

        delivery_rents = _scoped_model_query(DeliveryRent, scope_ctx).all()
        rent_data = [{
            'sale_id': r.sale_id,
            'delivery_person_name': r.delivery_person_name,
            'bill_no': r.bill_no,
            'amount': r.amount,
            'note': r.note,
            'date_posted': r.date_posted,
            'created_by': r.created_by,
            'is_void': r.is_void
        } for r in delivery_rents]
        pd.DataFrame(
            rent_data or [],
            columns=['sale_id', 'delivery_person_name', 'bill_no', 'amount', 'note', 'date_posted', 'created_by', 'is_void']
        ).to_excel(writer, sheet_name='DeliveryRents', index=False)

        # Export metadata so import can auto-detect correct parser path.
        users = User.query.order_by(User.username.asc()).all()
        user_cols = [c.name for c in User.__table__.columns if c.name != 'id']
        user_data = []
        for u in users:
            if (u.username or '').strip().lower() == 'root':
                continue
            user_data.append({c: getattr(u, c, None) for c in user_cols})
        pd.DataFrame(user_data or [], columns=user_cols).to_excel(writer, sheet_name='Users', index=False)

        pd.DataFrame(
            _export_meta_rows('all_business', scope_ctx),
            columns=['key', 'value']
        ).to_excel(writer, sheet_name=META_SHEET_NAME, index=False)

    return output.getvalue()


def _build_full_raw_export_bytes(scope_ctx=None):
    """Export physical tables with strict role/scope filtering."""
    if scope_ctx is None:
        scope_ctx = _default_scope_context()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for table in _full_raw_tables_for_scope(scope_ctx):
            q = _scope_table_select(table, scope_ctx)
            if q is None:
                continue
            rows = db.session.execute(q).mappings().all()
            cols = [c.name for c in table.columns]
            data = []
            for r in rows:
                row_data = {}
                for col_name in cols:
                    cell = r.get(col_name)
                    if isinstance(cell, (datetime, date)):
                        row_data[col_name] = cell.isoformat()
                    else:
                        row_data[col_name] = cell
                data.append(row_data)
            # Excel sheet names max length = 31
            sheet_name = table.name[:31]
            pd.DataFrame(data or [], columns=cols).to_excel(writer, sheet_name=sheet_name, index=False)
        pd.DataFrame(
            _export_meta_rows('literal_all', scope_ctx),
            columns=['key', 'value']
        ).to_excel(writer, sheet_name=META_SHEET_NAME, index=False)
    return output.getvalue()


