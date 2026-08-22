from ._common import *  # noqa

@bp.route('/suppliers')
@login_required
def suppliers():
    suppliers_list = Supplier.query.order_by(Supplier.name.asc()).all()
    # Bounded projection (2 queries + per-supplier payment buckets) instead of
    # building a full financial ledger per supplier (which issued a payment
    # lookup per GRN per supplier and got slower with every GRN).
    try:
        supplier_balances = build_supplier_payable_summaries(suppliers_list)
    except Exception:
        logging.exception('Supplier payable summaries failed; falling back to opening balances')
        supplier_balances = {s.id: float(s.opening_balance or 0) for s in suppliers_list}
    return render_template('suppliers.html', suppliers=suppliers_list, supplier_balances=supplier_balances)

