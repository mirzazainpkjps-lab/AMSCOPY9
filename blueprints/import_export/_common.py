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
