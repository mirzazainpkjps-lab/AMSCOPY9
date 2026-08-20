"""bills — split from sales.py."""
from ._common import *  # noqa

@bp.route('/view_bill_detail/<string:type>/<int:id>')
@login_required
def view_bill_detail(type, id):
    bill = None
    items = []
    if type == 'Booking':
        bill = Booking.query.get_or_404(id)
        items = bill.items
    elif type == 'Payment':
        bill = Payment.query.get_or_404(id)
    elif type == 'DirectSale':
        bill = DirectSale.query.get_or_404(id)
        sale_entries = Entry.query.filter(
            Entry.source_module == 'sales',
            Entry.source_table == 'direct_sale',
            Entry.source_id == bill.id,
            Entry.is_void == False
        ).order_by(Entry.id.asc()).all()
        entry_map = {}
        for e in sale_entries:
            key = ((e.material or '').strip(), float(e.qty or 0))
            entry_map.setdefault(key, []).append(e)

        items = []
        for it in (bill.items or []):
            key = ((it.product_name or '').strip(), float(it.qty or 0))
            entry = entry_map.get(key, []).pop(0) if entry_map.get(key) else None
            name = it.product_name
            if entry and entry.booked_material and entry.material and entry.booked_material.strip() != entry.material.strip():
                name = f"{entry.booked_material.strip()} > ALT > {entry.material.strip()}"
            items.append({'name': name, 'qty': it.qty, 'price_at_time': it.price_at_time})
    elif type == 'MaterialReturn':
        bill = MaterialReturn.query.get_or_404(id)
        items = bill.items
    else:
        return "Invalid Bill Type", 400

    all_clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    all_materials = Material.query.order_by(Material.name.asc()).all()
    return render_template('view_bill.html', bill=bill, type=type, items=items, clients=all_clients, materials=all_materials, pk_now=pk_now)

