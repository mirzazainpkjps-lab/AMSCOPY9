"""Shared imports and module globals."""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from datetime import date, datetime
from types import SimpleNamespace
from sqlalchemy import func, case, or_, and_
from models import db, Material, MaterialCategory, Entry, Client, PendingBill, DirectSale, Booking, Payment, GRN, Invoice

# Module configuration
MODULE_CONFIG = {
    'name': 'Inventory Module',
    'description': 'Stock and inventory management',
    'url_prefix': '/inventory',
    'enabled': True
}

inventory_bp = Blueprint('inventory', __name__)



