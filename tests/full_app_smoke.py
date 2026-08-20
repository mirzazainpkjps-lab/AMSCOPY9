"""Full-app smoke: every GET page + create/edit/delete on isolated SMOKE records."""
from __future__ import annotations

import os
import re
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "SMOKE_TEST_REPORT.md"

GET_PAGES = [
    "/", "/login",
    "/clients", "/materials", "/suppliers", "/delivery_persons",
    "/bookings", "/payments", "/direct_sales", "/material_returns",
    "/pending_bills", "/grn", "/dispatching", "/tracking",
    "/ledger", "/decision_ledger", "/financial_details",
    "/cash_flow", "/cash_flow_differences", "/profit_reports",
    "/unpaid_transactions", "/mixed_transactions",
    "/daily_transactions", "/delivery_rents",
    "/notifications", "/notifications/upcoming",
    "/settings", "/settings/activity",
    "/import_export/", "/import_export/history", "/import_export/uploads",
    "/inventory/stock_summary", "/inventory/daily_transactions", "/inventory/inventory_log",
    "/stock_summary",
    "/accounts/", "/accounts/accounts", "/accounts/accounts/add",
    "/accounts/receipts", "/accounts/transfers", "/accounts/transfers/add",
    "/accounts/expenditures", "/accounts/payments/clients",
    "/accounts/payments/suppliers", "/accounts/audit",
    "/accounts/kpi/cash_money", "/accounts/kpi/bank_accounts",
    "/accounts/kpi/cash_accounts", "/accounts/kpi/client_payments",
    "/accounts/kpi/supplier_payments", "/accounts/kpi/expenditures",
    "/accounts/kpi/receipts", "/accounts/kpi/company_money",
    "/pay_supplier",
    "/admin/", "/admin/modules", "/admin/api/health", "/admin/api/modules",
    "/system_report", "/void_audit", "/debug/db",
    "/api/notifications/due", "/api/client_next_code", "/api/material_next_code",
    "/api/clients/search", "/api/ui/theme",
]


def flashes(resp):
    html = resp.get_data(as_text=True)
    found = re.findall(r'class="[^"]*alert[^"]*"[^>]*>(.*?)</div>', html, flags=re.S | re.I)
    out = []
    for raw in found:
        txt = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
        if txt and txt not in out:
            out.append(txt[:240])
    return out


def title_of(html):
    m = re.search(r"<h[12][^>]*>(.*?)</h[12]>", html, flags=re.S | re.I)
    if not m:
        m = re.search(r"<title>(.*?)</title>", html, flags=re.S | re.I)
    if not m:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())[:80]


