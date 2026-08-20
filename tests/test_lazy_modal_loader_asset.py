"""Asset-level guards for static/lazy_modal_loader.js.

Regression 2026-08-19 — "click Edit on /direct_sales: spinner flashes, then
nothing; a grey layer stays on screen and the form never opens."

Root causes the asset guards against re-introducing:

1. The loader dialog must NOT carry Bootstrap's `fade` class.  With fade,
   Modal.show()/hide() run through async transitions and Bootstrap 5.3's
   `_isTransitioning` guard silently DROPS any hide()/show() issued while a
   transition is mid-flight.  A fast server response (<150ms backdrop fade)
   left the spinner + backdrop stuck on screen forever; a slow server with a
   throwing initializer made showError()'s show() vanish mid-hide, so the
   user watched the spinner close into an empty grey page.

2. A throwing onReady initializer must not abort the open path — the fetched
   form must still be displayed even if a page-specific initializer crashes.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = REPO_ROOT / "static" / "lazy_modal_loader.js"
LAYOUT = REPO_ROOT / "templates" / "layout.html"


def _loader_source() -> str:
    assert LOADER.exists(), "lazy_modal_loader.js is missing"
    return LOADER.read_text(encoding="utf-8")


def test_loader_modal_markup_has_no_fade_class():
    source = _loader_source()
    match = re.search(r'<div class="modal([^"]*)" id="amsLazyModalLoader"', source)
    assert match, "loader dialog markup not found"
    classes = match.group(1).split()
    assert "fade" not in classes, (
        "the loader dialog must stay transition-free: an animated loader lets "
        "Bootstrap 5.3 swallow hide()/show() calls during the transition window "
        "(stuck spinner/backdrop, or an error dialog that never appears)"
    )


def test_onready_initializer_failure_is_isolated():
    source = _loader_source()
    on_ready_call = re.search(
        r"typeof opts\.onReady === 'function'\s*\)\s*{?\s*try\s*{?\s*opts\.onReady\(modalEl\)",
        source,
        re.IGNORECASE | re.DOTALL,
    )
    assert on_ready_call, (
        "opts.onReady(modalEl) must be wrapped in try/catch so a throwing "
        "initializer cannot prevent the fetched modal from opening"
    )


def test_layout_requests_cache_busted_loader():
    layout = LAYOUT.read_text(encoding="utf-8")
    match = re.search(r"lazy_modal_loader\.js'\)\s*\}\}\?v=(\d+)", layout)
    assert match, "layout.html no longer includes lazy_modal_loader.js"
    assert int(match.group(1)) >= 3, (
        "layout.html must include lazy_modal_loader.js with a ?v= cache "
        "buster (>=3) so stale browsers/proxies pick up the race fix"
    )
