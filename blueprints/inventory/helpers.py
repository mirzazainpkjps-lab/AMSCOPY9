"""helpers — split from inventory.py."""
from ._common import *  # noqa

def _entry_best_bill_ref(entry_obj):
    if not entry_obj:
        return ''
    primary = (getattr(entry_obj, 'bill_no', None) or '').strip()
    auto = (getattr(entry_obj, 'auto_bill_no', None) or '').strip()
    if primary and not primary.upper().startswith('UNBILLED'):
        return primary
    if auto and not auto.upper().startswith('UNBILLED'):
        return auto
    inv_id = getattr(entry_obj, 'invoice_id', None)
    if inv_id:
        inv = db.session.get(Invoice, inv_id)
        if inv and not inv.is_void and inv.invoice_no:
            return (inv.invoice_no or '').strip()
    return ''


