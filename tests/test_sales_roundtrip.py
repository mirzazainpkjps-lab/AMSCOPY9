"""
Sales section round-trip audit.

Drives the real HTTP routes (booked sale, booked+due, due, cash, material
returns and client payments) through the Flask test client and then verifies
that the *derived* state — material ledger, booking allocations, pending bills,
stock and the client financial ledger — stays intact and correct after every
step.

Each scenario uses its own client + material so failures cannot bleed across
tests, and every money/qty assertion is expressed against an independently
computed expected value rather than echoing the same code under test.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


def _flashes(resp):
    html = resp.get_data(as_text=True)
    found = re.findall(
        r'class="([^"]*alert[^"]*)"[^>]*>(.*?)</div>',
        html,
        flags=re.S | re.I,
    )
    out = []
    for cls, raw in found:
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = " ".join(txt.split())
        if txt:
            out.append((cls, txt))
    return out


def _danger(resp):
    return [txt for cls, txt in _flashes(resp) if "alert-danger" in cls]


@pytest.fixture(scope="module")
def app():
    import tempfile
    db_file = tempfile.mktemp(suffix=".db")
    os.environ["APP_DB_PATH"] = db_file
    from app import create_app
    from models import db
    from app.services.schema import _ensure_model_columns

    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "rt-test",
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    with application.app_context():
        db.create_all()
        _ensure_model_columns()
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
    yield application
    try:
        os.remove(db_file)
    except OSError:
        pass


@pytest.fixture(scope="module")
def client(app):
    c = app.test_client()
    rv = c.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "remember_me": "1"},
        follow_redirects=False,
    )
    assert rv.status_code in (302, 303), f"login failed: {rv.status_code}"
    return c


@pytest.fixture(scope="module")
def base(app, client):
    """Create the shared master data + one GRN lot per scenario material."""
    from models import (
        db, Material, Client, MaterialCategory, GRN, GRNItem, Account,
        Entry, Settings,
    )

    with app.app_context():
        cat = MaterialCategory.query.filter_by(name="General").first()
        if not cat:
            cat = MaterialCategory(name="General")
            db.session.add(cat)
            db.session.flush()

        materials = {}
        clients = {}
        for key, mname in [
            ("a", "RT-A Cement"), ("b", "RT-B Cement"),
            ("c", "RT-C Cement"), ("d", "RT-D Cement"),
            ("e", "RT-E Cement"),
        ]:
            mat = Material(code=f"RT-M-{key.upper()}", name=mname, unit_price=1500,
                           total=0, category_id=cat.id, is_active=True)
            db.session.add(mat)
            materials[key] = mat

        for key, code, name in [
            ("a", "RT-CL-A", "Roundtrip Booked Client"),
            ("b", "RT-CL-B", "Roundtrip Mixed Client"),
            ("c", "RT-CL-C", "Roundtrip Credit Client"),
            ("d", "RT-CL-D", "Roundtrip Cash Client"),
            ("e", "RT-CL-E", "Roundtrip Overreturn Client"),
        ]:
            cli = Client(code=code, name=name, is_active=True, opening_balance=0)
            db.session.add(cli)
            clients[key] = cli

        acc = Account(name="RT CASH", category="cash", account_type="cash",
                      balance=1_000_000.0, is_active=True)
        db.session.add(acc)
        db.session.flush()
        acc_id = acc.id

        # One GRN per material so stock and FIFO cost tracking are realistic.
        g = GRN(supplier="RT Supplier", auto_bill_no="RT-GRN-1",
                date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(g)
        db.session.flush()
        for key, mname in [
            ("a", "RT-A Cement"), ("b", "RT-B Cement"),
            ("c", "RT-C Cement"), ("d", "RT-D Cement"),
            ("e", "RT-E Cement"),
        ]:
            gi = GRNItem(grn_id=g.id, mat_name=mname, qty=500,
                         price_at_time=1100, is_void=False)
            db.session.add(gi)
            materials[key].total = 500
            db.session.add(Entry(
                date="2026-01-01", time="08:00:00", type="IN", material=mname,
                client="RT Supplier", qty=500, bill_no="", auto_bill_no="RT-GRN-1",
                created_by="test", is_void=False,
            ))

        settings = Settings.query.first()
        if not settings:
            settings = Settings()
            db.session.add(settings)
        settings.allow_global_negative_stock = False

        db.session.commit()
        return {
            "materials": {k: v.id for k, v in materials.items()},
            "clients": {k: v.id for k, v in clients.items()},
            "acc_id": acc_id,
        }


def _post(client, path, data):
    return client.post(path, data=data, follow_redirects=True)


def _sale_by_client(app, code):
    from models import DirectSale
    with app.app_context():
        rows = DirectSale.query.filter_by(client_code=code, is_void=False).order_by(DirectSale.id.asc()).all()
        return [(r.id, r.category, float(r.amount or 0), float(r.paid_amount or 0),
                 [(it.product_name, float(it.qty or 0), float(it.price_at_time or 0)) for it in r.items])
                for r in rows]


def _pending_total(app, code):
    from models import PendingBill
    from sqlalchemy import func
    with app.app_context():
        val = PendingBill.query.filter(
            PendingBill.client_code == code,
            PendingBill.is_void == False,
            PendingBill.is_paid == False,
            PendingBill.amount > 0,
        ).with_entities(func.sum(PendingBill.amount)).scalar()
        return float(val or 0)


def _client_ledger_balance(app, code):
    from models import Client
    from app.services.financial_ledgers import build_client_financial_ledger
    with app.app_context():
        c = Client.query.filter_by(code=code).first()
        ledger = build_client_financial_ledger(c)
        return float(ledger["closing_balance"])


def _stock(app, mname):
    from models import Material
    with app.app_context():
        m = Material.query.filter_by(name=mname).first()
        return float(m.total or 0)


def _booking_allocation_total(app, code):
    from models import BookingAllocation, DirectSale, Booking, Client
    from sqlalchemy import func
    with app.app_context():
        cli = Client.query.filter_by(code=code).first()
        booking_ids = [b.id for b in Booking.query.filter(
            func.lower(func.trim(Booking.client_name)) == cli.name.strip().lower(),
            Booking.is_void == False).all()]
        sale_ids = [s.id for s in DirectSale.query.filter_by(client_code=code, is_void=False).all()]
        val = BookingAllocation.query.filter(
            BookingAllocation.is_void == False,
            BookingAllocation.sale_id.in_(sale_ids),
        ).with_entities(func.sum(BookingAllocation.qty)).scalar()
        return float(val or 0)


def _booked_returnable(app, code, mname):
    from models import Client
    from app.services.drafts import _client_booked_material_returnable_qty_map
    with app.app_context():
        cli = Client.query.filter_by(code=code).first()
        return float(_client_booked_material_returnable_qty_map(cli).get(mname, 0) or 0)


def _normal_returnable(app, code, mname):
    from models import Client
    from app.services.drafts import _client_material_returnable_qty_map
    with app.app_context():
        cli = Client.query.filter_by(code=code).first()
        return float(_client_material_returnable_qty_map(cli).get(mname, 0) or 0)


def _entries(app, code, mname=None):
    from models import Entry
    with app.app_context():
        q = Entry.query.filter(Entry.is_void == False)
        if mname:
            q = q.filter(Entry.material == mname)
        rows = q.filter(
            (Entry.client_code == code)
        ).order_by(Entry.id.asc()).all()
        return [(e.type, e.material, float(e.qty or 0), e.client_category,
                 e.nimbus_no, e.transaction_category, e.booked_material, e.bill_no)
                for e in rows]


def _returns(app, code):
    from models import MaterialReturn, Client
    from sqlalchemy import func
    with app.app_context():
        cli = Client.query.filter_by(code=code).first()
        rows = MaterialReturn.query.filter(
            func.lower(func.trim(MaterialReturn.client_name)) == cli.name.strip().lower(),
            MaterialReturn.is_void == False,
        ).order_by(MaterialReturn.id.asc()).all()
        return [(r.id, r.return_type, float(r.amount or 0)) for r in rows]


def test_booked_sale_then_booked_return_roundtrip(app, client, base):
    """Booked Sale (Booking Delivery) -> Booked Return stays intact."""
    acc_id = base["acc_id"]

    # 1. Booking: 20 bags @ 1200, 10000 paid now.
    rv = _post(client, "/add_booking", {
        "client_code": "RT-CL-A",
        "material_name[]": ["RT-A Cement"],
        "qty[]": ["20"],
        "unit_rate[]": ["1200"],
        "amount": "24000",
        "paid_amount": "10000",
        "payment_method": "Cash",
        "payment_account_id": str(acc_id),
    })
    assert not _danger(rv), _flashes(rv)

    # 2. Booked sale: 8 bags (rates present — must stay amount 0).
    rv = _post(client, "/add_direct_sale", {
        "client_code": "RT-CL-A",
        "category": "Booking Delivery",
        "driver_name": "RT Driver",
        "product_name[]": ["RT-A Cement"],
        "qty[]": ["8"],
        "unit_rate[]": ["1200"],
        "amount": "9600",
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
    })
    assert not _danger(rv), _flashes(rv)

    sales = _sale_by_client(app, "RT-CL-A")
    assert len(sales) == 1
    assert sales[0][1] == "Booking Delivery"
    assert abs(sales[0][2]) < 0.01, "Booked sale must have amount 0"
    assert abs(sales[0][3]) < 0.01
    assert sales[0][4] and abs(sales[0][4][0][2]) < 0.01, "Booked item price must be 0"

    ents = _entries(app, "RT-CL-A", "RT-A Cement")
    out = [e for e in ents if e[0] == "OUT"]
    assert len(out) == 1
    assert out[0][3] == "Booking Delivery" and abs(out[0][2] - 8) < 0.01

    assert abs(_booking_allocation_total(app, "RT-CL-A") - 8) < 0.01
    assert abs(_stock(app, "RT-A Cement") - 492) < 0.01
    # No due pending for a pure booked sale.
    assert _pending_total(app, "RT-CL-A") == 14000.0, "only booking pending remains"

    # 3. Booked return: 3 bags @ rent 200 = 600 credit.
    rv = _post(client, "/add_material_return", {
        "client_code": "RT-CL-A",
        "return_type": "booked",
        "material_name[]": ["RT-A Cement"],
        "qty[]": ["3"],
        "unit_rate[]": [""],
        "rent_rate[]": ["200"],
    })
    assert not _danger(rv), _flashes(rv)

    rets = _returns(app, "RT-CL-A")
    assert len(rets) == 1 and rets[0][1] == "booked" and abs(rets[0][2] - 600) < 0.01

    ents = _entries(app, "RT-CL-A", "RT-A Cement")
    ins = [e for e in ents if e[0] == "IN" and e[5] == "Booked Return"]
    assert len(ins) == 1 and abs(ins[0][2] - 3) < 0.01

    assert abs(_booked_returnable(app, "RT-CL-A", "RT-A Cement") - 5) < 0.01
    assert abs(_stock(app, "RT-A Cement") - 495) < 0.01
    assert abs(_pending_total(app, "RT-CL-A") - 13400) < 0.01
    assert abs(_client_ledger_balance(app, "RT-CL-A") - 13400) < 0.5


def test_booked_plus_due_then_returns_roundtrip(app, client, base):
    """Booked + Due (Mixed Transaction) -> booked + normal returns stay intact."""
    acc_id = base["acc_id"]

    rv = _post(client, "/add_booking", {
        "client_code": "RT-CL-B",
        "material_name[]": ["RT-B Cement"],
        "qty[]": ["20"],
        "unit_rate[]": ["1200"],
        "amount": "24000",
        "paid_amount": "10000",
        "payment_method": "Cash",
        "payment_account_id": str(acc_id),
    })
    assert not _danger(rv), _flashes(rv)

    # Mixed: one line of 25 => 20 booked + 5 credit @ 1500.
    rv = _post(client, "/add_direct_sale", {
        "client_code": "RT-CL-B",
        "category": "Mixed Transaction",
        "driver_name": "RT Driver",
        "product_name[]": ["RT-B Cement"],
        "qty[]": ["25"],
        "unit_rate[]": ["1500"],
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "RT-MIX-1",
    })
    assert not _danger(rv), _flashes(rv)

    sales = _sale_by_client(app, "RT-CL-B")
    assert len(sales) == 1
    assert sales[0][1] == "Mixed Transaction"
    assert abs(sales[0][2] - 7500) < 0.01
    assert abs(sales[0][3]) < 0.01

    ents = _entries(app, "RT-CL-B", "RT-B Cement")
    out_booking = [e for e in ents if e[0] == "OUT" and e[3] == "Booking Delivery"]
    out_credit = [e for e in ents if e[0] == "OUT" and e[3] == "Credit Customer"]
    assert len(out_booking) == 1 and abs(out_booking[0][2] - 20) < 0.01
    assert len(out_credit) == 1 and abs(out_credit[0][2] - 5) < 0.01

    assert abs(_booking_allocation_total(app, "RT-CL-B") - 20) < 0.01
    assert abs(_stock(app, "RT-B Cement") - 475) < 0.01
    assert abs(_pending_total(app, "RT-CL-B") - 21500) < 0.01  # 14000 booking + 7500 sale

    # Booked return 4 @ 200 = 800.
    rv = _post(client, "/add_material_return", {
        "client_code": "RT-CL-B",
        "return_type": "booked",
        "material_name[]": ["RT-B Cement"],
        "qty[]": ["4"],
        "unit_rate[]": [""],
        "rent_rate[]": ["200"],
    })
    assert not _danger(rv), _flashes(rv)

    # Normal return 2 @ 1500 = 3000.
    rv = _post(client, "/add_material_return", {
        "client_code": "RT-CL-B",
        "return_type": "normal",
        "material_name[]": ["RT-B Cement"],
        "qty[]": ["2"],
        "unit_rate[]": ["1500"],
        "rent_rate[]": [""],
    })
    assert not _danger(rv), _flashes(rv)

    assert abs(_booked_returnable(app, "RT-CL-B", "RT-B Cement") - 16) < 0.01
    assert abs(_normal_returnable(app, "RT-CL-B", "RT-B Cement") - 3) < 0.01
    assert abs(_stock(app, "RT-B Cement") - 481) < 0.01
    # Settlement applies oldest-first: booking pending absorbs both returns.
    assert abs(_pending_total(app, "RT-CL-B") - 17700) < 0.01
    assert abs(_client_ledger_balance(app, "RT-CL-B") - 17700) < 0.5


def test_due_sale_payment_return_roundtrip(app, client, base):
    """Due Sale (Credit Customer) -> payment -> normal return stay intact."""
    acc_id = base["acc_id"]

    rv = _post(client, "/add_direct_sale", {
        "client_code": "RT-CL-C",
        "category": "Credit Customer",
        "driver_name": "RT Driver",
        "product_name[]": ["RT-C Cement"],
        "qty[]": ["10"],
        "unit_rate[]": ["1500"],
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "RT-CR-1",
    })
    assert not _danger(rv), _flashes(rv)

    sales = _sale_by_client(app, "RT-CL-C")
    assert len(sales) == 1
    assert sales[0][1] == "Credit Customer"
    assert abs(sales[0][2] - 15000) < 0.01
    assert abs(_stock(app, "RT-C Cement") - 490) < 0.01
    assert abs(_pending_total(app, "RT-CL-C") - 15000) < 0.01

    # Payment 5000.
    rv = _post(client, "/add_payment", {
        "client_code": "RT-CL-C",
        "amount": "5000",
        "method": "Cash",
        "payment_account_id": str(acc_id),
    })
    assert not _danger(rv), _flashes(rv)
    assert abs(_pending_total(app, "RT-CL-C") - 10000) < 0.01

    # Normal return 2 @ 1500 = 3000.
    rv = _post(client, "/add_material_return", {
        "client_code": "RT-CL-C",
        "return_type": "normal",
        "material_name[]": ["RT-C Cement"],
        "qty[]": ["2"],
        "unit_rate[]": ["1500"],
        "rent_rate[]": [""],
    })
    assert not _danger(rv), _flashes(rv)

    assert abs(_normal_returnable(app, "RT-CL-C", "RT-C Cement") - 8) < 0.01
    assert abs(_stock(app, "RT-C Cement") - 492) < 0.01
    assert abs(_pending_total(app, "RT-CL-C") - 7000) < 0.01
    assert abs(_client_ledger_balance(app, "RT-CL-C") - 7000) < 0.5


def test_cash_sale_roundtrip(app, client, base):
    """Cash Sale fully paid -> stock + accounts + zero ledger due."""
    acc_id = base["acc_id"]

    rv = _post(client, "/add_direct_sale", {
        "client_code": "RT-CL-D",
        "category": "Cash",
        "driver_name": "RT Driver",
        "product_name[]": ["RT-D Cement"],
        "qty[]": ["5"],
        "unit_rate[]": ["1300"],
        "paid_amount": "6500",
        "payment_method": "Cash",
        "payment_account_id": str(acc_id),
        "track_as_cash": "1",
        "create_invoice": "0",
        "has_bill": "0",
    })
    assert not _danger(rv), _flashes(rv)

    sales = _sale_by_client(app, "RT-CL-D")
    assert len(sales) == 1
    assert sales[0][1] == "Cash"
    assert abs(sales[0][2] - 6500) < 0.01
    assert abs(sales[0][3] - 6500) < 0.01
    assert abs(_stock(app, "RT-D Cement") - 495) < 0.01
    # Fully paid cash sale leaves no outstanding due.
    assert abs(_pending_total(app, "RT-CL-D")) < 0.01
    assert abs(_client_ledger_balance(app, "RT-CL-D")) < 0.5

    from models import Account
    with app.app_context():
        acc = Account.query.get(acc_id)
        # 1000000 opening + booking advances (10000+10000) + payment 5000 + cash 6500
        assert float(acc.balance or 0) > 1_000_000.0


def test_booked_return_over_return_is_rejected(app, client, base):
    """Returning more booked material than was delivered must be refused."""
    acc_id = base["acc_id"]

    rv = _post(client, "/add_booking", {
        "client_code": "RT-CL-E",
        "material_name[]": ["RT-E Cement"],
        "qty[]": ["20"],
        "unit_rate[]": ["1200"],
        "amount": "24000",
        "paid_amount": "10000",
        "payment_method": "Cash",
        "payment_account_id": str(acc_id),
    })
    assert not _danger(rv), _flashes(rv)

    rv = _post(client, "/add_direct_sale", {
        "client_code": "RT-CL-E",
        "category": "Booking Delivery",
        "driver_name": "RT Driver",
        "product_name[]": ["RT-E Cement"],
        "qty[]": ["8"],
        "unit_rate[]": ["1200"],
        "amount": "9600",
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
    })
    assert not _danger(rv), _flashes(rv)

    # Only 8 bags were delivered as booked; returning 10 must be refused.
    rv = _post(client, "/add_material_return", {
        "client_code": "RT-CL-E",
        "return_type": "booked",
        "material_name[]": ["RT-E Cement"],
        "qty[]": ["10"],
        "unit_rate[]": [""],
        "rent_rate[]": ["200"],
    })
    assert _danger(rv), _flashes(rv)

    from models import MaterialReturn, Client
    from sqlalchemy import func
    with app.app_context():
        cli = Client.query.filter_by(code="RT-CL-E").first()
        count = MaterialReturn.query.filter(
            func.lower(func.trim(MaterialReturn.client_name)) == cli.name.strip().lower(),
            MaterialReturn.is_void == False,
        ).count()
        assert count == 0
    assert abs(_booked_returnable(app, "RT-CL-E", "RT-E Cement") - 8) < 0.01


def test_scoped_snapshot_matches_full_ledger(app, client, base):
    """The per-client financial summary must equal the full-snapshot ledger."""
    from models import Client
    from app.services.financial_ledgers import (
        build_client_financial_ledger,
        _client_snapshot_for,
        _client_snapshot,
    )

    def _rows_sig(ledger):
        return [
            (r["type"], r["reference"], round(float(r["debit"]), 2), round(float(r["credit"]), 2))
            for r in ledger["rows"]
        ]

    with app.app_context():
        for code in ["RT-CL-A", "RT-CL-B", "RT-CL-C", "RT-CL-D", "RT-CL-E"]:
            cli = Client.query.filter_by(code=code).first()
            full = build_client_financial_ledger(cli, snapshot=_client_snapshot())
            scoped = build_client_financial_ledger(cli, snapshot=_client_snapshot_for(cli))
            assert abs(float(full["closing_balance"]) - float(scoped["closing_balance"])) < 0.01, code
            assert abs(float(full["total_debit"]) - float(scoped["total_debit"])) < 0.01, code
            assert abs(float(full["total_credit"]) - float(scoped["total_credit"])) < 0.01, code
            assert _rows_sig(full) == _rows_sig(scoped), code


def test_duplicate_sale_submit_is_idempotent(app, client, base):
    """A re-submitted idempotency key must not create a second sale."""
    from models import (
        db, Material, Client, MaterialCategory, GRN, GRNItem, Entry, DirectSale,
    )
    acc_id = base["acc_id"]

    with app.app_context():
        cat = MaterialCategory.query.filter_by(name="General").first()
        mat = Material(code="RT-M-F", name="RT-F Cement", unit_price=1500,
                       total=0, category_id=cat.id, is_active=True)
        cli = Client(code="RT-CL-F", name="Roundtrip Idem Client", is_active=True, opening_balance=0)
        db.session.add_all([mat, cli])
        g = GRN(supplier="RT Supplier", auto_bill_no="RT-GRN-F", date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(g)
        db.session.flush()
        db.session.add(GRNItem(grn_id=g.id, mat_name="RT-F Cement", qty=100, price_at_time=1100, is_void=False))
        mat.total = 100
        db.session.add(Entry(date="2026-01-01", time="08:00:00", type="IN", material="RT-F Cement",
                             client="RT Supplier", qty=100, bill_no="", auto_bill_no="RT-GRN-F",
                             created_by="test", is_void=False))
        db.session.commit()

    payload = {
        "client_code": "RT-CL-F",
        "category": "Credit Customer",
        "driver_name": "RT Driver",
        "product_name[]": ["RT-F Cement"],
        "qty[]": ["5"],
        "unit_rate[]": ["1500"],
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "RT-IDEM-1",
        "idempotency_key": "IDEM-TEST-KEY-1",
    }
    rv1 = _post(client, "/add_direct_sale", dict(payload))
    assert not _danger(rv1), _flashes(rv1)
    rv2 = _post(client, "/add_direct_sale", dict(payload))
    assert not _danger(rv2), _flashes(rv2)

    with app.app_context():
        sales = DirectSale.query.filter_by(client_code="RT-CL-F", is_void=False).all()
        assert len(sales) == 1, f"expected 1 sale, got {len(sales)}"
        assert abs(float(sales[0].amount or 0) - 7500) < 0.01
    assert abs(_stock(app, "RT-F Cement") - 95) < 0.01


def test_ledger_integrity_after_roundtrips(app, client, base):
    """Whole-sales-section integrity audit must come back clean."""
    from app.services.financial_ledgers import financial_integrity_audit
    with app.app_context():
        report = financial_integrity_audit()
    assert report["ok"], report["issues"]
