"""GRN FIFO costing on cash/credit: oldest lot first, lock, freeze rate."""
import os
from datetime import datetime, timedelta

import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "fifo.db"
    os.environ["APP_DB_PATH"] = str(db_file)
    from app import create_app
    from models import db

    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_file}",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "LOGIN_DISABLED": True,
        }
    )
    with application.app_context():
        db.create_all()
        from app.services.schema import _ensure_model_columns
        _ensure_model_columns()
    yield application


def _seed(app):
    from models import db, Material, GRN, GRNItem, Client

    with app.app_context():
        mat = Material(code="M1", name="OPC 53", unit_price=1000, total=0)
        db.session.add(mat)
        cli = Client(code="C1", name="FIFO Client")
        db.session.add(cli)
        g1 = GRN(supplier="S1", auto_bill_no="GRN-1", date_posted=datetime(2026, 1, 1), is_void=False)
        g2 = GRN(supplier="S1", auto_bill_no="GRN-2", date_posted=datetime(2026, 1, 10), is_void=False)
        db.session.add_all([g1, g2])
        db.session.flush()
        i1 = GRNItem(grn_id=g1.id, mat_name="OPC 53", qty=10, price_at_time=1200, is_void=False)
        i2 = GRNItem(grn_id=g2.id, mat_name="OPC 53", qty=10, price_at_time=1350, is_void=False)
        db.session.add_all([i1, i2])
        mat.total = 20
        db.session.commit()
        return {
            "mat_id": mat.id,
            "i1": i1.id,
            "i2": i2.id,
            "g1": g1.id,
            "g2": g2.id,
        }


def test_fifo_splits_sale_and_freezes_rates(app):
    ids = _seed(app)
    from models import db, DirectSale, DirectSaleItem, GRNItem, GRNAllocation
    from app.services.sales_core import (
        _expand_chargeable_items_fifo,
        _apply_grn_allocations_for_sale,
        _grn_has_locked_lots,
        _frozen_cost_for_sale_item,
    )

    with app.app_context():
        lines = _expand_chargeable_items_fifo(
            [{
                "product_name": "OPC 53",
                "qty": 12,
                "price_at_time": 1500,
                "is_booking": False,
            }],
            as_of_dt=datetime(2026, 2, 1).date(),
        )
        assert len(lines) == 2
        assert lines[0]["qty"] == 10
        assert lines[0]["grn_item_id"] == ids["i1"]
        assert lines[0]["cost_rate_at_sale"] == 1200
        assert lines[1]["qty"] == 2
        assert lines[1]["grn_item_id"] == ids["i2"]
        assert lines[1]["cost_rate_at_sale"] == 1350

        sale = DirectSale(client_name="FIFO Client", category="Cash", amount=18000, paid_amount=18000, is_void=False)
        db.session.add(sale)
        db.session.flush()
        recs = []
        for row in lines:
            dsi = DirectSaleItem(
                sale_id=sale.id,
                product_name=row["product_name"],
                qty=row["qty"],
                price_at_time=row["price_at_time"],
                grn_item_id=row["grn_item_id"],
                cost_rate_at_sale=row["cost_rate_at_sale"],
            )
            db.session.add(dsi)
            recs.append((dsi, row))
        db.session.flush()
        _apply_grn_allocations_for_sale(sale, recs)
        db.session.commit()

        locked1 = db.session.get(GRNItem, ids["i1"])
        locked2 = db.session.get(GRNItem, ids["i2"])
        assert locked1.is_locked is True
        assert locked2.is_locked is True
        from models import GRN
        assert _grn_has_locked_lots(db.session.get(GRN, ids["g1"]))
        allocs = GRNAllocation.query.filter_by(sale_id=sale.id, is_void=False).all()
        assert len(allocs) == 2
        items = DirectSaleItem.query.filter_by(sale_id=sale.id).order_by(DirectSaleItem.id).all()
        r0, k0 = _frozen_cost_for_sale_item(items[0])
        r1, k1 = _frozen_cost_for_sale_item(items[1])
        assert k0 and r0 == 1200
        assert k1 and r1 == 1350


def test_locked_grn_cannot_delete(app):
    ids = _seed(app)
    from models import db, DirectSale, DirectSaleItem, GRN
    from app.services.sales_core import _expand_chargeable_items_fifo, _apply_grn_allocations_for_sale
    from app.services.void_rebuild import hard_delete_transaction

    with app.app_context():
        lines = _expand_chargeable_items_fifo(
            [{"product_name": "OPC 53", "qty": 3, "price_at_time": 1500, "is_booking": False}],
            as_of_dt=datetime(2026, 2, 1).date(),
        )
        sale = DirectSale(client_name="FIFO Client", category="Credit Customer", amount=4500, is_void=False)
        db.session.add(sale)
        db.session.flush()
        recs = []
        for row in lines:
            dsi = DirectSaleItem(
                sale_id=sale.id,
                product_name="OPC 53",
                qty=row["qty"],
                price_at_time=1500,
                grn_item_id=row["grn_item_id"],
                cost_rate_at_sale=row["cost_rate_at_sale"],
            )
            db.session.add(dsi)
            recs.append((dsi, row))
        db.session.flush()
        _apply_grn_allocations_for_sale(sale, recs)
        db.session.commit()

        with pytest.raises(ValueError):
            hard_delete_transaction("GRN", ids["g1"])

        hard_delete_transaction("DirectSale", sale.id)
        db.session.commit()
        assert hard_delete_transaction("GRN", ids["g1"]) is True
        db.session.commit()
        assert db.session.get(GRN, ids["g1"]) is None
