#!/usr/bin/env python
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from models import db
from main import create_app

app = create_app()
app.app_context().push()

print(f"Current DB URI: {app.config.get('SQLALCHEMY_DATABASE_URI')}")

from sqlalchemy import inspect
inspector = inspect(db.engine)
tables = inspector.get_table_names()
print(f"\nExisting tables in app: {len(tables)}")

# Count rows in key tables
for table in ['client', 'entry', 'material', 'supplier', 'pending_bill', 'invoice']:
    if table in tables:
        try:
            result = db.engine.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            count = result[0] if result else 0
            print(f"  {table}: {count} rows")
        except Exception as e:
            print(f"  {table}: Error - {str(e)[:50]}")
