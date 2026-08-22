"""Build a small demo database (fake data) to preview the fixed supplier ledger."""
import os, sys
os.environ["APP_DB_PATH"] = "/tmp/ams_demo.db"
os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["DEFAULT_ADMIN_USER"] = "Admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "Admin@fbm12345"

from datetime import datetime, timedelta

if os.path.exists("/tmp/ams_demo.db"):
    os.remove("/tmp/ams_demo.db")

from app import create_app
from models import db
app = create_app({"SECRET_KEY": "demo", "WTF_CSRF_ENABLED": False})
with app.app_context():
    db.create_all()
    from app.services.schema import _ensure_model_columns, _ensure_performance_indexes, _ensure_default_admin
    _ensure_model_columns()
    _ensure_performance_indexes()
    _ensure_default_admin()

    from models import Account, Supplier, GRN, GRNItem, SupplierPayment, Material
    from app.services.billing import AUTO_BILL_NAMESPACES, get_next_bill_no

    acc = Account(name="FBM CASH", category="cash", account_type="company",
                  balance=50_000_000, is_active=True)
    bank = Account(name="ALFALAH 37737", category="bank", account_type="company",
                   bank_name="ALFALAH", account_number="37737",
                   balance=50_000_000, is_active=True)
    db.session.add_all([acc, bank])
    mat = Material(code="M-1", name="PIONEER CEMENT", unit_price=1360, total=0, is_active=True)
    db.session.add(mat)
    sup = Supplier(name="ZIA DEMO TRADERS", is_active=True, opening_balance=1_430_000,
                   opening_balance_date=datetime(2026, 6, 30))
    db.session.add(sup)
    db.session.flush()

    # Three GRNs
    for i, (d, qty) in enumerate([(datetime(2026, 7, 2), 300), (datetime(2026, 7, 7), 600), (datetime(2026, 7, 11), 300)]):
        g = GRN(supplier=sup.name, supplier_id=sup.id, auto_bill_no=get_next_bill_no(AUTO_BILL_NAMESPACES["GRN"]),
                date_posted=d, is_void=False, paid_amount=0)
        db.session.add(g); db.session.flush()
        db.session.add(GRNItem(grn_id=g.id, mat_name="PIONEER CEMENT", qty=qty, price_at_time=1360))

    # Payments with REAL tracking numbers (like after the backfill)
    for d, amt, method, a in [
        (datetime(2026, 7, 5, 15, 57), 400000, "Bank Transfer", bank),
        (datetime(2026, 7, 7, 16, 0), 300000, "Bank Transfer", bank),
        (datetime(2026, 7, 24, 15, 4), 700000, "Bank Transfer", bank),
    ]:
        db.session.add(SupplierPayment(
            supplier_id=sup.id, amount=amt, method=method, payment_type="Payment",
            date_posted=d, payment_account_id=a.id,
            bank_name=a.bank_name or "", account_name=a.name, account_no=a.account_number or "",
            auto_bill_no=get_next_bill_no(AUTO_BILL_NAMESPACES["SUPPLIER_PAYMENT"]),
            is_void=False,
        ))
    db.session.commit()
    print("demo DB ready: supplier id", sup.id)
