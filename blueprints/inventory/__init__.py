"""Package split from inventory.py."""
from ._common import *  # noqa
from .helpers import *  # noqa
from .stock import *  # noqa
from .daily import *  # noqa

from utils.pkg_wire import wire_package
wire_package('blueprints.inventory')
