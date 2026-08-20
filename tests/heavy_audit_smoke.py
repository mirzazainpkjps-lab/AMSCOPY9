"""Heavy audit smoke: wipe DB, hit every major page/form, assert booking != credit."""
from __future__ import annotations

import os
import re
import traceback
from pathlib import Path

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"

ROOT = Path("/home/user")
DB = ROOT / "instance" / "ahmed_cement.db"
REPORT = ROOT / "HEAVY_AUDIT_REPORT.md"


def flashes(resp):
    html = resp.get_data(as_text=True)
    found = re.findall(r'class="[^"]*alert[^"]*"[^>]*>(.*?)</div>', html, flags=re.S | re.I)
    out = []
    for raw in found:
        txt = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
        if txt:
            out.append(txt)
    return out


def main():
    problems = []
    notes = []
    page_ok = []
    page_bad = []

    if DB.exists():
        DB.unlink()
    snap = ROOT / "instance" / "health_snapshot.json"
    if snap.exists():
        snap.unlink()

    from app import create_app
    from models import (
        db, Client, Material, Booking, DirectSale, Payment, MaterialReturn,
        Entry, PendingBill, Account, GRN, AccountTransaction, Invoice, Supplier,
    )

    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "heavy-audit",
        "WTF_CSRF_ENABLED": False,
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{DB}",
    })
    c = app.test_client()

    def hit(method, path, data=None, expect=(200, 302), label=None, follow=True):
        label = label or f"{method} {path}"
        try:
            if method == "GET":
                rv = c.get(path, follow_redirects=follow)
            else:
                rv = c.post(path, data=data or {}, follow_redirects=follow)
        except Exception as exc:
            problems.append(f"{label}: EXC {type(exc).__name__}: {exc}")
            page_bad.append(label)
            return None
        body = rv.get_data(as_text=True)
        if rv.status_code not in expect:
            problems.append(f"{label}: HTTP {rv.status_code}")
            page_bad.append(label)
        elif "Traceback" in body or "BuildError" in body or "jinja2.exceptions" in body:
            problems.append(f"{label}: traceback/build error in body")
            page_bad.append(label)
        else:
            page_ok.append(label)
        return rv

    # --- unauth pages ---
    for path in ["/", "/login"]:
        hit("GET", path, label=f"unauth GET {path}")

    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin, _bootstrap_database, _ensure_model_columns
        _bootstrap_database()
        _ensure_model_columns()
        _ensure_default_admin()

    login = c.post("/login", data={"username": "Admin", "password": "Admin@fbm12345", "remember_me": "1"}, follow_redirects=False)
    if login.status_code not in (302, 303):
        problems.append(f"LOGIN FAILED {login.status_code}")
    else:
        notes.append("login Admin ok")

    empty_gets = [
        "/", "/clients", "/materials", "/bookings", "/payments", "/direct_sales",
        "/material_returns", "/pending_bills", "/ledger", "/decision_ledger",
        "/cash_flow", "/profit_reports", "/unpaid_transactions", "/financial_details",
        "/grn", "/dispatching", "/tracking", "/accounts/", "/accounts/accounts",
        "/suppliers", "/delivery_persons", "/delivery_rents", "/settings",
        "/cash_flow", "/notifications", "/import_export/", "/inventory/stock_summary",
        "/mixed_transactions", "/daily_transactions", "/pay_supplier",
        "/accounts/receipts", "/accounts/transfers", "/accounts/expenditures",
        "/accounts/payments/clients", "/accounts/audit",
    ]
    for path in empty_gets:
        hit("GET", path, label=f"empty GET {path}")

    # masters
    hit("POST", "/add_client", {
        "name": "Audit Client", "code": "AUD-001", "phone": "03001112222",
        "category": "General", "opening_balance": "0",
    }, label="POST add_client")
    hit("POST", "/add_material", {
        "material_name": "OPC 53", "material_code": "OPC53", "material_unit": "Bags",
    }, label="POST add_material")
    with app.app_context():
        mat = Material.query.filter_by(name="OPC 53").first()
        if mat:
            mat.unit_price = 1200
            db.session.commit()
        else:
            problems.append("material not created")

    hit("GET", "/accounts/accounts/add", label="GET add account form")
    with app.app_context():
        from models import AccountCategory
        cats = AccountCategory.query.filter_by(is_active=True).all()
        cat_name = cats[0].name if cats else "Company"
    hit("POST", "/accounts/accounts/add", {
        "name": "AUDIT CASH", "category": "cash", "account_type": "company",
        "source_category": cat_name, "initial_balance": "50000",
    }, label="POST add cash account")
    with app.app_context():
        acc = Account.query.filter_by(name="AUDIT CASH").first()
        acc_id = acc.id if acc else None
        if not acc:
            problems.append("cash account missing")

    hit("POST", "/grn", {
        "action": "add", "supplier": "Audit Supplier",
        "mat_name[]": ["OPC 53"], "qty[]": ["200"], "price[]": ["1100"],
    }, label="POST GRN 200")

    # booking with receive account
    rv = hit("POST", "/add_booking", {
        "client_code": "AUD-001",
        "material_name[]": ["OPC 53"], "qty[]": ["30"], "unit_rate[]": ["1200"],
        "amount": "36000", "paid_amount": "12000",
        "payment_method": "Cash", "receive_in_account_id": str(acc_id or ""),
        "payment_account_id": str(acc_id or ""),
    }, label="POST booking paid 12000")
    if rv:
        notes.append(f"booking flashes: {flashes(rv)}")

    # booking with paid=0 should NOT require account
    rv = hit("POST", "/add_booking", {
        "client_code": "AUD-001",
        "material_name[]": ["OPC 53"], "qty[]": ["5"], "unit_rate[]": ["1200"],
        "amount": "6000", "paid_amount": "0",
    }, label="POST booking unpaid no-account")
    if rv:
        notes.append(f"unpaid booking flashes: {flashes(rv)}")

    rv = hit("POST", "/add_direct_sale", {
        "client_name": "Audit Client", "category": "Credit Customer",
        "driver_name": "Driver One",
        "product_name[]": ["OPC 53"], "qty[]": ["12"], "unit_rate[]": ["1250"],
        "paid_amount": "0", "create_invoice": "1", "has_bill": "1",
        "manual_bill_no": "CR-AUDIT-1",
    }, label="POST credit sale")
    if rv:
        notes.append(f"credit flashes: {flashes(rv)}")

    rv = hit("POST", "/add_direct_sale", {
        "client_name": "Audit Client", "category": "Cash",
        "driver_name": "Driver One",
        "product_name[]": ["OPC 53"], "qty[]": ["4"], "unit_rate[]": ["1300"],
        "paid_amount": "5200", "payment_method": "Cash",
        "payment_account_id": str(acc_id or ""), "track_as_cash": "1",
        "create_invoice": "0",
    }, label="POST cash sale")

    rv = hit("POST", "/add_payment", {
        "client_code": "AUD-001", "amount": "3000", "method": "Cash",
        "payment_account_id": str(acc_id or ""),
    }, label="POST client payment")

    rv = hit("POST", "/add_material_return", {
        "client_code": "AUD-001", "return_type": "normal",
        "material_name[]": ["OPC 53"], "qty[]": ["1"], "unit_rate[]": ["1250"],
    }, label="POST normal return")

    rv = hit("POST", "/add_direct_sale", {
        "client_name": "Audit Client", "category": "Booking Delivery",
        "driver_name": "Driver One",
        "product_name[]": ["OPC 53"], "qty[]": ["10"], "unit_rate[]": ["1200"],
        "amount": "12000", "paid_amount": "0", "create_invoice": "1",
    }, label="POST booked delivery (rates in form)")
    if rv:
        notes.append(f"booked delivery flashes: {flashes(rv)}")

    with app.app_context():
        cli = Client.query.filter_by(code="AUD-001").first()
        cli_id = cli.id if cli else None
        sales = DirectSale.query.filter_by(is_void=False).all()
        for s in sales:
            notes.append(
                f"SALE id={s.id} code={s.client_code} cat={s.category} "
                f"amt={s.amount} paid={s.paid_amount} inv={s.invoice_id} "
                f"items={[(i.product_name, i.qty, i.price_at_time) for i in s.items]}"
            )
        booked = [s for s in sales if s.category == "Booking Delivery"]
        credit = [s for s in sales if s.category == "Credit Customer"]
        if not booked:
            problems.append("AUDIT: Booking Delivery sale missing")
        else:
            bd = booked[0]
            if float(bd.amount or 0) != 0:
                problems.append(f"AUDIT: booked sale amount {bd.amount} (must be 0)")
            if bd.invoice_id:
                problems.append("AUDIT: booked sale created invoice")
            if any(float(i.price_at_time or 0) > 0 for i in bd.items):
                problems.append("AUDIT: booked sale items have rates")
            if not bd.client_code:
                problems.append("AUDIT: booked sale missing client_code")
        if not credit:
            problems.append("AUDIT: credit sale missing")
        else:
            if float(credit[0].amount or 0) <= 0:
                problems.append("AUDIT: credit sale amount not charged")
            if not credit[0].client_code:
                problems.append("AUDIT: credit sale missing client_code")

        bks = Booking.query.filter_by(is_void=False).all()
        notes.append(f"BOOKINGS={[(b.id, b.amount, b.paid_amount, b.receive_in_account_id) for b in bks]}")
        if len(bks) < 2:
            problems.append(f"expected 2 bookings, got {len(bks)}")
        paid_bk = next((b for b in bks if float(b.paid_amount or 0) > 0), None)
        if paid_bk and not paid_bk.receive_in_account_id:
            problems.append("paid booking did not store receive_in_account_id")

        stock = Material.query.filter_by(name="OPC 53").first()
        notes.append(f"stock={getattr(stock,'total',None)}")
        # 200 -12 credit -4 cash +1 return -10 booked = 175
        if stock and abs(float(stock.total or 0) - 175) > 0.01:
            problems.append(f"stock expected 175 got {stock.total}")

        txs = AccountTransaction.query.filter_by(is_void=False).all()
        notes.append(f"ACC_TX={[(t.transaction_type, t.amount, (t.description or '')[:50]) for t in txs]}")

        ents = Entry.query.filter_by(is_void=False).all()
        notes.append(f"ENTRIES={[(e.type, e.qty, e.client_category, e.nimbus_no) for e in ents]}")

        pbs = PendingBill.query.filter_by(is_void=False).all()
        notes.append(f"PENDING={[(p.bill_no, p.amount, p.reason, p.is_paid) for p in pbs]}")

    # pages after data
    after = []
    if cli_id:
        after += [
            f"/ledger/{cli_id}", f"/client_ledger/{cli_id}",
            f"/financial_ledger/{cli_id}",
            f"/download_client_ledger/{cli_id}",
            f"/download_client_clearance/{cli_id}",
            f"/download_full_client_history/{cli_id}",
        ]
    after += [
        "/bookings", "/direct_sales", "/payments", "/pending_bills",
        "/decision_ledger", "/cash_flow", "/profit_reports",
        "/unpaid_transactions", "/grn", "/tracking", "/accounts/",
        "/suppliers", "/pay_supplier",
    ]
    for path in after:
        rv = hit("GET", path, label=f"data GET {path}")
        if rv and path.endswith(f"/ledger/{cli_id}") and "Clearance Statement" not in rv.get_data(as_text=True):
            problems.append("client ledger missing Clearance Statement button")
        if rv and "download_client_clearance" in path:
            body = rv.get_data(as_text=True)
            if "Outstanding" not in body and "Clearance" not in body:
                problems.append("clearance document missing Outstanding heading")

    # delete credit sale — stock should return 12
    with app.app_context():
        cr = DirectSale.query.filter_by(category="Credit Customer", is_void=False).first()
        cr_id = cr.id if cr else None
    if cr_id:
        hit("POST", f"/delete_transaction/DirectSale/{cr_id}", label="DELETE credit sale")
        with app.app_context():
            gone = db.session.get(DirectSale, cr_id)
            stock = Material.query.filter_by(name="OPC 53").first()
            notes.append(f"after delete credit: sale={gone} stock={getattr(stock,'total',None)}")
            if gone is not None:
                problems.append("credit sale still in DB after hard delete")
            # 175 + 12 = 187
            if stock and abs(float(stock.total or 0) - 187) > 0.01:
                problems.append(f"stock after credit delete expected 187 got {stock.total}")

    # delete GRN should fail or reverse 200? we still have stock from remaining movements
    with app.app_context():
        grn = GRN.query.first()
        grn_id = grn.id if grn else None
    if grn_id:
        hit("POST", "/grn", {"action": "delete", "id": str(grn_id)}, label="DELETE GRN")
        with app.app_context():
            g2 = db.session.get(GRN, grn_id)
            notes.append(f"after GRN delete: grn={g2}")
            # Cash/credit sales still consume this GRN lot — FIFO lock must block delete.
            if g2 is None:
                problems.append("GRN deleted while cash/credit lots were still consumed")

            from models import AuditLog
            actor_rows = AuditLog.query.filter(AuditLog.username == "Admin").count()
            notes.append(f"audit_rows_by_Admin={actor_rows}")
            if actor_rows < 5:
                problems.append(f"expected actor-stamped audit rows, got {actor_rows}")

    lines = [
        "# AMS Heavy Audit + Smoke Report",
        "",
        f"DB wiped and rebuilt. Pages OK: **{len(page_ok)}**. Failures: **{len(page_bad)}**.",
        "",
        "## Checks",
        "- Login Admin / Admin@fbm12345",
        "- Masters, cash account, GRN 200",
        "- Booking paid (receive account) + unpaid booking (no account required)",
        "- Credit sale vs Booked delivery (rates in form must stay amount=0)",
        "- Cash sale, payment, normal return",
        "- Clearance statement + ledgers/reports",
        "- Hard delete credit sale restores stock",
        "- Hard delete GRN",
        "",
        "## Notes",
    ]
    for n in notes:
        lines.append(f"- {n}")
    lines += ["", "## Page failures"]
    if page_bad:
        for p in page_bad:
            lines.append(f"- {p}")
    else:
        lines.append("- none")
    lines += ["", "## Problems"]
    if problems:
        for p in problems:
            lines.append(f"- {p}")
    else:
        lines.append("- None. Heavy smoke passed.")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if problems:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
