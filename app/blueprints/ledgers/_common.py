from __future__ import annotations
"""Shared imports and module globals."""
"""HTTP routes: ledgers."""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, make_response, send_from_directory, abort, session
from flask_login import login_required, login_user, logout_user, current_user
from datetime import datetime, date, timedelta
from sqlalchemy import func, case, text, or_, and_, exists, not_
from types import SimpleNamespace
from decimal import Decimal, ROUND_HALF_UP
import os, io, json, re, logging, calendar

from models import *
from app.services.api import *  # noqa
from utils.audit import audit_log

from itertools import zip_longest
from sqlalchemy.orm import selectinload
from app.services.constants import (
    AUTO_BILL_NAMESPACES,
    AUTO_BILL_NS_DEFAULT,
    SALE_CATEGORY_CHOICES,
    OPEN_KHATA_CODE,
    OPEN_KHATA_NAME,
    ENDPOINT_PERMISSION_MAP,
)
from models import MaterialCategory


bp = Blueprint('ledgers', __name__)


