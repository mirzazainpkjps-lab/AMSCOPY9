"""The auto-discovering module loader must honour MODULE_CONFIG['enabled']."""
from __future__ import annotations

import textwrap

from flask import Flask

from utils.module_loader import load_modules


def _write_module(pkg_dir, name, enabled):
    (pkg_dir / f"{name}.py").write_text(
        textwrap.dedent(
            f"""
            from flask import Blueprint

            MODULE_CONFIG = {{
                'name': '{name}',
                'url_prefix': '/{name}',
                'enabled': {enabled},
            }}

            {name}_bp = Blueprint('{name}', __name__)

            @{name}_bp.route('/')
            def index():
                return 'ok'
            """
        ),
        encoding="utf-8",
    )


def test_disabled_module_is_not_registered(tmp_path, monkeypatch):
    pkg = tmp_path / "sample_blueprints"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    _write_module(pkg, "enabled_mod", True)
    _write_module(pkg, "disabled_mod", False)

    monkeypatch.syspath_prepend(str(tmp_path))
    app = Flask(__name__)
    load_modules(app, blueprint_dir=str(pkg))

    assert "enabled_mod" in app.blueprints
    assert "disabled_mod" not in app.blueprints


def test_scaffold_module_template_is_disabled():
    """blueprints/module_template.py renders templates that do not exist."""
    from blueprints import module_template

    assert module_template.MODULE_CONFIG["enabled"] is False