def main():
    os.environ.setdefault("ALLOW_EMPTY_DB", "1")
    from app import create_app
    from models import (
        db, Client, Material, Supplier, Account, AccountCategory,
        Booking, DirectSale, Payment, MaterialReturn, GRN, AuditLog,
    )

    app = create_app({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
    })
    c = app.test_client()
    results = []
    crud = []

    def rec(section, label, ok, detail=""):
        results.append({"section": section, "label": label, "ok": ok, "detail": detail})

    def req(method, path, data=None, follow=True):
        try:
            if method == "GET":
                return c.get(path, follow_redirects=follow)
            return c.post(path, data=data or {}, follow_redirects=follow)
        except Exception as exc:
            exc_text = f"EXC {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            class Fake:
                status_code = 0
                def get_data(self, as_text=True):
                    return exc_text
            return Fake()

    # --- login ---
    rv = req("GET", "/login", follow=False)
    rec("auth", "GET /login", rv.status_code in (200, 302), f"HTTP {rv.status_code}")
    rv = req("POST", "/login", {"username": "Admin", "password": "Admin@fbm12345", "remember_me": "1"}, follow=False)
    rec("auth", "POST /login Admin", rv.status_code in (302, 303), f"HTTP {rv.status_code}")
    if rv.status_code not in (302, 303):
        rec("auth", "login failed — abort", False, flashes(rv) or rv.get_data(as_text=True)[:200])
        write_report(results, crud)
        raise SystemExit(2)

    # --- every GET page ---
    seen = set()
    for path in GET_PAGES:
        if path in seen:
            continue
        seen.add(path)
        rv = req("GET", path)
        html = rv.get_data(as_text=True)
        bad = rv.status_code not in (200, 302) or any(
            x in html for x in ("Traceback (most recent call last)", "BuildError", "jinja2.exceptions", "Internal Server Error")
        )
        rec("GET pages", f"GET {path}", not bad,
            f"HTTP {rv.status_code} · {title_of(html) or '(no title)'} · flashes={flashes(rv) or '-'}")

    # extra GETs that need live IDs
    with app.app_context():
        cli = Client.query.filter(Client.is_active == True).first()
        mat = Material.query.first()
        acc = Account.query.filter(Account.is_active == True).first()
        sup = Supplier.query.first()
        extra = []
        if cli:
            extra += [
                f"/ledger/{cli.id}", f"/client_ledger/{cli.id}",
                f"/financial_ledger/{cli.id}",
                f"/api/client_booking_status/{cli.code or cli.name}",
                f"/api/client_financial_summary/{cli.code or cli.name}",
            ]
        if mat:
            extra.append(f"/material_ledger/{mat.id}")
        if acc:
            extra += [f"/accounts/ledger/{acc.id}", f"/accounts/{acc.id}/data"]
        if sup:
            extra += [f"/supplier_ledger/{sup.id}", f"/api/supplier_balance/{sup.id}"]
    for path in extra:
        rv = req("GET", path)
        html = rv.get_data(as_text=True)
        bad = rv.status_code not in (200, 302) or "Traceback (most recent call last)" in html
        rec("GET with IDs", f"GET {path}", not bad, f"HTTP {rv.status_code} · flashes={flashes(rv) or '-'}")

    # --- CRUD isolated records ---
    tag = datetime.now().strftime("%H%M%S")
    cname = f"SMOKE Client {tag}"
    ccode = f"SMK{tag}"
    mname = f"SMOKE MAT {tag}"
    sname = f"SMOKE Supplier {tag}"
    aname = f"SMOKE CASH {tag}"

    rv = req("POST", "/add_client", {
        "name": cname, "code": ccode, "phone": "03000000000",
        "category": "General", "opening_balance": "0",
    })
    crud.append(("CREATE client", rv.status_code, flashes(rv)))
    rec("CRUD create", "Create client", rv.status_code in (200, 302), f"{flashes(rv)}")

    rv = req("POST", "/add_material", {
        "material_name": mname, "material_code": f"SM{tag}", "material_unit": "Bags",
    })
    crud.append(("CREATE material", rv.status_code, flashes(rv)))
    rec("CRUD create", "Create material", rv.status_code in (200, 302), f"{flashes(rv)}")

    rv = req("POST", "/add_supplier", {"name": sname, "phone": "03001110000"})
    crud.append(("CREATE supplier", rv.status_code, flashes(rv)))
    rec("CRUD create", "Create supplier", rv.status_code in (200, 302), f"{flashes(rv)}")

    with app.app_context():
        cats = AccountCategory.query.filter_by(is_active=True).all()
        cat_name = cats[0].name if cats else "Company"
    rv = req("POST", "/accounts/accounts/add", {
        "name": aname, "category": "cash", "account_type": "company",
        "source_category": cat_name, "initial_balance": "10000",
    })
    crud.append(("CREATE account", rv.status_code, flashes(rv)))
    rec("CRUD create", "Create cash account (Accounts)", rv.status_code in (200, 302), f"{flashes(rv)}")

    with app.app_context():
        cli = Client.query.filter_by(code=ccode).first() or Client.query.filter_by(name=cname).first()
        mat = Material.query.filter_by(name=mname).first()
        acc = Account.query.filter_by(name=aname).first()
        sup = Supplier.query.filter_by(name=sname).first()
        cli_id = cli.id if cli else None
        mat_ok = bool(mat)
        acc_id = acc.id if acc else None
        sup_id = sup.id if sup else None
        if mat and (not mat.unit_price):
            mat.unit_price = 1000
            db.session.commit()

    rec("CRUD create", "Client persisted", bool(cli_id), f"id={cli_id}")
    rec("CRUD create", "Material persisted", mat_ok, mname)
    rec("CRUD create", "Account persisted", bool(acc_id), f"id={acc_id}")
    rec("CRUD create", "Supplier persisted", bool(sup_id), f"id={sup_id}")

    if acc_id and mat_ok:
        rv = req("POST", "/grn", {
            "action": "add", "supplier": sname,
            "mat_name[]": [mname], "qty[]": ["50"], "price[]": ["900"],
        })
        crud.append(("CREATE GRN", rv.status_code, flashes(rv)))
        rec("CRUD create", "Create GRN", rv.status_code in (200, 302), f"{flashes(rv)}")

    if cli_id and mat_ok:
        rv = req("POST", "/add_booking", {
            "client_code": ccode,
            "material_name[]": [mname], "qty[]": ["10"], "unit_rate[]": ["1000"],
            "amount": "10000", "paid_amount": "0",
        })
        crud.append(("CREATE booking", rv.status_code, flashes(rv)))
        rec("CRUD create", "Create booking (unpaid)", rv.status_code in (200, 302), f"{flashes(rv)}")

        rv = req("POST", "/add_direct_sale", {
            "client_name": cname, "client_code": ccode, "category": "Credit Customer",
            "driver_name": "Smoke Driver",
            "product_name[]": [mname], "qty[]": ["2"], "unit_rate[]": ["1100"],
            "paid_amount": "0", "create_invoice": "1", "has_bill": "1",
            "manual_bill_no": f"SMK-BILL-{tag}",
            "delivery_person_id[]": [""],
        })
        crud.append(("CREATE credit sale", rv.status_code, flashes(rv)))
        rec("CRUD create", "Create credit sale", rv.status_code in (200, 302), f"{flashes(rv)}")

        if acc_id:
            rv = req("POST", "/add_payment", {
                "client_code": ccode, "amount": "500", "method": "Cash",
                "payment_account_id": str(acc_id),
            })
            crud.append(("CREATE payment", rv.status_code, flashes(rv)))
            rec("CRUD create", "Create client payment", rv.status_code in (200, 302), f"{flashes(rv)}")

        rv = req("POST", "/add_material_return", {
            "client_code": ccode, "return_type": "normal",
            "material_name[]": [mname], "qty[]": ["1"], "unit_rate[]": ["1100"],
        })
        crud.append(("CREATE return", rv.status_code, flashes(rv)))
        rec("CRUD create", "Create material return", rv.status_code in (200, 302), f"{flashes(rv)}")

        rv = req("POST", "/add_pending_bill", {
            "client_code": ccode, "bill_no": f"SMK-PB-{tag}",
            "amount": "250", "reason": "Smoke pending",
        })
        crud.append(("CREATE pending bill", rv.status_code, flashes(rv)))
        rec("CRUD create", "Create pending bill", rv.status_code in (200, 302), f"{flashes(rv)}")

    if acc_id:
        rv = req("POST", "/cash_flow", {
            "action": "record_movement",
            "direction": "out",
            "amount": "100",
            "cash_account_id": str(acc_id),
            "category_name": "Fuel",
            "party_type": "other",
            "party_name": "Smoke pump",
            "description": "Smoke fuel spend",
        })
        crud.append(("CREATE cash flow spend", rv.status_code, flashes(rv)))
        rec("CRUD create", "Create cash-flow spend", rv.status_code in (200, 302), f"{flashes(rv)}")

        rv = req("POST", "/accounts/transactions/new", {
            "tx_mode": "receive",
            "amount": "200",
            "method": "Cash",
            "receive_from_category": "other_source",
            "receive_source_label": "Smoke other income",
            "receive_account_id": str(acc_id),
        })
        crud.append(("CREATE accounts receive", rv.status_code, flashes(rv)))
        rec("CRUD create", "Accounts receive (other source)", rv.status_code in (200, 302), f"{flashes(rv)}")

    # --- edits ---
    if cli_id:
        rv = req("POST", f"/edit_client/{cli_id}", {
            "name": cname + " Edited", "code": ccode, "phone": "03009998888",
            "category": "General",
        })
        crud.append(("EDIT client", rv.status_code, flashes(rv)))
        rec("CRUD edit", "Edit client name", rv.status_code in (200, 302), f"{flashes(rv)}")

    with app.app_context():
        mat = Material.query.filter_by(name=mname).first()
        mid = mat.id if mat else None
        sale = DirectSale.query.filter(DirectSale.manual_bill_no == f"SMK-BILL-{tag}").first()
        sale_id = sale.id if sale else None
        pay = Payment.query.filter(Payment.client_name.ilike(f"%SMOKE Client {tag}%")).order_by(Payment.id.desc()).first()
        pay_id = pay.id if pay else None
        bk = Booking.query.filter(Booking.client_name.ilike(f"%SMOKE Client {tag}%")).order_by(Booking.id.desc()).first() if cli_id else None
        bk_id = bk.id if bk else None
        pb = None
        try:
            from models import PendingBill
            pb = PendingBill.query.filter(PendingBill.bill_no == f"SMK-PB-{tag}").first()
        except Exception:
            pb = None
        pb_id = pb.id if pb else None

    if mid:
        rv = req("POST", f"/edit_material/{mid}", {
            "material_name": mname, "material_code": f"SM{tag}", "material_unit": "Bags",
            "unit_price": "1050",
        })
        crud.append(("EDIT material", rv.status_code, flashes(rv)))
        rec("CRUD edit", "Edit material", rv.status_code in (200, 302), f"{flashes(rv)}")

    if sale_id:
        rv = req("POST", f"/edit_bill/DirectSale/{sale_id}", {
            "client_name": cname, "client_code": ccode, "category": "Credit Customer",
            "product_name[]": [mname], "qty[]": ["2"], "unit_rate[]": ["1150"],
            "paid_amount": "0", "amount": "2300", "manual_bill_no": f"SMK-BILL-{tag}",
            "has_bill": "1",
        })
        crud.append(("EDIT sale", rv.status_code, flashes(rv)))
        rec("CRUD edit", "Edit credit sale", rv.status_code in (200, 302), f"{flashes(rv)}")

    if pay_id:
        rv = req("POST", f"/edit_bill/Payment/{pay_id}", {
            "client_code": ccode, "amount": "550", "method": "Cash",
            "payment_account_id": str(acc_id or ""),
        })
        crud.append(("EDIT payment", rv.status_code, flashes(rv)))
        rec("CRUD edit", "Edit payment", rv.status_code in (200, 302), f"{flashes(rv)}")

    if pb_id:
        rv = req("POST", f"/edit_pending_bill/{pb_id}", {
            "client_code": ccode, "bill_no": f"SMK-PB-{tag}",
            "amount": "275", "reason": "Smoke pending edited",
        })
        crud.append(("EDIT pending bill", rv.status_code, flashes(rv)))
        rec("CRUD edit", "Edit pending bill", rv.status_code in (200, 302), f"{flashes(rv)}")

    # --- deletes (hard) ---
    if pb_id:
        rv = req("POST", f"/delete_pending_bill/{pb_id}")
        crud.append(("DELETE pending bill", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete pending bill", rv.status_code in (200, 302), f"{flashes(rv)}")

    if pay_id:
        rv = req("POST", f"/delete_transaction/Payment/{pay_id}")
        crud.append(("DELETE payment", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete payment", rv.status_code in (200, 302), f"{flashes(rv)}")

    if sale_id:
        rv = req("POST", f"/delete_transaction/DirectSale/{sale_id}")
        crud.append(("DELETE sale", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete credit sale", rv.status_code in (200, 302), f"{flashes(rv)}")

    if bk_id:
        rv = req("POST", f"/delete_transaction/Booking/{bk_id}")
        crud.append(("DELETE booking", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete booking", rv.status_code in (200, 302), f"{flashes(rv)}")

    with app.app_context():
        ret = MaterialReturn.query.filter(MaterialReturn.client_name.ilike(f"%SMOKE%")).order_by(MaterialReturn.id.desc()).first() if hasattr(MaterialReturn, "client_name") else None
        # try common attrs
        if ret is None:
            try:
                ret = MaterialReturn.query.order_by(MaterialReturn.id.desc()).first()
            except Exception:
                ret = None
        ret_id = ret.id if ret else None
        grn = GRN.query.order_by(GRN.id.desc()).first()
        grn_id = grn.id if grn else None

    if ret_id:
        rv = req("POST", f"/delete_transaction/MaterialReturn/{ret_id}")
        crud.append(("DELETE return", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete material return", rv.status_code in (200, 302), f"{flashes(rv)}")

    if acc_id:
        rv = req("POST", f"/accounts/{acc_id}/toggle")
        crud.append(("TOGGLE account", rv.status_code, flashes(rv)))
        rec("CRUD update", "Deactivate smoke account", rv.status_code in (200, 302), f"{flashes(rv)}")

    if cli_id:
        rv = req("POST", f"/delete_client/{cli_id}")
        crud.append(("DELETE client", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete smoke client", rv.status_code in (200, 302), f"{flashes(rv)}")

    if mid:
        rv = req("POST", f"/delete_material/{mid}")
        crud.append(("DELETE material", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete smoke material", rv.status_code in (200, 302), f"{flashes(rv)}")

    if sup_id:
        rv = req("POST", f"/delete_supplier/{sup_id}")
        crud.append(("DELETE supplier", rv.status_code, flashes(rv)))
        rec("CRUD delete", "Delete smoke supplier", rv.status_code in (200, 302), f"{flashes(rv)}")

    # actor flash sample
    with app.app_context():
        n_audit = AuditLog.query.filter(AuditLog.username == "Admin").count()
    rec("audit", "Audit log has Admin rows", n_audit > 0, f"count={n_audit}")

    write_report(results, crud)
    fails = [r for r in results if not r["ok"]]
    print(f"SMOKE done. {len(results)-len(fails)} ok / {len(fails)} fail. Report: {REPORT}")
    if fails:
        raise SystemExit(2)


def write_report(results, crud):
    by = defaultdict(list)
    for r in results:
        by[r["section"]].append(r)
    ok = sum(1 for r in results if r["ok"])
    bad = sum(1 for r in results if not r["ok"])
    lines = [
        "# AMS full smoke test",
        "",
        f"Ran: **{datetime.now().strftime('%Y-%m-%d %H:%M')}** as **Admin**.",
        f"Checks: **{ok} passed**, **{bad} failed**, **{len(results)} total**.",
        "",
        "Isolated records used the `SMOKE …` prefix and were deleted where the app allowed.",
        "",
    ]
    for sec, rows in by.items():
        lines.append(f"## {sec}")
        lines.append("")
        lines.append("| Result | Check | Detail |")
        lines.append("|---|---|---|")
        for r in rows:
            mark = "PASS" if r["ok"] else "FAIL"
            det = str(r["detail"] or "").replace("|", "/")[:220]
            lines.append(f"| {mark} | {r['label']} | {det} |")
        lines.append("")
    if crud:
        lines.append("## Raw create / edit / delete flashes")
        lines.append("")
        for name, code, fl in crud:
            lines.append(f"- **{name}** — HTTP {code} — {fl or '(no flash)'}")
        lines.append("")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
