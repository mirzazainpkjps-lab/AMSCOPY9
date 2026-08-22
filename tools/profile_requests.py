"""AMS request-latency / query-count profiler (safe, read-only w.r.t. live DB).

It copies ``instance/ahmed_cement.db`` into a temp file and profiles against the
copy with ``TESTING=True``, so the live database and ``instance/health_snapshot.json``
are never touched.

Usage:
    python tools/profile_requests.py

Run it from the repository root with the project venv active.
"""
from __future__ import annotations
import os, shutil, sys, tempfile, time, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE = ROOT / "instance" / "ahmed_cement.db"
PROF = Path(tempfile.mktemp(suffix=".db"))
shutil.copy2(LIVE, PROF)

os.environ["APP_DB_PATH"] = str(PROF)
os.environ.setdefault("ALLOW_EMPTY_DB", "1")
os.environ["BACKUP_EMBEDDED_SCHEDULER"] = "0"

from app import create_app
from sqlalchemy import event
from models import db

app = create_app({
    "TESTING": True,
    "WTF_CSRF_ENABLED": False,
    "SESSION_COOKIE_SECURE": False,
    "SESSION_COOKIE_SAMESITE": "Lax",
})

_tl = threading.local()

with app.app_context():
    engine = db.engine

@event.listens_for(engine, "before_cursor_execute")
def _before(conn, cursor, statement, params, context, executemany):
    _tl.t0 = time.perf_counter()
    _tl.stmt = statement

@event.listens_for(engine, "after_cursor_execute")
def _after(conn, cursor, statement, params, context, executemany):
    dt = (time.perf_counter() - getattr(_tl, "t0", time.perf_counter())) * 1000
    q = getattr(_tl, "query_log", None)
    if q is not None:
        q.append((dt, statement))


def profile(label, fn, top=6):
    _tl.query_log = []
    t0 = time.perf_counter()
    rv = fn()
    dt = (time.perf_counter() - t0) * 1000
    qlog = sorted(_tl.query_log, reverse=True)
    total_sql = sum(x[0] for x in qlog)
    print(f"\n{label}: {dt:.1f} ms | {len(qlog)} queries | {total_sql:.1f} ms sql")
    for d, s in qlog[:top]:
        s = " ".join(s.split())
        print(f"    {d:6.1f}ms  {s[:140]}")
    return rv


def main():
    import secrets as _sec
    client = app.test_client()

    print("== login ==")
    profile("POST /login", lambda: client.post("/login", data={
        "username": "Admin", "password": "Admin@fbm12345", "remember_me": "1",
    }, follow_redirects=False))

    print("\n== page loads ==")
    for path in ["/direct_sales", "/", "/bookings", "/clients", "/payments", "/cash_flow"]:
        profile(f"GET {path}", lambda p=path: client.get(p), top=3)

    print("\n== save + edit round trip ==")
    sale_form = {
        "category": "Cash",
        "client_name": "WALK-IN CUSTOMER",
        "driver_name": "HAFIZ SHOAIB",
        "product_name[]": ["12MM STEEL"],
        "material_id[]": ["1"],
        "qty[]": ["1"],
        "unit_rate[]": ["100"],
        "alternate_material[]": [""],
        "alternate_material_id[]": [""],
        "grn_item_id[]": [""],
        "ignore_booking_item[]": [""],
        "paid_amount": "100",
        "payment_method": "Cash",
        "payment_account_id": "2",
        "manual_bill_no": "",
        "note": "perf test",
        "sale_date": "2026-08-22",
        "create_invoice": "0",
        "track_as_cash": "0",
        "delivery_rent": "0",
        "allow_negative_stock": "1",
        "idempotency_key": "perf-" + _sec.token_hex(8),
        "has_bill": "on",
    }
    rv = profile("POST /add_direct_sale", lambda: client.post(
        "/add_direct_sale", data=sale_form, follow_redirects=False), top=4)

    import sqlite3
    c = sqlite3.connect(str(PROF))
    new_id = c.execute("SELECT id FROM direct_sale ORDER BY id DESC LIMIT 1").fetchone()[0]
    c.close()

    edit_form = dict(sale_form)
    edit_form["idempotency_key"] = ""
    edit_form["qty[]"] = ["2"]
    edit_form["unit_rate[]"] = ["150"]
    edit_form["paid_amount"] = "300"
    profile(f"POST /edit_bill/DirectSale/{new_id}", lambda: client.post(
        f"/edit_bill/DirectSale/{new_id}", data=edit_form, follow_redirects=False), top=4)

    # what the browser waits for after a save: the redirect target page
    profile("GET /direct_sales (post-save redirect)", lambda: client.get("/direct_sales"), top=3)

    print("\nDone. (profile DB was a temp copy; live data untouched)")


if __name__ == "__main__":
    main()
