import os
import sys
sys.path.insert(0, os.getcwd())
import cash_flow_audit as c
from main import app

with app.app_context():
    dates = c.gather_dates()
    print('last dates', dates[-10:])
    print(len(dates))
    for d in dates[-10:]:
        print(d, c.cash_flow_day(d))
