"""Application factory — modular AMS ERP."""
from __future__ import annotations

import os
import secrets
import logging
from logging.handlers import RotatingFileHandler
from datetime import timedelta
from pathlib import Path

from flask import Flask
from flask_login import LoginManager
from sqlalchemy import event

from models import db
from utils.module_loader import load_modules


def create_app(test_config: dict | None = None) -> Flask:
    root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
        instance_path=str(root / "instance"),
        instance_relative_config=True,
    )

    instance_dir = root / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = instance_dir / ".tmp"
    (tmp_dir / "import_uploads").mkdir(parents=True, exist_ok=True)
    (tmp_dir / "import_reports").mkdir(parents=True, exist_ok=True)

    db_path = os.environ.get("APP_DB_PATH") or str(instance_dir / "ahmed_cement.db")
    # SQLite creates the database file on first connection, but it does not
    # create a missing custom parent directory.  Make a configured database
    # path just as safe as the default instance path on a fresh installation.
    db_parent = Path(db_path).expanduser().parent
    db_parent.mkdir(parents=True, exist_ok=True)
    max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "256") or "256")
    journal_mode = _resolve_sqlite_journal_mode(db_path)

    secret_file = instance_dir / "secret_key"
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
        if not secret:
            secret = secrets.token_hex(32)
            secret_file.write_text(secret, encoding="utf-8")

    # Yard PCs use plain HTTP (http://192.168.x.x:5000). Secure + SameSite=None
    # cookies are dropped on HTTP, so POST /login 302 then GET / bounces to login.
    # For HTTPS/iframe set AMS_HTTPS=1 (or SESSION_COOKIE_SECURE=1 + SAMESITE=None).
    env_secure = os.environ.get("SESSION_COOKIE_SECURE")
    env_samesite = os.environ.get("SESSION_COOKIE_SAMESITE")
    use_https = (os.environ.get("AMS_HTTPS") or "").strip() == "1"
    if env_secure is None:
        cookie_secure = bool(use_https)
    else:
        cookie_secure = env_secure.strip() not in ("0", "false", "False", "")
    cookie_samesite = (env_samesite or ("None" if cookie_secure else "Lax")).strip() or "Lax"
    if str(cookie_samesite).lower() == "none" and not cookie_secure:
        cookie_samesite = "Lax"

    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLITE_JOURNAL_MODE=journal_mode,
        MAX_CONTENT_LENGTH=max_upload_mb * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        REMEMBER_COOKIE_DURATION=timedelta(days=30),
        SESSION_COOKIE_NAME="ams_session",
        SESSION_COOKIE_PATH="/",
        SESSION_COOKIE_DOMAIN=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=cookie_samesite,
        SESSION_COOKIE_SECURE=cookie_secure,
        SESSION_REFRESH_EACH_REQUEST=True,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE=cookie_samesite,
        REMEMBER_COOKIE_SECURE=cookie_secure,
        PREFERRED_URL_SCHEME="https" if cookie_secure else "http",
        FULL_RAW_IMPORT_ENABLED="1",
        IMPORT_TMP_DIR=str(tmp_dir),
        IMPORT_UPLOADS_DIR=str(tmp_dir / "import_uploads"),
        IMPORT_REPORTS_DIR=str(tmp_dir / "import_reports"),
        IMPORT_ARTIFACT_RETENTION_SECONDS=int(
            os.environ.get("IMPORT_ARTIFACT_RETENTION_SECONDS", str(7 * 24 * 3600)) or "0"
        ),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", str(root / "static" / "uploads")),
        BACKUP_DIR=os.environ.get("BACKUP_DIR", str(instance_dir / "storage" / "backups")),
        MAINTENANCE_TEMP_DIR=os.environ.get("MAINTENANCE_TEMP_DIR", str(instance_dir / "storage" / "temp")),
        BACKUP_INTERVAL_SECONDS=int(os.environ.get("BACKUP_INTERVAL_SECONDS", "3600") or "3600"),
        BACKUP_RETENTION=int(os.environ.get("BACKUP_RETENTION", "3") or "3"),
        BACKUP_LOCK_STALE_SECONDS=int(os.environ.get("BACKUP_LOCK_STALE_SECONDS", "7200") or "7200"),
        TEMP_RETENTION_SECONDS=int(os.environ.get("TEMP_RETENTION_SECONDS", "86400") or "86400"),
        MIN_FREE_DISK_BYTES=int(os.environ.get("MIN_FREE_DISK_BYTES", str(100 * 1024 * 1024)) or "0"),
        # Do not create backup database files from the web process. Backups
        # remain available only through an explicit maintenance operation.
        BACKUP_EMBEDDED_SCHEDULER=(os.environ.get("BACKUP_EMBEDDED_SCHEDULER", "0").strip().lower() not in ("0", "false", "no")),
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    _configure_logging(app)
    db.init_app(app)

    # SQLite does not enforce declared foreign keys unless each connection
    # explicitly enables them. Register this before bootstrap opens the first
    # connection so future lifecycle regressions fail transactionally instead
    # of accumulating silent dangling rows.
    with app.app_context():
        engine = db.engine
        if engine.dialect.name == "sqlite" and not getattr(engine, "_ams_fk_listener", False):
            @event.listens_for(engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                    enabled = cursor.execute("PRAGMA foreign_keys").fetchone()
                    if not enabled or enabled[0] != 1:
                        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")
                finally:
                    cursor.close()

            engine._ams_fk_listener = True

    @app.before_request
    def _sqlite_wal_once():
        if app.config.get('_sqlite_wal_ready'):
            return
        try:
            from sqlalchemy import text as sql_text
            mode = app.config.get('SQLITE_JOURNAL_MODE', 'WAL')
            # WAL needs shared-memory (a -shm file) that every process mmaps.
            # Shared hosts such as PythonAnywhere keep /home on a network
            # filesystem where that is unsupported, so the pragma - or the very
            # next query - raises "unable to open database file" / "disk I/O
            # error" and every request turns into a 500.  DELETE journalling
            # works everywhere, so it is the safe mode there.
            db.session.execute(sql_text(f'PRAGMA journal_mode={mode}'))
            db.session.execute(sql_text('PRAGMA busy_timeout=8000'))
            db.session.commit()
            app.config['_sqlite_wal_ready'] = True
        except Exception:
            # Never let a pragma failure turn a normal page into a 500.
            try:
                db.session.rollback()
            except Exception:
                pass
            app.config['_sqlite_wal_ready'] = True
            logging.getLogger(__name__).warning(
                "Could not apply SQLite journal pragmas; continuing with the "
                "database default journal mode.",
                exc_info=True,
            )

    login_manager = LoginManager()
    login_manager.login_view = "login"
    # None: same user (or several managers) may stay logged in from many IPs/PCs.
    # "basic"/"strong" can drop a session when IP or User-Agent differs.
    login_manager.session_protection = None
    login_manager.init_app(app)

    from app.services.permissions import load_user

    login_manager.user_loader(load_user)

    # Core domain routes first so short names (clients, login, …) are not
    # stolen by later feature packs such as fbm_rentals.clients.
    from app.blueprints.core import bp as core_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.sales import bp as sales_bp
    from app.blueprints.masters import bp as masters_bp
    from app.blueprints.ledgers import bp as ledgers_bp
    from app.blueprints.ops import bp as ops_bp
    from app.blueprints.reports import bp as reports_bp
    from app.blueprints.api import bp as api_bp
    from app.blueprints.system import bp as system_bp
    from app.blueprints.misc import bp as misc_bp

    for bp in (
        core_bp,
        auth_bp,
        sales_bp,
        masters_bp,
        ledgers_bp,
        ops_bp,
        reports_bp,
        api_bp,
        system_bp,
        misc_bp,
    ):
        if bp.name not in app.blueprints:
            app.register_blueprint(bp)

    _alias_unprefixed_endpoints(app)

    load_modules(app, blueprint_dir=str(root / "blueprints"))

    from app.hooks import register_hooks

    register_hooks(app)

    from app.services.import_jobs import register_import_job_routes

    register_import_job_routes(app)

    with app.app_context():
        try:
            from app.services.health import (
                _guard_db_file_before_bootstrap,
                _db_health_check_after_bootstrap,
            )
            from app.services.schema import _bootstrap_database

            if app.config.get("TESTING"):
                from app.services.schema import _ensure_default_admin, _ensure_model_columns
                db.create_all()
                _ensure_model_columns()
                # Keep a fresh test database usable in the same way as a fresh
                # production database.  The smoke tests and local developers
                # rely on the documented Admin login even when no rows exist.
                _ensure_default_admin()
            else:
                _guard_db_file_before_bootstrap()
                _bootstrap_database()
                _db_health_check_after_bootstrap()
        except Exception:
            logging.getLogger(__name__).exception("bootstrap skipped/failed")

    # Start once at application startup, never from a user request. The
    # cross-process filesystem lock prevents duplicate work under Gunicorn.
    from app.services.maintenance import start_embedded_scheduler
    start_embedded_scheduler(app)

    return app


_NETWORK_FILESYSTEMS = {
    "nfs", "nfs4", "cifs", "smb", "smb2", "smbfs", "afs", "fuse.sshfs",
    "9p", "glusterfs", "lustre", "ceph", "beegfs", "afpfs", "ncpfs",
}


def _on_network_filesystem(path: str) -> bool:
    """Best-effort detection of a network-mounted filesystem.

    SQLite's WAL journal needs POSIX shared memory, which network filesystems
    do not provide.  Shared hosting such as PythonAnywhere serves /home over
    NFS-like storage, so a WAL database there fails with "unable to open
    database file" / "disk I/O error" on every request.
    """
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return False
    candidate = target if target.exists() else target.parent
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    best_point, best_type = "", ""
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point, fstype = parts[1].replace("\\040", " "), parts[2]
        try:
            mount_path = Path(point)
        except Exception:
            continue
        if candidate == mount_path or mount_path in candidate.parents:
            if len(point) >= len(best_point):
                best_point, best_type = point, fstype
    return best_type.lower() in _NETWORK_FILESYSTEMS


def _resolve_sqlite_journal_mode(db_path: str) -> str:
    """Pick a journal mode that actually works on this host.

    Override explicitly with SQLITE_JOURNAL_MODE=WAL|DELETE|TRUNCATE.
    """
    configured = (os.environ.get("SQLITE_JOURNAL_MODE") or "").strip().upper()
    allowed = {"WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}
    if configured in allowed:
        return configured
    # PythonAnywhere exports these markers in the web-app environment.
    on_pythonanywhere = any(
        key in os.environ
        for key in ("PYTHONANYWHERE_DOMAIN", "PYTHONANYWHERE_SITE")
    )
    if on_pythonanywhere or _on_network_filesystem(db_path):
        return "DELETE"
    return "WAL"


def _alias_unprefixed_endpoints(app: Flask) -> None:
    """Keep legacy url_for('login') / templates working after blueprint split."""
    existing = set(app.view_functions)
    extras = []
    for rule in list(app.url_map.iter_rules()):
        if "." not in rule.endpoint:
            continue
        short = rule.endpoint.split(".", 1)[1]
        if short in existing:
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        app.view_functions[short] = view
        extras.append((rule.rule, short, view, sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})))
        existing.add(short)
    for rule, short, view, methods in extras:
        try:
            app.add_url_rule(rule, endpoint=short, view_func=view, methods=methods or None)
        except Exception:
            pass


def _configure_logging(app: Flask) -> None:
    """Configure console output and a bounded technical diagnostic log."""
    root = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s]: %(message)s")
    if not any(getattr(handler, "_ams_console", False) for handler in root.handlers):
        console = logging.StreamHandler()
        console._ams_console = True
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        root.addHandler(console)

    log_dir = Path(app.instance_path) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = (log_dir / "errorlog.txt").resolve()
        existing = any(
            isinstance(handler, RotatingFileHandler)
            and Path(getattr(handler, "baseFilename", "")).resolve() == log_path
            for handler in root.handlers
        )
        if not existing:
            rotating = RotatingFileHandler(
                log_path,
                maxBytes=int(os.environ.get("ERROR_LOG_MAX_BYTES", str(2 * 1024 * 1024))),
                backupCount=int(os.environ.get("ERROR_LOG_BACKUP_COUNT", "3")),
                encoding="utf-8",
            )
            rotating.setLevel(logging.WARNING)
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
    except OSError:
        # A read-only log directory must not prevent the application starting.
        root.exception("Unable to configure rotating file logging")
    root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
