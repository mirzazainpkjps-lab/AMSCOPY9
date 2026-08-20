"""wipe — split from misc.py."""
from ._common import *  # noqa

@bp.route('/system_report')
@login_required
def system_report():
    if current_user.role != 'admin':
        flash('Unauthorized', 'danger')
        return redirect(url_for('index'))

    report = {
        'sync_issues': [],
        'stock_issues': [],
        'unpaid_count': 0,
        'zero_amount_bills': 0
    }

    # 1. Check Dispatch vs Pending Bill Sync
    # Find dispatch entries that have a bill number but NO pending bill record
    entries = db.session.query(Entry).filter(
        Entry.type == 'OUT',
        Entry.is_void == False,
        Entry.bill_no != None,
        Entry.bill_no != ''
    ).all()

    for e in entries:
        if e.bill_no.upper() == 'CASH': continue # Skip cash entries

        pb = PendingBill.query.filter_by(bill_no=e.bill_no, client_code=e.client_code, is_void=False).first()
        if not pb:
            report['sync_issues'].append({
                'type': 'Missing Pending Bill',
                'desc': f"Entry #{e.id}: Bill {e.bill_no} for {e.client} ({e.client_code}) is missing from Pending Bills."
            })

    # 2. Check Financial Data
    report['unpaid_count'] = PendingBill.query.filter_by(is_paid=False, is_void=False).count()

    zero_bills = PendingBill.query.filter_by(is_paid=False, is_void=False, amount=0).all()
    for zb in zero_bills:
        report['sync_issues'].append({
            'type': 'Zero Amount Bill',
            'desc': f"Bill {zb.bill_no} for {zb.client_name} has 0.00 amount."
        })
        report['zero_amount_bills'] += 1

    # 3. Check Stock Consistency
    materials = Material.query.all()
    for m in materials:
        total_in = db.session.query(func.sum(Entry.qty)).filter_by(material=m.name, type='IN', is_void=False).scalar() or 0
        total_out = db.session.query(func.sum(Entry.qty)).filter_by(material=m.name, type='OUT', is_void=False).scalar() or 0
        calc_total = total_in - total_out

        if abs(calc_total - (m.total or 0)) > 0.1:
            report['stock_issues'].append({
                'material': m.name,
                'db_stock': m.total,
                'calc_stock': calc_total,
                'diff': m.total - calc_total
            })

    return render_template('system_report.html', report=report)

