"""engine — split from import_export.py."""
from ._common import *  # noqa

def _upsert_pending_bill_from_booking(client_name, manual_bill_no, amount, paid_amount, note=''):
    bill_no = str(manual_bill_no or '').strip()
    if not bill_no:
        return

    booking_client_name = str(client_name or '').strip()
    resolved_client = None
    if booking_client_name:
        resolved_client = Client.query.filter_by(name=booking_client_name).first()

    final_client_code = resolved_client.code if resolved_client else ''
    final_client_name = resolved_client.name if resolved_client else booking_client_name

    bill_amount = float(amount or 0)
    paid = float(paid_amount or 0)
    is_paid = paid >= bill_amount and bill_amount > 0

    pb = PendingBill.query.filter_by(bill_no=bill_no).first()
    if pb:
        if final_client_code:
            pb.client_code = final_client_code
        if final_client_name:
            pb.client_name = final_client_name
        pb.amount = bill_amount
        pb.is_paid = is_paid
        pb.is_manual = True
        if note:
            pb.note = note
        if not pb.reason:
            pb.reason = 'Imported Booking'
        return

    new_pb = PendingBill(
        client_code=final_client_code,
        client_name=final_client_name,
        bill_no=bill_no,
        amount=bill_amount,
        reason='Imported Booking',
        is_paid=is_paid,
        created_at=pk_now().strftime('%Y-%m-%d %H:%M'),
        created_by=_actor_username(),
        is_manual=True,
        note=str(note or '').strip()
    )
    db.session.add(new_pb)


_USER_BOOL_COLS = {
    'can_view_stock', 'can_view_daily', 'can_view_history', 'can_import_export',
    'can_manage_directory', 'can_view_dashboard', 'can_manage_grn', 'can_manage_bookings',
    'can_manage_payments', 'can_manage_sales', 'can_view_delivery_rent',
    'can_manage_pending_bills', 'can_view_reports', 'can_manage_notifications',
    'can_view_client_ledger', 'can_view_supplier_ledger', 'can_view_decision_ledger',
    'can_manage_clients', 'can_manage_suppliers', 'can_manage_materials',
    'can_manage_delivery_persons', 'can_access_settings', 'restrict_backdated_edit',
}
_USER_COLS_BY_NAME = {c.name: c for c in User.__table__.columns}
_USER_COL_NAMES = set(_USER_COLS_BY_NAME)
_IMPORT_REPORT_MAX_INLINE_ISSUES = 5


def _as_bool_cell(val):
    if isinstance(val, bool):
        return val
    s = str(val or '').strip().lower()
    if s in ('1', 'true', 'yes', 'on', 'y'):
        return True
    if s in ('0', 'false', 'no', 'off', 'n', '', 'none', 'nan'):
        return False
    raise ValueError(f"invalid true/false value '{str(val)[:80]}'")


def _short_import_error(exc, limit=300):
    """Human-readable error without SQL text, parameters, or huge trace text."""
    original = getattr(exc, 'orig', None)
    message = str(original or exc or 'Unknown import error')
    message = re.sub(r'\s*\[SQL:.*$', '', message, flags=re.IGNORECASE | re.DOTALL)
    message = re.sub(r'\s*\(Background on this error at:.*$', '', message, flags=re.IGNORECASE | re.DOTALL)
    message = ' '.join(message.split())
    return (message[:limit - 1] + '…') if len(message) > limit else message


def _safe_report_payload(payload):
    """Serialize a row for diagnostics while keeping credentials out of reports."""
    safe = {}
    for key, value in (payload or {}).items():
        low = str(key).lower()
        if any(secret in low for secret in ('password', 'secret', 'token', 'code_hash')):
            safe[key] = '[redacted]'
        elif isinstance(value, (datetime, date)):
            safe[key] = value.isoformat()
        else:
            safe[key] = value
    return safe


def _user_payload_from_row(src):
    payload = {}
    for name, col in _USER_COLS_BY_NAME.items():
        if name == 'id':
            continue
        try:
            if name not in src:
                continue
            raw = src.get(name)
        except Exception:
            continue
        try:
            if raw is None or pd.isna(raw):
                continue
        except Exception:
            if raw is None:
                continue
        if isinstance(raw, str) and not raw.strip():
            continue

        # Use the same typed conversion as physical-table rows.  This is the
        # key protection for User.created_at being exported as ISO text.
        value = _normalize_excel_cell(raw, col)
        if name in _USER_BOOL_COLS:
            value = _as_bool_cell(value)
        elif name == 'role':
            role = str(value or '').strip().lower()
            value = 'admin' if role in ('admin', 'administrator') else 'user'
        elif name == 'status':
            status = str(value or '').strip().lower()
            value = 'inactive' if status in ('inactive', 'disabled', '0', 'false') else 'active'
        payload[name] = value
    return payload


