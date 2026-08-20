from ._common import *  # noqa

@bp.route('/materials')
@login_required
def materials():
    page = request.args.get('page', 1, type=int)
    category_id = (request.args.get('category_id') or '').strip()
    unit_filter = (request.args.get('unit') or '').strip()
    q = Material.query
    if category_id:
        try:
            q = q.filter(Material.category_id == int(category_id))
        except ValueError:
            pass
    if unit_filter:
        q = q.filter(Material.unit == unit_filter)

    pagination = q.order_by(Material.code.asc()).paginate(page=page, per_page=10)
    # Fetch all materials for the merge modal dropdown
    all_materials = Material.query.order_by(Material.name.asc()).all()
    categories = MaterialCategory.query.order_by(MaterialCategory.name.asc()).all()
    units = [r[0] for r in db.session.query(Material.unit).distinct().filter(Material.unit != None, Material.unit != '').order_by(Material.unit).all()]

    return render_template('materials.html',
                           materials=pagination.items,
                           pagination=pagination,
                           all_materials=all_materials,
                           categories=categories,
                           category_filter=category_id,
                           unit_filter=unit_filter,
                           units=units)

