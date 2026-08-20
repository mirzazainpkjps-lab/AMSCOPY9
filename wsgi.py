import os
import logging
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# PythonAnywhere may not put the project directory on sys.path when it loads
# the WSGI file.  Add it explicitly so imports work regardless of the working
# directory configured for the web app.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "instance", "ahmed_cement.db")
# The application factory reads APP_DB_PATH. Keep the live database beside
# this project and never substitute a backup database file.
os.environ.setdefault("APP_DB_PATH", DB_PATH)
# A missing live DB is a valid first-run state.
os.environ.setdefault("ALLOW_EMPTY_DB", "1")
# The web process must not create scheduled backup/database copies.
os.environ["BACKUP_EMBEDDED_SCHEDULER"] = "0"

# SQLite's WAL journal requires POSIX shared memory (the -shm file).  Shared
# hosts such as PythonAnywhere store /home on a network filesystem where that
# is unavailable, and a WAL database there fails with
# "sqlite3.OperationalError: unable to open database file" (or "disk I/O
# error") on every request, i.e. a permanent HTTP 500.  Force the portable
# rollback journal there.  Set SQLITE_JOURNAL_MODE=WAL to override on a host
# with local disk.
if any(k in os.environ for k in ("PYTHONANYWHERE_DOMAIN", "PYTHONANYWHERE_SITE")):
    os.environ.setdefault("SQLITE_JOURNAL_MODE", "DELETE")

logging.basicConfig(level=logging.INFO)

try:
    from app import create_app

    app = create_app()
    application = app
except Exception:  # pragma: no cover - startup diagnostics only
    # Without this, a factory error is only visible in the server error log and
    # the browser shows an opaque "Something went wrong" page.  Serve the real
    # traceback instead so the failure can be fixed quickly.
    STARTUP_TRACEBACK = traceback.format_exc()
    logging.getLogger("wsgi").critical("AMS failed to start:\n%s", STARTUP_TRACEBACK)

    def application(environ, start_response):  # type: ignore[misc]
        body = (
            "AMS failed to start.\n\n"
            + STARTUP_TRACEBACK
        ).encode("utf-8")
        start_response(
            "500 Internal Server Error",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    app = application
