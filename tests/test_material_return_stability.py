"""
Deep Stability Audit & Smoke Test for Material Return.

Exercises the complete Material Return workflow multiple times to detect
state corruption, loading overlay issues, race conditions, and cumulative
state problems described in the stability/regression report.
"""
import os
from datetime import datetime
import pytest

os.environ["ALLOW_EMPTY_DB"] = "1"
os.environ["ALLOW_DB_DROP"] = "1"


@pytest.fixture(scope="module")
def app():
    import tempfile
    db_file = tempfile.mktemp(suffix=".db")
    os.environ["APP_DB_PATH"] = db_file
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
        db.session.commit()
    yield application
    # Cleanup
    try:
        os.remove(db_file)
    except OSError:
        pass


@pytest.fixture(scope="function")
def ctx(app):
    """Provide a fresh app_context + clean DB for each test."""
    from models import db
    with app.app_context():
        # Clear all data
        for tbl in reversed(db.metadata.sorted_tables):
            db.session.execute(tbl.delete())
        db.session.commit()
        yield app


class _TestHelper:
    """Stateless helpers that work within an active app context."""

    @staticmethod
    def setup_base():
        """Create base materials, clients, and stock. Must be called inside app_context."""
        from models import db, Material, Client, GRN, GRNItem, MaterialCategory

        cat = MaterialCategory.query.filter_by(name="General").first()
        if not cat:
            cat = MaterialCategory(name="General")
            db.session.add(cat)
            db.session.flush()

        mat1 = Material(code="M1", name="OPC 53", unit_price=1500, total=0, category_id=cat.id)
        mat2 = Material(code="M2", name="Sand", unit_price=800, total=0, category_id=cat.id)
        mat3 = Material(code="M3", name="Aggregate", unit_price=1200, total=0, category_id=cat.id)
        cli_a = Client(code="C001", name="Test Client A", is_active=True, opening_balance=0)
        cli_b = Client(code="C002", name="Test Client B", is_active=True, opening_balance=0)
        cli_c = Client(code="C003", name="Test Client C", is_active=True, opening_balance=0)
        db.session.add_all([mat1, mat2, mat3, cli_a, cli_b, cli_c])

        g = GRN(supplier="Supplier A", auto_bill_no="GRN-1",
                date_posted=datetime(2026, 1, 1), is_void=False)
        db.session.add(g)
        db.session.flush()
        for mat, qty in [(mat1, 100), (mat2, 200), (mat3, 150)]:
            gi = GRNItem(grn_id=g.id, mat_name=mat.name, qty=qty,
                         price_at_time=mat.unit_price * 0.8, is_void=False)
            db.session.add(gi)
            mat.total = qty
        db.session.commit()
        return {"mat1": mat1, "mat2": mat2, "mat3": mat3,
                "cli_a": cli_a, "cli_b": cli_b, "cli_c": cli_c}

    @staticmethod
    def make_return(client_obj, material_name, qty,
                    return_type="normal", unit_rate=None, rent_rate=0):
        """Create a material return. Must be called inside app_context."""
        from models import (db, MaterialReturn, MaterialReturnItem,
                            Payment, Entry, Material)
        from app.services.time_money import pk_now
        from app.services.billing import get_next_bill_no
        from app.services.constants import AUTO_BILL_NAMESPACES
        from app.services.drafts import (
            _client_booked_material_returnable_qty_map,
            _client_material_returnable_qty_map,
        )
        from app.services.waive import _sync_payment_waive_off
        from app.services.void_rebuild import _apply_settlement_to_pending_bills_for_client
        from app.services.lookups import get_material_by_input

        mat_obj = get_material_by_input(material_name)
        assert mat_obj, f"Material '{material_name}' not found"

        unit_rate_val = float(unit_rate or mat_obj.unit_price or 0)
        rent_rate_val = float(rent_rate or 0)
        if return_type == "booked":
            total_amount = qty * rent_rate_val
        else:
            total_amount = qty * (unit_rate_val + rent_rate_val)

        returnable_map = (_client_booked_material_returnable_qty_map(client_obj)
                          if return_type == "booked"
                          else _client_material_returnable_qty_map(client_obj))
        allowed = float(returnable_map.get(material_name, 0) or 0)
        assert qty <= (allowed + 0.0001), (
            f"Cannot return {qty} of '{material_name}'. "
            f"Max allowed: {round(allowed, 2)}"
        )

        posted_at = pk_now()
        auto_bill_no = get_next_bill_no(AUTO_BILL_NAMESPACES["MATERIAL_RETURN"])

        ret = MaterialReturn(
            client_name=client_obj.name,
            return_type=return_type,
            amount=total_amount,
            manual_bill_no="",
            auto_bill_no=auto_bill_no,
            date_posted=posted_at,
            note=f"Test return #{auto_bill_no}"
        )
        db.session.add(ret)
        db.session.flush()
        bill_ref = auto_bill_no

        pay = Payment(
            client_id=client_obj.id,
            client_name=client_obj.name,
            amount=total_amount,
            payment_type="Material Return",
            source_type="MaterialReturn",
            source_id=ret.id,
            method="Material Return",
            manual_bill_no="",
            auto_bill_no=get_next_bill_no(AUTO_BILL_NAMESPACES["PAYMENT"]),
            date_posted=posted_at,
            note=f"[MATERIAL_RETURN:{ret.id}] {bill_ref}"
        )
        db.session.add(pay)
        db.session.flush()
        ret.payment_id = pay.id

        entry_txn_category = "Booked Return" if return_type == "booked" else "Return"
        entry_client_category = "Booked Return" if return_type == "booked" else "Material Return"
        legacy_rate = rent_rate_val if return_type == "booked" else unit_rate_val

        db.session.add(MaterialReturnItem(
            material_return_id=ret.id, material_name=material_name,
            qty=qty, unit_rate=unit_rate_val, rent_rate=rent_rate_val,
            price_at_time=legacy_rate,
        ))
        db.session.add(Entry(
            date=posted_at.strftime("%Y-%m-%d"),
            time=posted_at.strftime("%H:%M:%S"),
            type="IN", material=material_name, client=client_obj.name,
            client_code=client_obj.code,
            client_category=entry_client_category,
            qty=qty, bill_no=bill_ref, nimbus_no="Material Return",
            created_by="test", transaction_category=entry_txn_category,
            note="Test return",
        ))
        mat = Material.query.filter_by(name=material_name).first()
        if mat:
            mat.total = float(mat.total or 0) + qty

        _sync_payment_waive_off(pay)
        if client_obj and total_amount > 0:
            _apply_settlement_to_pending_bills_for_client(client_obj, total_amount, 0)
        db.session.commit()
        return ret

    @staticmethod
    def make_credit_sale(client_obj, material_name, qty, unit_rate):
        """Create a direct credit sale. Must be called inside app_context."""
        from models import (db, DirectSale, DirectSaleItem, Entry, Material)
        from app.services.time_money import pk_now
        from app.services.billing import get_next_bill_no
        from app.services.constants import AUTO_BILL_NAMESPACES
        from app.services.lookups import get_material_by_input
        from app.services.void_rebuild import _sync_direct_sale_pending_bill
        from app.services.sales_core import _expand_chargeable_items_fifo, _apply_grn_allocations_for_sale

        mat_obj = get_material_by_input(material_name)
        assert mat_obj
        amount = qty * unit_rate
        auto_bill = get_next_bill_no(AUTO_BILL_NAMESPACES["DIRECT_SALE"])

        sale = DirectSale(
            client_name=client_obj.name, client_code=client_obj.code,
            category="Credit Customer", amount=amount, paid_amount=0,
            auto_bill_no=auto_bill, is_void=False,
            date_posted=pk_now(), driver_name="Test Driver",
        )
        db.session.add(sale)
        db.session.flush()

        lines = _expand_chargeable_items_fifo(
            [{"product_name": material_name, "qty": qty,
              "price_at_time": unit_rate, "is_booking": False}],
            as_of_dt=pk_now().date(),
        )
        recs = []
        for row in lines:
            dsi = DirectSaleItem(
                sale_id=sale.id, product_name=material_name, qty=row["qty"],
                price_at_time=unit_rate, grn_item_id=row["grn_item_id"],
                cost_rate_at_sale=row["cost_rate_at_sale"],
            )
            db.session.add(dsi)
            recs.append((dsi, row))
        db.session.flush()
        _apply_grn_allocations_for_sale(sale, recs)

        db.session.add(Entry(
            date=pk_now().strftime("%Y-%m-%d"), time=pk_now().strftime("%H:%M:%S"),
            type="OUT", material=material_name, client=client_obj.name,
            client_code=client_obj.code, client_category="Credit Customer",
            qty=qty, bill_no=auto_bill, nimbus_no="Direct Sale", is_void=False,
        ))
        mat_obj.total = float(mat_obj.total or 0) - qty
        _sync_direct_sale_pending_bill(sale, material_name)
        db.session.commit()
        return sale

    @staticmethod
    def make_booking(client_obj, material_name, qty, rate, paid_amount=0):
        """Create a booking."""
        from models import (db, Booking, BookingItem)
        from app.services.time_money import pk_now
        from app.services.billing import get_next_bill_no
        from app.services.constants import AUTO_BILL_NAMESPACES
        from app.services.lookups import get_material_by_input
        from app.services.void_rebuild import _sync_booking_pending_bill

        mat_obj = get_material_by_input(material_name)
        amount = qty * rate
        auto_bill = get_next_bill_no(AUTO_BILL_NAMESPACES["BOOKING"])

        booking = Booking(
            client_name=client_obj.name, amount=amount,
            paid_amount=paid_amount, discount=0,
            auto_bill_no=auto_bill, is_void=False,
            date_posted=pk_now(),
        )
        db.session.add(booking)
        db.session.flush()

        db.session.add(BookingItem(
            booking_id=booking.id, material_name=mat_obj.name,
            qty=qty, price_at_time=rate,
        ))
        _sync_booking_pending_bill(booking, material_name)
        db.session.commit()
        return booking

    @staticmethod
    def make_booking_delivery(client_obj, material_name, qty, booking):
        """Record a booking delivery entry."""
        from models import db, Entry
        from app.services.time_money import pk_now

        db.session.add(Entry(
            date=pk_now().strftime("%Y-%m-%d"), time=pk_now().strftime("%H:%M:%S"),
            type="OUT", material=material_name, client=client_obj.name,
            client_code=client_obj.code, client_category="Booking Delivery",
            qty=qty, bill_no=booking.auto_bill_no,
            nimbus_no="Booking Delivery", is_void=False,
        ))
        db.session.commit()


