"""validate — split from import_export.py."""
from ._common import *  # noqa

def _validate_pandas_installed():
    """Check if pandas is installed. Returns True if available, False otherwise."""
    return pd is not None


