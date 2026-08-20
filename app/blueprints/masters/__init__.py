from ._common import *  # noqa
from .clients import *  # noqa
from .add_client import *  # noqa
from .edit_client import *  # noqa
from .client_opening_balance import *  # noqa
from .delete_client import *  # noqa
from .activate_all_clients import *  # noqa
from .transfer_client import *  # noqa
from .reclaim_client import *  # noqa
from .suppliers import *  # noqa
from .add_supplier import *  # noqa
from .edit_supplier import *  # noqa
from .supplier_opening_balance import *  # noqa
from .delete_supplier import *  # noqa
from .materials import *  # noqa
from .delivery_persons_page import *  # noqa
from .add_delivery_person import *  # noqa
from .toggle_delivery_person import *  # noqa
from .edit_delivery_person import *  # noqa
from .rename_material_label import *  # noqa
from .activate_all_materials import *  # noqa
from utils.pkg_wire import wire_package
wire_package("app.blueprints.masters")
