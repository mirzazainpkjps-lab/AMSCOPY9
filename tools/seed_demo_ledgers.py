"""Seed a demo DB exercising the sales / ledger / PDF paths for live preview."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "/tmp/ams_demo_ledgers.db"
if os.path.exists(DB):
    os.remove(DB)

os.environ["APP_DB_PATH"] = DB
os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["AMS_SCHEMA_VERSION"] = "v44"
os.environ["BACKUP_EMBEDDED_SCHEDULER"] = "0"
os.environ["DEFAULT_ADMIN_USER"] = "Admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "Admin@fbm12345"

from app import create_app

app = create_app({"SECRET_KEY": "demo"})
c = app.test_client()
c.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"})
with c.session_transaction() as s:
    tok = s.get("_csrf_token")


def post(url, **data):
    data["_csrf_token"] = tok
    r = c.post(url, data=data, follow_redirects=True)
    return r


from models import db, Material, Client, Account, Entry

post("/add_material", material_name="OPC CEMENT 50KG", material_unit="Bags")
post("/add_material", material_name="STEEL REBAR 12MM", material_unit="Tons")
post("/add_client", name="AL RAHMAN TRADERS", code="CL-1001", category="General",
     opening_balance="0")
post("/add_client", name="BILAL BUILDERS", code="CL-1002", category="General",
     opening_balance="0")
post("/accounts/accounts/add", name="MAIN CASH", class_category="Assets",
     class_subcategory="Cash", class_account_type="Main Cash",
     account_status="active", opening_amount="0", opening_position="debit",
     opening_effective_date="2026-01-01")

with app.app_context():
    m1 = Material.query.filter_by(name="OPC CEMENT 50KG").first()
    m2 = Material.query.filter_by(name="STEEL REBAR 12MM").first()
    a1 = Client.query.filter_by(code="CL-1001").first()
    acc = Account.query.filter(Account.name.like("%MAIN CASH%")).first()
    mid1, mid2, aid, acc_id = m1.id, m2.id, a1.id, acc.id
    db.session.add(Entry(date="2026-01-05", time="09:00:00", type="IN",
                         material=m1.name, qty=5000, bill_no="MB NO.9001",
                         client_category="Stock In"))
    db.session.add(Entry(date="2026-01-05", time="09:30:00", type="IN",
                         material=m2.name, qty=200, bill_no="MB NO.9002",
                         client_category="Stock In"))
    db.session.commit()

# Booking 1: 1000 bags OPC @ 1250 = 1,250,000 ; paid 500,000
post("/add_booking", client_code="CL-1001", **{
    "material_name[]": "OPC CEMENT 50KG", "material_id[]": str(mid1),
    "qty[]": "1000", "unit_rate[]": "1250", "amount": "1250000",
    "paid_amount": "500000", "manual_bill_no": "MB NO.7001",
    "date": "2026-01-10", "payment_account_id": str(acc_id),
    "payment_method": "Cash",
})

# Dispatch 300 bags against that booking
post("/add_record", date="2026-01-12", client="AL RAHMAN TRADERS", type="OUT",
     material="OPC CEMENT 50KG", material_id=str(mid1), qty="300",
     driver_name="IMRAN DRIVER", bill_no="MB NO.7001")

# Second dispatch 250 bags
post("/add_record", date="2026-01-18", client="AL RAHMAN TRADERS", type="OUT",
     material="OPC CEMENT 50KG", material_id=str(mid1), qty="250",
     driver_name="IMRAN DRIVER", bill_no="MB NO.7001")

# Payment 400,000
post("/add_payment", client_code="CL-1001", amount="400000",
     payment_type="Receipt", method="Cash", payment_account_id=str(acc_id),
     manual_bill_no="MB NO.7002", date="2026-01-20")

# Booking 2 for the second client
post("/add_booking", client_code="CL-1002", **{
    "material_name[]": "STEEL REBAR 12MM", "material_id[]": str(mid2),
    "qty[]": "40", "unit_rate[]": "285000", "amount": "11400000",
    "paid_amount": "2000000", "manual_bill_no": "MB NO.7003",
    "date": "2026-01-14", "payment_account_id": str(acc_id),
    "payment_method": "Cash",
})

with app.app_context():
    print("SEEDED:",
          "clients=", Client.query.count(),
          "materials=", Material.query.count(),
          "entries=", Entry.query.count())
print("DB:", DB)
