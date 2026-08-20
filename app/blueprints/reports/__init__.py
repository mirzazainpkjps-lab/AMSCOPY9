"""Package split from reports.py."""
from ._common import *  # noqa
from .profit import *  # noqa
from .cash import *  # noqa

from utils.pkg_wire import wire_package
wire_package('app.blueprints.reports')
