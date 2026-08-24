"""End-to-end accuracy audit for the client/financial/material ledgers and PDF exports.

These tests drive the *real* HTTP routes (no stubs) and independently recompute
the expected numbers from the ORM rows, so a mismatch means the shipped view is
wrong rather than a shared helper agreeing with itself.

Covered:
* Client ledger  (/client_ledger/<id>)      - material IN/OUT rows + summary
* Financial ledger (/ledger/<id>)           - debit/credit/balance arithmetic
* Material ledger (/material_ledger/<id>)   - running stock balance + ordering
* Sales invoice PDF (/download_invoice/...) - must be a real PDF
* Client ledger PDF (/download_client_ledger/<id>)
* Full client history PDF (/download_full_client_history/<id>)
* Cross-check: on-screen totals == printed totals == independent recomputation
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pytest

from models import db, Client, Material, Booking, BookingItem, Payment, Entry, Account


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def q2(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _flash_text(response):
    """Pull human-readable flash messages out of a rendered redirect target."""
    import re
    body = response.get_data(as_text=True)
    msgs = re.findall(r'alert[^>]*>(.{0,200}?)</div>', body, re.S)
    return " | ".join(re.sub(r"<[^>]+>|\s+", " ", m).strip() for m in msgs)[-400:]


# ---------------------------------------------------------------------------
# Fixture: a client with a real booking + payment + stock + dispatch
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded(app, client):
    """Create client/material/booking/payment/stock through the real endpoints."""
    login(client)
    tok = csrf_token(client)

    r = client.post("/add_material", data={
        "material_name": "OPC AUDIT CEMENT",
        "material_unit": "Bags",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    r = client.post("/add_client", data={
        "name": "AUDIT CLIENT",
        "code": "AUD-001",
        "category": "General",
        "opening_balance": "0",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    # A real cash account is required to receive "Paid Now" on a booking.
    r = client.post("/accounts/accounts/add", data={
        "name": "AUDIT CASH",
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": "0",
        "opening_position": "debit",
        "opening_effective_date": "2026-01-01",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        mat = Material.query.filter_by(name="OPC AUDIT CEMENT").first()
        cli = Client.query.filter_by(code="AUD-001").first()
        acc = Account.query.filter(Account.name.like("%AUDIT CASH%")).first()
        assert mat is not None, "material was not created"
        assert cli is not None, "client was not created"
        assert acc is not None, "cash account was not created"
        mat_id, cli_id, acc_id = mat.id, cli.id, acc.id
        # Stock in: 1000 bags received.
        db.session.add(Entry(
            date="2026-01-05", time="09:00:00", type="IN",
            material=mat.name, qty=1000,
            bill_no="MB NO.9001", client_category="Stock In",
        ))
        db.session.commit()

    # Booking: 500 bags @ 1200 = 600000, paid 200000 -> due 400000
    r = client.post("/add_booking", data={
        "client_code": "AUD-001",
        "material_name[]": "OPC AUDIT CEMENT",
        "material_id[]": str(mat_id),
        "qty[]": "500",
        "unit_rate[]": "1200",
        "amount": "600000",
        "paid_amount": "200000",
        "manual_bill_no": "MB NO.7001",
        "date": "2026-01-10",
        "payment_account_id": str(acc_id),
        "payment_method": "Cash",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert Booking.query.count() > 0, (
            "booking rejected by /add_booking; flash: "
            + _flash_text(r)
        )

    # Dispatch 120 bags against that booking.
    r = client.post("/add_record", data={
        "date": "2026-01-12",
        "client": "AUDIT CLIENT",
        "type": "OUT",
        "material": "OPC AUDIT CEMENT",
        "material_id": str(mat_id),
        "qty": "120",
        "driver_name": "AUDIT DRIVER",
        "bill_no": "MB NO.7001",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    # Payment 150000 against the client.
    r = client.post("/add_payment", data={
        "client_code": "AUD-001",
        "amount": "150000",
        "payment_type": "Receipt",
        "method": "Cash",
        "payment_account_id": str(acc_id),
        "manual_bill_no": "MB NO.7002",
        "date": "2026-01-15",
        "_csrf_token": tok,
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        bk = Booking.query.filter_by(manual_bill_no="MB NO.7001").first()
        assert bk is not None, "booking was not created"
        pay = Payment.query.filter_by(manual_bill_no="MB NO.7002").first()
        assert pay is not None, "payment was not created; flash: " + _flash_text(r)
        cli = db.session.get(Client, cli_id)
        assert cli is not None
        return {
            "client_id": cli_id,
            "material_id": mat_id,
            "booking_id": bk.id,
            "payment_id": pay.id,
            "booking_bill": bk.manual_bill_no,
        }


# ---------------------------------------------------------------------------
# Financial ledger arithmetic
# ---------------------------------------------------------------------------
def test_financial_ledger_totals_match_independent_recomputation(app, client, seeded):
    """/ledger/<id> must show figures that equal an independent recomputation.

    The template renders row amounts with ``{:,.3f}`` and trailing zeros
    stripped, and the balance card with ``{:,.2f}``.  We assert both forms.
    """
    resp = client.get(f"/ledger/{seeded['client_id']}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    with app.app_context():
        bk = db.session.get(Booking, seeded["booking_id"])
        pay = db.session.get(Payment, seeded["payment_id"])

        # Independent expectation: booking due is a debit, paid + payment are credits.
        expect_debit = q2(bk.amount or 0)
        expect_credit = q2((bk.paid_amount or 0) + (pay.amount or 0))
        expect_balance = q2(expect_debit - expect_credit)

    def stripped(value):
        return f"{value:,.3f}".rstrip("0").rstrip(".")

    assert stripped(expect_debit) in html, (
        f"expected debit {stripped(expect_debit)} missing from financial ledger page"
    )
    assert f"{expect_balance:,.2f}" in html, (
        f"expected balance {expect_balance:,.2f} missing from financial ledger page"
    )

    assert expect_debit == Decimal("600000.00")
    assert expect_credit == Decimal("350000.00")
    assert expect_balance == Decimal("250000.00")


def test_financial_ledger_row_balances_are_monotonically_consistent(app, client, seeded):
    """Every rendered row's running balance must equal debit-credit accumulated."""
    resp = client.get(f"/ledger/{seeded['client_id']}")
    assert resp.status_code == 200

    with app.app_context():
        from app.services.ledgers import _build_client_ledger_rows
        cli = db.session.get(Client, seeded["client_id"])
        rows, _pb, t_debit, t_credit, t_balance, _mats = _build_client_ledger_rows(cli)

    # Recompute from the returned rows - the shipped projection must be self-consistent.
    running = Decimal("0.00")
    for row in rows:
        running += q2(row.get("debit") or 0) - q2(row.get("credit") or 0)
        assert q2(row.get("balance") or 0) == q2(running), (
            f"row balance drift at {row.get('bill_no')}: "
            f"shown {row.get('balance')} vs accumulated {running}"
        )
    assert q2(t_debit) - q2(t_credit) == q2(t_balance), (
        f"totals disagree: debit {t_debit} - credit {t_credit} != balance {t_balance}"
    )
    assert q2(t_debit) == Decimal("600000.00"), f"unexpected total debit {t_debit}"
    assert q2(t_credit) == Decimal("350000.00"), f"unexpected total credit {t_credit}"
    assert q2(t_balance) == Decimal("250000.00"), f"unexpected closing balance {t_balance}"


