"""Package split from accounts.py."""
from ._common import *  # noqa
from .helpers import *  # noqa
from .extra import *  # noqa
from .dashboard import *  # noqa
from .payments import *  # noqa
from .accounts_crud import *  # noqa
from .transactions import *  # noqa
from .kpis import *  # noqa

from utils.pkg_wire import wire_package
wire_package('blueprints.accounts')
