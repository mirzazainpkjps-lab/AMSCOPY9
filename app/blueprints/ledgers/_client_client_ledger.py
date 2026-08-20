"""client — split from ledgers.py."""
from ._common import *  # noqa

@bp.route('/client_ledger/<int:id>')
@login_required
def client_ledger(id):
    client = db.session.get(Client, id)
    if client:
        page = request.args.get('page', 1, type=int)
        client_name_norm = (client.name or '').strip().lower()
        entry_filter = or_(
            Entry.client_code == client.code,
            func.lower(func.trim(Entry.client)) == client_name_norm
        )
        pagination = Entry.query.filter(entry_filter, Entry.is_void == False).order_by(
            Entry.date.desc()).paginate(page=page, per_page=10)
        summary_query = db.session.query(
            Entry.material,
            func.sum(case((Entry.type == 'IN', Entry.qty), else_=-Entry.qty)).label('total')
        ).filter(entry_filter, Entry.is_void == False, Entry.type.in_(['IN', 'OUT'])).group_by(Entry.material).all()
        summary = {row.material: row.total for row in summary_query}
        total_qty = sum(float(v or 0) for v in summary.values())

        pending_photos = {
            b.bill_no: b.photo_url
            for b in PendingBill.query.filter(PendingBill.photo_url != '', PendingBill.is_void == False).all() if b.bill_no
        }

        clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
        materials = Material.query.order_by(Material.name.asc()).all()

        return render_template('ledger.html',
                               client=client,
                               entries=pagination.items,
                               pagination=pagination,
                               total_qty=total_qty,
                               summary=summary,
                               pending_photos=pending_photos,
                               clients=clients,
                               materials=materials)
    return redirect(url_for('clients'))