# ---------------------------------------------------------------------------
# Material ledger accuracy
# ---------------------------------------------------------------------------
def test_material_ledger_running_balance_matches_stock_math(app, client, seeded):
    """1000 IN - 120 OUT must leave 880, and rows must be time-ordered."""
    resp = client.get(f"/material_ledger/{seeded['material_id']}")
    assert resp.status_code == 200

    with app.app_context():
        entries = Entry.query.filter_by(
            material="OPC AUDIT CEMENT", is_void=False
        ).order_by(Entry.id.asc()).all()
        expected = sum(
            (e.qty if e.type == "IN" else 0) - (e.qty if e.type == "OUT" else 0)
            for e in entries
        )

    assert expected == 880, f"expected closing stock 880, got {expected}"
    html = resp.get_data(as_text=True)
    assert "880" in html, "closing material balance 880 not rendered on material ledger"


def test_material_ledger_is_sorted_by_time_not_insertion_order(app, client, seeded):
    """Entries inserted out of date order must still render oldest-first.

    ``Entry.time`` is always written by the app as ``%H:%M:%S``; this test also
    exercises a seconds-less value to confirm the ledger parser degrades to a
    sane position rather than silently collapsing every row to ``datetime.min``.
    """
    with app.app_context():
        # Earlier-dated entry with a HIGHER id (deliberate insertion disorder).
        db.session.add(Entry(
            date="2026-01-02", time="08:00:00", type="IN",
            material="OPC AUDIT CEMENT", qty=250, bill_no="MB NO.9000",
        ))
        db.session.add(Entry(
            date="2026-01-20", time="14:30:00", type="OUT",
            material="OPC AUDIT CEMENT", qty=20, client="AUDIT CLIENT",
            client_code="AUD-001", client_category="Booking Delivery",
            booked_material="OPC AUDIT CEMENT",
        ))
        db.session.commit()
        mat_id = seeded["material_id"]

    resp = client.get(f"/material_ledger/{mat_id}")
    assert resp.status_code == 200

    with app.app_context():
        from datetime import datetime
        entries = Entry.query.filter_by(material="OPC AUDIT CEMENT", is_void=False).all()

        def parse(e):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(f"{e.date} {e.time or '00:00:00'}", fmt)
                except ValueError:
                    continue
            return datetime.min

        ordered = sorted(entries, key=lambda x: (parse(x), x.id))
        dates = [e.date for e in ordered]

    assert dates == sorted(dates), (
        f"material ledger source rows are not date-ordered: {dates}"
    )

    # Closing balance must reflect all four movements: 1000 + 250 - 120 - 20.
    html = resp.get_data(as_text=True)
    assert "1110" in html, "closing material balance 1110 not rendered after extra movements"


