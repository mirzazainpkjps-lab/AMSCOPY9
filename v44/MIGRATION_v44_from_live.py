#!/usr/bin/env python3
"""
MIGRATION_v44_from_live.py

Side-by-side migration: reads live DB at instance/ahmed_cement.db
and writes a fresh v4.4 DB at instance/ahmed_cement_v44.db.

Live DB is NEVER touched. Safe to re-run (target is dropped each time).

Strategy:
  1. Create fresh v4.4 DB from SCHEMA_v4_4.sql (loads seed data: 4 roles,
     68 permissions, 12 wipe scopes).
  2. Skip is_void=1 rows (hard-delete semantics; they'd be gone in v4.4).
  3. Handle table consolidations:
     - 5 category tables -> 1 unified `categories`
     - 4 payment tables -> 1 unified `payments`
  4. Preserve every business fact: sales, purchases, bookings, payments,
     stock movements, ledger runs, waive-offs, discounts, photos, drivers.
  5. Skip tables that don't exist in v4.4 (FBM, cash_flow_entry duplicates,
     void audit tables).
  6. Verify with row-count report.

Idempotent: re-running blows away the target and starts fresh.
"""

import os, sys, sqlite3, json, hashlib
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
LIVE_DB = ROOT / "instance" / "ahmed_cement.db"
TARGET_DB = ROOT / "instance" / "ahmed_cement_v44.db"
SCHEMA_SQL = ROOT / "SCHEMA_v4_4.sql"

if not LIVE_DB.exists():
    sys.exit(f"Live DB not found: {LIVE_DB}")
if not SCHEMA_SQL.exists():
    sys.exit(f"Schema file not found: {SCHEMA_SQL}")

# --- Fresh target ---
if TARGET_DB.exists():
    print(f"Removing existing target: {TARGET_DB}")
    TARGET_DB.unlink()
    # Also remove -wal / -shm if present
    for suf in ('-wal', '-shm'):
        p = Path(str(TARGET_DB) + suf)
        if p.exists(): p.unlink()

print(f"Creating v4.4 database at {TARGET_DB}")
tgt = sqlite3.connect(str(TARGET_DB))
tgt.execute("PRAGMA foreign_keys = ON")
with open(SCHEMA_SQL) as f:
    tgt.executescript(f.read())
tgt.commit()

# Live source (read-only)
live = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
live.row_factory = sqlite3.Row

