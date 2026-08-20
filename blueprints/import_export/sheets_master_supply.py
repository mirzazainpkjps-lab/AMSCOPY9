"""sheets_master — split from import_export.py."""
from ._common import *  # noqa

def _process_grn(df, strategy, report):
    for _, row in df.iterrows():
        supplier = str(row.get('supplier', '')).strip()
        manual_bill_no = str(row.get('manual_bill_no', '')).strip()
        auto_bill_no = str(row.get('auto_bill_no', '')).strip()
        note = str(row.get('note', '')).strip()
        date_posted = _parse_dt(row.get('date_posted')) or pk_now()

        existing = None
        if manual_bill_no:
            existing = GRN.query.filter_by(manual_bill_no=manual_bill_no).first()
        elif auto_bill_no:
            existing = GRN.query.filter_by(auto_bill_no=auto_bill_no).first()
        
        if existing:
            if strategy == 'update':
                existing.supplier = supplier
                existing.note = note
                existing.date_posted = date_posted
                report['updated'] += 1
            else:
                report['skipped'] += 1
            continue

        db.session.add(GRN(
            supplier=supplier,
            manual_bill_no=manual_bill_no or None,
            auto_bill_no=auto_bill_no or None,
            date_posted=date_posted,
            note=note
        ))
        report['imported'] += 1


def _process_grn_items(df, strategy, report):
    for _, row in df.iterrows():
        manual_bill = str(row.get('grn_manual_bill_no', '') or row.get('grn_manual_bill', '')).strip()
        auto_bill = str(row.get('grn_auto_bill_no', '') or row.get('grn_auto_bill', '')).strip()
        mat_name = str(row.get('material_name', '')).strip()
        if not mat_name:
            continue
            
        grn = None
        if manual_bill:
            grn = GRN.query.filter_by(manual_bill_no=manual_bill).first()
        elif auto_bill:
            grn = GRN.query.filter_by(auto_bill_no=auto_bill).first()
            
        if not grn:
            report['skipped'] += 1
            continue

        qty = float(row.get('qty', 0) or row.get('quantity', 0) or 0)
        price = float(row.get('price', 0) or row.get('rate', 0) or 0)

        # Ensure material exists
        mat = Material.query.filter(func.lower(Material.name) == mat_name.lower()).first()
        if not mat:
            mat = Material(name=mat_name, code=f"MAT-{pk_now().strftime('%f')}", category_id=_default_material_category_id())
            db.session.add(mat)
            db.session.flush()

        exists = GRNItem.query.filter_by(
            grn_id=grn.id,
            mat_name=mat_name,
            qty=qty,
            price_at_time=price
        ).first()
        
        if exists:
            continue

        db.session.add(GRNItem(
            grn_id=grn.id,
            mat_name=mat_name,
            qty=qty,
            price_at_time=price
        ))
        report['imported'] += 1