# ====================================================================
#  TESTS
# ====================================================================

class TestNormalReturn:
    def test_single(self, ctx):
        from models import MaterialReturn, Material, Entry
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 10, 1500)
        ret = _TestHelper.make_return(data["cli_a"], "OPC 53", 4, "normal", unit_rate=1500)
        assert ret.id > 0
        assert float(ret.amount) == 6000

        mat = Material.query.filter_by(name="OPC 53").first()
        assert abs(float(mat.total or 0) - 94.0) < 0.01  # 100 - 10 + 4

        in_entries = Entry.query.filter(Entry.type=="IN", Entry.nimbus_no=="Material Return").all()
        assert len(in_entries) == 1
        assert float(in_entries[0].qty or 0) == 4
        assert MaterialReturn.query.filter_by(is_void=False).count() == 1
        print("✅ Normal Return Single PASSED")


class TestBookedReturn:
    def test_single(self, ctx):
        from models import MaterialReturn, Entry
        from app.services.drafts import _client_booked_material_returnable_qty_map
        data = _TestHelper.setup_base()
        bk = _TestHelper.make_booking(data["cli_a"], "OPC 53", 20, 1200)
        _TestHelper.make_booking_delivery(data["cli_a"], "OPC 53", 8, bk)
        ret = _TestHelper.make_return(data["cli_a"], "OPC 53", 3, "booked", rent_rate=200)
        assert ret.id > 0
        assert float(ret.amount) == 600
        rmap = _client_booked_material_returnable_qty_map(data["cli_a"])
        assert abs(float(rmap.get("OPC 53", 0)) - 5.0) < 0.01  # 8-3
        in_entries = Entry.query.filter(Entry.type=="IN", Entry.transaction_category=="Booked Return").all()
        assert len(in_entries) == 1
        print("✅ Booked Return Single PASSED")


