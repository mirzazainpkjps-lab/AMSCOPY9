from .__base import *  # noqa
from .sales import *  # noqa
from .stock import *  # noqa
from .parties import *  # noqa
from .cash import *  # noqa
from .delivery import *  # noqa
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from utils.money import sync_money_fields
from .helpers import (
    _normalize_auto_bill_model,
    _normalize_manual_bill_model,
    _normalize_namespace_model,
    _parse_bill_kind_model,
)

# Money columns that are plain ``db.Float`` with no integer-minor mirror.
# Binary floats cannot represent most decimal money values, so arithmetic like
# ``qty * unit_rate`` drifts and the drift reaches storage — a PendingBill was
# found holding ``99999.69999999998``. Every write already passes through
# ``before_flush``, so quantising here is the one place that covers all
# mutation routes, including legacy ones that assign a raw float.
_FLOAT_MONEY_FIELDS = {
    'AccountReconciliation': ('adjustment_amount',),
    'Booking': ('amount', 'discount', 'paid_amount'),
    'BookingItem': ('price_at_time',),
    'CashFlowDifferenceAdjustment': ('amount',),
    'Client': ('opening_balance',),
    'DeliveryPerson': ('opening_balance',),
    'DeliveryPersonPayment': ('waive_off_amount',),
    'DeliveryRent': ('amount',),
    'DirectSale': ('amount', 'delivery_rent_cost', 'discount', 'paid_amount'),
    'DirectSaleDraft': ('total_amount',),
    'DirectSaleItem': ('cost_rate_at_sale', 'price_at_time'),
    'FBMRental': ('discount_amount', 'paid_amount', 'total_amount'),
    'FbmCashDrawerEntry': ('amount',),
    'GRN': ('adjustment_amount', 'discount', 'freight_cost', 'loading_cost',
            'paid_amount', 'tax_amount'),
    'GRNAllocation': ('cost_rate',),
    'GRNItem': ('price_at_time',),
    'Invoice': ('balance', 'total_amount'),
    'Material': ('unit_price',),
    'MaterialReturn': ('amount',),
    'MaterialReturnItem': ('price_at_time', 'rent_rate', 'unit_rate'),
    'PendingBill': ('amount',),
    'SaleDeliveryPerson': ('rent_amount',),
    'Supplier': ('opening_balance',),
    'WaiveOff': ('amount',),
}


def _quantize_float_money(obj):
    """Round an object's float money columns to exactly 2 decimal places."""
    fields = _FLOAT_MONEY_FIELDS.get(type(obj).__name__)
    if not fields:
        return
    for attr in fields:
        value = getattr(obj, attr, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            rounded = float(
                Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            )
        except (InvalidOperation, TypeError, ValueError):
            continue
        if rounded == 0.0:
            rounded = 0.0  # normalise -0.0
        if rounded != value:
            setattr(obj, attr, rounded)


@event.listens_for(db.session, 'before_flush')
def _normalize_bill_identities(session, flush_context, instances):
    # Normalize bill identities and exact minor-unit mirrors before writing any
    # changed/new object.  This also protects older mutation routes that still
    # assign a Python float to a legacy column.
    for obj in list(session.new) + list(session.dirty):
        _quantize_float_money(obj)
        if isinstance(obj, Account):
            sync_money_fields(obj, 'balance', 'balance_minor')
            if getattr(obj, 'opening_balance', None) is not None:
                sync_money_fields(obj, 'opening_balance', 'opening_balance_minor')
        elif isinstance(obj, AccountTransaction):
            sync_money_fields(obj, 'amount', 'amount_minor')
        elif isinstance(obj, DeliveryPersonPayment):
            sync_money_fields(obj, 'amount_paid', 'amount_paid_minor')
            sync_money_fields(obj, 'waive_off_amount', 'waive_off_minor')
        elif isinstance(obj, AccountReconciliation):
            for value_attr, minor_attr in (
                ('previous_balance', 'previous_balance_minor'),
                ('opening_balance', 'opening_balance_minor'),
                ('transaction_in', 'transaction_in_minor'),
                ('transaction_out', 'transaction_out_minor'),
                ('transaction_net', 'transaction_net_minor'),
                ('expected_balance', 'expected_balance_minor'),
                ('actual_balance', 'actual_balance_minor'),
                ('difference', 'difference_minor'),
                ('final_reconciled_balance', 'final_reconciled_balance_minor'),
            ):
                sync_money_fields(obj, value_attr, minor_attr)

        if isinstance(obj, Booking):
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='BK')
            obj.manual_bill_no = _normalize_manual_bill_model(getattr(obj, 'manual_bill_no', None))
        elif isinstance(obj, Payment):
            sync_money_fields(obj, 'amount', 'amount_minor')
            sync_money_fields(obj, 'discount', 'discount_minor')
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='CP')
            obj.manual_bill_no = _normalize_manual_bill_model(getattr(obj, 'manual_bill_no', None))
        elif isinstance(obj, SupplierPayment):
            sync_money_fields(obj, 'amount', 'amount_minor')
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='SP')
            obj.manual_bill_no = _normalize_manual_bill_model(getattr(obj, 'manual_bill_no', None))
        elif isinstance(obj, DirectSale):
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='SL')
            obj.manual_bill_no = _normalize_manual_bill_model(getattr(obj, 'manual_bill_no', None))
        elif isinstance(obj, MaterialReturn):
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='RTN')
            obj.manual_bill_no = _normalize_manual_bill_model(getattr(obj, 'manual_bill_no', None))
        elif isinstance(obj, GRN):
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='GRN')
            obj.manual_bill_no = _normalize_manual_bill_model(getattr(obj, 'manual_bill_no', None))
        elif isinstance(obj, Entry):
            obj.auto_bill_no = _normalize_auto_bill_model(getattr(obj, 'auto_bill_no', None), namespace='EN')
        elif isinstance(obj, PendingBill):
            bill_no = (getattr(obj, 'bill_no', None) or '').strip()
            if bill_no:
                if bool(getattr(obj, 'is_manual', False)):
                    obj.bill_no = _normalize_manual_bill_model(bill_no)
                else:
                    obj.bill_no = _normalize_auto_bill_model(bill_no, namespace='GEN') or _normalize_manual_bill_model(bill_no)
            obj.bill_kind = _parse_bill_kind_model(getattr(obj, 'bill_no', None))
        elif isinstance(obj, Invoice):
            inv_no = (getattr(obj, 'invoice_no', None) or '').strip()
            if inv_no and (not inv_no.upper().startswith('INV-')):
                if bool(getattr(obj, 'is_manual', False)):
                    obj.invoice_no = _normalize_manual_bill_model(inv_no)
                else:
                    obj.invoice_no = _normalize_auto_bill_model(inv_no, namespace='EN') or _normalize_manual_bill_model(inv_no)
        elif isinstance(obj, BillCounter):
            obj.namespace = _normalize_namespace_model(getattr(obj, 'namespace', None))