def _find_user_sheet(xls):
    names = list(xls.sheet_names or [])
    for want in ('user', 'users', 'User', 'Users'):
        if want in names:
            return want
    for name in names:
        if str(name).strip().lower() in ('user', 'users'):
            return name
    return None


def _restore_users_from_excel(xls):
    """Restore each valid manager independently; never delete local users.

    Every row is flushed inside a SAVEPOINT.  A bad date, duplicate username,
    or future-schema value is reported for that user while subsequent rows and
    business tables continue importing with a healthy Session.
    """
    sheet = _find_user_sheet(xls)
    result = {
        'name': 'user', 'status': 'skipped', 'inserted': 0, 'updated': 0,
        'skipped': 0, 'failed': 0, 'error': '', 'people': [], 'issue_rows': [],
    }
    if not sheet:
        result['error'] = 'User sheet not present (older/partial backup); users already on this system were kept.'
        result['issue_rows'].append({
            'table': 'user', 'sheet_row': '', 'status': 'unavailable',
            'reason': result['error'], 'primary_key': '', 'label': '', 'row_json': '',
        })
        return result
    try:
        df = pd.read_excel(xls, sheet).fillna('')
        df.columns = [str(c).strip() for c in df.columns]
    except Exception as exc:
        result['status'] = 'failed'
        result['failed'] = 1
        result['error'] = _short_import_error(exc)
        result['issue_rows'].append({
            'table': 'user', 'sheet_row': '', 'status': 'failed',
            'reason': result['error'], 'primary_key': '', 'label': '', 'row_json': '',
        })
        return result

    me = (getattr(current_user, 'username', None) or '').strip().lower()
    messages = []
    for source_index, src in df.iterrows():
        excel_row = int(source_index) + 2 if isinstance(source_index, int) else str(source_index)
        raw_username = str(src.get('username', '') or '').strip()
        try:
            payload = _user_payload_from_row(src)
            username = str(payload.get('username') or '').strip()
            if not username:
                result['skipped'] += 1
                reason = f'Row {excel_row}: missing username; row skipped.'
                messages.append(reason)
                result['issue_rows'].append({
                    'table': 'user', 'sheet_row': excel_row, 'status': 'skipped',
                    'reason': 'missing_username', 'primary_key': '', 'label': '', 'row_json': '',
                })
                continue
            if username.lower() == 'root':
                result['skipped'] += 1
                result['people'].append({'username': username, 'role': payload.get('role') or 'root', 'action': 'kept (protected)'})
                result['issue_rows'].append({
                    'table': 'user', 'sheet_row': excel_row, 'status': 'skipped',
                    'reason': 'protected_root_user', 'primary_key': '', 'label': username, 'row_json': '',
                })
                continue

            with db.session.no_autoflush:
                existing = User.query.filter(
                    func.lower(func.trim(User.username)) == username.lower()
                ).first()

            if existing and (existing.username or '').strip().lower() == me:
                # Keep the operator logged in while still restoring permissions.
                for key in ('password_hash', 'password_plain', 'status'):
                    payload.pop(key, None)

            with db.session.begin_nested():
                if existing:
                    for key, value in payload.items():
                        if key in _USER_COL_NAMES and key != 'id':
                            setattr(existing, key, value)
                    db.session.flush()
                    action = 'updated'
                else:
                    clean = {k: v for k, v in payload.items() if k in _USER_COL_NAMES and k != 'id'}
                    db.session.add(User(**clean))
                    db.session.flush()
                    action = 'created'

            if action == 'updated':
                result['updated'] += 1
            else:
                result['inserted'] += 1
            result['people'].append({
                'username': username,
                'role': payload.get('role') or (getattr(existing, 'role', None) if existing else 'user'),
                'action': action,
            })
        except Exception as exc:
            reason = _short_import_error(exc)
            result['failed'] += 1
            username = raw_username or '(missing username)'
            messages.append(f'Row {excel_row} ({username}): {reason}')
            result['people'].append({
                'username': username, 'role': '', 'action': 'failed', 'error': reason,
            })
            result['issue_rows'].append({
                'table': 'user', 'sheet_row': excel_row, 'status': 'failed',
                'reason': reason, 'primary_key': '', 'label': username, 'row_json': '',
            })

    if result['failed']:
        result['status'] = 'partial' if (result['inserted'] or result['updated']) else 'failed'
    elif result['skipped'] and not (result['inserted'] or result['updated']):
        result['status'] = 'skipped'
    else:
        result['status'] = 'ok'
    result['error'] = ' | '.join(messages[:_IMPORT_REPORT_MAX_INLINE_ISSUES])
    if len(messages) > _IMPORT_REPORT_MAX_INLINE_ISSUES:
        result['error'] += f" | +{len(messages) - _IMPORT_REPORT_MAX_INLINE_ISSUES} more (download report)"
    return result


