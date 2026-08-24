"""Package split from accounts.py."""
from ._common import *  # noqa
from .helpers import *  # noqa
from .extra import *  # noqa
from .dashboard import *  # noqa
from .payments import *  # noqa
from .accounts_crud import *  # noqa
from .transactions import *  # noqa
from .kpis import *  # noqa
# Classification registry + shared form logic are imported by the create/edit
# views; imported here so they are part of the package namespace.
from . import classification, account_form  # noqa: F401

from utils.pkg_wire import wire_package
wire_package('blueprints.accounts')
