"""Tests for ensuring notes in Sales, Bookings, Payments, Pending Bills, etc.
are displayed in PDFs, History PDFs, Client Ledger Print PDFs, and relevant views.
"""
import io
import re
import pytest
from pypdf import PdfReader

from models.catalog import Material
from models.core import User
from models.parties import Client
from models.cash import Account
from models.sales import Booking, Payment, DirectSale, PendingBill, Invoice
from models.stock import Entry
from app import db


def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def csrf_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


@pytest.fixture()
def setup_data(app, client):
    login(client)
    tok = csrf_token(client)

    # Add material
    client.post("/add_material", data={
        "material_name": "NOTE TEST CEMENT",
        "material_unit": "Bags",
        "_csrf_token": tok,
    }, follow_redirects=True)

    # Add client
    client.post("/add_client", data={
        "name": "NOTE TEST CLIENT",
        "code": "NTC-001",
        "category": "General",
        "opening_balance": "0",
        "_csrf_token": tok,
    }, follow_redirects=True)

    # Add account
    client.post("/accounts/accounts/add", data={
        "name": "NOTE CASH ACCOUNT",
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": "0",
        "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
        "_csrf_token": tok,
    }, follow_redirects=True)

    with app.app_context():
        mat = Material.query.filter_by(name="NOTE TEST CEMENT").first()
        cli = Client.query.filter_by(code="NTC-001").first()
        acc = Account.query.filter(Account.name.like("%NOTE CASH%")).first()

        # Stock in
        db.session.add(Entry(
            date="2026-01-01", time="09:00:00", type="IN",
            material=mat.name, qty=1000,
            bill_no="MB NO.9001", client_category="Stock In",
        ))
        from app.services.void_rebuild import _rebuild_material_totals
        _rebuild_material_totals()
        db.session.commit()
        return {
            "client_id": cli.id,
            "material_id": mat.id,
            "account_id": acc.id,
            "client_code": cli.code,
            "client_name": cli.name,
            "material_name": mat.name,
        }


