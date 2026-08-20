"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/execute_import', methods=['POST'])
@login_required
def execute_import():
    """Process the import with selected options."""
    # ===== PANDAS DEPENDENCY CHECK =====
    if not _validate_pandas_installed():
        return jsonify({'error': 'pandas library is not installed. Run: pip install pandas>=2.3.3'}), 500
    # ===== END DEPENDENCY CHECK =====
    
    file = request.files.get('file')
    dataset_type = request.form.get('dataset_type')
    conflict_strategy = request.form.get('conflict_strategy', 'skip') # skip, update
    missing_client_strategy = request.form.get('missing_client_strategy', 'skip') # create, skip, stop
    if file:
        try:
            raw = file.read()
            if hasattr(file, 'stream'):
                file.stream.seek(0)
            _archive_artifact_bytes(raw, f"execute_import_{dataset_type}_{file.filename}", kind='imports')
        except Exception:
            logging.exception('Failed to archive execute_import upload')
    
    # 1. Safety Backup
    success, msg = backup_database()
    if not success:
        return jsonify({'error': f"Backup failed: {msg}"}), 500
        
    try:
        report = {'imported': 0, 'updated': 0, 'skipped': 0, 'errors': 0, 'error_details': [], 'discrepancies': []}

        # 2. Process based on type
        if dataset_type == 'client_full':
            if not file.filename.lower().endswith(('.xlsx', '.xls')):
                return jsonify({'error': 'Client Full import requires Excel template (.xlsx/.xls).'}), 400
            xls = pd.ExcelFile(file)
            sheets = {}
            if 'Clients' in xls.sheet_names:
                d = pd.read_excel(xls, 'Clients').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['clients'] = d
            if 'Bookings' in xls.sheet_names:
                d = pd.read_excel(xls, 'Bookings').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['bookings'] = d
            if 'BookingItems' in xls.sheet_names:
                d = pd.read_excel(xls, 'BookingItems').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['booking_items'] = d
            if 'Dispatch' in xls.sheet_names:
                d = pd.read_excel(xls, 'Dispatch').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['dispatch'] = d
            if 'Payments' in xls.sheet_names:
                d = pd.read_excel(xls, 'Payments').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['payments'] = d
            if 'Sales' in xls.sheet_names:
                d = pd.read_excel(xls, 'Sales').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['sales'] = d
            if 'SaleItems' in xls.sheet_names:
                d = pd.read_excel(xls, 'SaleItems').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['sale_items'] = d
            if 'PendingBills' in xls.sheet_names:
                d = pd.read_excel(xls, 'PendingBills').fillna('')
                d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
                sheets['pending_bills'] = d

            name_to_code, code_to_name = _build_triage_maps(sheets.values())

            if 'clients' in sheets:
                _process_clients(_apply_triage(sheets['clients'], name_to_code, code_to_name), 'update', report)
            if 'bookings' in sheets:
                _process_bookings(_apply_triage(sheets['bookings'], name_to_code, code_to_name), conflict_strategy, report, allow_missing=True)
            if 'booking_items' in sheets:
                _process_booking_items(sheets['booking_items'], conflict_strategy, report)
            if 'dispatch' in sheets:
                d = sheets['dispatch']
                d.rename(columns={'cement_brand': 'item', 'client_name': 'customer', 'bill_date': 'date', 'nimbus': 'nimbus_no'}, inplace=True)
                _process_dispatch(_apply_triage(d, name_to_code, code_to_name), conflict_strategy, missing_client_strategy, report, allow_missing=True)
            if 'payments' in sheets:
                _process_payments(_apply_triage(sheets['payments'], name_to_code, code_to_name), conflict_strategy, report, allow_missing=True)
            if 'sales' in sheets:
                _process_sales(_apply_triage(sheets['sales'], name_to_code, code_to_name), conflict_strategy, report, allow_missing=True)
            if 'sale_items' in sheets:
                _process_sale_items(sheets['sale_items'], conflict_strategy, report)
            if 'pending_bills' in sheets:
                _process_pending_bills(_apply_triage(sheets['pending_bills'], name_to_code, code_to_name), conflict_strategy, missing_client_strategy, report, allow_missing=True)
        else:
            if file.filename.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)
            
            df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]
            df = df.fillna('')
        
        if dataset_type == 'client_full':
            pass
        elif dataset_type == 'clients':
            _process_clients(df, conflict_strategy, report)
        elif dataset_type == 'pending_bills':
            _process_pending_bills(df, conflict_strategy, missing_client_strategy, report, allow_missing=False)
        elif dataset_type == 'dispatch':
            # Add renaming for user's format
            df.rename(columns={
                'cement_brand': 'item',
                'client_name': 'customer',
                'bill_date': 'date',
                'nimbus': 'nimbus_no'
            }, inplace=True)
            _process_dispatch(df, conflict_strategy, missing_client_strategy, report, allow_missing=False)
        else:
            return jsonify({'error': 'Unknown dataset type'}), 400
            
        db.session.commit()
        try:
            audit_log(
                current_user,
                'import.execute',
                f'dataset={dataset_type} imported={report.get("imported")} updated={report.get("updated")} errors={report.get("errors")}',
            )
        except Exception:
            pass
        return jsonify({'success': True, 'report': report})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

