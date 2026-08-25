"""Tests for granular wipe / backup / restore / import-export.

Covers the regression where "wipe all data" left Financial Accounts (and
other modules) behind, plus the new granular per-module backup & restore:

* Full wipe (Check All) now erases every yard table — accounts, account
  transactions, cash flow, cash drawer, rentals, driver payments, drafts.
* Granular wipe options exist for every dataset shown in the settings UI.
* Module export writes exactly the selected tables; module restore imports
  exactly the selected tables and leaves the other modules untouched.
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime

import pandas as pd
import pytest

from models import (
    db,
    Account,
    AccountCategory,
    AccountReconciliation,
    AccountTransaction,
    BillCounter,
    Booking,
    BookingItem,
    CashFlowCategory,
    CashFlowDifferenceAdjustment,
    CashFlowEntry,
    CashFlowEntryAudit,
    CashFlowParty,
    CashFlowReconciliationAudit,
    CashFlowSubcategory,
    Client,
    Delivery,
    DeliveryItem,
    DeliveryPerson,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    DirectSaleDraft,
    DirectSaleItem,
    Entry,
    FbmCashDrawerCategory,
    FbmCashDrawerEntry,
    FBMClient,
    FBMRental,
    FBMRentalItem,
    FollowUpContact,
    FollowUpReminder,
    GRN,
    GRNItem,
    Invoice,
    Material,
    MaterialCategory,
    MaterialReturn,
    MaterialReturnItem,
    Payment,
    PendingBill,
    ReconBasket,
    SaleDeliveryPerson,
    StaffEmail,
    Supplier,
    SupplierPayment,
    User,
    WaiveOff,
)

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}

# Tables that must survive ANY wipe (identity, settings, audit, ops metadata).
PROTECTED_TABLES = {
    "user",
    "user_login_session",
    "settings",
    "schema_version",
    "audit_log",
    "accounting_audit_log",
    "future_account_audit_log",
    "system_lock",
    "root_recovery_code",
    "root_backup_settings",
    "root_backup_email_history",
    "import_upload",
    "import_job",
    "import_history_entry",
    "tenant_wipe_backup_history",
}


def login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]
    return resp


def business_clients():
    """Client master rows excluding the system-maintained OPEN-KHATA row."""
    return [
        c for c in Client.query.all()
        if (c.code or "").strip().upper() != "OPEN-KHATA"
    ]


def seed_all_modules():
    """Populate every wipeable module with one recognizable row each."""
    now = datetime(2026, 8, 24, 10, 0, 0)

    # --- Masters first (flush to get surrogate keys) ---
    material_cat = MaterialCategory(name="WipeCat")
    client = Client(code="FBMCL-00001", name="Wipe Client")
    supplier = Supplier(name="Wipe Supplier")
    delivery_person = DeliveryPerson(name="Wipe Driver", phone="0300-1")
    account = Account(
        name="Wipe Cash", type="cash", category="cash", account_type="company",
        balance=5000.0, class_category="Assets",
    )
    account_category = AccountCategory(name="Wipe Account Cat")
    cash_flow_category = CashFlowCategory(name="Wipe CF Cat")
    cash_flow_party = CashFlowParty(name="Wipe CF Party")
    drawer_category = FbmCashDrawerCategory(name="Wipe Drawer Cat")
    rental_item = FBMRentalItem(name="Wipe Rental Item", opening_qty=2, available_qty=2)
    rental_client = FBMClient(full_name="Wipe Rental Client")
    pending = PendingBill(client_code=client.code, client_name=client.name, bill_no="WPB-1", amount=150.0, reason="Test")
    db.session.add_all([
        material_cat, client, supplier, delivery_person, account,
        account_category, cash_flow_category, cash_flow_party, drawer_category,
        rental_item, rental_client, pending,
    ])
    db.session.flush()

    # --- Dependent rows ---
    material = Material(code="MAT-001", name="Test Material", category_id=material_cat.id, unit_price=10, total=100, unit="bag")
    account_tx = AccountTransaction(to_account_id=account.id, amount=5000.0, description="seed")
    reconciliation = AccountReconciliation(
        account_id=account.id, reconciliation_date=now.date(), final_reconciled_balance=5000.0,
    )
    cash_flow_sub = CashFlowSubcategory(category_id=cash_flow_category.id, name="Wipe CF Sub")
    cash_flow_entry = CashFlowEntry(
        direction="in", amount=100.0, account_id=account.id,
        category_id=cash_flow_category.id, subcategory_id=cash_flow_sub.id,
        party_id=cash_flow_party.id, description="seed cf",
    )
    drawer_entry = FbmCashDrawerEntry(entry_type="in", amount=50.0, category=drawer_category.name)
    cash_flow_adjustment = CashFlowDifferenceAdjustment(
        adjustment_date=now.date(), physical_cash_available=5000.0,
    )
    grn = GRN(supplier_id=supplier.id, supplier=supplier.name, manual_bill_no="WGRN-1")
    entry_out = Entry(
        date="24-08-2026", type="OUT", material=material.name,
        client=client.name, client_code=client.code, qty=5,
    )
    delivery = Delivery(client_name=client.name, manual_bill_no="WDLV-1")
    direct_sale = DirectSale(client_name=client.name, manual_bill_no="WDS-1", amount=200.0, paid_amount=100.0)
    direct_sale_draft = DirectSaleDraft(client_name=client.name, payload="{}")
    invoice = Invoice(invoice_no="WINV-1", client_name=client.name)
    payment = Payment(client_id=client.id, client_name=client.name, amount=100.0, manual_bill_no="WPAY-1")
    supplier_payment = SupplierPayment(supplier_id=supplier.id, amount=80.0)
    follow_reminder = FollowUpReminder(pending_bill_id=pending.id, remind_at=now)
    staff_email = StaffEmail(email="wipe@test.com")
    booking = Booking(client_name=client.name, amount=300.0, manual_bill_no="WBK-1")
    material_return = MaterialReturn(client_name=client.name, amount=20.0)
    rental = FBMRental(client_id=rental_client.id, item_id=rental_item.id, qty=1)
    recon_basket = ReconBasket(bill_no="WPB-1", inv_client=client.name)
    db.session.add_all([
        material, account_tx, reconciliation, cash_flow_sub, cash_flow_entry,
        drawer_entry, cash_flow_adjustment, grn, entry_out, delivery,
        direct_sale, direct_sale_draft, invoice, payment, supplier_payment,
        follow_reminder, staff_email, booking, material_return, rental, recon_basket,
    ])
    db.session.flush()

    # --- Leaf rows ---
    cash_flow_entry_audit = CashFlowEntryAudit(entry_id=cash_flow_entry.id, action="create")
    cash_flow_audit = CashFlowReconciliationAudit(
        reconciliation_id=cash_flow_adjustment.id, adjustment_date=now.date(), change_type="CREATE",
    )
    grn_item = GRNItem(grn_id=grn.id, mat_name=material.name, qty=10, price_at_time=10.0)
    delivery_item = DeliveryItem(delivery_id=delivery.id, product=material.name, qty=5)
    direct_sale_item = DirectSaleItem(sale_id=direct_sale.id, product_name=material.name, qty=2, price_at_time=100.0)
    waive = WaiveOff(payment_id=payment.id, amount=5.0)
    follow_contact = FollowUpContact(pending_bill_id=pending.id)
    booking_item = BookingItem(booking_id=booking.id, material_name=material.name, qty=3, price_at_time=100.0)
    material_return_item = MaterialReturnItem(material_return_id=material_return.id, material_name=material.name, qty=1)
    delivery_rent = DeliveryRent(sale_id=direct_sale.id, delivery_person_name=delivery_person.name, amount=30.0)
    sale_delivery_person = SaleDeliveryPerson(sale_id=direct_sale.id, delivery_person_id=delivery_person.id)
    driver_payment = DeliveryPersonPayment(delivery_person_id=delivery_person.id, sale_id=direct_sale.id, amount_paid=10.0)
    db.session.add_all([
        cash_flow_entry_audit, cash_flow_audit, grn_item, delivery_item,
        direct_sale_item, waive, follow_contact, booking_item,
        material_return_item, delivery_rent, sale_delivery_person, driver_payment,
    ])
    db.session.flush()
    # The driver payment owns one authoritative ledger row — the full wipe
    # must void + remove it together with the payment.
    driver_tx = AccountTransaction(
        from_account_id=account.id, amount=10.0, description="Driver payment ledger",
        transaction_type="Expense", source_type="DeliveryPersonPayment",
        source_id=driver_payment.id,
    )
    db.session.add(driver_tx)
    db.session.commit()


def full_row_counts():
    counts = {}
    for table in db.metadata.sorted_tables:
        counts[table.name] = int(db.session.query(db.func.count()).select_from(table).scalar() or 0)
    return counts


ALL_WIPE_TARGETS = [
    "clients", "suppliers", "supplier_payments", "pending_bills", "notifications",
    "dispatching", "receiving", "grn", "materials", "material_categories",
    "direct_sales", "material_returns", "delivery_rents", "delivery_persons",
    "invoices", "payments", "bookings", "accounts", "account_categories",
    "account_transactions", "account_reconciliations", "cash_flow_entries",
    "cash_flow_categories", "cash_reconciliation_data", "cash_reconciliation_audit",
    "cash_drawer_entries", "cash_drawer_categories", "delivery_person_payments",
    "direct_sale_drafts", "fbm_rentals", "fbm_rental_clients", "fbm_rental_items",
]


def post_wipe(client, targets, hard=False):
    data = {"confirm_text": "DELETE ALL DATA" if hard else "DELETE SELECTED"}
    if hard:
        data["hard_delete_override"] = "1"
    data["delete_targets"] = list(targets)
    return client.post("/delete_selected_data", data=data, follow_redirects=False)


def test_full_wipe_erases_every_module(client, app):
    """'Check All' (full wipe) must clear accounts and every other module."""
    login(client)
    with app.app_context():
        seed_all_modules()
        assert Account.query.count() == 1
        assert AccountTransaction.query.count() == 2  # seed tx + driver payment ledger tx
        assert CashFlowEntry.query.count() == 1
        assert FBMRental.query.count() == 1

    resp = post_wipe(client, ALL_WIPE_TARGETS, hard=True)
    assert resp.status_code in (302, 303)

    with app.app_context():
        counts = full_row_counts()
        leftover = {
            name: n for name, n in counts.items()
            if n > 0 and name not in PROTECTED_TABLES and name != "bill_counter"
        }
        # Nothing but identity/settings/audit/ops metadata may remain.
        assert not leftover, f"tables survived the full wipe: {leftover}"
        # Bill counter is reset, not deleted.
        assert counts["bill_counter"] == 1
        assert BillCounter.query.first().count == 1000
        # Identity survives.
        assert User.query.filter_by(username="Admin").first() is not None


def test_granular_wipe_cash_flow_only(client, app):
    login(client)
    with app.app_context():
        seed_all_modules()

    resp = post_wipe(client, ["cash_flow_entries", "cash_flow_categories", "account_reconciliations"])
    assert resp.status_code in (302, 303)
    with app.app_context():
        assert CashFlowEntry.query.count() == 0
        assert CashFlowEntryAudit.query.count() == 0
        assert CashFlowCategory.query.count() == 0
        assert CashFlowSubcategory.query.count() == 0
        assert CashFlowParty.query.count() == 0
        assert AccountReconciliation.query.count() == 0
        # Other modules must be untouched.  (The OPEN-KHATA walk-in master
        # client is a system-maintained row and is not part of the seed.)
        assert len(business_clients()) == 1
        assert Account.query.count() == 1
        assert Account.query.first().balance == 5000.0
        assert Payment.query.count() == 1
        assert FBMRental.query.count() == 1
        assert AccountTransaction.query.count() == 2  # untouched ledger


def test_granular_wipe_accounts_resets_balances(client, app):
    """Granular 'accounts' wipes the financial domain but keeps structure.

    The wipe registry expands 'accounts' to the whole financial domain
    (ledger, cash flow, payments, sales, GRN, rentals) — the existing
    aggressive domain-wipe design — while keeping the account rows
    themselves (reset to zero) and the party masters (clients/suppliers).
    """
    login(client)
    with app.app_context():
        seed_all_modules()

    resp = post_wipe(client, ["accounts"], hard=True)
    assert resp.status_code in (302, 303)
    with app.app_context():
        # Account rows kept (structure), balances zeroed, ledger cleared.
        assert Account.query.count() == 1
        assert Account.query.first().balance == 0
        assert AccountTransaction.query.count() == 0
        assert FbmCashDrawerEntry.query.count() == 0
        assert CashFlowDifferenceAdjustment.query.count() == 0
        assert CashFlowReconciliationAudit.query.count() == 0
        assert CashFlowEntry.query.count() == 0
        assert AccountReconciliation.query.count() == 0
        # Expanded financial domain is cleared too.
        assert Payment.query.count() == 0
        assert SupplierPayment.query.count() == 0
        assert GRN.query.count() == 0
        assert DirectSale.query.count() == 0
        assert DeliveryPersonPayment.query.count() == 0
        assert FBMRental.query.count() == 0
        # Party masters survive (excluding the system OPEN-KHATA row).
        assert len(business_clients()) == 1
        assert Supplier.query.count() == 1


def test_settings_ui_targets_are_all_supported_by_preview(client, app):
    """Every checkbox in the wipe UI must map to a real preview dataset."""
    login(client)
    from app.services.wipe import _wipe_dataset_preview_map

    html = client.get("/settings").get_data(as_text=True)
    assert html is not None
    ui_targets = set(re.findall(r'name="delete_targets"\s+value="([a-z_]+)"', html))
    assert ui_targets, "no wipe checkboxes found in settings UI"
    preview_map = _wipe_dataset_preview_map()
    unknown = ui_targets - set(preview_map.keys())
    assert not unknown, f"UI targets missing from wipe preview map: {sorted(unknown)}"
    # The datasets the user complained about must be individually selectable.
    for required in ("accounts", "account_transactions", "cash_flow_entries",
                     "cash_drawer_entries", "cash_drawer_categories",
                     "fbm_rentals", "fbm_rental_clients", "fbm_rental_items"):
        assert required in ui_targets, f"settings UI missing dataset: {required}"


def test_granular_module_export_contains_only_selected_tables(client, app):
    login(client)
    with app.app_context():
        seed_all_modules()

    resp = client.post(
        "/import_export/transfer/export",
        data={"sections": "literal_all", "modules": ["accounts", "clients"]},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    xls = pd.ExcelFile(io.BytesIO(resp.data))
    sheets = set(xls.sheet_names)
    expected = {"account", "account_category", "account_transaction", "client", "recon_basket", "__AMS_META__"}
    assert sheets == expected, f"unexpected sheets: {sorted(sheets)}"
    # Metadata must mark the workbook as a partial (module) backup.
    meta = pd.read_excel(xls, "__AMS_META__").set_index("key")["value"].to_dict()
    assert meta.get("partial") == "1"
    assert "account" in meta.get("partial_tables", "")
    assert "payment" not in meta.get("partial_tables", "")
    # Row fidelity check.
    accounts = pd.read_excel(xls, "account")
    assert len(accounts) == 1
    assert list(accounts["name"]) == ["Wipe Cash"]


def test_granular_module_restore_into_fresh_app(app_factory, client, app, tmp_path):
    """A module restore must only import the selected module's tables."""
    # --- Source store: seed everything, export full + module backups ---
    login(client)
    with app.app_context():
        seed_all_modules()
    full_backup = client.post(
        "/import_export/transfer/export",
        data={"sections": "literal_all"},
        follow_redirects=False,
    )
    assert full_backup.status_code == 200
    module_backup = client.post(
        "/import_export/transfer/export",
        data={"sections": "literal_all", "modules": ["accounts"]},
        follow_redirects=False,
    )
    assert module_backup.status_code == 200

    # --- Destination store: fresh app + fresh DB (kept in pytest's tmp dir
    #     so it can never touch the real instance directory) ---
    dest_db = os.path.join(str(tmp_path), "granular_dest.db")
    dest_app = app_factory(dest_db)
    from conftest import make_csrf_client
    dest_client = make_csrf_client(dest_app)
    login(dest_client)
    with dest_app.app_context():
        # Local data that must survive the restore.
        db.session.add(Client(code="FBMCL-99999", name="Local Client"))
        db.session.add(Supplier(name="Local Supplier"))
        db.session.add(Payment(client_name="Local Client", amount=12.5, manual_bill_no="LOCAL-1"))
        db.session.commit()

    # 1) Module restore (append): only the 'accounts' tables come across.
    resp = dest_client.post(
        "/import_export/transfer/import",
        data={
            "sections": ["literal_all"],
            "mode": "append",
            "modules": ["accounts"],
            "file": (io.BytesIO(module_backup.data), "module_backup.xlsx"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:500]
    with dest_app.app_context():
        assert Account.query.count() == 1
        assert Account.query.first().name == "Wipe Cash"
        # Untouched modules keep only their local rows (the OPEN-KHATA master
        # is the system-maintained client row).
        assert len(business_clients()) == 1
        assert business_clients()[0].name == "Local Client"
        assert Supplier.query.count() == 1
        assert Payment.query.count() == 1

    # 2) Full-file restore with module selection: overwrite 'clients' only.
    resp = dest_client.post(
        "/import_export/transfer/import",
        data={
            "sections": ["literal_all"],
            "mode": "replace_tenant_data",
            "modules": ["clients"],
            "file": (io.BytesIO(full_backup.data), "full_backup.xlsx"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:500]
    with dest_app.app_context():
        # clients replaced by the backup; the other modules are untouched.
        clients = business_clients()
        assert len(clients) == 1
        assert clients[0].name == "Wipe Client"
        assert Supplier.query.count() == 1      # local supplier kept
        assert Payment.query.count() == 1       # local payment kept
        assert Account.query.count() == 1       # module restore from step 1 kept
