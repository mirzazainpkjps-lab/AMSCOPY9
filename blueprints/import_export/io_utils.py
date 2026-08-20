"""io_utils — split from import_export.py."""
from ._common import *  # noqa

def _parse_dt(value):
    if value is None:
        return None
    txt = str(value).strip()
    if not txt:
        return None
    try:
        return pd.to_datetime(txt).to_pydatetime()
    except Exception:
        return None


