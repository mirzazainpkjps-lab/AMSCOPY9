"""Shared imports and module globals."""
import os
import shutil
try:
    import pandas as pd
except ImportError:
    pd = None
import io
import re
import zipfile
import csv
import json
import hashlib
import threading
import uuid
import logging
import sqlite3
import tempfile
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, Response, make_response, jsonify, current_app, session, g
from flask_login import login_required, current_user
from sqlalchemy import func, and_, or_, Boolean, Date, DateTime, Float, Integer, Numeric, select, text
from sqlalchemy.engine.url import make_url
from models import db, User, Material, MaterialCategory, Entry, Client, PendingBill, Booking, BookingItem, Payment, DirectSale, DirectSaleItem, GRN, GRNItem, Delivery, DeliveryItem, DeliveryPerson, DeliveryRent, Invoice, Settings, BillCounter, StaffEmail, FbmCashDrawerEntry, FbmCashDrawerCategory, get_or_create_material_category
from utils.audit import audit_log

# Module configuration
MODULE_CONFIG = {
    'name': 'Import/Export Module',
    'description': 'Data import and export functionality',
    'url_prefix': '/import_export',
    'enabled': True
}

# ===== DEPENDENCY VALIDATION =====


MODULE_CONFIG = {
    'name': 'Import/Export Module',
    'description': 'Data import and export functionality',
    'url_prefix': '/import_export',
    'enabled': True
}
import_export_bp = Blueprint('import_export', __name__)

PK_TZ = ZoneInfo('Asia/Karachi')
APP_UPGRADE_ENABLED = False
_WIPE_BACKUP_ENABLED = False
_AUTO_BACKUP_ENABLED = False
# Wipe never deletes these. Backup/restore DOES include users so a restore
# puts managers back exactly as they were before the wipe.
WIPE_PROTECTED_TABLES = {
    'user',
    'user_login_session',
}
FULL_RAW_EXCLUDE_TABLES = {
    # Root forensic log; keep out of tenant replace/restore data path.
    'tenant_wipe_backup_history',
    # Live sessions are not yard state — users themselves ARE backed up.
    'user_login_session',
}

_MASTER_IMPORT_PROGRESS = {}
_MASTER_IMPORT_PROGRESS_LOCK = threading.Lock()
_DEPLOY_PROGRESS = {}
_DEPLOY_PROGRESS_LOCK = threading.Lock()
_IMPORT_ACTOR_CTX = threading.local()

CLIENT_SCHEMA = [
    'code', 'name', 'phone', 'address', 'category',
    'financial_book_no', 'financial_page',
    'cement_book_no', 'cement_page',
    'steel_book_no', 'steel_page',
    'book_no', 'location_url', 'page_notes', 'status',
]
DISPATCH_SCHEMA = [
    'CLIENT_CODE', 'CLIENT_NAME', 'CLIENT_CATEGORY', 'TRANSACTION_CATEGORY',
    'BILL_NO', 'BILL_DATE', 'CEMENT_BRAND', 'QTY', 'NIMBUS', 'NOTES',
    'SOURCE', 'MATCH_STATUS',
]
PENDING_BILL_SCHEMA = ['client_code', 'bill_no', 'name', 'amount', 'reason', 'nimbus']
BOOKING_SCHEMA = ['client_name', 'manual_bill_no', 'amount', 'paid_amount', 'date_posted', 'note']
BOOKING_ITEM_SCHEMA = ['booking_bill_no', 'booking_client_name', 'material_name', 'qty', 'price_at_time']
PAYMENT_SCHEMA = ['client_name', 'manual_bill_no', 'amount', 'method', 'date_posted', 'note']
SALE_SCHEMA = [
    'client_name', 'manual_bill_no', 'auto_bill_no', 'category',
    'amount', 'paid_amount',
    'rent_item_revenue', 'delivery_rent_cost', 'rent_variance_loss',
    'date_posted', 'note',
]
SALE_ITEM_SCHEMA = ['sale_bill_no', 'sale_client_name', 'product_name', 'qty', 'price_at_time']
MASTER_SHEET_SECTIONS = {
    'clients': ['Clients'],
    'materials': ['MaterialCategories', 'Materials'],
    'dispatch': ['Dispatch'],
    'bookings': ['Bookings', 'BookingItems'],
    'payments': ['Payments', 'FBMCashDrawer', 'FBMCashDrawerCategories'],
    'sales': ['Sales', 'SaleItems'],
    'supplier': ['GRN', 'GRNItems'],
    'delivery': ['DeliveryPersons', 'DeliveryRents'],
    'pending': ['PendingBills'],
}
MASTER_ALL_SHEETS = [
    'Clients', 'MaterialCategories', 'Materials', 'PendingBills',
    'Dispatch', 'Bookings', 'BookingItems', 'Payments', 'Sales',
    'SaleItems', 'GRN', 'GRNItems', 'DeliveryPersons', 'DeliveryRents',
    'FBMCashDrawer', 'FBMCashDrawerCategories', 'Users',
]
META_SHEET_NAME = '__AMS_META__'

