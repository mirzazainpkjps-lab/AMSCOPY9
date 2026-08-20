"""Audit: compare every registered route against the permission system.

Usage:  .venv/bin/python tools/audit_permissions.py

Checks, for every live endpoint:
  1. exact endpoint name in ENDPOINT_PERMISSION_MAP
  2. short-name (legacy alias) in ENDPOINT_PERMISSION_MAP
  3. blueprint prefix in BLUEPRINT_PERMISSION_PREFIXES
  4. inline permission check in the view source (_user_can / role / 403)
  5. root-only guard (require_root) — disabled single-store, abort(404)
  6. blueprint-level role gate (e.g. admin.before_request)

Endpoints that are short aliases of the same view function are reported
once.  Output: stale map keys, inline-only protected, remaining gaps.
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import tempfile

os.environ.setdefault('APP_DB_PATH', os.path.join(tempfile.mkdtemp(prefix='ams_audit_'), 'audit.db'))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.services.constants import (  # noqa: E402
    BLUEPRINT_PERMISSION_PREFIXES,
    ENDPOINT_PERMISSION_MAP,
)

app = create_app({'TESTING': True})

PUBLIC_OK = {'login', 'logout', 'index', 'static', 'favicon.ico'}


def _mapped(endpoint: str):
    """Return the permission spec (str or tuple) if the endpoint is mapped."""
    spec = ENDPOINT_PERMISSION_MAP.get(endpoint)
    if not spec and '.' in endpoint:
        spec = ENDPOINT_PERMISSION_MAP.get(endpoint.rsplit('.', 1)[-1])
    if not spec and '.' in endpoint:
        spec = BLUEPRINT_PERMISSION_PREFIXES.get(endpoint.split('.', 1)[0])
    return spec


with app.app_context():
    rules = list(app.url_map.iter_rules())

    # Group endpoints by view function to collapse legacy short aliases.
    by_view: dict = {}
    for rule in rules:
        ep = rule.endpoint
        if ep in ('static', 'favicon.ico'):
            continue
        view = app.view_functions.get(ep)
        key = view.__name__ if view else ep
        by_view.setdefault(key, {'endpoints': [], 'rules': [], 'view': view})
        by_view[key]['endpoints'].append(ep)
        by_view[key]['rules'].append(rule.rule)

    stale = [k for k in ENDPOINT_PERMISSION_MAP
             if k not in {e for eps in by_view.values() for e in eps['endpoints']}
             and k not in {e.rsplit('.', 1)[-1] for eps in by_view.values() for e in eps['endpoints']}]

    CHECK_PATTERNS = re.compile(
        r'_user_can\(|current_user\.role|abort\(403|require_root\(|'
        r'role\s*==\s*[\'"]admin|role\s+not\s+in|getattr\(current_user,\s*[\'"]role'
    )
    ROOT_PATTERN = re.compile(r'require_root\(')

    gaps, inline, root_guard, role_gated, mapped_count, public = [], [], [], [], 0, []
    for key, info in sorted(by_view.items()):
        view = info['view']
        eps = info['endpoints']
        primary = next((e for e in eps if '.' in e), eps[0])
        rule = info['rules'][0]
        if all(e.split('.')[-1] in PUBLIC_OK or e in PUBLIC_OK for e in eps):
            public.append((primary, rule))
            continue
        try:
            src = inspect.getsource(view)
        except (OSError, TypeError):
            src = ''
        spec = next((_mapped(e) for e in eps if _mapped(e)), None)
        if spec:
            mapped_count += 1
            continue
        bp_name = primary.split('.', 1)[0] if '.' in primary else ''
        if bp_name == 'admin':
            role_gated.append((primary, rule))  # before_request role gate in blueprint
            continue
        if ROOT_PATTERN.search(src):
            root_guard.append((primary, rule))
            continue
        if CHECK_PATTERNS.search(src):
            inline.append((primary, rule))
            continue
        gaps.append((primary, rule, view.__module__ if view else '?'))

    print('=' * 72)
    print(f'UNIQUE VIEWS: {len(by_view)}   MAPPED: {mapped_count}   '
          f'INLINE: {len(inline)}   ROOT-ONLY: {len(root_guard)}   '
          f'ROLE-GATED: {len(role_gated)}   PUBLIC-OK: {len(public)}')
    print(f'ENDPOINT_PERMISSION_MAP: {len(ENDPOINT_PERMISSION_MAP)} entries, '
          f'PREFIXES: {BLUEPRINT_PERMISSION_PREFIXES}')
    print('=' * 72)
    print(f'\nSTALE MAP KEYS: {len(stale)}')
    for k in sorted(stale):
        print(f'  - {k} -> {ENDPOINT_PERMISSION_MAP[k]}')
    print(f'\nREMAINING GAPS: {len(gaps)}')
    for ep, rule, mod in gaps:
        print(f'  - {ep:50s} {rule:50s} {mod}')
    print(f'\nROLE-GATED (blueprint before_request): {len(role_gated)}')
    for ep, rule in role_gated:
        print(f'  - {ep:50s} {rule}')
    print(f'\nROOT-ONLY (require_root, disabled in single-store): {len(root_guard)}')
    for ep, rule in root_guard:
        print(f'  - {ep:50s} {rule}')
    print(f'\nINLINE-ONLY (has check, no map entry): {len(inline)}')
    for ep, rule in inline:
        print(f'  - {ep:50s} {rule}')
    print(f'\nPUBLIC-OK (login-only by design): {len(public)}')
    for ep, rule in public:
        print(f'  - {ep:50s} {rule}')