def test_sales_note_shows_in_invoice_pdf_and_view(app, client, setup_data):
    tok = csrf_token(client)

    # Create direct sale with a note
    r = client.post("/add_direct_sale", data={
        "client_code": setup_data["client_code"],
        "client_name": setup_data["client_name"],
        "category": "Credit Customer",
        "manual_bill_no": "MB NO.9101",
        "driver_name": "TEST DRIVER",
        "product_name[]": setup_data["material_name"],
        "material_id[]": str(setup_data["material_id"]),
        "qty[]": "20",
        "unit_rate[]": "1500",
        "paid_amount": "0",
        "note": "SPECIAL SALES NOTE 9988",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        sale = DirectSale.query.filter_by(manual_bill_no="MB NO.9101").first()
        assert sale is not None
        assert sale.note == "SPECIAL SALES NOTE 9988"
        sale_id = sale.id
        bill_ref = sale.manual_bill_no

    # View bill HTML
    resp_html = client.get(f"/view_bill/{bill_ref}?src=direct_sale&src_id={sale_id}")
    assert resp_html.status_code == 200
    html_text = resp_html.get_data(as_text=True)
    assert "SPECIAL SALES NOTE 9988" in html_text

    # Download Invoice PDF
    resp_pdf = client.get(f"/download_invoice/{bill_ref}?src=direct_sale&src_id={sale_id}")
    assert resp_pdf.status_code == 200
    assert resp_pdf.data[:5] == b"%PDF-"
    pdf_text = _pdf_text(resp_pdf.data)
    assert "SPECIAL SALES NOTE 9988" in pdf_text


def test_sales_note_shows_in_client_ledger_print_and_full_history(app, client, setup_data):
    tok = csrf_token(client)

    # Create direct sale with a note
    r = client.post("/add_direct_sale", data={
        "client_code": setup_data["client_code"],
        "client_name": setup_data["client_name"],
        "category": "Credit Customer",
        "manual_bill_no": "MB NO.9102",
        "driver_name": "TEST DRIVER",
        "product_name[]": setup_data["material_name"],
        "material_id[]": str(setup_data["material_id"]),
        "qty[]": "10",
        "unit_rate[]": "1500",
        "paid_amount": "0",
        "note": "DELIVERY SITE NOTE 4455",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    # 1. Download Client Ledger PDF
    resp_ledger_pdf = client.get(f"/download_client_ledger/{setup_data['client_id']}")
    assert resp_ledger_pdf.status_code == 200
    assert resp_ledger_pdf.data[:5] == b"%PDF-"
    ledger_pdf_text = " ".join(_pdf_text(resp_ledger_pdf.data).split())
    assert "DELIVERY SITE NOTE 4455" in ledger_pdf_text

    # 2. Download Full Client History PDF
    resp_history_pdf = client.get(f"/download_full_client_history/{setup_data['client_id']}")
    assert resp_history_pdf.status_code == 200
    assert resp_history_pdf.data[:5] == b"%PDF-"
    history_pdf_text = " ".join(_pdf_text(resp_history_pdf.data).split())
    assert "DELIVERY SITE NOTE 4455" in history_pdf_text


def test_edit_sales_note_updates_invoice_and_ledgers(app, client, setup_data):
    tok = csrf_token(client)

    # Create direct sale
    client.post("/add_direct_sale", data={
        "client_code": setup_data["client_code"],
        "client_name": setup_data["client_name"],
        "category": "Credit Customer",
        "manual_bill_no": "MB NO.9103",
        "driver_name": "TEST DRIVER",
        "product_name[]": setup_data["material_name"],
        "material_id[]": str(setup_data["material_id"]),
        "qty[]": "15",
        "unit_rate[]": "1500",
        "paid_amount": "0",
        "note": "ORIGINAL NOTE 1111",
        "_csrf_token": tok,
    }, follow_redirects=True)

    with app.app_context():
        sale = DirectSale.query.filter_by(manual_bill_no="MB NO.9103").first()
        sale_id = sale.id

    # Edit direct sale note
    r_edit = client.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "client_code": setup_data["client_code"],
        "client_name": setup_data["client_name"],
        "category": "Credit Customer",
        "manual_bill_no": "MB NO.9103",
        "driver_name": "TEST DRIVER",
        "product_name[]": setup_data["material_name"],
        "material_id[]": str(setup_data["material_id"]),
        "qty[]": "15",
        "unit_rate[]": "1500",
        "paid_amount": "0",
        "note": "UPDATED SALES NOTE 7766",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r_edit.status_code == 200

    with app.app_context():
        sale = db.session.get(DirectSale, sale_id)
        assert sale.note == "UPDATED SALES NOTE 7766"

    # Verify Invoice PDF shows updated note
    resp_pdf = client.get(f"/download_invoice/MB NO.9103?src=direct_sale&src_id={sale_id}")
    assert resp_pdf.status_code == 200
    pdf_text = " ".join(_pdf_text(resp_pdf.data).split())
    assert "UPDATED SALES NOTE 7766" in pdf_text
    assert "ORIGINAL NOTE 1111" not in pdf_text

    # Verify Client Ledger PDF shows updated note
    resp_ledger_pdf = client.get(f"/download_client_ledger/{setup_data['client_id']}")
    ledger_pdf_text = " ".join(_pdf_text(resp_ledger_pdf.data).split())
    assert "UPDATED SALES NOTE 7766" in ledger_pdf_text

    # Verify Full History PDF shows updated note
    resp_history_pdf = client.get(f"/download_full_client_history/{setup_data['client_id']}")
    history_pdf_text = " ".join(_pdf_text(resp_history_pdf.data).split())
    assert "UPDATED SALES NOTE 7766" in history_pdf_text


def test_booking_and_payment_notes_in_pdf_and_history(app, client, setup_data):
    tok = csrf_token(client)

    # Booking with note
    r_bk = client.post("/add_booking", data={
        "client_code": setup_data["client_code"],
        "material_name[]": setup_data["material_name"],
        "material_id[]": str(setup_data["material_id"]),
        "qty[]": "100",
        "unit_rate[]": "1200",
        "amount": "120000",
        "paid_amount": "50000",
        "manual_bill_no": "MB NO.8001",
        "payment_account_id": str(setup_data["account_id"]),
        "payment_method": "Cash",
        "note": "SPECIAL BOOKING NOTE 5544",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r_bk.status_code == 200

    # Payment with note
    r_pay = client.post("/add_payment", data={
        "client_code": setup_data["client_code"],
        "amount": "25000",
        "payment_type": "Receipt",
        "method": "Cash",
        "payment_account_id": str(setup_data["account_id"]),
        "manual_bill_no": "MB NO.8002",
        "note": "SPECIAL PAYMENT NOTE 3322",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r_pay.status_code == 200

    # Booking invoice PDF
    with app.app_context():
        bk = Booking.query.filter_by(manual_bill_no="MB NO.8001").first()
        pay = Payment.query.filter_by(manual_bill_no="MB NO.8002").first()

    bk_pdf = client.get(f"/download_invoice/MB NO.8001?src=booking&src_id={bk.id}")
    assert "SPECIAL BOOKING NOTE 5544" in _pdf_text(bk_pdf.data)

    # Payment receipt PDF
    pay_pdf = client.get(f"/download_invoice/MB NO.8002?src=payment&src_id={pay.id}")
    assert "SPECIAL PAYMENT NOTE 3322" in _pdf_text(pay_pdf.data)

    # Full history PDF
    hist_pdf = client.get(f"/download_full_client_history/{setup_data['client_id']}")
    hist_text = _pdf_text(hist_pdf.data)
    assert "SPECIAL BOOKING NOTE 5544" in hist_text
    assert "SPECIAL PAYMENT NOTE 3322" in hist_text


def test_pending_bill_notes_everywhere(app, client, setup_data):
    tok = csrf_token(client)

    # Add pending bill with note
    r = client.post("/add_pending_bill", data={
        "client_code": setup_data["client_code"],
        "bill_no": "MB NO.9501",
        "amount": "8500",
        "reason": "Pending Delivery Cement",
        "note": "PENDING NOTE SPECIAL 1234",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    # 1. Pending bills HTML list
    resp_list = client.get("/pending_bills")
    assert resp_list.status_code == 200
    assert "PENDING NOTE SPECIAL 1234" in resp_list.get_data(as_text=True)

    with app.app_context():
        pb = PendingBill.query.filter_by(bill_no="MB NO.9501").first()
        pb_id = pb.id

    # 2. Pending bill modal
    resp_modal = client.get(f"/pending_bills/{pb_id}/modals")
    assert resp_modal.status_code == 200
    assert "PENDING NOTE SPECIAL 1234" in resp_modal.get_data(as_text=True)

    # 3. Invoice PDF for pending bill
    resp_pdf = client.get(f"/download_invoice/MB NO.9501?src=pending_bill&src_id={pb_id}")
    assert resp_pdf.status_code == 200
    assert "PENDING NOTE SPECIAL 1234" in " ".join(_pdf_text(resp_pdf.data).split())

    # 4. Client Ledger Print PDF
    resp_ledger_pdf = client.get(f"/download_client_ledger/{setup_data['client_id']}")
    assert "PENDING NOTE SPECIAL 1234" in " ".join(_pdf_text(resp_ledger_pdf.data).split())

    # 5. Full Client History PDF
    resp_hist_pdf = client.get(f"/download_full_client_history/{setup_data['client_id']}")
    assert "PENDING NOTE SPECIAL 1234" in " ".join(_pdf_text(resp_hist_pdf.data).split())


def test_export_data_includes_notes(app, client, setup_data):
    tok = csrf_token(client)

    with app.app_context():
        db.session.add(Entry(
            date="2026-01-12", time="10:00:00", type="OUT",
            client=setup_data["client_name"], client_code=setup_data["client_code"],
            material=setup_data["material_name"], qty=10,
            bill_no="MB NO.9601", note="DISPATCH NOTE 55",
            client_category="Booking Delivery"
        ))
        db.session.add(PendingBill(
            client_code=setup_data["client_code"],
            client_name=setup_data["client_name"],
            bill_no="MB NO.9602",
            amount=5000,
            reason="Cement order",
            note="PENDING EXPORT NOTE 77"
        ))
        db.session.commit()

    # Export dispatch
    r_disp = client.get("/import_export/export?dataset=dispatch&format=csv")
    assert r_disp.status_code == 200
    assert "NOTES" in r_disp.get_data(as_text=True)
    assert "DISPATCH NOTE 55" in r_disp.get_data(as_text=True)

    # Export pending bills
    r_pb = client.get("/import_export/export?dataset=pending_bills&format=csv")
    assert r_pb.status_code == 200
    assert "note" in r_pb.get_data(as_text=True)
    assert "PENDING EXPORT NOTE 77" in r_pb.get_data(as_text=True)
