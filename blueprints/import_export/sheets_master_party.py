"""sheets_master — split from import_export.py."""
from ._common import *  # noqa

def _process_clients(df, strategy, report):
    for _, row in df.iterrows():
        code = str(row.get('code', '')).strip()
        if not code:
            report['errors'] += 1
            continue
        category_val = str(row.get('category', '')).strip()
            
        existing = Client.query.filter_by(code=code).first()
        
        if existing:
            if strategy == 'update':
                existing.name = row.get('name', existing.name)
                existing.phone = str(row.get('phone', existing.phone))
                existing.address = str(row.get('address', existing.address))
                if category_val:
                    existing.category = category_val
                existing.financial_book_no = str(row.get('financial_book_no', existing.financial_book_no or '') or '')
                existing.book_no = str(row.get('book_no', existing.book_no or '') or '')
                existing.location_url = str(row.get('location_url', existing.location_url or '') or '')
                existing.financial_page = str(row.get('financial_page', existing.financial_page or '') or '')
                existing.cement_book_no = str(row.get('cement_book_no', existing.cement_book_no or '') or '')
                existing.cement_page = str(row.get('cement_page', existing.cement_page or '') or '')
                existing.steel_book_no = str(row.get('steel_book_no', existing.steel_book_no or '') or '')
                existing.steel_page = str(row.get('steel_page', existing.steel_page or '') or '')
                existing.page_notes = str(row.get('page_notes', existing.page_notes or '') or '')
                status_val = str(row.get('status', '')).upper()
                if status_val:
                    existing.is_active = (status_val == 'ACTIVE' or status_val == 'TRUE')
                report['updated'] += 1
            else:
                report['skipped'] += 1
        else:
            new_client = Client(
                code=code,
                name=row.get('name', 'Unknown'),
                phone=str(row.get('phone', '')),
                address=str(row.get('address', '')),
                category=_clean_category(category_val),
                financial_book_no=str(row.get('financial_book_no', '') or ''),
                book_no=str(row.get('book_no', '') or ''),
                location_url=str(row.get('location_url', '') or ''),
                financial_page=str(row.get('financial_page', '') or ''),
                cement_book_no=str(row.get('cement_book_no', '') or ''),
                cement_page=str(row.get('cement_page', '') or ''),
                steel_book_no=str(row.get('steel_book_no', '') or ''),
                steel_page=str(row.get('steel_page', '') or ''),
                page_notes=str(row.get('page_notes', '') or ''),
                is_active=True
            )
            db.session.add(new_client)
            report['imported'] += 1


def _process_pending_bills(df, strategy, missing_client_strategy, report, allow_missing=False):
    for _, row in df.iterrows():
        bill_no = str(row.get('bill_no', '')).strip()
        client_code = str(row.get('client_code', '')).strip()
        
        if not bill_no or not client_code:
            report['errors'] += 1
            _record_discrepancy(report, f"PendingBills: Missing bill_no/client_code (bill_no='{bill_no}', client_code='{client_code}')")
            if not allow_missing:
                continue
            
        # Check Client Dependency
        client = Client.query.filter_by(code=client_code).first()
        if not client:
            if missing_client_strategy == 'stop':
                raise Exception(f"Missing client {client_code} for bill {bill_no}")
            elif missing_client_strategy == 'skip':
                if not allow_missing:
                    report['skipped'] += 1
                    continue
                _record_discrepancy(report, f"PendingBills: Missing client {client_code} for bill {bill_no} (imported as-is)")
            elif missing_client_strategy == 'create':
                client = Client(code=client_code, name=row.get('name', 'Imported Client'), is_active=True)
                db.session.add(client)
                db.session.flush() # Get ID
        
        existing = PendingBill.query.filter_by(bill_no=bill_no).first() if bill_no else None
        
        if existing:
            if strategy == 'update':
                existing.amount = float(row.get('amount', 0))
                existing.reason = row.get('reason', existing.reason)
                existing.nimbus_no = row.get('nimbus', existing.nimbus_no)
                report['updated'] += 1
            else:
                report['skipped'] += 1
        else:
            new_bill = PendingBill(
                client_code=client_code,
                client_name=client.name if client else str(row.get('name', '')).strip(),
                bill_no=bill_no,
                amount=float(row.get('amount', 0)),
                reason=row.get('reason', 'Imported'),
                nimbus_no=row.get('nimbus', ''),
                created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
                created_by=_actor_username()
            )
            db.session.add(new_bill)
            report['imported'] += 1


def _process_material_categories(df, report):
    for _, row in df.iterrows():
        name = str(row.get('name', '')).strip()
        if not name:
            continue
        
        # Check existence (case-insensitive)
        cat = MaterialCategory.query.filter(
            func.lower(MaterialCategory.name) == name.lower()
        ).first()
        
        if not cat:
            cat = MaterialCategory(name=name, is_active=True)
            db.session.add(cat)
            report['imported'] += 1
        else:
            # We don't overwrite existing categories to avoid breaking IDs, just ensure it exists
            pass


def _process_materials(df, report):
    for _, row in df.iterrows():
        code = str(row.get('code', '')).strip()
        name = str(row.get('name', '')).strip()
        if not name:
            continue
            
        cat_name = str(row.get('category_name', '')).strip()
        cat = None
        if cat_name:
            cat = MaterialCategory.query.filter(func.lower(MaterialCategory.name) == cat_name.lower()).first()
            if not cat:
                cat = MaterialCategory(name=cat_name, is_active=True)
                db.session.add(cat)
                db.session.flush()
        
        # Try finding by code first, then name
        mat = None
        if code:
            mat = Material.query.filter_by(code=code).first()
        if not mat:
            mat = Material.query.filter(func.lower(Material.name) == name.lower()).first()
            
        if mat:
            mat.name = name
            if code: mat.code = code
            if cat: mat.category_id = cat.id
            # During master import, always reactivate matched materials so dispatch/import doesn't block.
            mat.is_active = True
            try: mat.unit_price = float(row.get('unit_price', 0))
            except: pass
            try: mat.total = float(row.get('total', 0))
            except: pass
            if 'unit' in row: mat.unit = str(row.get('unit', '')).strip() or 'Bags'
            report['updated'] += 1
        else:
            new_mat = Material(
                code=code or f"MAT-{pk_now().strftime('%f')}",
                name=name,
                category_id=cat.id if cat else None,
                unit_price=float(row.get('unit_price', 0) or 0),
                total=float(row.get('total', 0) or 0),
                is_active=True,
                unit=str(row.get('unit', 'Bags')).strip() or 'Bags'
            )
            db.session.add(new_mat)
            report['imported'] += 1


