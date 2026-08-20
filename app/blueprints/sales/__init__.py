"""Package split from sales.py."""
from ._common import *  # noqa
from .bookings import *  # noqa
from .payments import *  # noqa
from .direct_sales import *  # noqa
from .returns import *  # noqa
from .bills import *  # noqa

from utils.pkg_wire import wire_package
wire_package('app.blueprints.sales')
