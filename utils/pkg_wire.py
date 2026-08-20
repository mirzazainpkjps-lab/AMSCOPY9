"""Copy callables/names across submodules of a split package."""
from __future__ import annotations

import importlib
import pkgutil


def wire_package(package_name: str):
    pkg = importlib.import_module(package_name)
    modules = [pkg]
    if hasattr(pkg, "__path__"):
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("__"):
                continue
            modules.append(importlib.import_module(f"{package_name}.{info.name}"))
    names = {}
    for mod in modules:
        for key, val in vars(mod).items():
            if key.startswith("__"):
                continue
            names[key] = val
    for mod in modules:
        g = vars(mod)
        for key, val in names.items():
            if key in g:
                continue
            # Copy private constants too (_WIPE_BACKUP_ENABLED, etc.).
            # Skip only dunders (already filtered) and bound methods that
            # would collide with a submodule's own definitions (key in g).
            g[key] = val
    return names