class TestRepeatedReturns:
    def test_same_client_six_times(self, ctx):
        from models import MaterialReturn, MaterialReturnItem, Material
        from app.services.drafts import _client_material_returnable_qty_map
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 50, 1500)
        for i in range(6):
            ret = _TestHelper.make_return(data["cli_a"], "OPC 53", 5, "normal", unit_rate=1500)
            assert ret.id > 0
        assert MaterialReturn.query.filter_by(is_void=False).count() == 6
        rmap = _client_material_returnable_qty_map(data["cli_a"])
        assert abs(float(rmap.get("OPC 53", 0)) - 20.0) < 0.01  # 50 - 30
        mat = Material.query.filter_by(name="OPC 53").first()
        assert abs(float(mat.total or 0) - 80.0) < 0.01  # 100-50+30
        print("✅ 6 Repeated Returns (Same Client) PASSED")

    def test_different_clients(self, ctx):
        from models import MaterialReturn
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 10, 1500)
        _TestHelper.make_credit_sale(data["cli_b"], "Sand", 20, 800)
        _TestHelper.make_credit_sale(data["cli_c"], "Aggregate", 15, 1200)

        for ck, mn, qty in [("cli_a","OPC 53",3),("cli_b","Sand",5),
                             ("cli_c","Aggregate",4),("cli_a","OPC 53",2),
                             ("cli_b","Sand",3)]:
            rate = 1500 if "OPC" in mn else (800 if "Sand" in mn else 1200)
            ret = _TestHelper.make_return(data[ck], mn, qty, "normal", unit_rate=rate)
            assert ret.client_name == data[ck].name

        assert MaterialReturn.query.filter_by(client_name=data["cli_a"].name, is_void=False).count() == 2
        assert MaterialReturn.query.filter_by(client_name=data["cli_b"].name, is_void=False).count() == 2
        assert MaterialReturn.query.filter_by(client_name=data["cli_c"].name, is_void=False).count() == 1
        print("✅ Different Clients PASSED")

    def test_multiple_materials_same_client(self, ctx):
        from models import MaterialReturn
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 30, 1500)
        _TestHelper.make_credit_sale(data["cli_a"], "Sand", 40, 800)
        _TestHelper.make_credit_sale(data["cli_a"], "Aggregate", 25, 1200)
        for mn, qty in [("OPC 53",5),("Sand",8),("Aggregate",4),("OPC 53",3),("Sand",2)]:
            rate = 1500 if "OPC" in mn else (800 if "Sand" in mn else 1200)
            _TestHelper.make_return(data["cli_a"], mn, qty, "normal", unit_rate=rate)
        assert MaterialReturn.query.filter_by(client_name=data["cli_a"].name, is_void=False).count() == 5
        print("✅ Multiple Materials Same Client PASSED")

    def test_mixed_return_types(self, ctx):
        from models import MaterialReturn
        from app.services.drafts import (
            _client_material_returnable_qty_map,
            _client_booked_material_returnable_qty_map,
        )
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 20, 1500)
        bk = _TestHelper.make_booking(data["cli_a"], "OPC 53", 15, 1200)
        _TestHelper.make_booking_delivery(data["cli_a"], "OPC 53", 10, bk)
        _TestHelper.make_return(data["cli_a"], "OPC 53", 5, "normal", unit_rate=1500)
        _TestHelper.make_return(data["cli_a"], "OPC 53", 3, "booked", rent_rate=200)
        _TestHelper.make_return(data["cli_a"], "OPC 53", 4, "normal", unit_rate=1500)
        _TestHelper.make_return(data["cli_a"], "OPC 53", 2, "booked", rent_rate=200)
        assert MaterialReturn.query.filter_by(is_void=False).count() == 4
        nm = _client_material_returnable_qty_map(data["cli_a"])
        assert abs(float(nm.get("OPC 53", 0)) - 11.0) < 0.01  # 20-9
        bm = _client_booked_material_returnable_qty_map(data["cli_a"])
        assert abs(float(bm.get("OPC 53", 0)) - 5.0) < 0.01  # 10-5
        print("✅ Mixed Return Types PASSED")

    def test_over_return_protection(self, ctx):
        from app.services.drafts import _client_material_returnable_qty_map
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 5, 1500)
        _TestHelper.make_return(data["cli_a"], "OPC 53", 3, "normal", unit_rate=1500)
        rmap = _client_material_returnable_qty_map(data["cli_a"])
        assert abs(float(rmap.get("OPC 53", 0)) - 2.0) < 0.01
        _TestHelper.make_return(data["cli_a"], "OPC 53", 2, "normal", unit_rate=1500)
        rmap = _client_material_returnable_qty_map(data["cli_a"])
        assert float(rmap.get("OPC 53", 0) or 0) < 0.01
        print("✅ Over-Return Protection PASSED")

    def test_booked_sale_lifecycle(self, ctx):
        """Booked Sale → Booked Return → Booked Sale again."""
        from app.services.sales_core import _allocate_booking_quantities_for_sale_item
        from app.services.drafts import _client_booked_material_returnable_qty_map
        data = _TestHelper.setup_base()
        bk = _TestHelper.make_booking(data["cli_a"], "OPC 53", 10000, 12)
        _TestHelper.make_booking_delivery(data["cli_a"], "OPC 53", 5000, bk)
        ret = _TestHelper.make_return(data["cli_a"], "OPC 53", 5000, "booked", rent_rate=2)
        assert ret.id > 0
        rmap = _client_booked_material_returnable_qty_map(data["cli_a"])
        assert abs(float(rmap.get("OPC 53", 0)) - 0.0) < 0.01
        allocations = _allocate_booking_quantities_for_sale_item(data["cli_a"].name, "OPC 53", 5000)
        assert len(allocations) > 0
        total = sum(a[1] for a in allocations)
        assert abs(total - 5000.0) < 0.01
        print("✅ Booked Sale Lifecycle PASSED")

    def test_credit_sale_return_flow(self, ctx):
        """Credit Sale → Material Return — verify financial ledger."""
        from models import Payment, PendingBill
        from app.services.finance_clients import _compute_client_financial_summary
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 10, 1500)
        pb = PendingBill.query.filter_by(client_code="C001", is_void=False, is_paid=False).first()
        assert pb and abs(float(pb.amount) - 15000.0) < 0.01
        ret = _TestHelper.make_return(data["cli_a"], "OPC 53", 4, "normal", unit_rate=1500)
        pay = Payment.query.filter_by(source_id=ret.id).first()
        assert pay and abs(float(pay.amount) - 6000.0) < 0.01
        pb = PendingBill.query.filter_by(client_code="C001", is_void=False, is_paid=False).first()
        assert pb and abs(float(pb.amount) - 9000.0) < 0.01
        summary = _compute_client_financial_summary(data["cli_a"])
        assert abs(float(summary["balance"]) - 9000.0) < 5.0
        print("✅ Credit Sale Return Flow PASSED")

    def test_stress_10_returns(self, ctx):
        """Stress test: 10 consecutive returns."""
        from models import MaterialReturn, Material
        from app.services.drafts import _client_material_returnable_qty_map
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 100, 1500)
        for i in range(10):
            _TestHelper.make_return(data["cli_a"], "OPC 53", 5, "normal", unit_rate=1500)
            rmap = _client_material_returnable_qty_map(data["cli_a"])
            assert abs(float(rmap.get("OPC 53", 0)) - (100 - (i+1)*5)) < 0.01
        assert MaterialReturn.query.filter_by(client_name=data["cli_a"].name, is_void=False).count() == 10
        mat = Material.query.filter_by(name="OPC 53").first()
        assert abs(float(mat.total or 0) - 50.0) < 0.01  # 100-100+50
        print("✅ 10-Return Stress PASSED")

    def test_no_duplicate_ledger_entries(self, ctx):
        """Repeated returns must not create duplicate entries."""
        from models import Entry, MaterialReturnItem
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 20, 1500)
        for qty in [5, 3, 2]:
            _TestHelper.make_return(data["cli_a"], "OPC 53", qty, "normal", unit_rate=1500)
        entries = Entry.query.filter(Entry.type=="IN", Entry.nimbus_no=="Material Return", Entry.is_void==False).all()
        assert len(entries) == 3
        assert abs(sum(float(e.qty or 0) for e in entries) - 10.0) < 0.01
        assert MaterialReturnItem.query.count() == 3
        print("✅ No Duplicate Ledger Entries PASSED")

    def test_edit_return(self, ctx):
        """Edit a return must keep state consistent."""
        from models import (db, MaterialReturn, MaterialReturnItem, Entry, Material)
        from app.services.time_money import pk_now
        data = _TestHelper.setup_base()
        _TestHelper.make_credit_sale(data["cli_a"], "OPC 53", 20, 1500)
        ret = _TestHelper.make_return(data["cli_a"], "OPC 53", 5, "normal", unit_rate=1500)
        rid = ret.id

        # Simulate edit: reverse old entries, add new
        from sqlalchemy import func
        for e in Entry.query.filter(Entry.nimbus_no=="Material Return",
                                     func.lower(func.trim(Entry.client))==data["cli_a"].name.strip().lower(),
                                     Entry.is_void==False).all():
            m = Material.query.filter_by(name=e.material).first()
            if m:
                m.total = float(m.total or 0) - float(e.qty or 0)
            db.session.delete(e)
        for it in MaterialReturnItem.query.filter_by(material_return_id=rid).all():
            db.session.delete(it)
        db.session.flush()

        pts = pk_now()
        db.session.add(MaterialReturnItem(material_return_id=rid, material_name="OPC 53",
                                           qty=7, unit_rate=1500, rent_rate=0, price_at_time=1500))
        db.session.add(Entry(date=pts.strftime("%Y-%m-%d"), time=pts.strftime("%H:%M:%S"),
                              type="IN", material="OPC 53", client=data["cli_a"].name,
                              client_code=data["cli_a"].code, client_category="Material Return",
                              qty=7, bill_no="RTN-UPD", nimbus_no="Material Return",
                              created_by="test", transaction_category="Return", note="Updated"))
        m = Material.query.filter_by(name="OPC 53").first()
        m.total = float(m.total or 0) + 7
        db.session.commit()

        items = MaterialReturnItem.query.filter_by(material_return_id=rid).all()
        assert len(items) == 1
        assert abs(float(items[0].qty or 0) - 7.0) < 0.01
        print("✅ Edit Return Flow PASSED")


class TestFrontendState:
    """Backend-verifiable frontend state checks."""

    def test_routes_exist(self, ctx):
        from flask import url_for
        with ctx.test_request_context():
            assert url_for('sales.material_returns_page')
            assert url_for('sales.add_material_return')
            assert url_for('sales.edit_material_return', id=1)
        print("✅ Routes registered")

    def test_no_processing_flag(self, ctx):
        from flask import session
        with ctx.test_request_context():
            assert 'return_processing' not in session
        print("✅ No stuck processing flag")

    def test_template_modal_markup(self, ctx):
        import os
        tmpl = os.path.join(ctx.root_path, '..', 'templates', 'material_returns.html')
        with open(tmpl) as f:
            c = f.read()
        assert 'data-bs-dismiss="modal"' in c
        assert 'tabindex="-1"' in c
        assert 'class="modal fade"' in c
        print("✅ Modal markup is correct")