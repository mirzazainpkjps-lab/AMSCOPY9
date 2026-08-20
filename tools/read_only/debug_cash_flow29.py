import os
import sys
from datetime import date
sys.path.insert(0, os.getcwd())
import cash_flow_audit as c
from main import app

with app.app_context():
    print(c.cash_flow_day(date(2026,5,29)))
    print(c.cash_flow_day(date(2026,5,30)))
