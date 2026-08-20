"""stock — split from inventory.py."""
from ._common import *  # noqa

@inventory_bp.route('/stock_summary')
@login_required
def stock_summary():
    date_from = request.args.get('date_from', date.today().strftime('%Y-%m-%d')).strip()
    date_to = request.args.get('date_to', date_from).strip()
    # Backward compatibility for old single-date link/query.
    single_date = request.args.get('date', '').strip()
    if single_date:
        date_from = single_date
        date_to = single_date
    category_id = request.args.get('material_category', '').strip()
    material_filter = request.args.get('material', '').strip()

    # Normalize invalid range silently to a safe value.
    if date_to < date_from:
        date_to = date_from

    sel_date = date_to

    prev_stats = db.session.query(
        Entry.material,
        func.sum(case(
            (Entry.type == 'IN', Entry.qty),
            (Entry.type == 'OUT', -Entry.qty),
            else_=0
        )).label('prev_net')
    ).filter(Entry.date < sel_date, Entry.is_void == False)

    if material_filter:
        prev_stats = prev_stats.filter(Entry.material == material_filter)
    prev_stats = prev_stats.group_by(Entry.material).all()
    prev_map = {row.material: float(row.prev_net or 0) for row in prev_stats}
    
    day_stats = db.session.query(
        Entry.material,
        func.sum(case((Entry.type == 'IN', Entry.qty), else_=0)).label('day_in'),
        func.sum(case((Entry.type == 'OUT', Entry.qty), else_=0)).label('day_out')
    ).filter(Entry.date == sel_date, Entry.is_void == False)
    if material_filter:
        day_stats = day_stats.filter(Entry.material == material_filter)
    day_stats = day_stats.group_by(Entry.material).all()
    day_map = {row.material: {'in': float(row.day_in or 0), 'out': float(row.day_out or 0)} for row in day_stats}
    
    all_material_objs = Material.query.all()
    category_map = {m.name: m.category.name if m.category else '' for m in all_material_objs}
    all_materials = set(prev_map.keys()) | set(day_map.keys())
    for mat in Material.query.with_entities(Material.name).all():
        all_materials.add(mat.name)

    if category_id:
        try:
            cat_id_int = int(category_id)
            allowed = {m.name for m in Material.query.filter(Material.category_id == cat_id_int).all()}
            all_materials = {m for m in all_materials if m in allowed}
        except ValueError:
            pass
    if material_filter:
        all_materials = {m for m in all_materials if m == material_filter}
    
    stats = []
    for mat_name in sorted([m for m in all_materials if m is not None]):
        prev_net = prev_map.get(mat_name, 0)
        day_in = day_map.get(mat_name, {}).get('in', 0)
        day_out = day_map.get(mat_name, {}).get('out', 0)
        
        stats.append({
            'name': mat_name,
            'category': category_map.get(mat_name, ''),
            'opening': int(prev_net),
            'in': int(day_in),
            'out': int(day_out),
            'closing': int(prev_net + day_in - day_out)
        })

    range_query = Entry.query.filter(
        Entry.is_void == False,
        Entry.date >= date_from,
        Entry.date <= date_to
    )
    if category_id:
        try:
            cat_id_int = int(category_id)
            allowed_materials = [m.name for m in Material.query.filter(Material.category_id == cat_id_int).all()]
            if allowed_materials:
                range_query = range_query.filter(Entry.material.in_(allowed_materials))
            else:
                range_query = range_query.filter(Entry.id == -1)
        except ValueError:
            pass
    if material_filter:
        range_query = range_query.filter(Entry.material == material_filter)

    totals_rows = db.session.query(
        Entry.material,
        func.sum(case((Entry.type == 'IN', Entry.qty), else_=0)).label('total_in'),
        func.sum(case((Entry.type == 'OUT', Entry.qty), else_=0)).label('total_out')
    ).filter(
        Entry.id.in_(range_query.with_entities(Entry.id))
    ).group_by(Entry.material).order_by(Entry.material.asc()).all()

    totals_by_material = [{
        'material': row.material,
        'received': float(row.total_in or 0),
        'delivered': float(row.total_out or 0),
        'net': float((row.total_in or 0) - (row.total_out or 0))
    } for row in totals_rows if row.material]

    daily_rows = db.session.query(
        Entry.date,
        func.sum(case((Entry.type == 'IN', Entry.qty), else_=0)).label('day_in'),
        func.sum(case((Entry.type == 'OUT', Entry.qty), else_=0)).label('day_out')
    ).filter(
        Entry.id.in_(range_query.with_entities(Entry.id))
    ).group_by(Entry.date).order_by(Entry.date.desc()).all()

    day_wise = [{
        'date': row.date,
        'received': float(row.day_in or 0),
        'delivered': float(row.day_out or 0),
        'net': float((row.day_in or 0) - (row.day_out or 0))
    } for row in daily_rows if row.date]

    grand_received = float(sum(r['received'] for r in totals_by_material))
    grand_delivered = float(sum(r['delivered'] for r in totals_by_material))
    grand_net = grand_received - grand_delivered

    # GRN incoming-history: every stock-IN movement in the range that traces
    # back to an active GRN (authoritative ledger — entry rows are created by
    # the GRN save; GRN is the source document).  Customer returns and manual
    # IN entries are separate and stay visible in the Received totals above.
    grn_receipts = _grn_receipt_history(date_from, date_to, material_filter)

    categories = MaterialCategory.query.order_by(MaterialCategory.name.asc()).all()
    materials = Material.query.order_by(Material.name.asc()).all()
    return render_template(
        'stock_summary.html',
        stats=stats,
        sel_date=sel_date,
        categories=categories,
        materials=materials,
        category_filter=category_id,
        date_from=date_from,
        date_to=date_to,
        material_filter=material_filter,
        totals_by_material=totals_by_material,
        day_wise=day_wise,
        grn_receipts=grn_receipts,
        grand_received=grand_received,
        grand_delivered=grand_delivered,
        grand_net=grand_net
    )


def _grn_receipt_history(date_from, date_to, material_filter=''):
    """GRN stock-in movements for the range, with the source GRN document.

    One authoritative pass over the entry ledger (IN, non-void) joined to the
    GRN master by auto bill number, so every row is traceable to a GRN
    (bill no + supplier) rather than appearing as an anonymous "added qty".
    """
    in_rows = Entry.query.filter(
        Entry.is_void == False,
        Entry.type == 'IN',
        Entry.date >= date_from,
        Entry.date <= date_to,
        Entry.auto_bill_no.isnot(None),
    )
    if material_filter:
        in_rows = in_rows.filter(Entry.material == material_filter)
    in_rows = in_rows.with_entities(
        Entry.date, Entry.material, Entry.qty, Entry.auto_bill_no, Entry.bill_no
    ).all()

    grn_map = {}
    for g in GRN.query.filter(GRN.is_void == False).with_entities(
            GRN.auto_bill_no, GRN.manual_bill_no, GRN.supplier).all():
        ab = (g.auto_bill_no or '').strip()
        if ab:
            grn_map[ab] = {'bill': g.manual_bill_no or ab, 'supplier': g.supplier or ''}

    receipts = []
    for row in in_rows:
        info = grn_map.get((row.auto_bill_no or '').strip())
        if not info:
            continue
        receipts.append({
            'date': row.date,
            'bill_no': info['bill'],
            'supplier': info['supplier'],
            'material': row.material,
            'qty': float(row.qty or 0),
        })
    return receipts


