"""Normal material return: stock, pending, financial vs booking material ledger."""
import os
from datetime import datetime

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "rtn.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    from app import create_app
    from models import db
    from app.services.schema import _ensure_model_columns

    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test",
        "LOGIN_DISABLED": True,
    })
    with application.app_context():
        db.create_all()
        _ensure_model_columns()
    yield application


def test_normal_return_does_not_touch_booking_remaining(app):
    from models import (
        db, Material, Client, GRN, GRNItem, Booking, BookingItem,
        DirectSale, DirectSaleItem, Entry, PendingBill, Payment, MaterialReturn,
    )
    from app.services.drafts import (
        _client_material_returnable_qty_map,
        _client_booked_material_returnable_qty_map,
    )
    from app.services.sales_core import _expand_chargeable_items_fifo, _apply_grn_allocations_for_sale
    from app.services.void_rebuild import _sync_direct_sale_pending_bill, _apply_settlement_to_pending_bills_for_client

    with app.app_context():
        mat = Material(code="M1", name="OPC 53", unit_price=1500, total=0)
        cli = Client(code="C1", name="Rtn Client")
        db.session.add_all([mat, cli])
        g = GRN(supplier="S", auto_bill_no="GRN-1", date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(g)
        db.session.flush()
        gi = GRNItem(grn_id=g.id, mat_name="OPC 53", qty=100, price_at_time=1200, is_void=False)
        db.session.add(gi)
        mat.total = 100
        bk = Booking(client_name="Rtn Client", amount=24000, paid_amount=0, auto_bill_no="BK-1", is_void=False)
        db.session.add(bk)
        db.session.flush()
        db.session.add(BookingItem(booking_id=bk.id, material_name="OPC 53", qty=20, price_at_time=1200))
        db.session.add(Entry(
            date="2026-01-02", time="10:00:00", type="OUT", material="OPC 53",
            client="Rtn Client", client_code="C1", client_category="Booking Delivery",
            qty=8, bill_no="SL-BK", nimbus_no="Direct Sale", is_void=False,
        ))
        mat.total = 92
        db.session.commit()

        # Booking remaining 20-8=12; booked returnable=8; normal returnable=0
        assert _client_booked_material_returnable_qty_map(cli).get("OPC 53") == 8
        assert _client_material_returnable_qty_map(cli).get("OPC 53", 0) == 0

        # Credit sale 10 bags @ 1500
        sale = DirectSale(
            client_name="Rtn Client", client_code="C1", category="Credit Customer",
            amount=15000, paid_amount=0, auto_bill_no="SL-CR", is_void=False,
        )
        db.session.add(sale)
        db.session.flush()
        lines = _expand_chargeable_items_fifo(
            [{"product_name": "OPC 53", "qty": 10, "price_at_time": 1500, "is_booking": False}],
            as_of_dt=datetime(2026, 2, 1).date(),
        )
        recs = []
        for row in lines:
            dsi = DirectSaleItem(
                sale_id=sale.id, product_name="OPC 53", qty=row["qty"],
                price_at_time=1500, grn_item_id=row["grn_item_id"],
                cost_rate_at_sale=row["cost_rate_at_sale"],
            )
            db.session.add(dsi)
            recs.append((dsi, row))
        db.session.flush()
        _apply_grn_allocations_for_sale(sale, recs)
        db.session.add(Entry(
            date="2026-02-01", time="11:00:00", type="OUT", material="OPC 53",
            client="Rtn Client", client_code="C1", client_category="Credit Customer",
            qty=10, bill_no="SL-CR", nimbus_no="Direct Sale", is_void=False,
        ))
        mat.total = 82
        _sync_direct_sale_pending_bill(sale, "OPC 53")
        db.session.commit()

        assert _client_material_returnable_qty_map(cli).get("OPC 53") == 10
        pending = PendingBill.query.filter_by(client_code="C1", is_void=False, is_paid=False).first()
        assert pending and abs(float(pending.amount) - 15000) < 0.01

        # Normal return 4 @ 1500 = 6000 credit
        from models import MaterialReturnItem
        ret = MaterialReturn(
            client_name="Rtn Client", return_type="normal", amount=6000,
            auto_bill_no="RTN-1", is_void=False, date_posted=datetime(2026, 2, 2),
        )
        db.session.add(ret)
        db.session.flush()
        pay = Payment(client_name="Rtn Client", amount=6000, method="Material Return", auto_bill_no="CP-R", is_void=False)
        db.session.add(pay)
        db.session.flush()
        ret.payment_id = pay.id
        db.session.add(MaterialReturnItem(
            material_return_id=ret.id, material_name="OPC 53", qty=4, unit_rate=1500, rent_rate=0, price_at_time=1500,
        ))
        db.session.add(Entry(
            date="2026-02-02", time="12:00:00", type="IN", material="OPC 53",
            client="Rtn Client", client_code="C1", client_category="Material Return",
            transaction_category="Return", qty=4, bill_no="RTN-1",
            nimbus_no="Material Return", is_void=False,
        ))
        mat.total = 86
        _apply_settlement_to_pending_bills_for_client(cli, 6000, 0)
        db.session.commit()

        assert _client_material_returnable_qty_map(cli).get("OPC 53") == 6
        assert _client_booked_material_returnable_qty_map(cli).get("OPC 53") == 8
        pending = PendingBill.query.filter_by(client_code="C1", is_void=False).first()
        assert pending and abs(float(pending.amount) - 9000) < 0.01

        # Financial: debit 15000 + booking 24000, credit 6000
        from app.services.finance_clients import _compute_client_financial_summary
        summary = _compute_client_financial_summary(cli)
        # 24000 booking + 15000 credit − 6000 return = 33000
        assert abs(float(summary["balance"]) - 33000) < 0.5
