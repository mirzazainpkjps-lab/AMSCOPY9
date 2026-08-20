"""dispatch — split from ops.py."""
from ._common import *  # noqa

@bp.route('/dispatching')
@login_required
def dispatching():
    mats = Material.query.filter_by(is_active=True).order_by(Material.name.asc()).all()
    cls = Client.query.filter(Client.is_active == True).order_by(Client.name.asc()).all()
    dps = DeliveryPerson.query.filter_by(is_active=True).order_by(DeliveryPerson.name.asc()).all()
    today = pk_today().strftime('%Y-%m-%d')
    return render_template('dispatching.html',
                           materials=mats,
                           clients=cls,
                           delivery_persons=dps,
                           today_date=today)