# ===== Granular (per-module) backup / restore / import / export =====
# Each module maps to the *physical tables* that make up that module.
# Exporting a module writes exactly those tables (full fidelity, same engine
# as the full XLSX backup); restoring a module imports exactly the tables the
# workbook supplies, so other modules are left untouched.
EXPORT_MODULES = {
    'clients': {
        'label': 'Clients',
        'tables': ['client', 'recon_basket'],
    },
    'suppliers': {
        'label': 'Suppliers & Supplier Payments',
        'tables': ['supplier', 'supplier_payment'],
    },
    'pending_bills': {
        'label': 'Pending Bills & Follow-ups',
        'tables': ['pending_bill', 'follow_up_contact', 'follow_up_reminder'],
    },
    'notifications': {
        'label': 'Notification Data (Staff Emails)',
        'tables': ['staff_email', 'follow_up_contact', 'follow_up_reminder'],
    },
    'stock_movements': {
        'label': 'Stock Dispatch & Receiving (IN/OUT)',
        'tables': ['entry', 'delivery', 'delivery_item'],
    },
    'grn': {
        'label': 'GRN (Stock In)',
        'tables': ['grn', 'grn_item', 'grn_allocation'],
    },
    'materials': {
        'label': 'Materials & Categories',
        'tables': ['material_category', 'material'],
    },
    'direct_sales': {
        'label': 'Direct Sales, Invoices & Driver Payments',
        'tables': [
            'direct_sale', 'direct_sale_item', 'direct_sale_draft',
            'invoice', 'delivery_rent', 'sale_delivery_persons',
            'delivery_person_payment',
        ],
    },
    'material_returns': {
        'label': 'Material Returns',
        'tables': ['material_return', 'material_return_item'],
    },
    'bookings': {
        'label': 'Bookings',
        'tables': ['booking', 'booking_item', 'booking_allocation',
                  'booking_allocation_repair_archive'],
    },
    'payments': {
        'label': 'Payments',
        'tables': ['payment', 'waive_off'],
    },
    'delivery_persons': {
        'label': 'Delivery Persons & Driver Payments',
        'tables': ['delivery_person', 'delivery_person_payment',
                  'sale_delivery_persons', 'delivery_rent'],
    },
    'accounts': {
        'label': 'Financial Accounts (Khata)',
        'tables': ['account', 'account_category', 'account_transaction'],
    },
    'cash_drawer': {
        'label': 'Cash Drawer',
        'tables': ['fbm_cash_drawer_entry', 'fbm_cash_drawer_category'],
    },
    'cash_flow': {
        'label': 'Cash Flow & Reconciliations',
        'tables': [
            'cash_flow_entry', 'cash_flow_entry_audit', 'cash_flow_category',
            'cash_flow_subcategory', 'cash_flow_party',
            'account_reconciliation', 'cash_flow_difference_adjustment',
            'cash_flow_reconciliation_audit',
        ],
    },
    'rentals': {
        'label': 'Rental Management (FBM)',
        'tables': ['fbm_rental', 'fbm_rental_item', 'fbm_client'],
    },
}


def _tables_for_modules(module_keys):
    """Expand user-selected module keys into the union of their table names."""
    tables = []
    seen = set()
    for key in module_keys or []:
        for table in EXPORT_MODULES.get(key, {}).get('tables', []):
            if table not in seen:
                seen.add(table)
                tables.append(table)
    return tables
