"""Read-only live smoke test: boot the app against the migrated DB, log in,
and GET every page. Reports any 500 / traceback / BuildError."""
from __future__ import annotations

import os
import re
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

os.environ.setdefault("ALLOW_EMPTY_DB", "1")
os.environ.setdefault("ALLOW_DB_DROP", "1")

from app import create_app
from models import db, Client, Material, Account, Supplier

GET_PAGES = [
    "/", "/clients", "/materials", "/suppliers", "/delivery_persons",
    "/bookings", "/payments", "/direct_sales", "/material_returns",
    "/pending_bills", "/grn", "/dispatching", "/tracking",
    "/ledger", "/decision_ledger", "/financial_details",
    "/cash_flow", "/cash_flow_differences", "/profit_reports",
    "/unpaid_transactions", "/mixed_transactions",
    "/daily_transactions", "/delivery_rents",
    "/notifications", "/notifications/upcoming",
    "/settings", "/settings/activity",
    "/import_export/", "/import_export/history", "/import_export/uploads",
    "/inventory/stock_summary", "/inventory/daily_transactions",
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
    "/system_report", "/void_audit",
    "/api/notifications/due", "/api/client_next_code", "/api/material_next_code",
    "/api/clients/search", "/api/ui/theme",
]


def main():
    app = create_app()
    c = app.test_client()
    problems = []

    # login
    rv = c.post("/login", data={"username": "Admin", "password": "Admin@fbm12345",
                                "remember_me": "1"}, follow_redirects=False)
    if rv.status_code not in (302, 303):
        problems.append(f"LOGIN FAILED: HTTP {rv.status_code}")
        print("LOGIN FAILED:", rv.status_code, rv.get_data(as_text=True)[:300])
    else:
        print("LOGIN OK (Admin)")

    for path in GET_PAGES:
        rv = c.get(path, follow_redirects=True)
        html = rv.get_data(as_text=True)
        bad = rv.status_code not in (200, 302) or any(
            x in html for x in (
                "Traceback (most recent call last)", "BuildError",
                "jinja2.exceptions", "Internal Server Error",
            )
        )
        if bad:
            problems.append(f"GET {path}: HTTP {rv.status_code}")
            print(f"  FAIL {path} -> {rv.status_code}")
        else:
            print(f"  ok   {path} -> {rv.status_code}")

    # dynamic pages with live IDs
    with app.app_context():
        cli = Client.query.filter(Client.is_active == True).first()
        mat = Material.query.first()
        acc = Account.query.filter(Account.is_active == True).first()
        sup = Supplier.query.first()
    extra = []
    if cli:
        extra += [f"/ledger/{cli.id}", f"/client_ledger/{cli.id}", f"/financial_ledger/{cli.id}"]
    if mat:
        extra.append(f"/material_ledger/{mat.id}")
    if acc:
        extra += [f"/accounts/ledger/{acc.id}"]
    if sup:
        extra += [f"/supplier_ledger/{sup.id}"]
    for path in extra:
        rv = c.get(path, follow_redirects=True)
        html = rv.get_data(as_text=True)
        bad = rv.status_code not in (200, 302) or "Traceback (most recent call last)" in html
        print(f"  {'ok  ' if not bad else 'FAIL'} {path} -> {rv.status_code}")
        if bad:
            problems.append(f"GET {path}: HTTP {rv.status_code}")

    # key data sanity
    with app.app_context():
        counts = {
            "clients": Client.query.count(),
            "materials": Material.query.count(),
            "accounts": Account.query.count(),
            "suppliers": Supplier.query.count(),
        }
    print("COUNTS:", counts)

    print("=" * 60)
    if problems:
        print(f"SMOKE FAILURES: {len(problems)}")
        for p in problems:
            print("  -", p)
    else:
        print("SMOKE PASS — all pages load, no 500s")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
