"""import helpers."""
from ._common import *  # noqa

def _archive_artifact_bytes(content, filename, kind='artifacts'):
    # Single-store mode: never write artifacts (exports/imports/backups) to disk automatically.
    return None


def _read_meta_kind_from_excel(xls):
    try:
        if META_SHEET_NAME not in (xls.sheet_names or []):
            return None
        meta_df = pd.read_excel(xls, META_SHEET_NAME).fillna('')
        if 'key' not in meta_df.columns or 'value' not in meta_df.columns:
            return None
        kv = {}
        for _, row in meta_df.iterrows():
            k = str(row.get('key', '')).strip().lower()
            if not k:
                continue
            kv[k] = str(row.get('value', '')).strip().lower()
        return kv.get('export_kind') or None
    except Exception:
        return None


def backup_database():
    """Single-store: backups are disabled (no files are written)."""
    return True, "Backup skipped (disabled in single-store mode)"


def _sqlite_db_file_path():
    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    try:
        parsed = make_url(uri)
    except Exception:
        return None
    if (parsed.drivername or '').startswith('sqlite'):
        return parsed.database
    return None


def _normalize_sqlite_value_for_column(value, col):
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        if isinstance(col.type, DateTime):
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return value
        if isinstance(col.type, Date):
            try:
                if 'T' in s:
                    s = s.split('T', 1)[0]
                elif ' ' in s:
                    s = s.split(' ', 1)[0]
                return date.fromisoformat(s)
            except Exception:
                return value
    return value


def _download_stamp(dt=None):
    dt = dt or pk_now()
    return dt.strftime('%d-%m-%Y_%I-%M%p')


def _download_filename(section, ext='xlsx', dt=None):
    sec = re.sub(r'[^A-Za-z0-9]+', '', (section or 'DOWNLOAD')).upper() or 'DOWNLOAD'
    ext = (ext or '').lstrip('.').lower() or 'dat'
    return f"{sec}-{_download_stamp(dt)}.{ext}"


def _selected_master_sheets(section_keys):
    selected = []
    seen = set()
    for key in section_keys or []:
        for sheet in MASTER_SHEET_SECTIONS.get(key, []):
            if sheet not in seen:
                seen.add(sheet)
                selected.append(sheet)
    return selected


def _filter_excel_bytes_to_sheets(file_bytes, allowed_sheets):
    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    available = [s for s in xls.sheet_names if s in set(allowed_sheets or [])]
    if not available:
        raise ValueError('No selected section sheets were found in the uploaded file.')
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        for sheet in available:
            pd.read_excel(xls, sheet).to_excel(writer, sheet_name=sheet[:31], index=False)
    return out.getvalue()


