"""
Booked-sale edit regression + smoke tests.

These tests reproduce and guard against the bug where editing an existing
Booked Sale (or a Booked+Due sale) reconstructed every line as a single
chargeable item, losing the ``is_booking`` flag, dropping all booking
allocations, and -- once the frontend prefills a non-zero reserved rate --
failing with "Booked Sale can only contain reserved items (rate 0)."
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
    from models import (
        db, Material, Client, MaterialCategory, GRN, GRNItem, Account, Entry, Settings,
    )
    with app.app_context():
        cat = MaterialCategory.query.filter_by(name="General").first()
        if not cat:
            cat = MaterialCategory(name="General")
            db.session.add(cat)
            db.session.flush()

        mats = {}
        for key, name in [
            ("a", "BKEDIT-A"), ("b", "BKEDIT-B"),
            ("c", "BKEDIT-C"), ("d", "BKEDIT-D"),
            ("alt", "BKEDIT-ALT"),
        ]:
            m = Material(code=f"BK-M-{key.upper()}", name=name, unit_price=1500,
                         total=1000, category_id=cat.id, is_active=True)
            db.session.add(m)
            mats[key] = m

        clients = {}
        for key, code, name in [
            ("booked", "BK-CL-B", "BKEDIT Booked Client"),
            ("mixed",  "BK-CL-M", "BKEDIT Mixed Client"),
            ("credit", "BK-CL-C", "BKEDIT Credit Client"),
        ]:
            c = Client(code=code, name=name, is_active=True, opening_balance=0)
            db.session.add(c)
            clients[key] = c

        acc = Account(name="BK CASH", category="cash", account_type="cash",
                      balance=10_000_000, is_active=True)
        db.session.add(acc)
        db.session.flush()
        acc_id = acc.id

        g = GRN(supplier="BK Supplier", auto_bill_no="BK-GRN",
                date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(g); db.session.flush()
        for m in mats.values():
            db.session.add(GRNItem(grn_id=g.id, mat_name=m.name, qty=1000,
                                   price_at_time=1100, is_void=False))
            db.session.add(Entry(
                date="2026-01-01", time="08:00:00", type="IN", material=m.name,
                client="BK Supplier", qty=1000, bill_no="", auto_bill_no="BK-GRN",
                created_by="test", is_void=False,
            ))

        s = Settings.query.first() or Settings()
        s.allow_global_negative_stock = False
        db.session.add(s)
        db.session.commit()
        return {
            "materials": {k: v.name for k, v in mats.items()},
            "material_ids": {k: v.id for k, v in mats.items()},
            "clients": {k: v.code for k, v in clients.items()},
            "acc_id": acc_id,
        }


def _post(client, path, data):
    return client.post(path, data=data, follow_redirects=True)


def _sale_state(app, sale_id):
    from models import DirectSale, DirectSaleItem, BookingAllocation, Entry
    with app.app_context():
        sale = DirectSale.query.get(sale_id)
        return {
            "category": sale.category,
            "amount": float(sale.amount or 0),
            "paid": float(sale.paid_amount or 0),
            "items": [(it.product_name, float(it.qty or 0), float(it.price_at_time or 0))
                      for it in sale.items],
            "allocs": sorted([(a.booking_item_id, float(a.qty or 0))
                              for a in BookingAllocation.query.filter_by(sale_id=sale_id, is_void=False)]),
            "entries": sorted([(e.material, float(e.qty or 0), e.client_category,
                                e.booked_material)
                               for e in Entry.query.filter_by(source_module="sales",
                                                              source_id=sale_id,
                                                              is_void=False)]),
        }


def _make_booking(client, base, client_key, mat_key, qty, rate=1200):
    return _post(client, "/add_booking", {
        "client_code": base["clients"][client_key],
        "material_name[]": [base["materials"][mat_key]],
        "qty[]": [str(qty)],
        "unit_rate[]": [str(rate)],
        "amount": str(qty * rate),
        "paid_amount": str(qty * rate),
        "payment_method": "Cash",
        "payment_account_id": str(base["acc_id"]),
    })


def _latest_sale_id(app, client_code):
    from models import DirectSale
    with app.app_context():
        s = DirectSale.query.filter_by(client_code=client_code, is_void=False)\
                            .order_by(DirectSale.id.desc()).first()
        return s.id if s else None


# ---------------------------------------------------------------------------
# TEST 1: Open existing booked sale — existing items are NOT non-booked
# ---------------------------------------------------------------------------
def test_edit_modal_prefills_booked_identity(app, client, base):
    assert not _danger(_make_booking(client, base, "booked", "a", 100)), "booking failed"
    rv = _post(client, "/add_direct_sale", {
        "client_code": base["clients"]["booked"],
        "category": "Booking Delivery",
        "driver_name": "BK Driver",
        "product_name[]": [base["materials"]["a"]],
        "qty[]": ["20"],
        "unit_rate[]": ["1200"],
        "amount": "0",
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "BKEDIT-BOOKED-1",
    })
    assert not _danger(rv), _flashes(rv)
    sale_id = _latest_sale_id(app, base["clients"]["booked"])
    assert sale_id

    rv = client.get(f"/direct_sales/{sale_id}/edit-modal")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    # The material row is present, category is Booking Delivery, and
    # rate=0 is pre-filled (NOT the 1200 reserved rate which would cause
    # the server to see it as a chargeable item).
    assert 'value="Booking Delivery" selected' in body
    assert base["materials"]["a"] in body
    # The booked line's rate input renders with value="0" (or 0.0); verify that
    # it does NOT carry the reserved unit price (1200) which the UI would otherwise
    # re-submit and cause the "rate 0" validation to trip.
    rate_inputs = re.findall(r'<input[^>]*name="unit_rate\[\]"[^>]*>', body)
    assert rate_inputs, "no unit_rate inputs rendered"
    assert not any('value="1200"' in x for x in rate_inputs), \
        "booked line must not prefill the reserved rate 1200"
    assert any(re.search(r'value="0(\.0+)?"', x) for x in rate_inputs), \
        "booked line must prefill rate 0"


# ---------------------------------------------------------------------------
# TEST 2: Save without changes — allocations / entries / stock all preserved
# ---------------------------------------------------------------------------
def test_booked_sale_save_without_changes(app, client, base):
    sale_id = _latest_sale_id(app, base["clients"]["booked"])
    before = _sale_state(app, sale_id)
    assert before["category"] == "Booking Delivery"
    assert len(before["allocs"]) == 1, before
    assert abs(before["allocs"][0][1] - 20) < 0.001

    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Booking Delivery",
        "client_code": base["clients"]["booked"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-BOOKED-1",
        "product_name[]": [base["materials"]["a"]],
        "alternate_material[]": [""],
        "grn_item_id[]": [""],
        "qty[]": ["20"],
        "unit_rate[]": ["0"],
        "amount": "0",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert not _danger(rv), _flashes(rv)
    after = _sale_state(app, sale_id)
    assert after == before, (before, after)


# ---------------------------------------------------------------------------
# TEST 3: Edit quantity within remaining reservation
# ---------------------------------------------------------------------------
def test_edit_booked_quantity(app, client, base):
    sale_id = _latest_sale_id(app, base["clients"]["booked"])
    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Booking Delivery",
        "client_code": base["clients"]["booked"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-BOOKED-1",
        "product_name[]": [base["materials"]["a"]],
        "qty[]": ["30"],
        "unit_rate[]": ["0"],
        "amount": "0",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert not _danger(rv), _flashes(rv)
    after = _sale_state(app, sale_id)
    assert after["items"][0][1] == 30
    assert len(after["allocs"]) == 1
    assert abs(after["allocs"][0][1] - 30) < 0.001
    assert len(after["entries"]) == 1 and abs(after["entries"][0][1] - 30) < 0.001
    # No duplicate allocation: exactly one.
    assert len(after["allocs"]) == 1


# ---------------------------------------------------------------------------
# TEST 4: Add another reserved item
# ---------------------------------------------------------------------------
def test_add_reserved_item(app, client, base):
    # second booking for material B on same client
    assert not _danger(_make_booking(client, base, "booked", "b", 50))
    sale_id = _latest_sale_id(app, base["clients"]["booked"])
    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Booking Delivery",
        "client_code": base["clients"]["booked"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-BOOKED-1",
        "product_name[]": [base["materials"]["a"], base["materials"]["b"]],
        "qty[]": ["30", "10"],
        "unit_rate[]": ["0", "0"],
        "amount": "0",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert not _danger(rv), _flashes(rv)
    after = _sale_state(app, sale_id)
    assert sorted([i[0] for i in after["items"]]) == sorted(
        [base["materials"]["a"], base["materials"]["b"]])
    assert len(after["allocs"]) == 2
    qtys = sorted([a[1] for a in after["allocs"]])
    assert qtys == [10, 30]


# ---------------------------------------------------------------------------
# TEST 5: Remove a booked item — allocation is released, no orphans
# ---------------------------------------------------------------------------
def test_remove_booked_item(app, client, base):
    sale_id = _latest_sale_id(app, base["clients"]["booked"])
    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Booking Delivery",
        "client_code": base["clients"]["booked"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-BOOKED-1",
        "product_name[]": [base["materials"]["b"]],
        "qty[]": ["10"],
        "unit_rate[]": ["0"],
        "amount": "0",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert not _danger(rv), _flashes(rv)
    after = _sale_state(app, sale_id)
    assert [i[0] for i in after["items"]] == [base["materials"]["b"]]
    # only the B allocation remains — A was released (deleted).
    assert len(after["allocs"]) == 1


# ---------------------------------------------------------------------------
# TEST 6: Alternate-material booked sale
# ---------------------------------------------------------------------------
def test_alternate_material_booked_sale(app, client, base):
    # Booking on C, delivered as ALT.
    assert not _danger(_make_booking(client, base, "booked", "c", 40))
    rv = _post(client, "/add_direct_sale", {
        "client_code": base["clients"]["booked"],
        "category": "Booking Delivery",
        "driver_name": "BK Driver",
        "product_name[]": [base["materials"]["c"]],
        "alternate_material[]": [base["materials"]["alt"]],
        "qty[]": ["15"],
        "unit_rate[]": ["0"],
        "amount": "0",
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "BKEDIT-ALT-1",
    })
    assert not _danger(rv), _flashes(rv)
    alt_sale = _latest_sale_id(app, base["clients"]["booked"])
    state = _sale_state(app, alt_sale)
    # delivered product_name is ALT; entry.booked_material is C.
    assert state["items"][0][0] == base["materials"]["alt"]
    assert state["entries"][0][0] == base["materials"]["alt"]
    assert state["entries"][0][3] == base["materials"]["c"]
    assert len(state["allocs"]) == 1

    # The edit modal must prefill the alternate field.
    rv = client.get(f"/direct_sales/{alt_sale}/edit-modal")
    assert rv.status_code == 200
    assert base["materials"]["alt"] in rv.get_data(as_text=True)

    # Re-save unchanged.
    rv = _post(client, f"/edit_bill/DirectSale/{alt_sale}", {
        "category": "Booking Delivery",
        "client_code": base["clients"]["booked"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-ALT-1",
        "product_name[]": [base["materials"]["c"]],
        "alternate_material[]": [base["materials"]["alt"]],
        "qty[]": ["15"],
        "unit_rate[]": ["0"],
        "amount": "0",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert not _danger(rv), _flashes(rv)
    after = _sale_state(app, alt_sale)
    assert after == state, (state, after)


# ---------------------------------------------------------------------------
# TEST 7: Genuinely non-booked chargeable item in Booked Sale is rejected
# ---------------------------------------------------------------------------
def test_chargeable_item_rejected_in_booked_sale(app, client, base):
    # Operate on a fresh sale with a unique bill number so the test is not
    # cross-coupled to prior tests in the module (each sale must have a unique
    # manual bill_no).  Reuse material B which is already booked for this
    # client, then attempt to add a genuinely unbooked material (C was used by
    # the alternate-material test but its booking balance is fully consumed).
    import uuid
    unique_bill = f"BKEDIT-REJ-{uuid.uuid4().hex[:8]}"
    rv = _post(client, "/add_direct_sale", {
        "client_code": base["clients"]["booked"],
        "category": "Booking Delivery",
        "driver_name": "BK Driver",
        "product_name[]": [base["materials"]["b"]],
        "qty[]": ["10"],
        "unit_rate[]": ["0"],
        "amount": "0",
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": unique_bill,
    })
    assert not _danger(rv), _flashes(rv)
    sale_id = _latest_sale_id(app, base["clients"]["booked"])
    before = _sale_state(app, sale_id)
    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Booking Delivery",
        "client_code": base["clients"]["booked"],
        "driver_name": "BK Driver",
        "manual_bill_no": unique_bill,
        # BKEDIT-ALT has no booking for this client — genuinely non-booked.
        "product_name[]": [base["materials"]["b"], base["materials"]["alt"]],
        "qty[]": ["10", "5"],
        "unit_rate[]": ["0", "1500"],
        "amount": "7500",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert _danger(rv), "chargeable item must be rejected in Booking Delivery"
    msgs = " ".join(_danger(rv))
    assert "reserved items" in msgs.lower() or "rate 0" in msgs.lower()
    after = _sale_state(app, sale_id)
    # Atomicity: nothing changed.
    assert after == before, "failed validation must not mutate state"


# ---------------------------------------------------------------------------
# TEST 8: Booked + Due (Mixed Transaction) edit preserves both slices
# ---------------------------------------------------------------------------
def test_mixed_transaction_edit(app, client, base):
    assert not _danger(_make_booking(client, base, "mixed", "a", 20))
    rv = _post(client, "/add_direct_sale", {
        "client_code": base["clients"]["mixed"],
        "category": "Mixed Transaction",
        "driver_name": "BK Driver",
        "product_name[]": [base["materials"]["a"]],
        "qty[]": ["25"],
        "unit_rate[]": ["1500"],
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "BKEDIT-MIX-1",
    })
    assert not _danger(rv), _flashes(rv)
    sale_id = _latest_sale_id(app, base["clients"]["mixed"])
    state = _sale_state(app, sale_id)
    # 20 booked + 5 chargeable @ 1500 = 7500 amount.
    assert abs(state["amount"] - 7500) < 0.01
    cats = sorted(e[2] for e in state["entries"])
    assert cats == ["Booking Delivery", "Credit Customer"], state
    assert len(state["allocs"]) == 1 and abs(state["allocs"][0][1] - 20) < 0.001

    # Re-save unchanged through edit.
    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Mixed Transaction",
        "client_code": base["clients"]["mixed"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-MIX-1",
        "product_name[]": [base["materials"]["a"]],
        "qty[]": ["25"],
        "unit_rate[]": ["1500"],
        "amount": "7500",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert not _danger(rv), _flashes(rv)
    after = _sale_state(app, sale_id)
    assert after["category"] == "Mixed Transaction"
    assert abs(after["amount"] - 7500) < 0.01
    cats = sorted(e[2] for e in after["entries"])
    assert cats == ["Booking Delivery", "Credit Customer"]
    assert len(after["allocs"]) == 1 and abs(after["allocs"][0][1] - 20) < 0.001


# ---------------------------------------------------------------------------
# TEST 11: Atomicity on validation failure — a credit-sale edit that fails
# must not leave any partial state.
# ---------------------------------------------------------------------------
def test_failed_edit_is_atomic(app, client, base):
    # Create a due sale first.
    rv = _post(client, "/add_direct_sale", {
        "client_code": base["clients"]["credit"],
        "category": "Credit Customer",
        "driver_name": "BK Driver",
        "product_name[]": [base["materials"]["d"]],
        "qty[]": ["3"],
        "unit_rate[]": ["1500"],
        "paid_amount": "0",
        "has_bill": "1",
        "create_invoice": "0",
        "manual_bill_no": "BKEDIT-CR-1",
    })
    assert not _danger(rv), _flashes(rv)
    sale_id = _latest_sale_id(app, base["clients"]["credit"])
    before = _sale_state(app, sale_id)

    # Try editing with a duplicate bill_no that conflicts → fail.
    rv = _post(client, f"/edit_bill/DirectSale/{sale_id}", {
        "category": "Credit Customer",
        "client_code": base["clients"]["credit"],
        "driver_name": "BK Driver",
        "manual_bill_no": "BKEDIT-MIX-1",  # conflicts with the mixed sale
        "product_name[]": [base["materials"]["d"]],
        "qty[]": ["3"],
        "unit_rate[]": ["1500"],
        "amount": "4500",
        "paid_amount": "0",
        "discount": "0",
        "delivery_rent": "0",
        "has_bill": "1",
    })
    assert _danger(rv), "duplicate bill_no must be rejected"
    after = _sale_state(app, sale_id)
    assert after == before, "failed edit must roll back all changes"
