"""Discover and register Flask blueprints from a directory (modules or packages)."""
from __future__ import annotations

import importlib
import inspect
import logging
import sys
from pathlib import Path

from flask import Blueprint

logger = logging.getLogger(__name__)


def _iter_module_names(blueprint_dir: str):
    path = Path(blueprint_dir)
    names = []
    for file in path.glob("*.py"):
        if file.name.startswith("_"):
            continue
        names.append(file.stem)
    for child in path.iterdir():
        if child.is_dir() and not child.name.startswith("_") and (child / "__init__.py").exists():
            names.append(child.name)
    return sorted(set(names))


def load_modules(app, blueprint_dir="blueprints"):
    blueprint_path = Path(blueprint_dir)
    if not blueprint_path.exists():
        return
    if getattr(app, "_modules_loaded", False):
        return

    # Ensure the parent of the package is importable
    parent = str(blueprint_path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    pkg_name = blueprint_path.name

    for module_name in _iter_module_names(blueprint_dir):
        try:
            module = importlib.import_module(f"{pkg_name}.{module_name}")
        except Exception:
            logger.exception("Error loading module '%s'", module_name)
            continue

        found = [obj for _, obj in inspect.getmembers(module) if isinstance(obj, Blueprint)]
        if not found:
            continue
        for blueprint in found:
            url_prefix = f"/{blueprint.name}"
            config = getattr(module, "MODULE_CONFIG", None) or {}
            if "url_prefix" in config:
                url_prefix = config["url_prefix"]
            if blueprint.name in app.blueprints:
                continue
            app.register_blueprint(blueprint, url_prefix=url_prefix)
            logger.info(
                "Registered blueprint '%s' from module '%s' at '%s'",
                blueprint.name,
                module_name,
                url_prefix,
            )

    app._modules_loaded = True


def get_modules_info(blueprint_dir="blueprints"):
    info = []
    path = Path(blueprint_dir)
    if not path.exists():
        return info
    parent = str(path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    pkg_name = path.name
    for module_name in _iter_module_names(blueprint_dir):
        try:
            module = importlib.import_module(f"{pkg_name}.{module_name}")
        except Exception:
            continue
        bps = [obj.name for _, obj in inspect.getmembers(module) if isinstance(obj, Blueprint)]
        if bps:
            info.append((module_name, bps))
    return info