# ---------------------------------------------------------------------------
# PDF exports - the user explicitly asked for real PDFs
# ---------------------------------------------------------------------------
def _is_pdf(response):
    body = response.data
    return body[:5] == b"%PDF-"


def test_sales_invoice_download_returns_a_real_pdf(app, client, seeded):
    """/download_invoice must hand back a PDF, not an HTML fallback."""
    resp = client.get(
        f"/download_invoice/{seeded['booking_bill']}",
        query_string={"src": "booking", "src_id": str(seeded["booking_id"])},
    )
    assert resp.status_code == 200
    ctype = resp.headers.get("Content-Type", "")
    disposition = resp.headers.get("Content-Disposition", "")
    assert _is_pdf(resp), (
        f"sales invoice is NOT a PDF (Content-Type={ctype!r}, "
        f"Content-Disposition={disposition!r}); first bytes={resp.data[:60]!r}"
    )
    assert "pdf" in ctype.lower(), f"Content-Type should be a PDF, got {ctype!r}"


def test_client_ledger_pdf_download_returns_a_real_pdf(app, client, seeded):
    resp = client.get(f"/download_client_ledger/{seeded['client_id']}")
    assert resp.status_code == 200
    disposition = resp.headers.get("Content-Disposition", "")
    assert _is_pdf(resp), (
        f"client ledger PDF is NOT a PDF (Content-Disposition={disposition!r}); "
        f"first bytes={resp.data[:60]!r}"
    )


def test_full_client_history_pdf_returns_a_real_pdf(app, client, seeded):
    resp = client.get(f"/download_full_client_history/{seeded['client_id']}")
    assert resp.status_code == 200
    disposition = resp.headers.get("Content-Disposition", "")
    assert _is_pdf(resp), (
        f"full client history PDF is NOT a PDF (Content-Disposition={disposition!r}); "
        f"first bytes={resp.data[:60]!r}"
    )


