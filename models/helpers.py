from .__base import *  # noqa

def pk_model_now():
    """Default timestamp for all transactional records in Pakistan Standard Time."""
    return datetime.now(PK_TZ).replace(tzinfo=None)


def _normalize_namespace_model(namespace):
    ns = (namespace or AUTO_BILL_NS_DEFAULT_MODEL).strip().upper()
    if not ns:
        ns = AUTO_BILL_NS_DEFAULT_MODEL
    if not re.fullmatch(r'[A-Z][A-Z0-9]{1,7}', ns):
        ns = AUTO_BILL_NS_DEFAULT_MODEL
    return ns


def _extract_sb_parts_model(value):
    raw = (value or '').strip()
    if not raw:
        return (None, None)
    txt = raw.upper()
    if txt.startswith('MB NO.'):
        return (None, None)

    m = re.match(r'^SB\s*-\s*([A-Z][A-Z0-9]{1,7})\s*-\s*(\d+)$', txt)
    if m:
        return (_normalize_namespace_model(m.group(1)), int(m.group(2)))

    body = raw
    if txt.startswith('SB NO.'):
        body = raw.split('.', 1)[1].strip() if '.' in raw else ''
    elif txt.startswith('SB '):
        body = raw[2:].strip()
    elif txt.startswith('AUTO '):
        body = raw[5:].strip()
        body_up = body.upper()
        if body_up.startswith('SB NO.'):
            body = body.split('.', 1)[1].strip() if '.' in body else ''
        elif body_up.startswith('SB '):
            body = body[2:].strip()

    if body.startswith('#'):
        body = body[1:].strip()
    if re.fullmatch(r'\d+\.0+', body or ''):
        body = body.split('.', 1)[0]
    if re.fullmatch(r'\d+', body or ''):
        return (None, int(body))
    return (None, None)


def _normalize_auto_bill_model(value, namespace=AUTO_BILL_NS_DEFAULT_MODEL):
    ns_default = _normalize_namespace_model(namespace)
    parsed_ns, seq = _extract_sb_parts_model(value)
    if seq is None:
        return None
    ns = parsed_ns or ns_default
    return f"SB-{ns}-{int(seq)}"


def _normalize_manual_bill_model(value):
    raw = (value or '').strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper.startswith('MB NO.') or upper.startswith('SB NO.'):
        body = raw.split('.', 1)[1].strip() if '.' in raw else ''
    else:
        body = raw
    if body.startswith('#'):
        body = body[1:].strip()
    if re.fullmatch(r'\d+\.0+', body or ''):
        body = body.split('.', 1)[0]
    if not body:
        return None
    if re.fullmatch(r'\d+', body):
        body = str(int(body))
    return f"MB NO.{body}"


def _parse_bill_kind_model(value):
    txt = (value or '').strip().upper()
    if txt.startswith('SB NO.') or txt.startswith('SB-'):
        return 'SB'
    if txt.startswith('MB NO.'):
        return 'MB'
    _, seq = _extract_sb_parts_model(value)
    if seq is not None:
        return 'SB'
    return 'UNKNOWN'


def get_or_create_material_category(name='General'):
    from models.catalog import MaterialCategory
    target = (name or 'General').strip()
    if not target:
        target = 'General'
    q = MaterialCategory.query.filter(
        func.lower(func.trim(MaterialCategory.name)) == target.lower()
    )
    cat = q.first()
    if cat:
        if not cat.is_active:
            cat.is_active = True
        return cat
    cat = MaterialCategory(name=target, is_active=True)
    db.session.add(cat)
    db.session.flush()
    return cat


def ensure_import_tables():
    db.create_all()

