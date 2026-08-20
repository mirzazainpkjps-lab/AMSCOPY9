"""Shared imports and module globals."""
"""
Accounts module for financial management.
Provides comprehensive finance management including payments, receipts, expenditures, and account transfers.
"""

import logging
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError
from types import SimpleNamespace
from models import db, Account, AccountCategory, AccountTransaction, AccountingAuditLog, Payment, SupplierPayment, FbmCashDrawerEntry, DirectSale, GRN, GRNItem, Supplier, Client, Booking, PendingBill, BillCounter, DeliveryPerson, DeliveryPersonPayment
from utils.audit import audit_log

# Module configuration
MODULE_CONFIG = {
    'name': 'Accounts Module',
    'description': 'Financial management and accounting',
    'url_prefix': '/accounts',
    'enabled': True,
    'requires_login': True,
    'allowed_roles': ['admin', 'user']
}

accounts_bp = Blueprint('accounts', __name__)
PK_TZ = ZoneInfo('Asia/Karachi')
logger = logging.getLogger(__name__)

AUTO_BILL_NS_DEFAULT = 'GEN'
AUTO_BILL_NAMESPACES = {
    'PAYMENT': 'CP',
    'SUPPLIER_PAYMENT': 'SP',
}