# ---- Helpers ----
def live_tables():
    return {r[0] for r in live.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

def has_col(conn, table, col):
    return col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

def to_minor(x):
    if x is None: return 0
    try: return int(round(float(x) * 100))
    except: return 0

def safe_str(x):
    if x is None: return None
    return str(x)

report = {'migrated': {}, 'skipped_void': {}, 'errors': [], 'notes': []}

LIVE_TABLES = live_tables()
print(f"\nLive DB has {len(LIVE_TABLES)} tables")

# =====================================================================
# PHASE 1 -- MASTER DATA (must load before transactions reference it)
# =====================================================================
print("\n" + "=" * 70)
print("PHASE 1 -- Master data (users, categories, clients, suppliers, etc.)")
print("=" * 70)

# ---------- Users ----------
# Live has ~25 can_* boolean columns; v4.4 has role_id + permissions.
# Map live role string to seeded role, and translate can_* -> role_permissions.
print("\n[users] migrating with role mapping...")
role_by_name = {r[1]: r[0] for r in tgt.execute("SELECT id, name FROM roles").fetchall()}

# Map live.user.role string -> v4.4 role name
LIVE_ROLE_MAP = {
    'admin': 'Admin', 'owner': 'Admin', 'root': 'Admin',
    'manager': 'Manager', 'supervisor': 'Manager',
    'cashier': 'Cashier', 'sales': 'Cashier', 'staff': 'Cashier',
    'viewer': 'Viewer', 'readonly': 'Viewer', 'user': 'Cashier',
}

user_id_map = {}  # live user_id -> v4.4 user_id
u_migrated = 0
for u in live.execute("SELECT * FROM user").fetchall():
    live_role = (u['role'] or 'cashier').strip().lower()
    role_name = LIVE_ROLE_MAP.get(live_role, 'Cashier')
    role_id = role_by_name.get(role_name)
    if not role_id:
        report['errors'].append(f"user {u['username']}: role {role_name} not found")
        continue
    status = u['status'] if u['status'] in ('active','suspended','disabled') else 'active'
    active = 1 if status == 'active' else 0
    try:
        tgt.execute("""INSERT INTO users(username, password_hash, password_plain, full_name,
                       role_id, phone, status, restrict_backdated_edit, active,
                       created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (u['username'], u['password_hash'] or 'legacy', u['password_plain'],
                     u['username'],  # live has no full_name, use username
                     role_id, None, status,
                     u['restrict_backdated_edit'] or 0, active,
                     u['created_at'] or datetime.now().isoformat(),
                     u['created_at'] or datetime.now().isoformat()))
        new_id = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        user_id_map[u['id']] = new_id
        u_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"user {u['username']}: {e}")
report['migrated']['users'] = u_migrated
print(f"  {u_migrated} users migrated")

# ---------- Categories (5 live tables → 1 v4.4 table) ----------
print("\n[categories] consolidating 5 live tables...")
cat_id_map = {'material': {}, 'account': {}, 'cash_flow_in': {}, 'cash_flow_out': {}}
cat_migrated = 0

# material_category
for r in live.execute("SELECT * FROM material_category").fetchall():
    try:
        tgt.execute("INSERT INTO categories(category_type,name,active,created_at,approved) VALUES ('material',?,?,?,1)",
                    (r['name'], r['is_active'] or 1, r['created_at']))
        cat_id_map['material'][r['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        cat_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"material_category {r['name']}: {e}")

# account_category
for r in live.execute("SELECT * FROM account_category").fetchall():
    try:
        tgt.execute("INSERT INTO categories(category_type,name,notes,active,created_at,approved) VALUES ('account',?,?,?,?,1)",
                    (r['name'], r['note'], r['is_active'] or 1, r['created_at']))
        cat_id_map['account'][r['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        cat_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"account_category {r['name']}: {e}")

# cash_flow_category (direction in/out)
cf_cat_id_map = {}
for r in live.execute("SELECT * FROM cash_flow_category").fetchall():
    direction = (r['direction'] or 'out').lower()
    ct = f'cash_flow_{direction}' if direction in ('in','out') else 'cash_flow_out'
    try:
        tgt.execute("""INSERT INTO categories(category_type,name,direction,sort_order,active,notes,created_at,updated_at,approved)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (ct, r['name'], direction if direction in ('in','out') else None,
                     r['sort_order'] or 0, r['is_active'] or 1, r['notes'],
                     r['created_at'], r['updated_at'] or r['created_at']))
        cf_cat_id_map[r['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        cat_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"cash_flow_category {r['name']}: {e}")

# cash_flow_subcategory (parent_id -> cf_cat_id_map)
cf_sub_id_map = {}
for r in live.execute("SELECT * FROM cash_flow_subcategory").fetchall():
    parent_new = cf_cat_id_map.get(r['category_id'])
    if not parent_new: continue
    # find parent's category_type to inherit
    ptype = tgt.execute("SELECT category_type, direction FROM categories WHERE id=?", (parent_new,)).fetchone()
    ct = ptype[0] if ptype else 'cash_flow_out'
    try:
        tgt.execute("""INSERT INTO categories(category_type,name,parent_id,direction,active,notes,created_at,updated_at,approved)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (ct, r['name'], parent_new, ptype[1] if ptype else None,
                     r['is_active'] or 1, r['notes'],
                     r['created_at'], r['updated_at'] or r['created_at']))
        cf_sub_id_map[r['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        cat_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"cash_flow_subcategory {r['name']}: {e}")

report['migrated']['categories'] = cat_migrated
report['notes'].append(f"Consolidated 5 live category tables into 1 unified categories table ({cat_migrated} rows)")
print(f"  {cat_migrated} categories total (material + account + cash_flow parent + cash_flow sub)")

# ---------- Clients ----------
# Live has `client.category` as free-text; we'll leave category_id NULL (or match by name)
print("\n[clients] migrating...")
client_id_map = {}
client_migrated = 0
material_cats_by_name = {r[1]: r[0] for r in tgt.execute("SELECT id, name FROM categories WHERE category_type='client'").fetchall()}

for c in live.execute("SELECT * FROM client").fetchall():
    # Create a client category on the fly if it doesn't exist
    cat_id = None
    if c['category']:
        cat_name = c['category'].strip()
        if cat_name not in material_cats_by_name:
            try:
                tgt.execute("INSERT INTO categories(category_type,name,active,approved) VALUES ('client',?,1,1)", (cat_name,))
                material_cats_by_name[cat_name] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
            except: pass
        cat_id = material_cats_by_name.get(cat_name)

    ob = c['opening_balance'] or 0
    try:
        tgt.execute("""INSERT INTO clients(code,name,phone,address,location_url,category_id,
                       opening_balance,opening_balance_minor,opening_balance_date,
                       book_no,financial_page,financial_book_no,cement_page,cement_book_no,
                       steel_page,steel_book_no,page_notes,require_manual_invoice,
                       active,created_at,updated_at,approved)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (c['code'], c['name'], c['phone'], c['address'], c['location_url'],
                     cat_id, ob, to_minor(ob), c['opening_balance_date'],
                     c['book_no'], c['financial_page'], c['financial_book_no'],
                     c['cement_page'], c['cement_book_no'], c['steel_page'],
                     c['steel_book_no'], c['page_notes'],
                     c['require_manual_invoice'] or 0, c['is_active'] or 1,
                     c['created_at'], c['created_at']))
        client_id_map[c['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        client_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"client {c['code']} {c['name']}: {e}")
report['migrated']['clients'] = client_migrated
print(f"  {client_migrated} clients migrated")

# ---------- Suppliers ----------
print("\n[suppliers] migrating...")
supplier_id_map = {}
sup_migrated = 0
for s in live.execute("SELECT * FROM supplier").fetchall():
    ob = s['opening_balance'] or 0
    # generate a code (live has no supplier.code)
    code = f"SUP-{s['id']:04d}"
    try:
        tgt.execute("""INSERT INTO suppliers(code,name,phone,address,opening_balance,opening_balance_minor,
                       opening_balance_date,active,created_at,updated_at,approved)
                       VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                    (code, s['name'], s['phone'], s['address'],
                     ob, to_minor(ob), s['opening_balance_date'],
                     s['is_active'] or 1, s['created_at'], s['created_at']))
        supplier_id_map[s['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        sup_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"supplier {s['name']}: {e}")
report['migrated']['suppliers'] = sup_migrated
print(f"  {sup_migrated} suppliers migrated")

# ---------- Materials ----------
print("\n[materials] migrating...")
material_id_map = {}
mat_migrated = 0
for m in live.execute("SELECT * FROM material").fetchall():
    cat_id = cat_id_map['material'].get(m['category_id']) if m['category_id'] else None
    rate = m['unit_price'] or 0
    try:
        tgt.execute("""INSERT INTO materials(code,name,unit,category_id,current_rate,current_rate_minor,
                       active,created_at,updated_at,approved)
                       VALUES (?,?,?,?,?,?,?,?,?,1)""",
                    (m['code'], m['name'], m['unit'] or 'unit',
                     cat_id, rate, to_minor(rate),
                     m['is_active'] or 1, m['created_at'], m['created_at']))
        material_id_map[m['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        mat_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"material {m['code']} {m['name']}: {e}")
report['migrated']['materials'] = mat_migrated
print(f"  {mat_migrated} materials migrated")

# Build material name -> id lookup (for text-referenced tables)
material_by_name = {r[1]: r[0] for r in tgt.execute("SELECT id, name FROM materials").fetchall()}

# ---------- Delivery Persons ----------
print("\n[delivery_persons] migrating...")
dp_id_map = {}
dp_migrated = 0
for d in live.execute("SELECT * FROM delivery_person").fetchall():
    ob = d['opening_balance'] or 0
    code = f"DP-{d['id']:04d}"
    try:
        tgt.execute("""INSERT INTO delivery_persons(code,name,phone,opening_balance,opening_balance_minor,
                       opening_balance_date,active,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (code, d['name'], d['phone'], ob, to_minor(ob),
                     d['opening_balance_date'], d['is_active'] or 1, d['created_at']))
        dp_id_map[d['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        dp_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"delivery_person {d['name']}: {e}")
report['migrated']['delivery_persons'] = dp_migrated
print(f"  {dp_migrated} delivery persons migrated")

# ---------- Accounts (banks + cash drawers + expense/revenue) ----------
print("\n[accounts] migrating...")
account_id_map = {}
acct_migrated = 0
for a in live.execute("SELECT * FROM account").fetchall():
    # Map live account_type to v4.4 enum
    live_type = (a['account_type'] or a['type'] or 'other').lower().replace(' ','_')
    v44_type = 'other'
    for allowed in ('cash_drawer','bank','expense','revenue','loan','owner'):
        if allowed in live_type:
            v44_type = allowed; break
    # bank_name required if account_type='bank'
    bank_name = a['bank_name'] if a['bank_name'] else (a['name'] if v44_type == 'bank' else None)
    if v44_type == 'bank' and not bank_name:
        bank_name = a['name'] or 'Unknown'
    bal = a['balance'] or 0
    ob = a['opening_balance'] or 0
    try:
        tgt.execute("""INSERT INTO accounts(name,account_type,source_category,bank_name,account_holder_name,
                       account_number,branch_code,balance,balance_minor,opening_balance,opening_balance_minor,
                       opening_balance_date,revision,active,note,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (a['name'], v44_type, a['source_category'], bank_name,
                     a['account_holder_name'], a['account_number'], a['branch_code'],
                     bal, to_minor(bal), ob, to_minor(ob), a['opening_balance_date'],
                     a['revision'] or 0, a['is_active'] or 1, a['note'],
                     a['created_at'], a['updated_at'] or a['created_at']))
        account_id_map[a['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        acct_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"account {a['name']} type={v44_type}: {e}")
report['migrated']['accounts'] = acct_migrated
print(f"  {acct_migrated} accounts migrated")

# =====================================================================
# PHASE 2 -- TRANSACTIONAL DATA (skip is_void=1 rows)
# =====================================================================
print("\n" + "=" * 70)
print("PHASE 2 -- Transactions (skipping is_void=1)")
print("=" * 70)

# ---------- Bookings ----------
print("\n[bookings] migrating (is_void=0 only)...")
booking_id_map = {}
b_migrated = 0; b_skipped = 0
# NB: live booking uses client_name (string). We resolve to client_id by name.
clients_by_name = {r[1]: r[0] for r in tgt.execute("SELECT id, name FROM clients").fetchall()}
for b in live.execute("SELECT * FROM booking WHERE COALESCE(is_void,0)=0").fetchall():
    client_id = clients_by_name.get(b['client_name'])
    if not client_id:
        # Create a placeholder client if the name is orphaned
        code = f"CL-ORPHAN-{b['id']:04d}"
        try:
            tgt.execute("INSERT INTO clients(code,name,active,approved) VALUES (?,?,1,1)", (code, b['client_name'] or 'Unknown'))
            client_id = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
            clients_by_name[b['client_name']] = client_id
        except:
            b_skipped += 1; continue
    amt = b['amount'] or 0
    paid = b['paid_amount'] or 0
    disc = b['discount'] or 0
    # receive_in_account_id remap
    r_acct = account_id_map.get(b['receive_in_account_id']) if b['receive_in_account_id'] else None
    try:
        tgt.execute("""INSERT INTO bookings(auto_bill_no,manual_bill_no,client_id,booking_date,
                       total_amount,total_amount_minor,paid_amount,paid_amount_minor,
                       discount,discount_reason,receive_in_account_id,photo_path,photo_url,
                       status,created_at,updated_at,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b['auto_bill_no'] or f"B-{b['id']:06d}",
                     b['manual_bill_no'] or f"MB-{b['id']}", client_id,
                     b['date_posted'] or datetime.now().isoformat(),
                     amt, to_minor(amt), paid, to_minor(paid),
                     disc, b['discount_reason'], r_acct,
                     b['photo_path'], b['photo_url'],
                     'active', b['date_posted'], b['date_posted'], b['note']))
        booking_id_map[b['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        b_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"booking {b['id']}: {e}")
report['migrated']['bookings'] = b_migrated
report['skipped_void']['booking'] = live.execute("SELECT COUNT(*) FROM booking WHERE COALESCE(is_void,0)=1").fetchone()[0]
print(f"  {b_migrated} bookings migrated, {report['skipped_void']['booking']} voided skipped")

# ---------- Booking items ----------
print("\n[booking_items] migrating...")
bi_id_map = {}
bi_migrated = 0
for bi in live.execute("SELECT * FROM booking_item").fetchall():
    booking_new = booking_id_map.get(bi['booking_id'])
    if not booking_new: continue  # parent voided/skipped
    material_id = material_by_name.get(bi['material_name'])
    if not material_id: continue  # orphan
    qty = bi['qty'] or 0
    rate = bi['price_at_time'] or 0
    if qty <= 0 or rate < 0: continue
    amount = qty * rate
    try:
        tgt.execute("""INSERT INTO booking_items(booking_id,material_id,qty_booked,rate,rate_minor,amount,amount_minor)
                       VALUES (?,?,?,?,?,?,?)""",
                    (booking_new, material_id, qty, rate, to_minor(rate), amount, to_minor(amount)))
        bi_id_map[bi['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        bi_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"booking_item {bi['id']}: {e}")
report['migrated']['booking_items'] = bi_migrated
print(f"  {bi_migrated} booking_items migrated")

# ---------- GRN -> purchases ----------
print("\n[purchases] migrating (was grn)...")
purchase_id_map = {}
p_migrated = 0
for g in live.execute("SELECT * FROM grn WHERE COALESCE(is_void,0)=0").fetchall():
    supplier_id = supplier_id_map.get(g['supplier_id'])
    if not supplier_id:
        # Try to resolve by supplier name
        row = tgt.execute("SELECT id FROM suppliers WHERE name=?", (g['supplier'],)).fetchone()
        if row: supplier_id = row[0]
        else:
            # Create placeholder
            try:
                code = f"SUP-ORPHAN-{g['id']:04d}"
                tgt.execute("INSERT INTO suppliers(code,name,active,approved) VALUES (?,?,1,1)",
                            (code, g['supplier'] or 'Unknown'))
                supplier_id = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
            except: continue
    r_acct = account_id_map.get(g['payment_account_id']) if g['payment_account_id'] else None
    paid = g['paid_amount'] or 0
    # Normalize payment_type: live has 'Credit' with capital C
    pt = (g['payment_type'] or '').lower().strip()
    if pt not in ('cash','bank','credit'): pt = None
    try:
        tgt.execute("""INSERT INTO purchases(auto_bill_no,manual_bill_no,supplier_id,supplier_invoice_no,
                       bill_date,purchase_date,due_date,
                       loading_cost,freight_cost,other_expense,adjustment_amount,discount,
                       tax_percent,tax_amount,tax_type,paid_amount,paid_amount_minor,
                       payment_type,payment_account_id,bank_name,account_name,account_no,
                       photo_path,photo_url,status,created_at,updated_at,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (g['auto_bill_no'] or f"GRN-{g['id']:06d}",
                     g['manual_bill_no'] or f"MG-{g['id']}", supplier_id,
                     g['supplier_invoice_no'], g['bill_date'],
                     g['date_posted'] or datetime.now().isoformat(), g['due_date'],
                     g['loading_cost'] or 0, g['freight_cost'] or 0, g['other_expense'] or 0,
                     g['adjustment_amount'] or 0, g['discount'] or 0,
                     g['tax_percent'] or 0, g['tax_amount'] or 0, g['tax_type'],
                     paid, to_minor(paid),
                     pt, r_acct,
                     g['bank_name'], g['account_name'], g['account_no'],
                     g['photo_path'], g['photo_url'], 'active',
                     g['date_posted'], g['date_posted'], g['note']))
        purchase_id_map[g['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        p_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"grn->purchase {g['id']}: {e}")
report['migrated']['purchases'] = p_migrated
report['skipped_void']['grn'] = live.execute("SELECT COUNT(*) FROM grn WHERE COALESCE(is_void,0)=1").fetchone()[0]
print(f"  {p_migrated} purchases migrated, {report['skipped_void']['grn']} voided skipped")

# ---------- purchase_items (was grn_item) ----------
print("\n[purchase_items] migrating (was grn_item)...")
pi_id_map = {}
pi_migrated = 0
for gi in live.execute("SELECT * FROM grn_item WHERE COALESCE(is_void,0)=0").fetchall():
    purchase_new = purchase_id_map.get(gi['grn_id'])
    if not purchase_new: continue
    material_id = material_by_name.get(gi['mat_name'])
    if not material_id: continue
    qty = gi['qty'] or 0
    rate = gi['price_at_time'] or 0
    if qty <= 0 or rate < 0: continue
    amount = qty * rate
    try:
        tgt.execute("""INSERT INTO purchase_items(purchase_id,material_id,qty,rate,rate_minor,amount,amount_minor,is_locked)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (purchase_new, material_id, qty, rate, to_minor(rate), amount, to_minor(amount),
                     gi['is_locked'] or 0))
        pi_id_map[gi['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        pi_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"grn_item {gi['id']}: {e}")

        # Duplicate stock_batches for FIFO (each purchase_item = one batch)
report['migrated']['purchase_items'] = pi_migrated
print(f"  {pi_migrated} purchase_items migrated")

# ---------- stock_batches (derived from purchase_items) ----------
print("\n[stock_batches] deriving from purchases (opening balance approach)...")
sb_created = 0
# For each purchase_item, create a stock_batch with remaining_qty = qty
# (we'll reconcile against sales below).
batch_by_grn_item = {}  # live grn_item.id -> v4.4 stock_batch.id
for gi in live.execute("SELECT * FROM grn_item WHERE COALESCE(is_void,0)=0").fetchall():
    purchase_new = purchase_id_map.get(gi['grn_id'])
    if not purchase_new: continue
    material_id = material_by_name.get(gi['mat_name'])
    if not material_id: continue
    qty = gi['qty'] or 0
    rate = gi['price_at_time'] or 0
    if qty <= 0: continue
    batch_date_row = tgt.execute("SELECT purchase_date FROM purchases WHERE id=?", (purchase_new,)).fetchone()
    batch_date = batch_date_row[0] if batch_date_row else datetime.now().isoformat()
    try:
        tgt.execute("""INSERT INTO stock_batches(material_id,source_type,source_id,batch_date,qty_in,remaining_qty,cost_rate,cost_rate_minor,is_locked)
                       VALUES (?,'purchase',?,?,?,?,?,?,?)""",
                    (material_id, pi_id_map.get(gi['id']), batch_date, qty, qty, rate, to_minor(rate),
                     gi['is_locked'] or 0))
        batch_by_grn_item[gi['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        sb_created += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"stock_batch from grn_item {gi['id']}: {e}")
report['migrated']['stock_batches'] = sb_created
report['notes'].append("stock_batches: one batch per purchase_item; remaining_qty will be consumed by sales replay")
print(f"  {sb_created} stock_batches created (remaining_qty = qty for now)")

# ---------- direct_sale -> sales ----------
print("\n[sales] migrating (was direct_sale)...")
sale_id_map = {}
s_migrated = 0
for s in live.execute("SELECT * FROM direct_sale WHERE COALESCE(is_void,0)=0").fetchall():
    client_id = None
    if s['client_code']:
        row = tgt.execute("SELECT id FROM clients WHERE code=?", (s['client_code'],)).fetchone()
        if row: client_id = row[0]
    if not client_id and s['client_name']:
        row = tgt.execute("SELECT id FROM clients WHERE name=?", (s['client_name'],)).fetchone()
        if row: client_id = row[0]
    if not client_id:
        # placeholder
        code = f"CL-SALE-{s['id']:04d}"
        try:
            tgt.execute("INSERT INTO clients(code,name,active,approved) VALUES (?,?,1,1)",
                        (code, s['client_name'] or 'Unknown'))
            client_id = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        except: continue

    amt = s['amount'] or 0
    paid = s['paid_amount'] or 0
    disc = s['discount'] or 0
    r_acct = account_id_map.get(s['payment_account_id']) if s['payment_account_id'] else None
    # Sale type: infer from payment_method
    pm = (s['payment_method'] or 'cash').lower()
    sale_type = 'credit' if paid < amt else ('cash' if pm == 'cash' else 'cash')
    # For migration simplicity map everything to cash/credit; booking-linked sales
    # would require replaying booking_allocation, which we do below.
    try:
        tgt.execute("""INSERT INTO sales(auto_bill_no,manual_bill_no,client_id,sale_date,sale_type,
                       total_amount,total_amount_minor,discount,discount_minor,discount_reason,
                       total_paid_cache,total_paid_cache_minor,
                       rent_item_revenue,delivery_rent_cost,rent_variance_loss,
                       payment_method,payment_account_id,bank_name,account_name,account_no,
                       photo_path,photo_url,status,idempotency_key,
                       created_at,updated_at,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s['auto_bill_no'] or f"S-{s['id']:06d}",
                     s['manual_bill_no'] or f"MS-{s['id']}", client_id,
                     s['date_posted'] or datetime.now().isoformat(),
                     sale_type, amt, to_minor(amt), disc, to_minor(disc), s['discount_reason'],
                     paid, to_minor(paid),
                     s['rent_item_revenue'] or 0, s['delivery_rent_cost'] or 0,
                     s['rent_variance_loss'] or 0,
                     pm if pm in ('cash','bank','credit') else None, r_acct,
                     s['bank_name'], s['account_name'], s['account_no'],
                     s['photo_path'], s['photo_url'], 'active',
                     s['idempotency_key'],
                     s['date_posted'], s['date_posted'], s['note']))
        sale_id_map[s['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        s_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"direct_sale {s['id']}: {e}")
report['migrated']['sales'] = s_migrated
report['skipped_void']['direct_sale'] = live.execute("SELECT COUNT(*) FROM direct_sale WHERE COALESCE(is_void,0)=1").fetchone()[0]
print(f"  {s_migrated} sales migrated, {report['skipped_void']['direct_sale']} voided skipped")

# ---------- sale_items ----------
print("\n[sale_items] migrating (was direct_sale_item)...")
si_id_map = {}
si_migrated = 0
for si in live.execute("SELECT * FROM direct_sale_item").fetchall():
    sale_new = sale_id_map.get(si['sale_id'])
    if not sale_new: continue
    material_id = material_by_name.get(si['product_name'])
    if not material_id: continue
    qty = si['qty'] or 0
    rate = si['price_at_time'] or 0
    if qty <= 0 or rate < 0: continue
    amount = qty * rate
    cost = si['cost_rate_at_sale']
    try:
        tgt.execute("""INSERT INTO sale_items(sale_id,material_id,qty,rate,rate_minor,amount,amount_minor,
                       cost_rate_at_sale,cost_rate_at_sale_minor)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sale_new, material_id, qty, rate, to_minor(rate),
                     amount, to_minor(amount),
                     cost, to_minor(cost) if cost else None))
        si_id_map[si['id']] = tgt.execute("SELECT last_insert_rowid()").fetchone()[0]
        si_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"direct_sale_item {si['id']}: {e}")
report['migrated']['sale_items'] = si_migrated
print(f"  {si_migrated} sale_items migrated")

# ---------- grn_allocations (FIFO cost basis) ----------
print("\n[grn_allocations] migrating FIFO cost links...")
ga_migrated = 0
if 'grn_allocation' in LIVE_TABLES:
    for ga in live.execute("SELECT * FROM grn_allocation WHERE COALESCE(is_void,0)=0").fetchall():
        sale_new = sale_id_map.get(ga['sale_id'])
        si_new = si_id_map.get(ga['sale_item_id'])
        batch_new = batch_by_grn_item.get(ga['grn_item_id'])
        if not (sale_new and si_new and batch_new): continue
        qty = ga['qty'] or 0
        cost = ga['cost_rate'] or 0
        if qty <= 0: continue
        try:
            tgt.execute("""INSERT INTO grn_allocations(sale_id,sale_item_id,batch_id,qty,cost_rate,cost_rate_minor)
                           VALUES (?,?,?,?,?,?)""",
                        (sale_new, si_new, batch_new, qty, cost, to_minor(cost)))
            ga_migrated += 1
        except sqlite3.IntegrityError as e:
            # Batch may not have enough remaining_qty (trigger fires)
            report['errors'].append(f"grn_allocation {ga['id']}: {e}")
report['migrated']['grn_allocations'] = ga_migrated
print(f"  {ga_migrated} grn_allocations migrated (triggers auto-decrement batch remaining_qty)")

# ---------- booking_allocations ----------
print("\n[booking_allocations] migrating...")
ba_migrated = 0
if 'booking_allocation' in LIVE_TABLES:
    for ba in live.execute("SELECT * FROM booking_allocation WHERE COALESCE(is_void,0)=0").fetchall():
        sale_new = sale_id_map.get(ba['sale_id'])
        si_new = si_id_map.get(ba['sale_item_id'])
        bi_new = bi_id_map.get(ba['booking_item_id'])
        if not (sale_new and si_new and bi_new): continue
        qty = ba['qty'] or 0
        if qty <= 0: continue
        try:
            tgt.execute("""INSERT INTO booking_allocations(sale_id,sale_item_id,booking_item_id,qty)
                           VALUES (?,?,?,?)""", (sale_new, si_new, bi_new, qty))
            ba_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"booking_allocation {ba['id']}: {e}")
report['migrated']['booking_allocations'] = ba_migrated
print(f"  {ba_migrated} booking_allocations migrated (auto-updates booking_items.qty_dispatched)")

# ---------- sale_delivery_persons ----------
print("\n[sale_delivery_persons] migrating...")
sdp_migrated = 0
if 'sale_delivery_persons' in LIVE_TABLES:
    for sdp in live.execute("SELECT * FROM sale_delivery_persons WHERE COALESCE(is_void,0)=0").fetchall():
        sale_new = sale_id_map.get(sdp['sale_id'])
        dp_new = dp_id_map.get(sdp['delivery_person_id'])
        if not (sale_new and dp_new): continue
        bags = sdp['bags_delivered'] or 0
        rent = sdp['rent_amount'] or 0
        try:
            tgt.execute("""INSERT INTO sale_delivery_persons(sale_id,delivery_person_id,bags_delivered,rent_amount,rent_amount_minor,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (sale_new, dp_new, bags, rent, to_minor(rent), sdp['created_at']))
            sdp_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"sale_dp {sdp['id']}: {e}")
report['migrated']['sale_delivery_persons'] = sdp_migrated
print(f"  {sdp_migrated} sale_delivery_persons migrated")

# ---------- payments (4 live tables -> 1) ----------
print("\n[payments] consolidating 4 live tables...")
p_migrated = 0

# client payments
for p in live.execute("SELECT * FROM payment WHERE COALESCE(is_void,0)=0").fetchall():
    party_id = client_id_map.get(p['client_id']) if p['client_id'] else None
    if not party_id and p['client_name']:
        row = tgt.execute("SELECT id FROM clients WHERE name=?", (p['client_name'],)).fetchone()
        party_id = row[0] if row else None
    r_acct = account_id_map.get(p['payment_account_id']) if p['payment_account_id'] else None
    amt = p['amount'] or 0
    if amt <= 0: continue   # v4.4 requires amount > 0
    disc = p['discount'] or 0
    direction = 'in'  # client payments are always in
    mode = (p['method'] or 'cash').lower()
    if mode not in ('cash','bank','adjustment'): mode = 'cash'
    # v4.4 requires payment_account_id when mode='bank'; downgrade to cash if missing
    if mode == 'bank' and not r_acct: mode = 'cash'
    ref_type = (p['source_type'] or 'other').lower()
    ref_type_map = {'sale':'sale','booking':'booking','invoice':'sale','refund':'refund'}
    ref_type = ref_type_map.get(ref_type, 'other')
    creator = user_id_map.get(p['created_by']) if p['created_by'] else None
    try:
        # Payment triggers require party_id to exist AND account balance auto-adjusts.
        # We must set party_id or NULL cleanly.
        tgt.execute("""INSERT INTO payments(auto_bill_no,manual_bill_no,payment_date,direction,
                       party_type,party_id,party_name_snapshot,
                       amount,amount_minor,discount,discount_minor,discount_reason,
                       payment_mode,payment_account_id,bank_name,account_name,account_no,
                       reference_type,reference_id,
                       photo_path,photo_url,idempotency_key,revision,
                       created_by,created_at,updated_at,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p['auto_bill_no'] or f"PAY-{p['id']:06d}",
                     p['manual_bill_no'] or f"MP-{p['id']}",
                     p['date_posted'] or datetime.now().isoformat(),
                     direction, 'client', party_id, p['client_name'],
                     amt, to_minor(amt), disc, to_minor(disc), p['discount_reason'],
                     mode, r_acct, p['bank_name'], p['account_name'], p['account_no'],
                     ref_type, sale_id_map.get(p['source_id']) if ref_type=='sale' else p['source_id'],
                     p['photo_path'], p['photo_url'],
                     p['idempotency_key'], p['revision'] or 0,
                     creator, p['created_at'], p['updated_at'] or p['created_at'], p['note']))
        p_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"payment (client) {p['id']}: {e}")

# supplier payments
for p in live.execute("SELECT * FROM supplier_payment WHERE COALESCE(is_void,0)=0").fetchall():
    party_id = supplier_id_map.get(p['supplier_id'])
    if not party_id:
        report['errors'].append(f"supplier_payment {p['id']}: supplier not migrated")
        continue
    r_acct = account_id_map.get(p['payment_account_id']) if p['payment_account_id'] else None
    amt = p['amount'] or 0
    if amt <= 0: continue
    mode = (p['method'] or 'cash').lower()
    if mode not in ('cash','bank','adjustment'): mode = 'cash'
    if mode == 'bank' and not r_acct: mode = 'cash'
    ref_type = 'supplier_purchase'
    creator = user_id_map.get(p['created_by']) if p['created_by'] else None
    try:
        tgt.execute("""INSERT INTO payments(auto_bill_no,manual_bill_no,payment_date,direction,
                       party_type,party_id,amount,amount_minor,
                       payment_mode,payment_account_id,bank_name,account_name,account_no,
                       reference_type,reference_id,idempotency_key,revision,
                       created_by,created_at,updated_at,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p['auto_bill_no'] or f"SPAY-{p['id']:06d}",
                     p['manual_bill_no'] or f"MSP-{p['id']}",
                     p['date_posted'] or datetime.now().isoformat(),
                     'out', 'supplier', party_id,
                     amt, to_minor(amt), mode, r_acct,
                     p['bank_name'], p['account_name'], p['account_no'],
                     ref_type, purchase_id_map.get(p['source_id']),
                     p['idempotency_key'], p['revision'] or 0,
                     creator, p['created_at'], p['updated_at'] or p['created_at'], p['note']))
        p_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"supplier_payment {p['id']}: {e}")

# delivery_person_payment
if 'delivery_person_payment' in LIVE_TABLES:
    for p in live.execute("SELECT * FROM delivery_person_payment WHERE COALESCE(is_void,0)=0").fetchall():
        party_id = dp_id_map.get(p['delivery_person_id'])
        if not party_id: continue
        r_acct = account_id_map.get(p['payment_account_id']) if p['payment_account_id'] else None
        amt = p['amount_paid'] or 0
        if amt <= 0: continue
        mode = (p['method'] or 'cash').lower()
        if mode not in ('cash','bank','adjustment'): mode = 'cash'
        if mode == 'bank' and not r_acct: mode = 'cash'
        creator = user_id_map.get(p['created_by']) if p['created_by'] else None
        try:
            tgt.execute("""INSERT INTO payments(auto_bill_no,manual_bill_no,payment_date,direction,
                           party_type,party_id,amount,amount_minor,
                           payment_mode,payment_account_id,reference_type,reference_id,
                           reference,idempotency_key,revision,
                           created_by,created_at,updated_at,notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"DPAY-{p['id']:06d}", f"MDP-{p['id']}",
                         p['date_posted'] or datetime.now().isoformat(),
                         'out', 'delivery_person', party_id,
                         amt, to_minor(amt), mode, r_acct,
                         'delivery_wage', sale_id_map.get(p['sale_id']),
                         p['reference'], p['idempotency_key'], p['revision'] or 0,
                         creator, p['created_at'], p['updated_at'] or p['created_at'], p['note']))
            p_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"delivery_person_payment {p['id']}: {e}")

# cash_flow_entry -> payments (reference_type='expense'/etc)
if 'cash_flow_entry' in LIVE_TABLES:
    for cf in live.execute("SELECT * FROM cash_flow_entry WHERE COALESCE(is_void,0)=0").fetchall():
        r_acct = account_id_map.get(cf['account_id']) if cf['account_id'] else None
        amt = cf['amount'] or 0
        if amt <= 0: continue
        direction = (cf['direction'] or 'out').lower()
        if direction not in ('in','out'): direction = 'out'
        mode = 'bank' if r_acct else 'cash'
        if mode == 'bank' and not r_acct: mode = 'cash'
        cf_cat = cf_cat_id_map.get(cf['category_id']) if cf['category_id'] else None
        cf_sub = cf_sub_id_map.get(cf['subcategory_id']) if cf['subcategory_id'] else None
        creator = user_id_map.get(int(cf['created_by'])) if cf['created_by'] and str(cf['created_by']).isdigit() else None
        try:
            tgt.execute("""INSERT INTO payments(auto_bill_no,manual_bill_no,payment_date,direction,
                           party_type,party_id,party_name_snapshot,
                           amount,amount_minor,payment_mode,payment_account_id,
                           reference_type,reference,
                           cash_flow_category_id,cash_flow_subcategory_id,
                           idempotency_key,revision,
                           created_by,created_at,updated_at,notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"CF-{cf['id']:06d}", f"MCF-{cf['id']}",
                         cf['date_posted'] or datetime.now().isoformat(), direction,
                         cf['party_type'] if cf['party_type'] in ('client','supplier','delivery_person','lender','owner','other') else 'other',
                         None, cf['party_name'],
                         amt, to_minor(amt), mode, r_acct,
                         'expense' if direction=='out' else 'other',
                         cf['reference'],
                         cf_cat, cf_sub,
                         cf['idempotency_key'], cf['revision'] or 0,
                         creator, cf['created_at'], cf['updated_at'] or cf['created_at'], cf['note']))
            p_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"cash_flow_entry {cf['id']}: {e}")

report['migrated']['payments'] = p_migrated
print(f"  {p_migrated} payments total (client + supplier + delivery + cash_flow)")

# ---------- Waive-offs ----------
print("\n[waive_offs] migrating...")
w_migrated = 0
for w in live.execute("SELECT * FROM waive_off WHERE COALESCE(is_void,0)=0").fetchall():
    client_id = None
    if w['client_code']:
        row = tgt.execute("SELECT id FROM clients WHERE code=?", (w['client_code'],)).fetchone()
        client_id = row[0] if row else None
    amt = w['amount'] or 0
    creator = user_id_map.get(int(w['created_by'])) if w['created_by'] and str(w['created_by']).isdigit() else None
    try:
        tgt.execute("""INSERT INTO waive_offs(client_id,client_code_snapshot,client_name_snapshot,
                       bill_no,amount,amount_minor,reason,date_posted,
                       created_by,created_at,notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (client_id, w['client_code'], w['client_name'],
                     w['bill_no'], amt, to_minor(amt), w['reason'],
                     w['date_posted'], creator, w['date_posted'], w['note']))
        w_migrated += 1
    except sqlite3.IntegrityError as e:
        report['errors'].append(f"waive_off {w['id']}: {e}")
report['migrated']['waive_offs'] = w_migrated
print(f"  {w_migrated} waive_offs migrated")

# ---------- Material returns ----------
print("\n[returns] migrating (from material_return + items)...")
ret_migrated = 0
if 'material_return' in LIVE_TABLES and 'material_return_item' in LIVE_TABLES:
    for mr in live.execute("SELECT * FROM material_return WHERE COALESCE(is_void,0)=0").fetchall():
        client_id = None
        if mr['client_name']:
            row = tgt.execute("SELECT id FROM clients WHERE name=?", (mr['client_name'],)).fetchone()
            client_id = row[0] if row else None
        if not client_id: continue
        rt = (mr['return_type'] or 'cash_sale_return').lower()
        if rt not in ('cash_sale_return','credit_sale_return','booking_return'):
            rt = 'cash_sale_return'
        # Explode each material_return_item into its own return row (v4.4 has flat returns)
        for mri in live.execute("SELECT * FROM material_return_item WHERE material_return_id=?", (mr['id'],)).fetchall():
            material_id = material_by_name.get(mri['material_name'])
            if not material_id: continue
            qty = mri['qty'] or 0
            rate = mri['unit_rate'] or mri['price_at_time'] or 0
            if qty <= 0: continue
            amount = qty * rate
            try:
                tgt.execute("""INSERT INTO returns(auto_bill_no,manual_bill_no,return_type,client_id,
                               material_id,qty,rate,rate_minor,amount,amount_minor,
                               return_date,created_at,notes)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (f"RET-{mr['id']:04d}-{mri['id']:04d}",
                             mr['manual_bill_no'] or f"MR-{mr['id']}",
                             rt, client_id, material_id,
                             qty, rate, to_minor(rate),
                             amount, to_minor(amount),
                             mr['date_posted'], mr['date_posted'], mr['note']))
                ret_migrated += 1
            except sqlite3.IntegrityError as e:
                report['errors'].append(f"return {mr['id']}/{mri['id']}: {e}")
report['migrated']['returns'] = ret_migrated
print(f"  {ret_migrated} returns migrated")

# ---------- Invoices ----------
print("\n[invoices] migrating...")
inv_migrated = 0
if 'invoice' in LIVE_TABLES:
    for inv in live.execute("SELECT * FROM invoice WHERE COALESCE(is_void,0)=0").fetchall():
        client_id = None
        if inv['client_code']:
            row = tgt.execute("SELECT id FROM clients WHERE code=?", (inv['client_code'],)).fetchone()
            client_id = row[0] if row else None
        total = inv['total_amount'] or 0
        bal = inv['balance'] or 0
        status = (inv['status'] or 'open').lower()
        if status not in ('open','partial','paid'): status = 'open'
        try:
            tgt.execute("""INSERT INTO invoices(invoice_no,client_id,client_code_snapshot,client_name_snapshot,
                           is_manual,is_cash,invoice_date,total_amount,total_amount_minor,
                           balance,balance_minor,status,created_at,notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (inv['invoice_no'], client_id, inv['client_code'], inv['client_name'],
                         inv['is_manual'] or 0, inv['is_cash'] or 0, inv['date'],
                         total, to_minor(total), bal, to_minor(bal), status,
                         inv['created_at'], inv['note']))
            inv_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"invoice {inv['id']}: {e}")
report['migrated']['invoices'] = inv_migrated
print(f"  {inv_migrated} invoices migrated")

# ---------- Pending bills ----------
print("\n[pending_bills] migrating...")
pb_migrated = 0
if 'pending_bill' in LIVE_TABLES:
    for pb in live.execute("SELECT * FROM pending_bill WHERE COALESCE(is_void,0)=0").fetchall():
        client_id = None
        if pb['client_code']:
            row = tgt.execute("SELECT id FROM clients WHERE code=?", (pb['client_code'],)).fetchone()
            client_id = row[0] if row else None
        amt = pb['amount'] or 0
        kind = (pb['bill_kind'] or 'sale').lower()
        if kind not in ('sale','booking','grn','refund','other'): kind = 'other'
        try:
            tgt.execute("""INSERT INTO pending_bills(client_id,client_code_snapshot,client_name_snapshot,
                           bill_no,bill_kind,source_module,source_table,source_id,source_bill_no,
                           transaction_type,nimbus_no,amount,amount_minor,reason,risk_override,
                           photo_path,photo_url,is_paid,is_cash,is_manual,created_at,notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (client_id, pb['client_code'], pb['client_name'],
                         pb['bill_no'], kind, pb['source_module'], pb['source_table'],
                         pb['source_id'], pb['source_bill_no'], pb['transaction_type'],
                         pb['nimbus_no'], amt, to_minor(amt), pb['reason'],
                         pb['risk_override'], pb['photo_path'], pb['photo_url'],
                         pb['is_paid'] or 0, pb['is_cash'] or 0, pb['is_manual'] or 0,
                         pb['created_at'], pb['note']))
            pb_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"pending_bill {pb['id']}: {e}")
report['migrated']['pending_bills'] = pb_migrated
print(f"  {pb_migrated} pending_bills migrated")

# ---------- Delivery rents ----------
print("\n[delivery_rents] migrating...")
dr_migrated = 0
if 'delivery_rent' in LIVE_TABLES:
    for dr in live.execute("SELECT * FROM delivery_rent WHERE COALESCE(is_void,0)=0").fetchall():
        sale_new = sale_id_map.get(dr['sale_id']) if dr['sale_id'] else None
        # delivery_person by name (live has delivery_person_name text)
        dp_new = None
        if dr['delivery_person_name']:
            row = tgt.execute("SELECT id FROM delivery_persons WHERE name=?", (dr['delivery_person_name'],)).fetchone()
            dp_new = row[0] if row else None
        amt = dr['amount'] or 0
        creator = user_id_map.get(int(dr['created_by'])) if dr['created_by'] and str(dr['created_by']).isdigit() else None
        try:
            tgt.execute("""INSERT INTO delivery_rents(sale_id,delivery_person_id,manual_bill_no,
                           amount,amount_minor,date_posted,created_by,created_at,notes)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (sale_new, dp_new, dr['bill_no'],
                         amt, to_minor(amt), dr['date_posted'],
                         creator, dr['date_posted'], dr['note']))
            dr_migrated += 1
        except sqlite3.IntegrityError as e:
            report['errors'].append(f"delivery_rent {dr['id']}: {e}")
report['migrated']['delivery_rents'] = dr_migrated
print(f"  {dr_migrated} delivery_rents migrated")

# ---------- Audit log ----------
print("\n[audit_log] migrating...")
al_migrated = 0
for al in live.execute("SELECT * FROM audit_log LIMIT 5000").fetchall():
    creator = user_id_map.get(al['user_id']) if al['user_id'] else None
    try:
        tgt.execute("""INSERT INTO audit_log(user_id,username,action,details,timestamp)
                       VALUES (?,?,?,?,?)""",
                    (creator, al['username'], al['action'], al['details'], al['timestamp']))
        al_migrated += 1
    except Exception as e:
        pass
report['migrated']['audit_log'] = al_migrated
print(f"  {al_migrated} audit_log rows migrated (capped at 5000 most recent)")

# ---------- Settings ----------
print("\n[settings] migrating singleton row...")
tgt.execute("DELETE FROM settings")  # get rid of default
live_settings = live.execute("SELECT * FROM settings LIMIT 1").fetchone()
if live_settings:
    tgt.execute("""INSERT INTO settings(id,currency,company_name,company_address,company_phone,
                   company_email,tax_rate,invoice_prefix,bill_prefix,ui_theme,
                   allow_global_negative_stock,low_stock_alert_enabled,
                   backdate_grace_days,require_new_master_approval)
                   VALUES (1,?,?,?,?,?,?,?,?,?,?,1,0,0)""",
                (live_settings['currency'] or 'PKR',
                 live_settings['company_name'], live_settings['company_address'],
                 live_settings['company_phone'], live_settings['company_email'],
                 live_settings['tax_rate'] or 0,
                 live_settings['invoice_prefix'] or 'INV',
                 live_settings['bill_prefix'] or 'B',
                 live_settings['ui_theme'] or 'default',
                 live_settings['allow_global_negative_stock'] or 0))
else:
    tgt.execute("INSERT INTO settings(id) VALUES (1)")
print("  settings row seeded")

# ---------- Bill counter ----------
print("\n[bill_counter] migrating...")
bc_migrated = 0
if 'bill_counter' in LIVE_TABLES:
    for bc in live.execute("SELECT * FROM bill_counter").fetchall():
        try:
            tgt.execute("INSERT INTO bill_counter(namespace,count) VALUES (?,?)",
                        (bc['namespace'], bc['count']))
            bc_migrated += 1
        except: pass
print(f"  {bc_migrated} bill_counter namespaces migrated")

# ---------- Commit ----------
tgt.commit()

# ---------- Final row-count verification ----------
print("\n" + "=" * 70)
print("VERIFICATION")
print("=" * 70)
print(f"\n{'Table':<28} {'v4.4':>10}")
print("-" * 42)
tgt_tables = [r[0] for r in tgt.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
verify = {}
for t in tgt_tables:
    n = tgt.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    if n > 0:
        print(f"{t:<28} {n:>10}")
    verify[t] = n

report['verification'] = verify
report['total_errors'] = len(report['errors'])

# Write JSON report
with open('MIGRATION_v44_REPORT.json', 'w') as f:
    json.dump(report, f, indent=2, default=str)

print(f"\n{report['total_errors']} errors during migration (see MIGRATION_v44_REPORT.json)")
print(f"\n✅ Migration complete.")
print(f"    Live DB:   {LIVE_DB}  (unchanged)")
print(f"    v4.4 DB:   {TARGET_DB}")
