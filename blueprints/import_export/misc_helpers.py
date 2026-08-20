"""import helpers."""
from ._common import *  # noqa

def pk_now():
    return datetime.now(PK_TZ).replace(tzinfo=None)


def pk_today():
    return pk_now().date()


def ensure_pandas_installed():
    if pd is None:
        flash(
            'The Import/Export module requires pandas and openpyxl. Install them to use this feature.',
            'danger'
        )
        return False
    return True


def _safe_name(value, fallback='unknown'):
    raw = str(value or '').strip()
    if not raw:
        raw = fallback
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', raw)


def _export_meta_rows(export_kind, scope_ctx):
    return [
        {'key': 'export_kind', 'value': str(export_kind or '').strip().lower()},
        {'key': 'exported_at', 'value': pk_now().isoformat()},
        {'key': 'scope', 'value': scope_ctx.get('scope')},
        {'key': 'tenant_id', 'value': scope_ctx.get('target_tenant_id')},
        {'key': 'tenant_name', 'value': scope_ctx.get('target_tenant_name')},
        {'key': 'format_version', 'value': '2026-04'},
    ]


def _detect_transfer_workbook_kind(file_bytes):
    """
    Detect workbook type so import stays compatible even if user picks wrong option.
    Returns one of: literal_all | all_business | unknown.
    """
    try:
        xls = pd.ExcelFile(io.BytesIO(file_bytes))
    except Exception:
        return 'unknown'

    meta_kind = _read_meta_kind_from_excel(xls)
    if meta_kind in ['literal_all', 'all_business']:
        return meta_kind

    sheets = set(xls.sheet_names or [])
    full_raw_sheets = {t.name[:31] for t in db.metadata.sorted_tables if t.name not in FULL_RAW_EXCLUDE_TABLES}
    master_sheets = set(MASTER_ALL_SHEETS)
    full_raw_hits = len(sheets.intersection(full_raw_sheets))
    master_hits = len(sheets.intersection(master_sheets))

    if full_raw_hits >= 8 and full_raw_hits > master_hits:
        return 'literal_all'
    if master_hits >= 4:
        return 'all_business'
    return 'unknown'


def generate_client_code():
    """Generate next client code in format FBMCL-00001."""
    prefix = 'FBMCL-'
    max_num = 0
    rx = re.compile(r'^FBMCL-(\d+)$', re.IGNORECASE)
    for (raw_code,) in Client.query.with_entities(Client.code).all():
        code = (raw_code or '').strip()
        m = rx.match(code)
        if not m:
            continue
        try:
            max_num = max(max_num, int(m.group(1)))
        except Exception:
            continue
    return f"{prefix}{(max_num + 1):05d}"


def _record_discrepancy(report, msg):
    if 'discrepancies' not in report:
        report['discrepancies'] = []
    report['discrepancies'].append(msg)


def _clean_category(value, fallback='General'):
    cat = str(value or '').strip()
    return cat or fallback


def _default_material_category_id():
    try:
        cat = get_or_create_material_category('General')
        return cat.id if cat else None
    except Exception:
        return None


def validate_client_row(row):
    errors = []
    # Relaxed code validation for legacy + current client code formats.
    code_raw = str(row.get('code', '')).strip()
    if not re.match(r'^(FBMCL-\d+|FBM-\d+|tmpc-\d+)$', code_raw, re.IGNORECASE):
        pass 
    
    if str(row.get('status', '')).upper() not in ['ACTIVE', 'INACTIVE', 'TRUE', 'FALSE', '1', '0', '']:
        errors.append("Invalid Status")
        
    return errors


def validate_dispatch_row(row):
    errors = []
    try:
        float(row.get('qty', 0))
    except:
        errors.append("Qty must be numeric")
        
    bill = str(row.get('bill_no', '')).upper()
    if bill != 'NOT BILLED' and bill != '' and not bill.replace('-','').isalnum():
         pass # Allow alphanumeric bills
         
    return errors


def validate_pending_bill_row(row):
    errors = []
    if not row.get('client_code'):
        errors.append("Missing Client Code")
    return errors


def _full_raw_import_enabled():
    return str(
        os.environ.get('FULL_RAW_IMPORT_ENABLED', current_app.config.get('FULL_RAW_IMPORT_ENABLED', '0'))
    ).strip().lower() in ['1', 'true', 'on', 'yes']


def _serialize_payload(payload):
    out = {}
    for k, v in payload.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _flatten_single_root_folder(release_dir):
    entries = [e for e in os.listdir(release_dir) if e not in ['__MACOSX']]
    if len(entries) != 1:
        return
    inner = os.path.join(release_dir, entries[0])
    if not os.path.isdir(inner):
        return
    for name in os.listdir(inner):
        shutil.move(os.path.join(inner, name), os.path.join(release_dir, name))
    shutil.rmtree(inner, ignore_errors=True)


