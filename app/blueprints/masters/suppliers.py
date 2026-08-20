from ._common import *  # noqa

@bp.route('/suppliers')
@login_required
def suppliers():
    suppliers_list = Supplier.query.order_by(Supplier.name.asc()).all()
    supplier_balances = {}
    for s in suppliers_list:
        try:
            ledger = build_supplier_financial_ledger(s)
            supplier_balances[s.id] = float(ledger.get('closing_balance') or 0)
        except Exception:
            supplier_balances[s.id] = float(s.opening_balance or 0)
    return render_template('suppliers.html', suppliers=suppliers_list, supplier_balances=supplier_balances)