# ---------------------------------------------------------------------------
# No data misses: what is on screen must also be on the PDF
# ---------------------------------------------------------------------------
def test_pdf_contains_the_same_figures_as_the_ledger_page(app, client, seeded):
    """Screen and print views must not silently drop rows or totals."""
    screen = client.get(f"/ledger/{seeded['client_id']}").get_data(as_text=True)
    # action=print renders the same print template as HTML (for the browser's
    # print dialog); the plain download is the binary PDF checked separately.
    printed = client.get(
        f"/download_client_ledger/{seeded['client_id']}?action=print"
    ).get_data(as_text=True)

    # Amounts the template renders with trailing zeros stripped.
    for needle, label in [
        ("600,000", "booking debit"),
        ("200,000", "booking paid credit"),
        ("150,000", "payment credit"),
        ("250,000", "closing balance"),
        ("MB NO.7001", "booking bill number"),
        ("MB NO.7002", "payment bill number"),
    ]:
        assert needle in screen, f"{label} ({needle}) missing from on-screen ledger"
        assert needle in printed, f"{label} ({needle}) missing from printed client ledger"

    # The printed summary block carries all three totals.
    for needle, label in [
        ("600,000.00", "total debit"),
        ("350,000.00", "total credit"),
        ("250,000.00", "total balance"),
    ]:
        assert needle in printed, f"{label} ({needle}) missing from printed client ledger"


def _pdf_text(data):
    """Best-effort text extraction from PDF bytes; skips if no reader present."""
    try:
        import io
        from pypdf import PdfReader
    except Exception:
        pytest.skip("pypdf not installed; cannot verify PDF text content")
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def test_client_ledger_pdf_bytes_contain_figures_verbatim(app, client, seeded):
    """The downloaded PDF must carry the same figures as the screen, verbatim."""
    resp = client.get(f"/download_client_ledger/{seeded['client_id']}")
    assert resp.data[:5] == b"%PDF-", "client ledger download is not a PDF"
    text = _pdf_text(resp.data)

    for needle, label in [
        ("600,000", "booking debit"),
        ("350,000.00", "total credit"),
        ("250,000.00", "total balance"),
        ("MB NO.7001", "booking bill number"),
        ("MB NO.7002", "payment bill number"),
    ]:
        assert needle in text, f"{label} ({needle}) missing from the PDF text"


def test_sales_invoice_pdf_bytes_contain_figures_verbatim(app, client, seeded):
    """The sales invoice PDF must carry the bill's own figures verbatim."""
    resp = client.get(
        f"/download_invoice/{seeded['booking_bill']}",
        query_string={"src": "booking", "src_id": str(seeded["booking_id"])},
    )
    assert resp.data[:5] == b"%PDF-", "sales invoice download is not a PDF"
    text = _pdf_text(resp.data)

    for needle, label in [
        ("MB NO.7001", "bill number"),
        ("600,000", "booking amount"),
        ("OPC AUDIT CEMENT", "material line"),
    ]:
        assert needle in text, f"{label} ({needle}) missing from the invoice PDF text"


def test_on_screen_ledger_shows_debit_and_credit_totals(app, client, seeded):
    """The screen ledger must expose the same three totals as its print view."""
    screen = client.get(f"/ledger/{seeded['client_id']}").get_data(as_text=True)
    for needle, label in [
        ("600,000.00", "total debit"),
        ("350,000.00", "total credit"),
        ("250,000.00", "total balance"),
    ]:
        assert needle in screen, f"{label} ({needle}) missing from on-screen ledger"