@import_export_bp.route('/transfer/export', methods=['POST'])
@login_required
def transfer_export():
    try:
        scope_ctx = _resolve_scope_context(
            scope_raw=request.form.get('scope'),
            tenant_id_raw=request.form.get('tenant_id'),
        )
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('import_export.import_export_page'))

    sections = [str(x).strip().lower() for x in request.form.getlist('sections') if str(x).strip()]
    if not sections:
        sections = ['all_business']

    if 'literal_all' in sections:
        content = _build_full_raw_export_bytes(scope_ctx=scope_ctx)
        fname = _download_filename('ALLEXPORT', 'xlsx')
    elif 'all_business' in sections:
        content = _build_master_export_bytes(scope_ctx=scope_ctx)
        fname = _download_filename('MASTERBACKUP', 'xlsx')
    else:
        sheet_names = _selected_master_sheets(sections)
        if not sheet_names:
            flash('Select at least one section for export.', 'warning')
            return redirect(url_for('import_export.import_export_page'))
        content = _filter_excel_bytes_to_sheets(
            _build_master_export_bytes(scope_ctx=scope_ctx),
            sheet_names,
        )
        fname = _download_filename('SECTIONEXPORT', 'xlsx')

    _archive_artifact_bytes(content, fname, kind='exports')
    output = io.BytesIO(content)
    output.seek(0)
    sheet_names = ''
    try:
        sheet_names = ','.join(pd.ExcelFile(io.BytesIO(content)).sheet_names or [])
    except Exception:
        sheet_names = ''
    resp = send_file(
        output,
        as_attachment=True,
        download_name=fname,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    if sheet_names:
        resp.headers['X-AMS-Export-Sheets'] = sheet_names[:2000]
        resp.headers['Access-Control-Expose-Headers'] = 'X-AMS-Export-Sheets, Content-Disposition'
    return resp


@import_export_bp.route('/master/export')
@login_required
def export_master():
    """Export all datasets into a single Excel file with multiple sheets."""
    try:
        scope_ctx = _resolve_scope_context(
            scope_raw=request.args.get('scope'),
            tenant_id_raw=request.args.get('tenant_id'),
        )
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('import_export.import_export_page'))
    content = _build_master_export_bytes(scope_ctx=scope_ctx)
    _archive_artifact_bytes(content, _download_filename('MASTERBACKUP', 'xlsx'), kind='exports')
    output = io.BytesIO(content)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=_download_filename('MASTERBACKUP', 'xlsx'))


def _master_import_worker(flask_app, job_id, file_bytes, username, tenant_id, role, scope):
    with flask_app.app_context():
        with flask_app.test_request_context('/import_export/master/import'):
            g.user = None
            g.is_root = (role == 'root')
            g.tenant_id = tenant_id
            g.enforce_tenant = (tenant_id is not None) and (scope == 'tenant' or not g.is_root)
            _set_import_actor_context(username=username, tenant_id=tenant_id, role=role)
            try:
                _set_master_import_progress(job_id, percent=2, message='Started master import...', done=False, success=False, user=username)
                report = _run_master_import_bytes(
                    file_bytes=file_bytes,
                    actor_username=username,
                    progress_cb=lambda p, m: _set_master_import_progress(job_id, percent=p, message=m, done=False, success=False, user=username),
                )
                _set_master_import_progress(
                    job_id,
                    percent=100,
                    message='Master import completed.',
                    done=True,
                    success=True,
                    user=username,
                    report=report,
                )
            except Exception as e:
                db.session.rollback()
                _set_master_import_progress(
                    job_id,
                    percent=100,
                    message='Master import failed.',
                    done=True,
                    success=False,
                    user=username,
                    error=str(e),
                )
            finally:
                _clear_import_actor_context()


@import_export_bp.route('/master/import/start', methods=['POST'])
@login_required
def master_import_start():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    if not file.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'error': 'Joint Master Import requires Excel file (.xlsx/.xls).'}), 400

    try:
        scope_ctx = _resolve_scope_context(
            scope_raw=request.form.get('scope'),
            tenant_id_raw=request.form.get('tenant_id'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if getattr(current_user, 'role', None) == 'root' and scope_ctx.get('scope') == 'all_tenants':
        return jsonify({
            'error': 'Root all-tenant master import is blocked. Use Literal Full Import for all-tenant restore.'
        }), 400

    job_id = uuid.uuid4().hex
    username = getattr(current_user, 'username', None)
    tenant_id = scope_ctx.get('target_tenant_id')
    role = getattr(current_user, 'role', None)
    _set_master_import_progress(job_id, percent=0, message='Queued...', done=False, success=False, user=username)
    file_bytes = file.read()
    _archive_artifact_bytes(file_bytes, f"master_import_{file.filename}", kind='imports')

    flask_app = current_app._get_current_object()
    t = threading.Thread(
        target=_master_import_worker,
        args=(flask_app, job_id, file_bytes, username, tenant_id, role, scope_ctx.get('scope')),
        daemon=True,
        name=f"master-import-{job_id[:8]}",
    )
    t.start()
    return jsonify({'job_id': job_id})


@import_export_bp.route('/master/import', methods=['POST'])
@login_required
def import_master():
    """Import multiple datasets from a single Excel file (sync endpoint for compatibility)."""
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    try:
        scope_ctx = _resolve_scope_context(
            scope_raw=request.form.get('scope'),
            tenant_id_raw=request.form.get('tenant_id'),
        )
        if getattr(current_user, 'role', None) == 'root' and scope_ctx.get('scope') == 'all_tenants':
            return jsonify({
                'error': 'Root all-tenant master import is blocked. Use Literal Full Import for all-tenant restore.'
            }), 400
        file_bytes = file.read()
        _archive_artifact_bytes(file_bytes, f"master_import_sync_{file.filename}", kind='imports')
        _set_import_actor_context(
            username=getattr(current_user, 'username', None),
            tenant_id=scope_ctx.get('target_tenant_id'),
            role=getattr(current_user, 'role', None)
        )
        g.enforce_tenant = (scope_ctx.get('scope') == 'tenant' and scope_ctx.get('target_tenant_id') is not None)
        g.tenant_id = scope_ctx.get('target_tenant_id')
        report = _run_master_import_bytes(
            file_bytes=file_bytes,
            actor_username=getattr(current_user, 'username', None),
            progress_cb=None,
        )
        return jsonify({'success': True, 'report': report})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        _clear_import_actor_context()