def _full_import_report_dir():
    from app.services.import_artifacts import reports_dir
    return str(reports_dir())


def _write_full_import_report(report, issue_rows, mode, scope_ctx, source_file_name):
    """Persist an issue-only CSV plus metadata; report creation cannot undo data."""
    stamp = pk_now().strftime('%Y%m%d_%H%M%S_%f')
    report_name = f'full_raw_import_report_{stamp}.csv'
    report_dir = _full_import_report_dir()
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, report_name)
    fields = ['table', 'sheet_row', 'status', 'reason', 'primary_key', 'label', 'row_json']
    with open(report_path, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for issue in issue_rows:
            writer.writerow({key: issue.get(key, '') for key in fields})

    report_meta = {
        'name': report_name,
        'created_at': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
        'mode': mode,
        'scope': scope_ctx.get('scope'),
        'inserted': report.get('inserted', 0),
        'updated': report.get('updated', 0),
        'skipped': report.get('skipped', 0),
        'failed': report.get('failed', 0),
        'warnings': report.get('warnings', 0),
        'tables': report.get('tables', 0),
        'status': report.get('status'),
        'source_file': source_file_name,
        'issue_rows_count': len(issue_rows),
    }
    meta_path = os.path.join(report_dir, report_name.replace('.csv', '.meta.json'))
    with open(meta_path, 'w', encoding='utf-8') as handle:
        json.dump(report_meta, handle, ensure_ascii=False, indent=2)
    return report_name, report_meta


def _run_full_raw_import_bytes(file_bytes, scope_ctx, mode, source_file_name):
    try:
        xls = pd.ExcelFile(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(f'Invalid Excel file: {_short_import_error(exc)}') from exc

    scoped_tables = _full_raw_tables_for_scope(scope_ctx)
    sheet_to_table = {}
    for table in scoped_tables:
        sheet_name = table.name[:31]
        if sheet_name in sheet_to_table and sheet_to_table[sheet_name].name != table.name:
            raise RuntimeError(
                f"Two database tables map to Excel sheet '{sheet_name}'. Export/import cannot continue safely."
            )
        sheet_to_table[sheet_name] = table

    workbook_sheets = list(xls.sheet_names or [])
    selected_tables = [sheet_to_table[name] for name in workbook_sheets if name in sheet_to_table]
    if not selected_tables:
        raise ValueError(
            'No importable sheets found for current scope. The file does not look like an '
            'export from this app — use "Export Full XLSX" on the Import & Export page to '
            'create a compatible backup, then import that file.'
        )

    report = {
        'inserted': 0, 'updated': 0, 'skipped': 0, 'failed': 0, 'errors': 0,
        'warnings': 0, 'tables': len(selected_tables), 'status': 'ok',
        'table_results': [], 'users': [],
    }
    issue_rows = []
    target_tenant_id = scope_ctx.get('target_tenant_id')

    # Explain sheets this app version does not understand instead of silently
    # pretending they were restored.
    ignored_sheets = [
        name for name in workbook_sheets
        if name not in sheet_to_table and name != META_SHEET_NAME
    ]
    for sheet_name in ignored_sheets:
        report['warnings'] += 1
        note = 'No matching database table exists in this app version; sheet was not imported.'
        report['table_results'].append({
            'name': sheet_name, 'status': 'unavailable', 'inserted': 0, 'updated': 0,
            'skipped': 0, 'failed': 0, 'error': note,
        })
        issue_rows.append({
            'table': sheet_name, 'sheet_row': '', 'status': 'unavailable',
            'reason': note, 'primary_key': '', 'label': '', 'row_json': '',
        })

    # A literal export with a missing sheet is incomplete.  Keep that table's
    # current rows (even in overwrite mode) and identify it clearly.
    if _read_meta_kind_from_excel(xls) == 'literal_all':
        workbook_set = set(workbook_sheets)
        for table in scoped_tables:
            expected = table.name[:31]
            if expected in workbook_set:
                continue
            report['warnings'] += 1
            note = 'Sheet is not available in this backup; existing table data was kept.'
            report['table_results'].append({
                'name': table.name, 'status': 'unavailable', 'inserted': 0, 'updated': 0,
                'skipped': 0, 'failed': 0, 'error': note,
            })
            issue_rows.append({
                'table': table.name, 'sheet_row': '', 'status': 'unavailable',
                'reason': note, 'primary_key': '', 'label': '', 'row_json': '',
            })

    if mode == 'replace_tenant_data':
        # Clear only tables actually supplied by the workbook. Missing sheets
        # remain untouched, which makes older/partial backups recoverable.
        for table in reversed(selected_tables):
            if table.name in WIPE_PROTECTED_TABLES or table.name == 'user':
                continue
            if scope_ctx.get('scope') == 'tenant' and 'tenant_id' in table.c:
                db.session.execute(table.delete().where(table.c.tenant_id == target_tenant_id))
            else:
                db.session.execute(table.delete())

    user_restore = _restore_users_from_excel(xls)
    report['users'] = user_restore.get('people') or []
    user_stat = {
        'name': 'user (roles/managers)',
        'status': user_restore.get('status'),
        'inserted': user_restore.get('inserted', 0),
        'updated': user_restore.get('updated', 0),
        'skipped': user_restore.get('skipped', 0),
        'failed': user_restore.get('failed', 0),
        'error': user_restore.get('error') or '',
    }
    report['table_results'].append(user_stat)
    report['inserted'] += user_stat['inserted']
    report['updated'] += user_stat['updated']
    report['skipped'] += user_stat['skipped']
    report['failed'] += user_stat['failed']
    report['errors'] += user_stat['failed']
    if user_stat['status'] == 'skipped' and user_stat['error']:
        already_reported_missing_user = any(
            row.get('name') == 'user' and row.get('status') == 'unavailable'
            for row in report['table_results'][:-1]
        )
        if not already_reported_missing_user:
            report['warnings'] += 1
    issue_rows.extend(user_restore.get('issue_rows') or [])

    for table in selected_tables:
        if table.name == 'user':
            continue
        table_stat = {
            'name': table.name, 'status': 'ok', 'inserted': 0, 'updated': 0,
            'skipped': 0, 'failed': 0, 'error': '',
        }
        messages = []
        try:
            df = pd.read_excel(xls, table.name[:31]).fillna('')
            df.columns = [str(column).strip() for column in df.columns]
        except Exception as exc:
            reason = _short_import_error(exc)
            table_stat.update(status='failed', failed=1, error=reason)
            report['failed'] += 1
            report['errors'] += 1
            issue_rows.append({
                'table': table.name, 'sheet_row': '', 'status': 'failed',
                'reason': reason, 'primary_key': '', 'label': '', 'row_json': '',
            })
            report['table_results'].append(table_stat)
            continue

        unknown_columns = [name for name in df.columns if name not in table.c]
        if unknown_columns:
            report['warnings'] += 1
            messages.append('Ignored unavailable column(s): ' + ', '.join(unknown_columns[:12]))
            issue_rows.append({
                'table': table.name, 'sheet_row': '', 'status': 'warning',
                'reason': messages[-1], 'primary_key': '', 'label': '', 'row_json': '',
            })

        pk_cols = list(table.primary_key.columns)
        pk_names = {column.name for column in pk_cols}
        for source_index, src in df.iterrows():
            excel_row = int(source_index) + 2 if isinstance(source_index, int) else str(source_index)
            payload = {}
            try:
                for column in table.columns:
                    name = column.name
                    if name not in df.columns:
                        continue
                    value = _normalize_excel_cell(src.get(name), column)
                    if name == 'tenant_id' and scope_ctx.get('scope') == 'tenant':
                        value = target_tenant_id
                    if column.primary_key and value in (None, ''):
                        # Let an integer autoincrement key be generated.
                        continue
                    payload[name] = value

                if not payload:
                    report['skipped'] += 1
                    table_stat['skipped'] += 1
                    issue_rows.append({
                        'table': table.name, 'sheet_row': excel_row, 'status': 'skipped',
                        'reason': 'empty_row', 'primary_key': '', 'label': '', 'row_json': '',
                    })
                    continue

                duplicate_values = None
                with db.session.begin_nested():
                    if pk_cols and all(payload.get(column.name) not in (None, '') for column in pk_cols):
                        pk_values = [payload[column.name] for column in pk_cols]
                        condition = and_(*[column == value for column, value in zip(pk_cols, pk_values)])
                        with db.session.no_autoflush:
                            if db.session.execute(select(table).where(condition).limit(1)).first():
                                duplicate_values = pk_values
                    if duplicate_values is None:
                        db.session.execute(table.insert().values(**payload))

                if duplicate_values is not None:
                    report['skipped'] += 1
                    table_stat['skipped'] += 1
                    issue_rows.append({
                        'table': table.name, 'sheet_row': excel_row, 'status': 'skipped',
                        'reason': 'duplicate_primary_key',
                        'primary_key': ','.join(str(value) for value in duplicate_values),
                        'label': _build_report_label(payload, pk_names),
                        'row_json': json.dumps(_safe_report_payload(payload), ensure_ascii=False, default=str),
                    })
                    continue

                report['inserted'] += 1
                table_stat['inserted'] += 1
            except Exception as exc:
                reason = _short_import_error(exc)
                report['failed'] += 1
                report['errors'] += 1
                table_stat['failed'] += 1
                if len(messages) < _IMPORT_REPORT_MAX_INLINE_ISSUES:
                    messages.append(f'Row {excel_row}: {reason}')
                issue_rows.append({
                    'table': table.name, 'sheet_row': excel_row, 'status': 'failed',
                    'reason': reason,
                    'primary_key': ','.join(
                        str(payload.get(column.name, '')) for column in pk_cols
                    ),
                    'label': _build_report_label(payload, pk_names),
                    'row_json': json.dumps(_safe_report_payload(payload), ensure_ascii=False, default=str),
                })

        if table_stat['failed']:
            table_stat['status'] = 'partial' if table_stat['inserted'] else 'failed'
        elif unknown_columns:
            table_stat['status'] = 'warning'
        table_stat['error'] = ' | '.join(messages)
        if table_stat['failed'] > _IMPORT_REPORT_MAX_INLINE_ISSUES:
            table_stat['error'] += (
                f" | +{table_stat['failed'] - _IMPORT_REPORT_MAX_INLINE_ISSUES} more failed row(s) "
                '(download report)'
            )
        report['table_results'].append(table_stat)

    if report['failed']:
        report['status'] = 'partial' if (report['inserted'] or report['updated']) else 'failed'
    elif report['warnings']:
        report['status'] = 'warning'
    else:
        report['status'] = 'ok'

    # SAVEPOINT failures above are already rolled back. This commit persists all
    # valid rows and proves the Session is still usable after rejected rows.
    db.session.commit()

    from app.services.import_artifacts import (
        discard_import_artifacts,
        purge_expired_failed_artifacts,
        should_discard_artifacts,
        verify_post_import_integrity,
    )

    integrity_ok, integrity_reason = verify_post_import_integrity()
    if not integrity_ok:
        report['status'] = 'failed'
        report['failed'] = int(report.get('failed') or 0) + 1
        report['table_results'].append({
            'name': 'post-import integrity', 'status': 'failed', 'inserted': 0,
            'updated': 0, 'skipped': 0, 'failed': 1,
            'error': f'Import committed but integrity verification failed: {integrity_reason}',
        })

    report_name = None
    keep_file = (not should_discard_artifacts(report.get('status'), failed_count=int(report.get('failed') or 0))) or not integrity_ok
    if keep_file:
        try:
            report_name, report_meta = _write_full_import_report(
                report, issue_rows, mode, scope_ctx, source_file_name
            )
            session['full_raw_import_report'] = report_name
            session['full_raw_import_report_meta'] = report_meta
        except Exception as exc:
            report['warnings'] += 1
            report['status'] = 'warning' if report['status'] == 'ok' else report['status']
            report['table_results'].append({
                'name': 'import report file', 'status': 'warning', 'inserted': 0,
                'updated': 0, 'skipped': 0, 'failed': 0,
                'error': f'Data imported, but the downloadable report could not be written: {_short_import_error(exc)}',
            })
    else:
        # Success: summary stays in the DB/session payload; files are disposable.
        session.pop('full_raw_import_report', None)
        session['full_raw_import_report_meta'] = {
            'name': None,
            'created_at': pk_now().strftime('%Y-%m-%d %H:%M:%S'),
            'mode': mode,
            'scope': scope_ctx.get('scope'),
            'inserted': report.get('inserted', 0),
            'updated': report.get('updated', 0),
            'skipped': report.get('skipped', 0),
            'failed': report.get('failed', 0),
            'warnings': report.get('warnings', 0),
            'tables': report.get('tables', 0),
            'status': report.get('status'),
            'source_file': source_file_name,
            'issue_rows_count': 0,
            'persisted_file': False,
        }
        discard_import_artifacts()
    purge_expired_failed_artifacts()
    return report, report_name


def _process_master_rows(sheet_name, df, processor, report):
    """Run legacy/master processors one row per SAVEPOINT.

    The processors predate partial imports and normally receive a full
    DataFrame. Supplying one row at a time means a conversion/integrity error
    can be rolled back and reported without discarding good rows from the same
    sheet or leaving the Session in a failed state.
    """
    counter_keys = ('imported', 'updated', 'skipped', 'errors')
    for ordinal, (source_index, _row) in enumerate(df.iterrows(), start=2):
        before = {key: int(report.get(key, 0) or 0) for key in counter_keys}
        try:
            with db.session.begin_nested():
                processor(df.loc[[source_index]].copy())
                db.session.flush()
        except Exception as exc:
            for key in counter_keys:
                report[key] = before[key]
            report['errors'] = before['errors'] + 1
            reason = _short_import_error(exc)
            report.setdefault('error_details', []).append(
                f'{sheet_name} row {ordinal}: {reason}'
            )


def _run_master_import_bytes(file_bytes, actor_username=None, progress_cb=None):
    ok, msg = backup_database()
    if not ok:
        raise RuntimeError(f"Backup failed: {msg}")

    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    report = {
        'imported': 0, 'updated': 0, 'skipped': 0, 'errors': 0,
        'failed': 0, 'warnings': 0, 'status': 'ok',
        'error_details': [], 'discrepancies': [], 'table_results': [], 'users': [],
    }
    user_restore = _restore_users_from_excel(xls)
    report['users'] = user_restore.get('people') or []
    report['table_results'].append({
        'name': 'user (roles/managers)',
        'status': user_restore.get('status'),
        'inserted': user_restore.get('inserted') or 0,
        'updated': user_restore.get('updated') or 0,
        'skipped': 0,
        'failed': user_restore.get('failed') or 0,
        'error': user_restore.get('error') or '',
    })
    report['imported'] += user_restore.get('inserted') or 0
    report['updated'] += user_restore.get('updated') or 0
    if user_restore.get('failed'):
        report['errors'] += user_restore.get('failed') or 0
    steps = [
        'Clients', 'MaterialCategories', 'Materials', 'PendingBills',
        'Dispatch', 'Bookings', 'BookingItems', 'Payments', 'FBMCashDrawer',
        'FBMCashDrawerCategories', 'Sales', 'SaleItems', 'GRN', 'GRNItems',
        'DeliveryPersons', 'DeliveryRents'
    ]
    total = len(steps)

    def _read_sheet(name):
        d = pd.read_excel(xls, name)
        d.columns = [c.lower().strip().replace(' ', '_') for c in d.columns]
        return d.fillna('')

    for idx, sheet_name in enumerate(steps, start=1):
        pct = 8 + int(((idx - 1) / total) * 84)
        exists = sheet_name in xls.sheet_names
        if progress_cb:
            msg = f"Processing sheet {idx}/{total}: {sheet_name}" if exists else f"Skipping missing sheet {idx}/{total}: {sheet_name}"
            progress_cb(pct, msg)
        if not exists:
            continue

        before_counts = {
            'imported': report.get('imported', 0),
            'updated': report.get('updated', 0),
            'skipped': report.get('skipped', 0),
            'errors': report.get('errors', 0),
        }
        before_error_details = len(report.get('error_details') or [])
        if sheet_name == 'Clients':
            df = _read_sheet('Clients')
            _process_master_rows(sheet_name, df, lambda one: _process_clients(one, 'update', report), report)
        elif sheet_name == 'MaterialCategories':
            df = _read_sheet('MaterialCategories')
            _process_master_rows(sheet_name, df, lambda one: _process_material_categories(one, report), report)
        elif sheet_name == 'Materials':
            df = _read_sheet('Materials')
            _process_master_rows(sheet_name, df, lambda one: _process_materials(one, report), report)
        elif sheet_name == 'PendingBills':
            df = _read_sheet('PendingBills')
            _process_master_rows(
                sheet_name, df,
                lambda one: _process_pending_bills(one, 'update', 'create', report),
                report,
            )
        elif sheet_name == 'Dispatch':
            df = _read_sheet('Dispatch')
            df.rename(columns={'cement_brand': 'item', 'client_name': 'customer', 'bill_date': 'date', 'nimbus': 'nimbus_no'}, inplace=True)
            _process_master_rows(
                sheet_name, df,
                lambda one: _process_dispatch(one, 'skip', 'create', report),
                report,
            )
        elif sheet_name == 'Bookings':
            df = _read_sheet('Bookings')
            _process_master_rows(sheet_name, df, lambda one: _process_bookings(one, 'update', report), report)
        elif sheet_name == 'BookingItems':
            df = _read_sheet('BookingItems')
            _process_master_rows(sheet_name, df, lambda one: _process_booking_items(one, 'update', report), report)
        elif sheet_name == 'Payments':
            df = _read_sheet('Payments')
            _process_master_rows(sheet_name, df, lambda one: _process_payments(one, 'update', report), report)
        elif sheet_name == 'FBMCashDrawer':
            df = _read_sheet('FBMCashDrawer')
            for _, row in df.iterrows():
                try:
                    amount = float(row.get('amount', 0) or 0)
                except Exception:
                    amount = 0
                entry_type = str(row.get('entry_type', 'out')).strip().lower()
                if entry_type not in ['in', 'out']:
                    entry_type = 'out'
                method = str(row.get('method', 'Cash')).strip() or 'Cash'
                category = str(row.get('category', '')).strip()
                note = str(row.get('note', '')).strip()
                source = str(row.get('source', 'manual')).strip() or 'manual'
                created_by = str(row.get('created_by', '')).strip() or (actor_username or _actor_username())
                is_void = str(row.get('is_void', '')).strip().lower() in ['true', '1', 'yes', 'on']
                posted_raw = row.get('date_posted')
                posted_at = posted_raw if isinstance(posted_raw, datetime) else None
                if posted_at is None:
                    try:
                        posted_at = datetime.fromisoformat(str(posted_raw).strip())
                    except Exception:
                        posted_at = pk_now()

                existing = None
                raw_id = str(row.get('id', '')).strip()
                if raw_id:
                    try:
                        existing = db.session.get(FbmCashDrawerEntry, int(float(raw_id)))
                    except Exception:
                        existing = None

                if existing:
                    existing.entry_type = entry_type
                    existing.amount = amount
                    existing.category = category
                    existing.method = method
                    existing.note = note
                    existing.source = source
                    existing.date_posted = posted_at
                    existing.created_by = created_by
                    existing.is_void = is_void
                    report['updated'] += 1
                else:
                    db.session.add(FbmCashDrawerEntry(
                        entry_type=entry_type,
                        amount=amount,
                        category=category,
                        method=method,
                        note=note,
                        source=source,
                        date_posted=posted_at,
                        created_by=created_by,
                        is_void=is_void,
                    ))
                    report['imported'] += 1
        elif sheet_name == 'FBMCashDrawerCategories':
            df = _read_sheet('FBMCashDrawerCategories')
            for _, row in df.iterrows():
                name = str(row.get('name', '')).strip()
                if not name:
                    report['skipped'] += 1
                    continue
                is_active_raw = str(row.get('is_active', '')).strip().lower()
                is_active = is_active_raw not in ['false', '0', 'no', 'off']
                existing = FbmCashDrawerCategory.query.filter(
                    func.lower(func.trim(FbmCashDrawerCategory.name)) == name.lower()
                ).first()
                if existing:
                    existing.is_active = is_active
                    report['updated'] += 1
                else:
                    db.session.add(FbmCashDrawerCategory(name=name, is_active=is_active))
                    report['imported'] += 1
        elif sheet_name == 'Sales':
            df = _read_sheet('Sales')
            _process_master_rows(sheet_name, df, lambda one: _process_sales(one, 'update', report), report)
        elif sheet_name == 'SaleItems':
            df = _read_sheet('SaleItems')
            _process_master_rows(sheet_name, df, lambda one: _process_sale_items(one, 'update', report), report)
        elif sheet_name == 'GRN':
            df = _read_sheet('GRN')
            _process_master_rows(sheet_name, df, lambda one: _process_grn(one, 'update', report), report)
        elif sheet_name == 'GRNItems':
            df = _read_sheet('GRNItems')
            _process_master_rows(sheet_name, df, lambda one: _process_grn_items(one, 'update', report), report)
        elif sheet_name == 'DeliveryPersons':
            df = _read_sheet('DeliveryPersons')
            for _, row in df.iterrows():
                name = str(row.get('name', '')).strip()
                if not name:
                    continue
                phone = str(row.get('phone', '')).strip()
                existing = DeliveryPerson.query.filter(
                    func.lower(func.trim(DeliveryPerson.name)) == name.lower()
                ).first()
                is_active_raw = str(row.get('is_active', '')).strip().lower()
                is_active = is_active_raw not in ['false', '0', 'no', 'off']
                if existing:
                    existing.is_active = is_active
                    if phone:
                        existing.phone = phone
                    report['updated'] += 1
                else:
                    db.session.add(DeliveryPerson(name=name, phone=phone or None, is_active=is_active))
                    report['imported'] += 1
        elif sheet_name == 'DeliveryRents':
            df = _read_sheet('DeliveryRents')
            for _, row in df.iterrows():
                driver_name = str(row.get('delivery_person_name', '')).strip()
                bill_no = str(row.get('bill_no', '')).strip()
                if not driver_name:
                    report['skipped'] += 1
                    continue
                try:
                    amount = float(row.get('amount', 0) or 0)
                except Exception:
                    amount = 0

                sale_id = None
                raw_sale_id = str(row.get('sale_id', '')).strip()
                if raw_sale_id:
                    try:
                        sale_id = int(float(raw_sale_id))
                    except Exception:
                        sale_id = None

                if not sale_id and bill_no:
                    sale = DirectSale.query.filter(
                        or_(DirectSale.manual_bill_no == bill_no, DirectSale.auto_bill_no == bill_no)
                    ).first()
                    if sale:
                        sale_id = sale.id

                existing = None
                if sale_id:
                    existing = DeliveryRent.query.filter_by(sale_id=sale_id).first()
                if not existing and bill_no:
                    existing = DeliveryRent.query.filter(
                        DeliveryRent.bill_no == bill_no,
                        func.lower(func.trim(DeliveryRent.delivery_person_name)) == driver_name.lower()
                    ).first()

                is_void_raw = str(row.get('is_void', '')).strip().lower()
                is_void = is_void_raw in ['true', '1', 'yes', 'on']
                note = str(row.get('note', '')).strip()
                created_by = str(row.get('created_by', '')).strip() or (actor_username or _actor_username())

                if existing:
                    existing.delivery_person_name = driver_name
                    existing.bill_no = bill_no
                    existing.amount = amount
                    existing.note = note
                    existing.created_by = created_by
                    existing.is_void = is_void
                    report['updated'] += 1
                else:
                    db.session.add(DeliveryRent(
                        sale_id=sale_id,
                        delivery_person_name=driver_name,
                        bill_no=bill_no,
                        amount=amount,
                        note=note,
                        created_by=created_by,
                        is_void=is_void
                    ))
                    report['imported'] += 1

        imported_delta = report.get('imported', 0) - before_counts['imported']
        updated_delta = report.get('updated', 0) - before_counts['updated']
        skipped_delta = report.get('skipped', 0) - before_counts['skipped']
        error_delta = report.get('errors', 0) - before_counts['errors']
        new_error_details = (report.get('error_details') or [])[before_error_details:]
        report['table_results'].append({
            'name': sheet_name,
            'status': 'partial' if error_delta else 'ok',
            'inserted': imported_delta,
            'updated': updated_delta,
            'skipped': skipped_delta,
            'failed': error_delta,
            'error': ' | '.join(new_error_details[:_IMPORT_REPORT_MAX_INLINE_ISSUES]),
        })

    if report.get('errors'):
        report['failed'] = report.get('errors', 0)
        report['status'] = 'partial' if (report.get('imported') or report.get('updated')) else 'failed'
    if progress_cb:
        progress_cb(97, 'Finalizing import...')
    db.session.commit()
    return report


