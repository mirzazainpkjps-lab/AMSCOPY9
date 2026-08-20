"""import helpers."""
from ._common import *  # noqa

def _build_triage_maps(dfs):
    name_to_code = {}
    code_to_name = {}
    name_cols = ['client_name', 'name', 'customer']
    code_cols = ['client_code', 'code']
    for df in dfs:
        if df is None:
            continue
        cols = set(df.columns)
        for _, row in df.iterrows():
            code = ''
            for c in code_cols:
                if c in cols:
                    code = str(row.get(c, '')).strip()
                    if code:
                        break
            name = ''
            for n in name_cols:
                if n in cols:
                    name = str(row.get(n, '')).strip()
                    if name:
                        break
            if name and code:
                name_to_code[name] = code
                code_to_name[code] = name
    return name_to_code, code_to_name


def _apply_triage(df, name_to_code, code_to_name):
    if df is None:
        return df
    cols = set(df.columns)
    name_cols = [c for c in ['client_name', 'name', 'customer'] if c in cols]
    has_code = 'client_code' in cols
    for idx, row in df.iterrows():
        code = str(row.get('client_code', '')).strip() if has_code else ''
        name = ''
        for n in name_cols:
            name = str(row.get(n, '')).strip()
            if name:
                break
        if has_code and not code and name and name in name_to_code:
            df.at[idx, 'client_code'] = name_to_code[name]
            code = name_to_code[name]
        if name_cols and not name and code and code in code_to_name:
            df.at[idx, name_cols[0]] = code_to_name[code]
    return df


def _build_report_label(payload, pk_names):
    preferred_keys = [
        'name', 'code', 'username', 'action', 'details', 'client_name', 'client',
        'bill_no', 'manual_bill_no', 'auto_bill_no', 'nimbus_no', 'material',
        'mat_name', 'supplier', 'product_name', 'category', 'phone'
    ]
    parts = []
    for k in preferred_keys:
        if k in payload and k not in pk_names:
            v = payload.get(k)
            if v not in [None, '']:
                parts.append(f"{k}={v}")
        if len(parts) >= 3:
            break
    if not parts:
        for k in sorted(payload.keys()):
            if k in pk_names:
                continue
            v = payload.get(k)
            if v not in [None, '']:
                parts.append(f"{k}={v}")
            if len(parts) >= 3:
                break
    return "; ".join(parts)


@import_export_bp.route('/full_raw_import_report/<report_name>')
@login_required
def full_raw_import_report(report_name):
    if not current_app.config.get('LOGIN_DISABLED') and getattr(current_user, 'role', None) not in ['admin', 'root']:
        return "Forbidden", 403
    safe_name = os.path.basename(report_name or '')
    if not safe_name.endswith('.csv') or safe_name != report_name:
        return "Invalid report", 400
    report_dir = _get_full_raw_report_dir()
    report_path = os.path.join(report_dir, safe_name)
    if not os.path.exists(report_path):
        return "Report not found", 404
    return send_file(report_path, as_attachment=True, download_name=_download_filename('IMPORTREPORT', 'csv'), mimetype='text/csv')


def _get_full_raw_report_dir():
    from app.services.import_artifacts import reports_dir
    return str(reports_dir())


def _list_full_raw_reports():
    report_dir = _get_full_raw_report_dir()
    if not os.path.exists(report_dir):
        return []
    reports = []
    for name in os.listdir(report_dir):
        if not name.startswith('full_raw_import_report_') or not name.endswith('.csv'):
            continue
        path = os.path.join(report_dir, name)
        meta_path = os.path.join(report_dir, name.replace('.csv', '.meta.json'))
        try:
            stat = os.stat(path)
            created_at = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            with open(path, 'r', encoding='utf-8') as f:
                row_count = max(0, sum(1 for _ in f) - 1)
        except Exception:
            created_at = ''
            row_count = ''
        meta = None
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception:
                meta = None
        reports.append({
            'name': name,
            'created_at': created_at,
            'row_count': row_count,
            'scope': (meta or {}).get('scope'),
            'mode': (meta or {}).get('mode'),
            'tenant_name': (meta or {}).get('tenant_name'),
            'status': (meta or {}).get('status'),
            'inserted': (meta or {}).get('inserted'),
            'updated': (meta or {}).get('updated'),
            'skipped': (meta or {}).get('skipped'),
            'failed': (meta or {}).get('failed'),
            'warnings': (meta or {}).get('warnings'),
            'tables': (meta or {}).get('tables'),
            'source_file': (meta or {}).get('source_file'),
        })
    reports.sort(key=lambda r: r['name'], reverse=True)
    return reports


