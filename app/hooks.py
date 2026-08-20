"""Legacy request hooks. Bound in create_app via register_hooks()."""
from __future__ import annotations
import os, time, logging, secrets
from flask import request
from werkzeug.exceptions import RequestEntityTooLarge

def register_hooks(app):
    from flask import jsonify, flash, redirect, url_for, session
    from flask_login import current_user
    from models import Client, Material, DeliveryPerson, Settings
    from app.services.backup import _start_hourly_backup_worker, _start_reconcile_worker
    from app.services.permissions import _user_can
    from app.services.constants import (
        _AUTO_BACKUP_ENABLED,
        BLUEPRINT_PERMISSION_PREFIXES,
        ENDPOINT_PERMISSION_MAP,
    )
    @app.after_request
    def _cache_control_headers(response):
        # On-demand edit/view modal fragments must never be served stale —
        # a stale fragment is the classic "click edit -> stuck overlay".
        endpoint = request.endpoint or ''
        short = endpoint.rsplit('.', 1)[-1] if endpoint else ''
        if short in (
            'direct_sale_edit_modal',
            'booking_edit_modal',
            'pending_bill_modals',
            'client_modals',
        ):
            response.headers['Cache-Control'] = 'no-store'
            return response
        # HTML pages: revalidate every time.  Yard PCs share this server over
        # LAN and code updates deploy often; without this, a cached page can
        # run OLD page scripts against NEW routes and hang (edit modal stuck
        # forever).  Static assets keep their own caching rules.
        if (response.headers.get('Content-Type') or '').startswith('text/html'):
            response.headers['Cache-Control'] = 'no-cache'
        return response

    @app.after_request
    def allow_iframe_and_cors(response):
        if os.environ.get('ALLOW_OPEN_CORS', '').lower() in ('1', 'true', 'yes'):
            response.headers["X-Frame-Options"] = "ALLOWALL"
            response.headers["Content-Security-Policy"] = "frame-ancestors *"
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
            response.headers["Access-Control-Allow-Credentials"] = "false"
            return response

        # Arena / e2b live preview is a cross-origin iframe. Blocking it
        # looks like a broken login (blank page after POST /login).
        response.headers["X-Frame-Options"] = "ALLOWALL"
        response.headers["Content-Security-Policy"] = "frame-ancestors *"
        return response

    @app.before_request
    def _request_log_start():
        request._log_started_at = time.time()

    @app.before_request
    def _touch_concurrent_login_session():
        try:
            if current_user.is_authenticated:
                from utils.sessions import touch_login_session
                touch_login_session(current_user)
        except Exception:
            pass

    @app.after_request
    def _stamp_actor_on_crud(response):
        """Every mutating request: flash who did it + write audit_log."""
        try:
            method = (request.method or '').upper()
            if method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
                return response
            path = request.path or ''
            if path.startswith('/static') or path.startswith('/import_export/jobs/'):
                return response
            endpoint = request.endpoint or ''
            if endpoint in ('login', 'logout', 'static'):
                return response
            who = None
            try:
                if current_user and current_user.is_authenticated:
                    who = (current_user.username or '').strip() or None
            except Exception:
                who = None
            if not who:
                return response
            flashes = session.get('_flashes')
            if flashes:
                tagged = []
                marker = f'— by {who}'
                for cat, msg in flashes:
                    text = str(msg or '')
                    if cat in ('success', 'warning', 'info', 'danger') and marker not in text:
                        text = f'{text} {marker}'
                    tagged.append((cat, text))
                session['_flashes'] = tagged
            if 200 <= int(response.status_code or 0) < 400:
                from utils.audit import audit_log
                audit_log(
                    current_user,
                    f'http.{method.lower()}.{endpoint or "unknown"}',
                    f'path={path} status={response.status_code}',
                )
        except Exception:
            pass
        return response

    @app.after_request
    def _log_request_summary(response):
        started = getattr(request, '_log_started_at', None)
        elapsed_ms = ((time.time() - started) * 1000.0) if started else 0.0
        logging.getLogger('request').info(
            '%s - "%s %s" %s %.1fms',
            request.headers.get('X-Forwarded-For', request.remote_addr or '-'),
            request.method,
            request.full_path.rstrip('?') if request.query_string else request.path,
            response.status_code,
            elapsed_ms
        )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_request_too_large(_err):
        max_mb = max(1, int((app.config.get('MAX_CONTENT_LENGTH') or 0) / (1024 * 1024)))
        msg = f"Uploaded file is too large. Max allowed size is {max_mb} MB."
        if request.path.startswith('/import_export/app_upgrade/start') or request.path.startswith('/import_export/master/import/start'):
            return jsonify({'error': msg}), 413
        flash(msg, 'danger')
        return redirect(request.referrer or url_for('index'))

    @app.before_request
    def _protect_against_csrf():
        """Session-bound CSRF protection for financial Accounts mutations."""
        token = session.get('_csrf_token')
        if not token:
            token = secrets.token_urlsafe(32)
            session['_csrf_token'] = token
        if app.config.get('TESTING') or app.config.get('WTF_CSRF_ENABLED') is False:
            return None
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None
        endpoint = request.endpoint or ''
        if not (endpoint == 'accounts' or endpoint.startswith('accounts.')):
            return None
        supplied = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
        if supplied and secrets.compare_digest(str(supplied), str(token)):
            return None
        logging.getLogger('security').warning('CSRF rejected: endpoint=%s ip=%s', endpoint, request.remote_addr)
        return jsonify({'error': 'Invalid or expired form token. Reload the page and try again.'}), 400

    @app.context_processor
    def _inject_csrf_token():
        token = session.get('_csrf_token')
        if not token:
            token = secrets.token_urlsafe(32)
            session['_csrf_token'] = token
        return {'csrf_token': token}

    @app.before_request
    def _http_lan_session_cookies():
        """Other PCs open login over http://192.168.x.x — never mark cookies Secure."""
        proto = (request.headers.get('X-Forwarded-Proto') or request.scheme or 'http').split(',')[0].strip().lower()
        https = proto == 'https' or bool(request.is_secure)
        if https:
            return None
        app.config['SESSION_COOKIE_SECURE'] = False
        app.config['REMEMBER_COOKIE_SECURE'] = False
        if str(app.config.get('SESSION_COOKIE_SAMESITE') or '').lower() == 'none':
            app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
            app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

    @app.after_request
    def _strip_secure_cookie_on_http(response):
        proto = (request.headers.get('X-Forwarded-Proto') or request.scheme or 'http').split(',')[0].strip().lower()
        if proto == 'https' or request.is_secure:
            return response
        # Werkzeug may still emit Secure if config was True at set time.
        cookies = response.headers.getlist('Set-Cookie')
        if not cookies:
            return response
        response.headers.pop('Set-Cookie', None)
        for raw in cookies:
            parts = [p.strip() for p in raw.split(';')]
            kept = [p for p in parts if p.lower() != 'secure']
            # SameSite=None without Secure is rejected — force Lax on HTTP
            out = []
            for p in kept:
                if p.lower().startswith('samesite='):
                    val = p.split('=', 1)[-1].strip().lower()
                    if val == 'none':
                        out.append('SameSite=Lax')
                        continue
                out.append(p)
            response.headers.add('Set-Cookie', '; '.join(out))
        return response

    @app.before_request
    def _ensure_background_workers_started():
        if _AUTO_BACKUP_ENABLED:
            _start_hourly_backup_worker()
        _start_reconcile_worker()

    @app.before_request
    def _enforce_user_permissions():
        if not current_user.is_authenticated:
            return None
        if current_user.role in ('admin', 'root'):
            return None
        endpoint = request.endpoint or ''
        # Core blueprints are registered before the legacy short aliases, so
        # Flask normally reports endpoints such as ``sales.add_booking`` while
        # the permission table historically used the short name.  Falling
        # back to the final component keeps both route forms protected.
        needed = ENDPOINT_PERMISSION_MAP.get(endpoint)
        if not needed and '.' in endpoint:
            needed = ENDPOINT_PERMISSION_MAP.get(endpoint.rsplit('.', 1)[-1])
        # Whole-feature-pack fallback (e.g. every /accounts route).
        if not needed and '.' in endpoint:
            needed = BLUEPRINT_PERMISSION_PREFIXES.get(endpoint.split('.', 1)[0])
        if not needed:
            return None
        # A tuple means "any of these permissions" (OR semantics).
        if isinstance(needed, (tuple, list, set)):
            allowed = any(_user_can(p) for p in needed)
        else:
            allowed = _user_can(needed)
        if allowed:
            return None
        flash('Permission denied for this module.', 'danger')
        return redirect(url_for('index'))

    @app.context_processor
    def inject_dropdown_data():
        if current_user.is_authenticated:
            now_ts = time.time()
            cache_ttl = 20
            cache_obj = getattr(app, '_dropdown_cache', None)
            if (
                not isinstance(cache_obj, dict)
                or (now_ts - float(cache_obj.get('ts', 0) or 0)) > cache_ttl
            ):
                cache_obj = {
                    'ts': now_ts,
                    'clients': Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all(),
                    'materials': Material.query.order_by(Material.name.asc()).all(),
                    'delivery_persons': DeliveryPerson.query.filter_by(is_active=True).order_by(DeliveryPerson.name.asc()).all(),
                    'settings': Settings.query.first(),
                }
                app._dropdown_cache = cache_obj
            return dict(
                clients=cache_obj.get('clients') or [],
                materials=cache_obj.get('materials') or [],
                delivery_persons=cache_obj.get('delivery_persons') or [],
                settings=cache_obj.get('settings'),
                user_can=_user_can,
                ui_theme_preference=session.get('ui_theme')
            )
        return dict(ui_theme_preference=session.get('ui_theme'))

