"""direct_sales — split from sales.py."""
from ._common import *  # noqa

@bp.route('/add_sale', methods=['POST'])
@login_required
def add_sale():
    return add_direct_sale()

