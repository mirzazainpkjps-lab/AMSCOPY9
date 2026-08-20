#!/usr/bin/env python3
"""Profile representative pages against an isolated read-only database copy.

The benchmark reports client-observed wall time, Flask server time, cumulative
SQL cursor time, template-render time, SQL query count, and uncompressed
response bytes. The first request is a warm-up; medians come from three warm
requests. No request is made against the source database.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _median(samples: list[dict], key: str, digits: int = 2):
    value = statistics.median(sample[key] for sample in samples)
    if key == "query_count":
        return int(value) if float(value).is_integer() else value
    return round(value, digits)


def main():
    from flask import before_render_template, request_finished, request_started, template_rendered
    from sqlalchemy import event, func
    from werkzeug.security import generate_password_hash

    from app import create_app
    from models import User, db

    source = ROOT / "instance" / "ahmed_cement.db"
    with tempfile.TemporaryDirectory(prefix="ams-benchmark-") as folder:
        target = Path(folder) / "benchmark.db"
        # sqlite backup gives a transactionally consistent isolated copy and
        # mode=ro guarantees this profiler cannot mutate the supplied database.
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        src.backup(dst)
        dst.close()
        src.close()

        started = time.perf_counter()
        app = create_app(
            {
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{target}",
                "BACKUP_EMBEDDED_SCHEDULER": False,
                "WTF_CSRF_ENABLED": False,
                "SECRET_KEY": "benchmark-only",
            }
        )
        startup_ms = (time.perf_counter() - started) * 1000

        # Authenticate with an in-memory random credential after changing only
        # the isolated copy; the profiler never needs a real account password.
        benchmark_password = secrets.token_urlsafe(32)
        with app.app_context():
            benchmark_user = (
                User.query.filter(func.lower(func.trim(User.username)) == "admin")
                .order_by(User.id.asc())
                .first()
            )
            if benchmark_user is None:
                raise RuntimeError("isolated database has no Admin benchmark account")
            benchmark_user.password_hash = generate_password_hash(benchmark_password)
            benchmark_user.password_plain = None
            benchmark_user.status = "active"
            db.session.commit()
            benchmark_username = benchmark_user.username
            engine = db.engine

        state: dict = {"current": None, "latest": None}

        def before_cursor_execute(conn, cursor, statement, params, context, many):
            context._benchmark_started = time.perf_counter()

        def after_cursor_execute(conn, cursor, statement, params, context, many):
            current = state.get("current")
            started_at = getattr(context, "_benchmark_started", None)
            if current is not None and started_at is not None:
                current["query_count"] += 1
                current["db_ms"] += (time.perf_counter() - started_at) * 1000

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        event.listen(engine, "after_cursor_execute", after_cursor_execute)

        @request_started.connect_via(app)
        def on_request_started(sender, **extra):
            state["current"] = {
                "server_started": time.perf_counter(),
                "query_count": 0,
                "db_ms": 0.0,
                "template_ms": 0.0,
                "template_starts": [],
            }

        @before_render_template.connect_via(app)
        def on_before_render(sender, template, context, **extra):
            current = state.get("current")
            if current is not None:
                current["template_starts"].append(time.perf_counter())

        @template_rendered.connect_via(app)
        def on_template_rendered(sender, template, context, **extra):
            current = state.get("current")
            if current is not None and current["template_starts"]:
                current["template_ms"] += (
                    time.perf_counter() - current["template_starts"].pop()
                ) * 1000

        @request_finished.connect_via(app)
        def on_request_finished(sender, response, **extra):
            current = state.get("current")
            if current is not None:
                current["server_ms"] = (
                    time.perf_counter() - current["server_started"]
                ) * 1000
                state["latest"] = current
                state["current"] = None

        client = app.test_client()
        login_started = time.perf_counter()
        login = client.post(
            "/login",
            data={
                "username": benchmark_username,
                "password": benchmark_password,
                "remember_me": "0",
            },
            follow_redirects=False,
        )
        login_ms = (time.perf_counter() - login_started) * 1000

        pages = [
            "/",
            "/clients",
            "/materials",
            "/bookings",
            "/direct_sales",
            "/pending_bills",
            "/inventory/stock_summary",
            "/accounts/",
            "/profit_reports",
            "/api/clients/search?q=ahm",
        ]
        results = {}
        for path in pages:
            # Warm caches, imports, and template compilation before measuring.
            client.get(path, follow_redirects=False)
            samples = []
            response = None
            for _ in range(3):
                tick = time.perf_counter()
                response = client.get(path, follow_redirects=False)
                total_ms = (time.perf_counter() - tick) * 1000
                metric = state["latest"]
                samples.append(
                    {
                        "total_ms": total_ms,
                        "server_ms": metric["server_ms"],
                        "db_ms": metric["db_ms"],
                        "template_ms": metric["template_ms"],
                        "query_count": metric["query_count"],
                    }
                )
            results[path] = {
                "status": response.status_code,
                "response_bytes": len(response.data),
                "median_total_ms": _median(samples, "total_ms"),
                "median_server_ms": _median(samples, "server_ms"),
                "median_db_ms": _median(samples, "db_ms"),
                "median_template_ms": _median(samples, "template_ms"),
                "median_query_count": _median(samples, "query_count"),
            }

        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "after_cursor_execute", after_cursor_execute)
        print(
            json.dumps(
                {
                    "database_copy": "isolated",
                    "warm_samples_per_page": 3,
                    "startup_ms": round(startup_ms, 2),
                    "login_status": login.status_code,
                    "login_ms": round(login_ms, 2),
                    "route_rules": len(list(app.url_map.iter_rules())),
                    "pages": results,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
