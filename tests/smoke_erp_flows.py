"""End-to-end smoke: wipe DB, post real transactions, hit ledgers & reports."""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"

ROOT = Path("/home/user")
DB = ROOT / "instance" / "ahmed_cement.db"
REPORT = ROOT / "SMOKE_TEST_REPORT.md"


def flashes(resp) -> list[str]:
    html = resp.get_data(as_text=True)
    found = re.findall(
        r'class="[^"]*alert[^"]*"[^>]*>(.*?)</div>',
        html,
        flags=re.S | re.I,
    )
    texts = []
    for raw in found:
        txt = re.sub(r"<[^>]+>", " ", raw)
        txt = " ".join(txt.split())
        if txt:
            texts.append(txt)
    return texts


def main():
    problems = []
    notes = []

    if DB.exists():
        DB.unlink()
    snap = ROOT / "instance" / "health_snapshot.json"
    if snap.exists():
        snap.unlink()

    from app import create_app
    from models import (
        db,
        Client,
        Material,
        Booking,
        DirectSale,
        Payment,
        MaterialReturn,
        Entry,
        PendingBill,
        Account,
        GRN,
        AccountTransaction,
        Invoice,
    )

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "smoke",
            "WTF_CSRF_ENABLED": False,
            "SESSION_COOKIE_SECURE": False,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{DB}",
        }
    )
    client = app.test_client()

    def hit(method, path, data=None, expect=(200, 302), label=None):
        label = label or f"{method} {path}"
        try:
            if method == "GET":
                rv = client.get(path, follow_redirects=True)
            else:
                rv = client.post(path, data=data or {}, follow_redirects=True)
        except Exception as exc:
            problems.append(f"{label}: EXCEPTION {type(exc).__name__}: {exc}")
            return None
        if rv.status_code not in expect:
            problems.append(f"{label}: HTTP {rv.status_code}")
        body = rv.get_data(as_text=True)
        if "BuildError" in body or "Traceback" in body:
            problems.append(f"{label}: error page / traceback in HTML")
        return rv

    empty_pages = [
        "/", "/login", "/clients", "/materials", "/bookings", "/payments",
        "/direct_sales", "/material_returns", "/pending_bills", "/ledger",
        "/decision_ledger", "/cash_flow", "/profit_reports",
        "/unpaid_transactions", "/financial_details", "/grn", "/dispatching",
        "/tracking", "/accounts/", "/accounts/accounts", "/suppliers",
        "/delivery_persons", "/delivery_rents", "/settings",
        "/cash_flow", "/notifications",
    ]
    for path in empty_pages:
        hit("GET", path, label=f"empty GET {path}")

    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin, _bootstrap_database
        _bootstrap_database()
        _ensure_default_admin()

    login_rv = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345", "remember_me": "1"},
        follow_redirects=False,
    )
    if login_rv.status_code not in (302, 303):
        problems.append(f"login failed HTTP {login_rv.status_code}")

    rv = hit("POST", "/add_client", {
        "name": "Smoke Client", "code": "SMK-001", "phone": "03001234567",
        "category": "General", "opening_balance": "0",
    }, label="add client")
    if rv:
        notes.append(f"add client flashes: {flashes(rv)}")

    rv = hit("POST", "/add_material", {
        "material_name": "OPC 53", "material_code": "OPC53", "material_unit": "Bags",
    }, label="add material")
    if rv:
        notes.append(f"add material flashes: {flashes(rv)}")

    with app.app_context():
        mat = Material.query.filter_by(name="OPC 53").first()
        if mat:
            mat.unit_price = 1200
            mat.total = 0
            db.session.commit()
            mat_id = mat.id
        else:
            problems.append("Material OPC 53 not created")
            mat_id = None
        cli = Client.query.filter_by(code="SMK-001").first()
        cli_id = cli.id if cli else None
        if not cli:
            problems.append("Client SMK-001 not created")

    hit("GET", "/accounts/accounts/add", label="open add account")
    with app.app_context():
        from models import AccountCategory
        cats = AccountCategory.query.filter_by(is_active=True).all()
        cat_name = cats[0].name if cats else ""
    if not cat_name:
        hit("POST", "/accounts/categories/add", {"name": "Company"}, label="add account category")
        cat_name = "Company"

    rv = hit("POST", "/accounts/accounts/add", {
        "name": "SMOKE CASH", "category": "cash", "account_type": "company",
        "source_category": cat_name, "initial_balance": "0",
    }, label="add cash account")
    if rv:
        notes.append(f"add account flashes: {flashes(rv)}")

    with app.app_context():
        acc = Account.query.filter_by(name="SMOKE CASH").first()
        acc_id = acc.id if acc else None
        if not acc:
            problems.append("Cash account SMOKE CASH not created")

    rv = hit("POST", "/grn", {
        "action": "add", "supplier": "Smoke Supplier",
        "mat_name[]": ["OPC 53"], "qty[]": ["100"], "price[]": ["1100"],
    }, label="GRN receive 100 bags")
    if rv:
        notes.append(f"GRN flashes: {flashes(rv)}")

    with app.app_context():
        mat = Material.query.filter_by(name="OPC 53").first()
        stock = float(mat.total or 0) if mat else None
        notes.append(f"stock after GRN: {stock}")
        if stock is not None and abs(stock - 100) > 0.01:
            problems.append(f"Stock after GRN expected 100, got {stock}")

    rv = hit("POST", "/add_booking", {
        "client_code": "SMK-001",
        "material_name[]": ["OPC 53"],
        "qty[]": ["20"],
        "unit_rate[]": ["1200"],
        "amount": "24000",
        "paid_amount": "10000",
        "payment_method": "Cash",
        "method": "Cash",
        "payment_account_id": str(acc_id or ""),
    }, label="booking + advance 10000")
    if rv:
        notes.append(f"booking flashes: {flashes(rv)}")

    rv = hit("POST", "/add_direct_sale", {
        "client_name": "Smoke Client",
        "category": "Credit Customer",
        "driver_name": "Driver Ali",
        "product_name[]": ["OPC 53"],
        "qty[]": ["10"],
        "unit_rate[]": ["1250"],
        "paid_amount": "0",
        "payment_method": "Cash",
        "create_invoice": "1",
        "has_bill": "1",
    }, label="credit sale 10 bags unpaid")
    if rv:
        notes.append(f"credit sale flashes: {flashes(rv)}")

    rv = hit("POST", "/add_direct_sale", {
        "client_name": "Smoke Client",
        "category": "Cash",
        "driver_name": "Driver Ali",
        "product_name[]": ["OPC 53"],
        "qty[]": ["5"],
        "unit_rate[]": ["1300"],
        "paid_amount": "6500",
        "payment_method": "Cash",
        "payment_account_id": str(acc_id or ""),
        "track_as_cash": "1",
        "create_invoice": "0",
    }, label="cash sale 5 bags paid 6500")
    if rv:
        notes.append(f"cash sale flashes: {flashes(rv)}")

    rv = hit("POST", "/add_payment", {
        "client_code": "SMK-001",
        "amount": "5000",
        "method": "Cash",
        "payment_account_id": str(acc_id or ""),
    }, label="client payment 5000")
    if rv:
        notes.append(f"payment flashes: {flashes(rv)}")

    rv = hit("POST", "/add_material_return", {
        "client_code": "SMK-001",
        "return_type": "normal",
        "material_name[]": ["OPC 53"],
        "qty[]": ["2"],
        "unit_rate[]": ["1250"],
    }, label="material return 2 bags")
    if rv:
        notes.append(f"return flashes: {flashes(rv)}")

    # Booked sale with rates in the form (UI fills unit price) — must NOT become credit.
    rv = hit("POST", "/add_direct_sale", {
        "client_name": "Smoke Client",
        "category": "Booking Delivery",
        "driver_name": "Driver Ali",
        "product_name[]": ["OPC 53"],
        "qty[]": ["8"],
        "unit_rate[]": ["1200"],
        "amount": "9600",
        "paid_amount": "0",
        "create_invoice": "1",
        "has_bill": "1",
    }, label="booking delivery 8 bags (rates present)")
    if rv:
        notes.append(f"booking delivery flashes: {flashes(rv)}")

    with app.app_context():
        counts = {
            "clients": Client.query.count(),
            "materials": Material.query.count(),
            "bookings": Booking.query.filter_by(is_void=False).count(),
            "sales": DirectSale.query.filter_by(is_void=False).count(),
            "payments": Payment.query.filter_by(is_void=False).count(),
            "returns": MaterialReturn.query.filter_by(is_void=False).count(),
            "entries": Entry.query.filter_by(is_void=False).count(),
            "pending": PendingBill.query.filter_by(is_void=False).count(),
            "accounts": Account.query.count(),
            "grn": GRN.query.count(),
        }
        notes.append(f"counts={counts}")
        if counts["bookings"] < 1:
            problems.append("No booking saved")
        if counts["sales"] < 3:
            problems.append(f"Expected 3 sales, got {counts['sales']}")

        bk = Booking.query.filter_by(is_void=False).first()
        if bk:
            notes.append(f"BOOKING amount={bk.amount} paid={bk.paid_amount}")
            if float(bk.amount or 0) <= 0:
                problems.append("Booking amount should be 24000 (reservation value), got 0")

        sales = DirectSale.query.filter_by(is_void=False).order_by(DirectSale.id.asc()).all()
        sale_rows = []
        for s in sales:
            items = [(it.product_name, float(it.qty or 0), float(it.price_at_time or 0)) for it in (s.items or [])]
            sale_rows.append((s.category, float(s.amount or 0), float(s.paid_amount or 0), items, s.invoice_id))
            notes.append(f"SALE id={s.id} cat={s.category} amt={s.amount} paid={s.paid_amount} inv={s.invoice_id} items={items}")

        booked = [s for s in sales if s.category == "Booking Delivery"]
        credit = [s for s in sales if s.category == "Credit Customer"]
        cash = [s for s in sales if s.category == "Cash"]
        if not booked:
            problems.append("Booking Delivery sale missing")
        else:
            bd = booked[0]
            if abs(float(bd.amount or 0)) > 0.01:
                problems.append(f"Booking Delivery behaved like credit: amount={bd.amount} (must be 0)")
            if bd.invoice_id:
                inv = db.session.get(Invoice, bd.invoice_id)
                problems.append(f"Booking Delivery created invoice {getattr(inv, 'invoice_no', bd.invoice_id)}")
            if any(float(it.price_at_time or 0) > 0 for it in (bd.items or [])):
                problems.append(f"Booking Delivery items have chargeable rates: {[(it.product_name, it.price_at_time) for it in bd.items]}")
        if not credit:
            problems.append("Credit sale missing")
        else:
            cs = credit[0]
            if float(cs.amount or 0) <= 0:
                problems.append(f"Credit sale amount should be > 0, got {cs.amount}")
        if not cash:
            problems.append("Cash sale missing")

        ents = Entry.query.filter_by(is_void=False).order_by(Entry.id.asc()).all()
        einfo = [(e.type, e.qty, e.nimbus_no, e.client_category, e.bill_no) for e in ents]
        notes.append(f"ENTRIES={einfo}")
        bd_out = [e for e in ents if e.type == "OUT" and e.client_category == "Booking Delivery"]
        cr_out = [e for e in ents if e.type == "OUT" and e.client_category == "Credit Customer"]
        if not bd_out:
            problems.append("No OUT entry with client_category=Booking Delivery")
        if not cr_out:
            problems.append("No OUT entry with client_category=Credit Customer")

        pbs = PendingBill.query.filter_by(is_void=False).all()
        pbinfo = [(pb.bill_no, pb.amount, pb.reason, pb.is_paid) for pb in pbs]
        notes.append(f"PENDING={pbinfo}")
        for pb in pbs:
            reason = (pb.reason or "").lower()
            if "direct sale" in reason and "booking" in reason:
                problems.append(f"Pending bill looks like booking-as-credit: {pb.reason} amt={pb.amount}")
            if "booking delivery" in reason and float(pb.amount or 0) > 0:
                problems.append(f"Booking delivery created due pending {pb.amount}")

        txs = AccountTransaction.query.filter_by(is_void=False).all()
        notes.append(f"ACCOUNT TX={[(t.transaction_type, t.amount, t.description) for t in txs]}")
        if acc_id:
            acc = db.session.get(Account, acc_id)
            notes.append(f"cash account balance={getattr(acc, 'balance', None)}")

        mat = Material.query.filter_by(name="OPC 53").first()
        notes.append(f"final stock={getattr(mat, 'total', None)}")
        # 100 -10 credit -5 cash +2 return -8 booking = 79
        if mat and abs(float(mat.total or 0) - 79) > 0.01:
            problems.append(f"Stock expected 79, got {mat.total}")

        if cli_id:
            from app.services.finance_clients import _client_balance_as_of
            from app.services.ledgers import _build_client_ledger_rows
            cli = db.session.get(Client, cli_id)
            try:
                bal = _client_balance_as_of(cli)
                notes.append(f"client balance as-of={bal}")
            except Exception as exc:
                problems.append(f"_client_balance_as_of failed: {exc}")
            try:
                fin, pends, td, tc, tb, matg = _build_client_ledger_rows(cli)
                notes.append(f"financial rows={len(fin)} pending={len(pends)} bal={tb}")
                for r in fin:
                    notes.append(f"  FIN {r.get('description')} debit={r.get('debit')} credit={r.get('credit')} bill={r.get('bill_no')}")
                # Booking delivery must not appear as a due Direct Sale
                for r in fin:
                    desc = (r.get("description") or "")
                    if r.get("type") == "DirectSale" and r.get("id") in {s.id for s in booked}:
                        if float(r.get("debit") or 0) > 0:
                            problems.append(f"Booking Delivery on financial ledger as due: {r}")
            except Exception as exc:
                problems.append(f"_build_client_ledger_rows failed: {exc}")

    pages = []
    if cli_id:
        pages += [
            f"/ledger/{cli_id}",
            f"/client_ledger/{cli_id}",
            f"/financial_ledger/{cli_id}",
            f"/download_client_ledger/{cli_id}",
        ]
    if mat_id:
        pages.append(f"/material_ledger/{mat_id}")
    pages += [
        "/decision_ledger", "/cash_flow", "/profit_reports",
        "/unpaid_transactions", "/financial_details", "/pending_bills",
        "/bookings", "/payments", "/direct_sales", "/material_returns",
        "/tracking", "/accounts/", "/accounts/kpi/receipts",
        "/accounts/kpi/client_payments", "/accounts/kpi/expenditures",
        "/grn", "/suppliers", "/delivery_rents",
    ]
    for path in pages:
        rv = hit("GET", path, label=f"report GET {path}")
        if rv and rv.status_code == 200:
            body = rv.get_data(as_text=True)
            if "Internal Server Error" in body or "jinja2.exceptions" in body:
                problems.append(f"{path}: error content")
            if len(body) < 200:
                problems.append(f"{path}: suspiciously short page ({len(body)} bytes)")

    lines = [
        "# ERP smoke test report",
        "",
        "Database wiped (`instance/ahmed_cement.db`) and rebuilt from HTTP posts.",
        "",
        "## Flows posted",
        "- Client SMK-001 / Smoke Client",
        "- Material OPC 53 @ 1200",
        "- Cash account SMOKE CASH",
        "- GRN receive 100 bags",
        "- Booking 20 bags + 10,000 advance (into SMOKE CASH)",
        "- Credit sale 10 bags unpaid (due)",
        "- Cash sale 5 bags paid 6,500",
        "- Extra payment 5,000",
        "- Material return 2 bags (normal, not booked)",
        "- Booking delivery 8 bags (form sent rates — must stay amount=0)",
        "",
        "## Notes",
    ]
    for n in notes:
        lines.append(f"- {n}")
    lines += ["", "## Problems"]
    if problems:
        for p in problems:
            lines.append(f"- {p}")
    else:
        lines.append("- None detected by automated HTTP smoke.")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if problems:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
