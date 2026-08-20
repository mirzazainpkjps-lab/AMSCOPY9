"""Package split from ledgers.py."""
from ._common import *  # noqa
from .client import *  # noqa
from .booking_cancel import *  # noqa
from .other import *  # noqa
from .delivery_person import *  # noqa

from utils.pkg_wire import wire_package
wire_package('app.blueprints.ledgers')