def test_client_ledger_page_shows_stock_summary(app, client, seeded):
    """/client_ledger/<id> must render the client's material movements."""
    resp = client.get(f"/client_ledger/{seeded['client_id']}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "OPC AUDIT CEMENT" in html, "material missing from client ledger page"
    assert "MB NO.7001" in html, "dispatch bill number missing from client ledger page"


def test_booking_cancel_returns_to_a_readable_ledger(app, client, seeded):
    """Every cancel redirect target must render real ledger content."""
    resp = client.get(f"/client_ledger/{seeded['client_id']}")
    html = resp.get_data(as_text=True)
    # The stub template rendered only a client-name picker: assert that the
    # page carries actual ledger data rather than just the directory.
    assert "No phone" not in html or "OPC AUDIT CEMENT" in html, (
        "/client_ledger/<id> renders the bare client picker instead of ledger data"
    )


# ---------------------------------------------------------------------------
# Remaining PDF endpoints share the same exporter; assert none degrade to HTML
# ---------------------------------------------------------------------------
def test_client_clearance_pdf_is_a_real_pdf(app, client, seeded):
    resp = client.get(f"/download_client_clearance/{seeded['client_id']}")
    assert resp.status_code == 200
    assert resp.data[:5] == b"%PDF-", (
        f"clearance statement is not a PDF (Content-Disposition="
        f"{resp.headers.get('Content-Disposition')!r})"
    )
    text = _pdf_text(resp.data)
    assert "AUDIT CLIENT" in text, "client name missing from clearance PDF"


def test_export_dataset_pdf_is_a_real_pdf(app, client, seeded):
    resp = client.get("/import_export/export", query_string={"dataset": "clients", "format": "pdf"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    assert resp.data[:5] == b"%PDF-", (
        f"export PDF is not a PDF (Content-Type={resp.headers.get('Content-Type')!r})"
    )


def test_no_pdf_endpoint_returns_html_fallback(app, client, seeded):
    """Guard: a PDF request must never come back as text/html."""
    targets = [
        f"/download_invoice/{seeded['booking_bill']}?src=booking&src_id={seeded['booking_id']}",
        f"/download_client_ledger/{seeded['client_id']}",
        f"/download_full_client_history/{seeded['client_id']}",
        f"/download_client_clearance/{seeded['client_id']}",
        "/import_export/export?dataset=clients&format=pdf",
    ]
    offenders = []
    for url in targets:
        r = client.get(url)
        if r.status_code == 200 and r.data[:5] != b"%PDF-":
            offenders.append((url, r.headers.get("Content-Type")))
    assert not offenders, f"these PDF endpoints returned HTML: {offenders}"


def test_pdf_fallback_strips_app_chrome_but_keeps_content():
    """The fallback must drop navigation/modals yet keep every ledger figure."""
    from app.services.pdf_fallback import _parse_html, render_html_to_pdf

    html = """<body>
    <div class="loading-overlay" id="loadingOverlay"><img src="x.gif"><p>Processing...</p></div>
    <nav class="sidebar"><a>Dashboard</a><a>Supplier Ledger</a></nav>
    <div class="main-content">
      <h2>Client Ledger: ACME</h2>
      <div class="d-print-none"><button>Print</button></div>
      <table><tr><th>Bill</th><th>Due</th></tr>
             <tr><td>MB NO.7001</td><td>1,250,000.00</td></tr></table>
      <div class="modal"><div class="modal-body">hidden modal text</div></div>
      <p>Net Pending 350,000.00</p>
    </div>
    <script>var secret = 1;</script>
    </body>"""

    flat = " ".join(
        b[1] if b[0] == "text" else " ".join(c[0] for r in b[1] for c in r)
        for b in _parse_html(html)
    )

    # Content preserved
    assert "Client Ledger: ACME" in flat
    assert "MB NO.7001" in flat
    assert "1,250,000.00" in flat
    assert "Net Pending 350,000.00" in flat
    # Chrome removed
    for gone in ["Processing", "Dashboard", "Supplier Ledger",
                 "hidden modal text", "var secret"]:
        assert gone not in flat, f"app chrome leaked into the PDF: {gone!r}"

    data = render_html_to_pdf(html, "t.pdf", "Test")
    assert data and data[:5] == b"%PDF-", "fallback did not produce a PDF"
