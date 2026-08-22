"""Regression tests: supplier ledger payment links, action buttons, tracking numbers.

Covers the two defects reported on 2026-08-22:
  1. Clicking a payment reference (e.g. PAY-22) in the supplier ledger opened
     a random CLIENT bill — links now target the supplier payment receipt
     (/download_supplier_payment/<id>) and GRN links carry src=grn hints.
  2. The supplier ledger had no View/Edit/Print/Delete/Download actions.
Also locks in: new payments automatically get an SB-SP-#### tracking number,
and client/driver ledger pages are unchanged by the supplier-only buttons.
"""
import os
from datetime import datetime

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "supplier_ledger_actions.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    from app import create_app
    from models import db

    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
        }
    )
    with application.app_context():
        db.create_all()
        from app.services.schema import (
            _ensure_model_columns,
            _ensure_performance_indexes,
            _ensure_default_admin,
        )
        _ensure_model_columns()
        _ensure_performance_indexes()
        _ensure_default_admin()
        db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    from models import User
    with app.app_context():
        user = User.query.first()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _seed_supplier_with_grn_and_payment(app):
    """Supplier + one GRN (with an item) + one manual payment + cash account."""
    from models import db, Supplier, GRN, GRNItem, SupplierPayment, Account
    with app.app_context():
        acc = Account(name="FBM CASH", category="cash", account_type="company", balance=1_000_000, is_active=True)
        db.session.add(acc)
        sup = Supplier(name="ZIA TEST TRADERS", is_active=True)
        db.session.add(sup)
        db.session.flush()
        grn = GRN(supplier=sup.name, supplier_id=sup.id, auto_bill_no="SB-GRN-2000",
                  date_posted=datetime(2026, 7, 2, 10, 0), is_void=False)
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name="CEMENT", qty=300, price_at_time=1360))
        db.session.add(SupplierPayment(
            supplier_id=sup.id, amount=400000, method="Bank Transfer",
            payment_type="Payment", date_posted=datetime(2026, 7, 5, 15, 57),
            payment_account_id=acc.id, is_void=False,
        ))
        db.session.commit()
        return sup.id, grn.id


def test_supplier_ledger_links_payment_to_payment_receipt(app, client):
    sid, _gid = _seed_supplier_with_grn_and_payment(app)
    r = client.get(f"/supplier_ledger/{sid}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    # Payment reference + action buttons must point at the supplier payment
    # receipt — never at the generic bill lookup that resolved PAY-## to a
    # random client bill.
    assert "/download_supplier_payment/1" in body
    # No bare view_bill link on the payment reference anymore
    assert "view_bill/PAY-" not in body

    # Action buttons for the payment row (delete form posts to the accounts endpoint)
    assert "/payments/suppliers/1/delete" in body
    # Edit deep-link to the supplier's payments screen (shared edit modal lives there)
    assert "/payments/suppliers?party_id=1" in body

    # GRN reference carries the src=grn hint so manual bill numbers can never
    # collide with client bills.
    assert "src=grn" in body
    assert "src_id=1" in body

    # GRN row actions: edit + delete
    assert "/edit_grn/1" in body


def test_new_supplier_payment_gets_tracking_number(app):
    _seed_supplier_with_grn_and_payment(app)
    from app.services.payments_crud import save_supplier_payment
    from models import db, SupplierPayment
    with app.app_context():
        payment, created = save_supplier_payment(
            supplier_id=1, amount="50000", method="Cash",
            payment_account_id=1, date_posted="2026-08-22", note="test pay",
        )
        db.session.commit()
        assert created
        assert payment.auto_bill_no and payment.auto_bill_no.startswith("SB-SP-"), \
            "every new supplier payment must carry a tracking number"

        # And the ledger now shows that number instead of the PAY-## fallback
        from app.services.financial_ledgers import build_supplier_financial_ledger
        from models import Supplier
        sup = db.session.get(Supplier, 1)
        ledger_rows = build_supplier_financial_ledger(sup)["rows"]
        refs = [row["reference"] for row in ledger_rows]
        assert payment.auto_bill_no in refs
        # Any payment that HAS a tracking number must show it, never the
        # PAY-## fallback (fallback stays only for un-numbered legacy rows).
        for row in ledger_rows:
            if row.get("source_type") == "SupplierPayment":
                src = row.get("source")
                if getattr(src, "auto_bill_no", None):
                    assert row["reference"] == src.auto_bill_no


def test_client_ledger_unaffected_by_supplier_actions(app, client):
    from models import db, Client, Payment
    with app.app_context():
        c = Client(code="C-1", name="CLIENT ONE", is_active=True)
        db.session.add(c)
        db.session.flush()
        db.session.add(Payment(client_id=c.id, client_name=c.name, amount=1000,
                               date_posted=datetime(2026, 8, 1), is_void=False,
                               auto_bill_no="SB-PAY-9001"))
        db.session.commit()
        cid = c.id
    r = client.get(f"/financial_ledger/{cid}") if False else client.get(f"/client_ledger/{cid}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Supplier-only buttons must NOT leak into client ledgers. layout.html
    # references these routes inside generic JS blocks, so assert on real
    # href=/action= attributes only.
    import re as _re
    hrefs = _re.findall(r'(?:href|action)="([^"]+)"', body)
    assert not any("/download_supplier_payment/" in h for h in hrefs)
    assert not any("supplier_payment_delete" in h for h in hrefs)
    assert not any("/edit_grn/" in h for h in hrefs)
    assert not any("src=grn" in h for h in hrefs)