def _normalize_excel_cell(value, col=None):
    """Convert one workbook value to the Python type expected by SQLAlchemy.

    Full XLSX exports intentionally serialize dates as ISO text so they remain
    portable.  SQLite's Date/DateTime binders reject that text on restore;
    conversion therefore has to happen *before* an INSERT or ORM autoflush.
    Invalid typed values raise a short validation error which the importer can
    attach to that row without poisoning the surrounding transaction.
    """
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if hasattr(value, 'item') and not isinstance(value, (str, bytes, datetime, date)):
        try:
            value = value.item()
        except Exception:
            pass

    if col is None:
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        return value

    col_name = getattr(col, 'name', 'value')
    col_type = col.type
    if isinstance(value, str) and not value.strip():
        return None

    if isinstance(col_type, DateTime):
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            # The application stores local, timezone-naive timestamps.
            return value.replace(tzinfo=None) if value.tzinfo else value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return pd.to_datetime(value, unit='D', origin='1899-12-30').to_pydatetime()
            except Exception as exc:
                raise ValueError(f"{col_name}: invalid date/time '{value}'") from exc
        s = str(value).strip()
        if not s:
            return None
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        try:
            parsed = datetime.fromisoformat(s)
        except Exception:
            try:
                parsed = pd.to_datetime(s, errors='raise').to_pydatetime()
            except Exception as exc:
                raise ValueError(f"{col_name}: invalid date/time '{str(value)[:80]}'") from exc
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

    # DateTime is a Date subclass in SQLAlchemy, so this check must be second.
    if isinstance(col_type, Date):
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        s = str(value).strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s.split('T', 1)[0].split(' ', 1)[0])
        except Exception as exc:
            raise ValueError(f"{col_name}: invalid date '{str(value)[:80]}'") from exc

    if isinstance(col_type, Boolean):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        s = str(value).strip().lower()
        if s in ('1', 'true', 'yes', 'on', 'y'):
            return True
        if s in ('0', 'false', 'no', 'off', 'n'):
            return False
        raise ValueError(f"{col_name}: invalid true/false value '{str(value)[:80]}'")

    if isinstance(col_type, Integer):
        try:
            number = float(value)
            if not number.is_integer():
                raise ValueError
            return int(number)
        except Exception as exc:
            raise ValueError(f"{col_name}: invalid whole number '{str(value)[:80]}'") from exc

    if isinstance(col_type, (Float, Numeric)):
        try:
            return float(value)
        except Exception as exc:
            raise ValueError(f"{col_name}: invalid number '{str(value)[:80]}'") from exc

    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _list_data_upgrade_excels(folder):
    files = []
    if not folder or not os.path.isdir(folder):
        return files
    for root, _, names in os.walk(folder):
        for name in names:
            low = name.lower()
            if low.endswith('.xlsx') or low.endswith('.xls'):
                files.append(os.path.join(root, name))
    files.sort()
    return files


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _hash_bytes(blob):
    h = hashlib.sha256()
    h.update(blob or b'')
    return h.hexdigest()


def _get_sqlite_db_path():
    uri = str(current_app.config.get('SQLALCHEMY_DATABASE_URI', '') or '')
    prefix = 'sqlite:///'
    if uri.startswith(prefix):
        return uri[len(prefix):]
    return None


def _snapshot_sqlite_db(stamp, backup_dir=None):
    # Single-store mode: never snapshot/copy DB files automatically.
    return None
    db_path = _get_sqlite_db_path()
    if not db_path or not os.path.exists(db_path):
        return None
    backup_dir = backup_dir or os.path.join(current_app.instance_path, 'deploy_db_backups')
    os.makedirs(backup_dir, exist_ok=True)
    snap_db = os.path.join(backup_dir, f"db_before_migrate_{stamp}.db")
    shutil.copy2(db_path, snap_db)

    snap = {'src_db': db_path, 'snap_db': snap_db}
    for suffix in ['-wal', '-shm']:
        src_sidecar = db_path + suffix
        if os.path.exists(src_sidecar):
            snap_sidecar = snap_db + suffix
            shutil.copy2(src_sidecar, snap_sidecar)
            snap[f'snap_{suffix[1:]}'] = snap_sidecar
            snap[f'src_{suffix[1:]}'] = src_sidecar
    return snap


def _restore_sqlite_snapshot(snap):
    if not snap:
        return
    src_db = snap.get('src_db')
    snap_db = snap.get('snap_db')
    if src_db and snap_db and os.path.exists(snap_db):
        shutil.copy2(snap_db, src_db)
    for suffix in ['wal', 'shm']:
        src_sidecar = snap.get(f'src_{suffix}')
        snap_sidecar = snap.get(f'snap_{suffix}')
        if src_sidecar and snap_sidecar and os.path.exists(snap_sidecar):
            shutil.copy2(snap_sidecar, src_sidecar)


def _create_full_raw_backup(backup_dir=None):
    # Single-store mode: never write backup artifacts automatically.
    return None
    backup_dir = backup_dir or os.path.join(current_app.instance_path, 'full_raw_backups')
    os.makedirs(backup_dir, exist_ok=True)
    stamp = pk_now().strftime('%Y%m%d_%H%M%S')
    backup_name = f"full_raw_export_{stamp}.xlsx"
    backup_path = os.path.join(backup_dir, backup_name)
    data = _build_full_raw_export_bytes()
    with open(backup_path, 'wb') as f:
        f.write(data)
    return backup_name


@import_export_bp.route('/export_excel_all')
@login_required
def export_excel_all():
    """Legacy endpoint compatibility."""
    return redirect(url_for('import_export.export_master'))


