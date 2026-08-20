"""Package split from misc.py."""
from ._common import *  # noqa
from .materials import *  # noqa
from .pending import *  # noqa
from .extra import *  # noqa
from .wipe import *  # noqa
from .users_settings import *  # noqa
from .suppliers import *  # noqa

from utils.pkg_wire import wire_package
wire_package('app.blueprints.misc')
