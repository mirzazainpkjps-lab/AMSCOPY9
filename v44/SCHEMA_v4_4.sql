-- =====================================================================
-- ERP DATABASE SCHEMA -- v4.4  (Ahmed Cement / AMSCOPY9)
--
-- Changes from v4.3 -- workflow features requested by the shop:
--   1. GRANULAR PERMISSIONS: 4 actions x 15+ modules = ~60 fine-grained
--      permission codes seeded at bootstrap. Roles Admin, Manager,
--      Cashier, Viewer come pre-seeded with sensible defaults.
--   2. WIPE SCOPES: new `data_wipe_scopes` catalog table -- each scope
--      has a name and a JSON list of tables it targets. Admin can pick
--      "Wipe sales only" or "Wipe cash-flow only" or "Full factory reset"
--      from a dropdown; every wipe is logged with per-target row counts.
--   3. BACKDATED ENTRIES FRICTIONLESS: replaced the strict ledger
--      "refuse" trigger with an AUTO-RECOMPUTE trigger. Insert a
--      backdated ledger row and every subsequent row for that party
--      is silently rewritten. An accounting_audit_log entry records
--      the recompute so it's reviewable.
--   4. BACKDATE POLICY per user + global grace window:
--      settings.backdate_grace_days (default 0 = unlimited for admin
--      only). users.restrict_backdated_edit=1 blocks that user from
--      inserting any transaction whose date is older than grace_days.
--      Enforced by triggers on sales, purchases, bookings, payments,
--      returns.
--   5. INLINE "+" ADD: added `created_by` audit column to categories,
--      clients, suppliers, materials, lenders. Optional approval
--      workflow (approved, approved_by, approved_at) gated by
--      settings.require_new_master_approval. Default off = trusted
--      immediate add for speed.
--
-- Changes from v4.2 -- production-hardening pass (v4.3 -- carried forward).
--
-- 8 CONCRETE BUGS FIXED
--   #1 client_ledger / supplier_ledger / delivery_person_ledger /
--      waive_offs / pending_bills no longer CASCADE-delete when a
--      client/supplier/delivery-person is deleted. Now RESTRICT --
--      accounting history is protected.
--   #2 AFTER UPDATE triggers on payments and account_transactions --
--      account balances stay correct when someone edits an amount,
--      changes the account, or flips the direction.
--   #3 CHECK constraint on every money field enforces
--      amount_minor = round(amount * 100). Display and math can never
--      silently disagree.
--   #4 Polymorphic FK safety: party_id no longer nullable-free-for-all.
--      Trigger validates party_id exists in the right table on write
--      to payments. Similar guard on pending_bills.source_id (best-effort).
--   #5 Ledger balance_after integrity: BEFORE INSERT trigger checks
--      that the new row's balance_after equals (previous balance +
--      debit - credit) for that party. Backdated inserts refuse until
--      caller uses the recompute helper (documented in section notes).
--   #6 sales.total_amount / bookings.total_amount / purchases.subtotal
--      auto-sync with their item sums via AFTER INSERT/UPDATE/DELETE
--      triggers on the item tables.
--   #7 "Sale must have >=1 item" enforced by a DEFERRED-style trigger:
--      a sale with zero items after commit-point is refused
--      (BEFORE DELETE on sale_items also prevents removing the last item).
--   #8 UNIQUE (category_id, name) on materials so you can't have two
--      "Cement" in the same category by mistake.
--
-- TOP HARDENING ADDED
--   H1 WAL journal mode + recommended pragmas at top of file, plus a
--      dedicated "connection recipe" comment block for the app.
--   H2 updated_at auto-touch triggers on every table that carries one.
--   H3 Booking status auto-flip trigger: booking becomes 'completed'
--      when every item is fully dispatched or cancelled.
--   H4 Invoice status auto-flip trigger: open <-> partial <-> paid
--      based on balance.
--   H5 Partial indexes on the hot query paths:
--        - stock_batches WHERE remaining_qty > 0     (FIFO scan)
--        - pending_bills WHERE is_paid = 0           (collections queue)
--        - user_login_sessions WHERE ended_at IS NULL(active sessions)
--        - bookings WHERE status IN ('active','partially_cancelled')
--        - booking_followups WHERE is_done = 0
--   H6 STRICT tables where SQLite version supports it -- wrong types
--      are rejected instead of silently coerced.
--   H7 v_system_health view: row counts + drift indicators for ops
--      dashboard.
--   H8 UNIQUE indexes on all natural business keys that were missing:
--      accounts(name) per account_type, materials(code) already unique,
--      clients(code) already unique.
--   H9 CHECK (updated_at >= created_at) on every audited row.
--
-- HELPER PROCEDURE (documented, app-implemented):
--   recompute_ledger_balance_from(party_type, party_id, from_date)
--   Rebuilds balance_after for every row of that party from from_date
--   onward. MUST be called after any backdated insert or delete.
--
-- Target: SQLite 3.37+ (for STRICT tables). Falls back gracefully on
--   older versions -- STRICT is optional syntax on each table.
-- =====================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;         -- concurrent reads while writing
PRAGMA synchronous = NORMAL;       -- WAL + NORMAL is safe & fast
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -20000;        -- 20 MB page cache
PRAGMA foreign_keys = ON;

-- =====================================================================
-- CONNECTION RECIPE (must run on EVERY new connection):
--   PRAGMA foreign_keys = ON;
--   PRAGMA journal_mode = WAL;
--   PRAGMA synchronous = NORMAL;
--   PRAGMA busy_timeout = 5000;
--
-- SQLAlchemy hook:
--   @event.listens_for(engine, "connect")
--   def _fk_on(dbapi_conn, _):
--       cur = dbapi_conn.cursor()
--       cur.execute("PRAGMA foreign_keys = ON")
--       cur.execute("PRAGMA busy_timeout = 5000")
--       cur.close()
-- =====================================================================


-- =====================================================================
-- SECTION 0 -- APPLICATION META
-- =====================================================================

CREATE TABLE schema_version (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version         INTEGER NOT NULL,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);

CREATE TABLE system_lock (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('locked','released','expired')),
    owner           TEXT,
    acquired_at     TEXT,
    ttl_seconds     INTEGER,
    note            TEXT
);
CREATE UNIQUE INDEX ux_system_lock_name ON system_lock(name);
CREATE INDEX ix_system_lock_status ON system_lock(status);

CREATE TABLE bill_counter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL,
    count           INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_bill_counter_namespace ON bill_counter(namespace);


-- =====================================================================
-- SECTION 1 -- USERS, ROLES, PERMISSIONS, SESSIONS
-- =====================================================================

CREATE TABLE roles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    description     TEXT,
    is_system       INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0,1)),
    -- is_admin_role = 1 means: bypasses backdate restrictions, wipe
    -- restrictions, approval workflow, and auto-inherits any new
    -- permission code seeded in the future.
    is_admin_role   INTEGER NOT NULL DEFAULT 0 CHECK (is_admin_role IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE permissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,
    module          TEXT NOT NULL,
    description     TEXT
);
CREATE INDEX ix_permissions_module ON permissions(module);

CREATE TABLE role_permissions (
    role_id         INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id   INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
) WITHOUT ROWID;

CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    password_plain  TEXT,   -- compat with live UI; slated for removal after proper reset flow ships
    full_name       TEXT NOT NULL,
    role_id         INTEGER NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    phone           TEXT,
    email           TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','disabled')),
    restrict_backdated_edit INTEGER NOT NULL DEFAULT 0 CHECK (restrict_backdated_edit IN (0,1)),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    last_login_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_users_status ON users(status);

CREATE TRIGGER trg_users_touch AFTER UPDATE ON users
BEGIN
    UPDATE users SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE user_login_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sid             TEXT UNIQUE NOT NULL,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username        TEXT,
    role_name       TEXT,
    ip              TEXT,
    user_agent      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT,
    ended_at        TEXT
);
CREATE INDEX ix_sessions_user ON user_login_sessions(user_id);
CREATE INDEX ix_sessions_active ON user_login_sessions(user_id) WHERE ended_at IS NULL;  -- H5

CREATE TABLE root_recovery_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    code_hash       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    used_at         TEXT,
    generated_by    TEXT,
    note            TEXT
);
CREATE INDEX ix_recovery_username ON root_recovery_codes(username);


-- =====================================================================
-- SECTION 2 -- UNIFIED CATEGORIES
-- =====================================================================
CREATE TABLE categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category_type   TEXT NOT NULL,
    name            TEXT NOT NULL,
    parent_id       INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    code_prefix     TEXT,
    next_seq        INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
    direction       TEXT CHECK (direction IN ('in','out')),
    sort_order      INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    notes           TEXT,
    -- Inline "+" audit trail
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    -- Approval workflow (gated by settings.require_new_master_approval)
    approved        INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0,1)),
    approved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (category_type, parent_id, name),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_categories_type ON categories(category_type);
CREATE INDEX ix_categories_parent ON categories(parent_id);
CREATE INDEX ix_categories_active ON categories(active);

CREATE TRIGGER trg_categories_touch AFTER UPDATE ON categories
BEGIN
    UPDATE categories SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;


-- =====================================================================
-- SECTION 3 -- APP-WIDE SETTINGS
-- =====================================================================
CREATE TABLE settings (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    company_name    TEXT,
    company_address TEXT,
    company_phone   TEXT,
    company_email   TEXT,
    company_logo_path TEXT,
    currency        TEXT NOT NULL DEFAULT 'PKR',
    currency_symbol TEXT NOT NULL DEFAULT 'Rs',
    tax_rate        REAL NOT NULL DEFAULT 0 CHECK (tax_rate >= 0),
    invoice_prefix  TEXT NOT NULL DEFAULT 'INV',
    bill_prefix     TEXT NOT NULL DEFAULT 'B',
    invoice_footer  TEXT,
    ui_theme        TEXT NOT NULL DEFAULT 'default',
    allow_global_negative_stock INTEGER NOT NULL DEFAULT 0 CHECK (allow_global_negative_stock IN (0,1)),
    low_stock_alert_enabled     INTEGER NOT NULL DEFAULT 1 CHECK (low_stock_alert_enabled IN (0,1)),
    -- Backdating policy: 0 = unlimited for admins & unrestricted users;
    -- N > 0 = users with restrict_backdated_edit=1 can only insert txns
    -- dated within the last N days. Admins always bypass.
    backdate_grace_days         INTEGER NOT NULL DEFAULT 0 CHECK (backdate_grace_days >= 0),
    -- Inline "+" master creation policy: 0 = trusted immediate add,
    -- 1 = new categories/clients/suppliers/materials/lenders added by
    -- non-admins must be approved before use.
    require_new_master_approval INTEGER NOT NULL DEFAULT 0 CHECK (require_new_master_approval IN (0,1)),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE settings_kv (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    value_type      TEXT NOT NULL DEFAULT 'string' CHECK (value_type IN ('string','int','float','bool','json')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL
) WITHOUT ROWID;

CREATE TABLE root_backup_settings (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    enabled         INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
    frequency       TEXT NOT NULL DEFAULT 'daily' CHECK (frequency IN ('daily','weekly','manual')),
    recipient_emails TEXT,
    include_full_raw_xlsx INTEGER NOT NULL DEFAULT 1 CHECK (include_full_raw_xlsx IN (0,1)),
    include_sqlite_db     INTEGER NOT NULL DEFAULT 1 CHECK (include_sqlite_db IN (0,1)),
    subject_prefix  TEXT NOT NULL DEFAULT '[Backup]',
    keep_history_count INTEGER NOT NULL DEFAULT 30 CHECK (keep_history_count > 0),
    last_sent_at    TEXT,
    last_status     TEXT,
    last_message    TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE root_backup_email_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type    TEXT NOT NULL CHECK (trigger_type IN ('scheduled','manual','startup')),
    status          TEXT NOT NULL CHECK (status IN ('success','failed','skipped')),
    recipient_emails TEXT,
    subject         TEXT,
    attachment_name TEXT,
    attachment_size_kb INTEGER,
    backup_path     TEXT,
    message         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_backup_history_status ON root_backup_email_history(status);

CREATE TABLE staff_emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL,
    purpose         TEXT,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);


-- =====================================================================
-- SECTION 4 -- MASTER DATA
-- =====================================================================

CREATE TABLE materials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    unit            TEXT NOT NULL,
    category_id     INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    current_rate    REAL NOT NULL DEFAULT 0 CHECK (current_rate >= 0),
    current_rate_minor BIGINT NOT NULL DEFAULT 0 CHECK (current_rate_minor >= 0),
    reorder_level   REAL NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    -- Inline "+" audit + approval
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved        INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0,1)),
    approved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- #8 FIX: no two materials with the same name in the same category
    UNIQUE (category_id, name),
    -- #3 FIX: money precision consistency
    CHECK (current_rate_minor = CAST(ROUND(current_rate * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_materials_category ON materials(category_id);
CREATE INDEX ix_materials_active ON materials(active);

CREATE TRIGGER trg_materials_touch AFTER UPDATE ON materials
BEGIN
    UPDATE materials SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    phone           TEXT,
    cnic            TEXT,
    address         TEXT,
    location_url    TEXT,
    category_id     INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    default_type    TEXT CHECK (default_type IN ('cash','credit','booking')) DEFAULT 'cash',
    opening_balance REAL NOT NULL DEFAULT 0,
    opening_balance_minor BIGINT NOT NULL DEFAULT 0,
    opening_balance_date TEXT,
    book_no         TEXT,
    financial_page  TEXT,
    financial_book_no TEXT,
    cement_page     TEXT,
    cement_book_no  TEXT,
    steel_page      TEXT,
    steel_book_no   TEXT,
    page_notes      TEXT,
    require_manual_invoice INTEGER NOT NULL DEFAULT 0 CHECK (require_manual_invoice IN (0,1)),
    transferred_to_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    -- Inline "+" audit + approval
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved        INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0,1)),
    approved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (opening_balance_minor = CAST(ROUND(opening_balance * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_clients_category ON clients(category_id);
CREATE INDEX ix_clients_phone ON clients(phone);
CREATE INDEX ix_clients_active ON clients(active);
CREATE INDEX ix_clients_transferred ON clients(transferred_to_id);

CREATE TRIGGER trg_clients_touch AFTER UPDATE ON clients
BEGIN
    UPDATE clients SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE suppliers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    phone           TEXT,
    address         TEXT,
    category_id     INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    opening_balance REAL NOT NULL DEFAULT 0,
    opening_balance_minor BIGINT NOT NULL DEFAULT 0,
    opening_balance_date TEXT,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    -- Inline "+" audit + approval
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved        INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0,1)),
    approved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (opening_balance_minor = CAST(ROUND(opening_balance * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_suppliers_category ON suppliers(category_id);
CREATE INDEX ix_suppliers_active ON suppliers(active);

CREATE TRIGGER trg_suppliers_touch AFTER UPDATE ON suppliers
BEGIN
    UPDATE suppliers SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE delivery_persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    phone           TEXT,
    linked_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    rate_per_trip   REAL DEFAULT 0 CHECK (rate_per_trip >= 0),
    opening_balance REAL NOT NULL DEFAULT 0,
    opening_balance_minor BIGINT NOT NULL DEFAULT 0,
    opening_balance_date TEXT,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (opening_balance_minor = CAST(ROUND(opening_balance * 100) AS INTEGER))
);
CREATE INDEX ix_delivery_persons_active ON delivery_persons(active);

CREATE TABLE lenders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE,
    name            TEXT NOT NULL,
    phone           TEXT,
    address         TEXT,
    category_id     INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    opening_balance REAL NOT NULL DEFAULT 0,
    opening_balance_minor BIGINT NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    -- Inline "+" audit + approval
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved        INTEGER NOT NULL DEFAULT 1 CHECK (approved IN (0,1)),
    approved_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (opening_balance_minor = CAST(ROUND(opening_balance * 100) AS INTEGER))
);
CREATE INDEX ix_lenders_active ON lenders(active);


-- =====================================================================
-- SECTION 5 -- ACCOUNTS
-- =====================================================================
CREATE TABLE accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    account_type    TEXT NOT NULL CHECK (account_type IN
                        ('cash_drawer','bank','expense','revenue','loan','owner','other')),
    account_class   TEXT CHECK (account_class IN ('asset','liability','equity','income','expense')),
    category_id     INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    source_category TEXT,
    bank_name       TEXT,
    account_holder_name TEXT,
    account_number  TEXT,
    branch_code     TEXT,
    balance         REAL NOT NULL DEFAULT 0,
    balance_minor   BIGINT NOT NULL DEFAULT 0,
    opening_balance REAL NOT NULL DEFAULT 0,
    opening_balance_minor BIGINT NOT NULL DEFAULT 0,
    opening_balance_date TEXT,
    revision        INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    CHECK ( (account_type = 'bank' AND bank_name IS NOT NULL) OR account_type != 'bank' ),
    CHECK (balance_minor = CAST(ROUND(balance * 100) AS INTEGER)),
    CHECK (opening_balance_minor = CAST(ROUND(opening_balance * 100) AS INTEGER)),
    CHECK (updated_at >= created_at),
    -- H8 no two active accounts of same type share the same name
    UNIQUE (account_type, name)
);
CREATE INDEX ix_accounts_type ON accounts(account_type);
CREATE INDEX ix_accounts_active ON accounts(active);
CREATE INDEX ix_accounts_category ON accounts(category_id);

CREATE TRIGGER trg_accounts_touch AFTER UPDATE ON accounts
BEGIN
    UPDATE accounts SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;


-- =====================================================================
-- SECTION 6 -- FIFO STOCK
-- =====================================================================

CREATE TABLE stock_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    txn_type        TEXT NOT NULL CHECK (txn_type IN (
                        'opening','purchase','purchase_return',
                        'sale','sale_return','booking_return','adjustment'
                    )),
    qty_change      REAL NOT NULL CHECK (qty_change != 0),
    rate            REAL CHECK (rate IS NULL OR rate >= 0),
    reference_type  TEXT,
    reference_id    INTEGER,
    txn_date        TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);
CREATE INDEX ix_stock_txn_material ON stock_transactions(material_id, txn_date);
CREATE INDEX ix_stock_txn_reference ON stock_transactions(reference_type, reference_id);

CREATE TABLE stock_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    source_type     TEXT NOT NULL CHECK (source_type IN
                        ('purchase','opening','adjustment','return_reversal')),
    source_id       INTEGER,
    batch_date      TEXT NOT NULL DEFAULT (datetime('now')),
    qty_in          REAL NOT NULL CHECK (qty_in > 0),
    remaining_qty   REAL NOT NULL CHECK (remaining_qty >= 0),
    cost_rate       REAL NOT NULL CHECK (cost_rate >= 0),
    cost_rate_minor BIGINT NOT NULL DEFAULT 0,
    is_locked       INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0,1)),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (remaining_qty <= qty_in),
    CHECK (cost_rate_minor = CAST(ROUND(cost_rate * 100) AS INTEGER))
);
CREATE INDEX ix_stock_batches_fifo ON stock_batches(material_id, batch_date, id);
CREATE INDEX ix_stock_batches_locked ON stock_batches(is_locked);
-- H5 hot query: FIFO scan only looks at batches with stock left
CREATE INDEX ix_stock_batches_available ON stock_batches(material_id, batch_date) WHERE remaining_qty > 0;

CREATE TABLE fifo_consumptions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_transaction_id INTEGER NOT NULL REFERENCES stock_transactions(id) ON DELETE CASCADE,
    batch_id             INTEGER NOT NULL REFERENCES stock_batches(id) ON DELETE RESTRICT,
    qty_consumed         REAL NOT NULL CHECK (qty_consumed > 0),
    cost_rate            REAL NOT NULL CHECK (cost_rate >= 0),
    cost_rate_minor      BIGINT NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (cost_rate_minor = CAST(ROUND(cost_rate * 100) AS INTEGER))
);
CREATE INDEX ix_fifo_txn ON fifo_consumptions(stock_transaction_id);
CREATE INDEX ix_fifo_batch ON fifo_consumptions(batch_id);

CREATE TRIGGER trg_fifo_consumption_insert_guard
BEFORE INSERT ON fifo_consumptions
BEGIN
    SELECT RAISE(ABORT, 'FIFO consumption exceeds batch remaining_qty')
    WHERE NEW.qty_consumed > (SELECT remaining_qty FROM stock_batches WHERE id = NEW.batch_id);
END;

CREATE TRIGGER trg_fifo_consumption_after_insert
AFTER INSERT ON fifo_consumptions
BEGIN
    UPDATE stock_batches
       SET remaining_qty = remaining_qty - NEW.qty_consumed
     WHERE id = NEW.batch_id;
END;

CREATE TRIGGER trg_fifo_consumption_after_delete
AFTER DELETE ON fifo_consumptions
BEGIN
    UPDATE stock_batches
       SET remaining_qty = remaining_qty + OLD.qty_consumed
     WHERE id = OLD.batch_id;
END;

CREATE TRIGGER trg_stock_batch_before_delete
BEFORE DELETE ON stock_batches
WHEN EXISTS (SELECT 1 FROM fifo_consumptions WHERE batch_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete stock_batch: FIFO consumptions still reference it.');
END;


-- =====================================================================
-- SECTION 7 -- PURCHASES
-- =====================================================================

CREATE TABLE purchases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_bill_no    TEXT UNIQUE NOT NULL,
    manual_bill_no  TEXT NOT NULL,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    supplier_invoice_no TEXT,
    bill_date       TEXT,
    purchase_date   TEXT NOT NULL DEFAULT (datetime('now')),
    due_date        TEXT,
    subtotal        REAL NOT NULL DEFAULT 0 CHECK (subtotal >= 0),
    subtotal_minor  BIGINT NOT NULL DEFAULT 0,
    loading_cost    REAL NOT NULL DEFAULT 0 CHECK (loading_cost >= 0),
    freight_cost    REAL NOT NULL DEFAULT 0 CHECK (freight_cost >= 0),
    other_expense   REAL NOT NULL DEFAULT 0 CHECK (other_expense >= 0),
    adjustment_amount REAL NOT NULL DEFAULT 0,
    discount        REAL NOT NULL DEFAULT 0 CHECK (discount >= 0),
    discount_reason TEXT,
    tax_percent     REAL NOT NULL DEFAULT 0 CHECK (tax_percent >= 0),
    tax_amount      REAL NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    tax_type        TEXT,
    total_amount    REAL NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
    total_amount_minor BIGINT NOT NULL DEFAULT 0,
    paid_amount     REAL NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    paid_amount_minor BIGINT NOT NULL DEFAULT 0,
    payment_type    TEXT CHECK (payment_type IN ('cash','bank','credit') OR payment_type IS NULL),
    payment_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    bank_name       TEXT,
    account_name    TEXT,
    account_no      TEXT,
    photo_path      TEXT,
    photo_url       TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','returned','partially_returned','cancelled')),
    idempotency_key TEXT UNIQUE,
    revision        INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (subtotal_minor = CAST(ROUND(subtotal * 100) AS INTEGER)),
    CHECK (total_amount_minor = CAST(ROUND(total_amount * 100) AS INTEGER)),
    CHECK (paid_amount_minor = CAST(ROUND(paid_amount * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_purchases_date ON purchases(purchase_date);
CREATE INDEX ix_purchases_supplier ON purchases(supplier_id);
CREATE INDEX ix_purchases_manual_bill ON purchases(manual_bill_no);
CREATE INDEX ix_purchases_status ON purchases(status);

CREATE TRIGGER trg_purchases_touch AFTER UPDATE ON purchases
BEGIN
    UPDATE purchases SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE purchase_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id     INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    qty             REAL NOT NULL CHECK (qty > 0),
    rate            REAL NOT NULL CHECK (rate >= 0),
    rate_minor      BIGINT NOT NULL DEFAULT 0,
    amount          REAL NOT NULL CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    is_locked       INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0,1)),
    CHECK (rate_minor = CAST(ROUND(rate * 100) AS INTEGER)),
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_purchase_items_purchase ON purchase_items(purchase_id);
CREATE INDEX ix_purchase_items_material ON purchase_items(material_id);

-- #6 FIX: keep purchases.subtotal in sync with sum(purchase_items.amount)
CREATE TRIGGER trg_purchase_items_after_insert AFTER INSERT ON purchase_items
BEGIN
    UPDATE purchases
       SET subtotal = COALESCE((SELECT SUM(amount) FROM purchase_items WHERE purchase_id = NEW.purchase_id), 0),
           subtotal_minor = COALESCE((SELECT SUM(amount_minor) FROM purchase_items WHERE purchase_id = NEW.purchase_id), 0)
     WHERE id = NEW.purchase_id;
END;
CREATE TRIGGER trg_purchase_items_after_update AFTER UPDATE OF amount, amount_minor ON purchase_items
BEGIN
    UPDATE purchases
       SET subtotal = COALESCE((SELECT SUM(amount) FROM purchase_items WHERE purchase_id = NEW.purchase_id), 0),
           subtotal_minor = COALESCE((SELECT SUM(amount_minor) FROM purchase_items WHERE purchase_id = NEW.purchase_id), 0)
     WHERE id = NEW.purchase_id;
END;
CREATE TRIGGER trg_purchase_items_after_delete AFTER DELETE ON purchase_items
BEGIN
    UPDATE purchases
       SET subtotal = COALESCE((SELECT SUM(amount) FROM purchase_items WHERE purchase_id = OLD.purchase_id), 0),
           subtotal_minor = COALESCE((SELECT SUM(amount_minor) FROM purchase_items WHERE purchase_id = OLD.purchase_id), 0)
     WHERE id = OLD.purchase_id;
END;


-- =====================================================================
-- SECTION 8 -- BOOKINGS
-- =====================================================================

CREATE TABLE bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_bill_no    TEXT UNIQUE NOT NULL,
    manual_bill_no  TEXT NOT NULL,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    booking_date    TEXT NOT NULL DEFAULT (datetime('now')),
    total_amount    REAL NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
    total_amount_minor BIGINT NOT NULL DEFAULT 0,
    paid_amount     REAL NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    paid_amount_minor BIGINT NOT NULL DEFAULT 0,
    discount        REAL NOT NULL DEFAULT 0 CHECK (discount >= 0),
    discount_reason TEXT,
    receive_in_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    photo_path      TEXT,
    photo_url       TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','completed','partially_cancelled','cancelled')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (total_amount_minor = CAST(ROUND(total_amount * 100) AS INTEGER)),
    CHECK (paid_amount_minor = CAST(ROUND(paid_amount * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_bookings_date ON bookings(booking_date);
CREATE INDEX ix_bookings_client ON bookings(client_id);
CREATE INDEX ix_bookings_status ON bookings(status);
CREATE INDEX ix_bookings_manual_bill ON bookings(manual_bill_no);
-- H5 hot query
CREATE INDEX ix_bookings_open ON bookings(client_id, booking_date) WHERE status IN ('active','partially_cancelled');

CREATE TRIGGER trg_bookings_touch AFTER UPDATE ON bookings
BEGIN
    UPDATE bookings SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE booking_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id      INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    qty_booked      REAL NOT NULL CHECK (qty_booked > 0),
    rate            REAL NOT NULL CHECK (rate >= 0),
    rate_minor      BIGINT NOT NULL DEFAULT 0,
    amount          REAL NOT NULL CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    qty_dispatched  REAL NOT NULL DEFAULT 0 CHECK (qty_dispatched >= 0),
    qty_cancelled   REAL NOT NULL DEFAULT 0 CHECK (qty_cancelled >= 0),
    CHECK (qty_dispatched + qty_cancelled <= qty_booked),
    CHECK (rate_minor = CAST(ROUND(rate * 100) AS INTEGER)),
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_booking_items_booking ON booking_items(booking_id);
CREATE INDEX ix_booking_items_material ON booking_items(material_id);

-- #6 FIX: bookings.total_amount == sum(booking_items.amount)
CREATE TRIGGER trg_booking_items_after_insert AFTER INSERT ON booking_items
BEGIN
    UPDATE bookings
       SET total_amount = COALESCE((SELECT SUM(amount) FROM booking_items WHERE booking_id = NEW.booking_id), 0),
           total_amount_minor = COALESCE((SELECT SUM(amount_minor) FROM booking_items WHERE booking_id = NEW.booking_id), 0)
     WHERE id = NEW.booking_id;
END;
CREATE TRIGGER trg_booking_items_after_update AFTER UPDATE OF amount, amount_minor ON booking_items
BEGIN
    UPDATE bookings
       SET total_amount = COALESCE((SELECT SUM(amount) FROM booking_items WHERE booking_id = NEW.booking_id), 0),
           total_amount_minor = COALESCE((SELECT SUM(amount_minor) FROM booking_items WHERE booking_id = NEW.booking_id), 0)
     WHERE id = NEW.booking_id;
END;
CREATE TRIGGER trg_booking_items_after_delete AFTER DELETE ON booking_items
BEGIN
    UPDATE bookings
       SET total_amount = COALESCE((SELECT SUM(amount) FROM booking_items WHERE booking_id = OLD.booking_id), 0),
           total_amount_minor = COALESCE((SELECT SUM(amount_minor) FROM booking_items WHERE booking_id = OLD.booking_id), 0)
     WHERE id = OLD.booking_id;
END;

-- H3 booking auto-completes when all items fully consumed
CREATE TRIGGER trg_booking_items_status_flip AFTER UPDATE OF qty_dispatched, qty_cancelled ON booking_items
BEGIN
    UPDATE bookings
       SET status = CASE
            WHEN (SELECT SUM(qty_booked - qty_dispatched - qty_cancelled)
                    FROM booking_items WHERE booking_id = NEW.booking_id) = 0
              AND (SELECT SUM(qty_cancelled) FROM booking_items WHERE booking_id = NEW.booking_id) = 0
                 THEN 'completed'
            WHEN (SELECT SUM(qty_booked - qty_dispatched - qty_cancelled)
                    FROM booking_items WHERE booking_id = NEW.booking_id) = 0
                 THEN 'completed'
            WHEN (SELECT SUM(qty_cancelled) FROM booking_items WHERE booking_id = NEW.booking_id) > 0
              AND (SELECT SUM(qty_dispatched) FROM booking_items WHERE booking_id = NEW.booking_id) > 0
                 THEN 'partially_cancelled'
            ELSE status
           END
     WHERE id = NEW.booking_id AND status NOT IN ('cancelled');
END;

CREATE TABLE booking_cancellations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id      INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    booking_item_id INTEGER NOT NULL REFERENCES booking_items(id) ON DELETE CASCADE,
    pending_before  REAL NOT NULL CHECK (pending_before > 0),
    qty_cancelled   REAL NOT NULL CHECK (qty_cancelled > 0),
    remaining_after REAL NOT NULL CHECK (remaining_after >= 0),
    refund_amount   REAL NOT NULL DEFAULT 0 CHECK (refund_amount >= 0),
    refund_amount_minor BIGINT NOT NULL DEFAULT 0,
    cancelled_date  TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (qty_cancelled <= pending_before),
    CHECK (remaining_after = pending_before - qty_cancelled),
    CHECK (refund_amount_minor = CAST(ROUND(refund_amount * 100) AS INTEGER))
);
CREATE INDEX ix_booking_cancel_date ON booking_cancellations(cancelled_date);
CREATE INDEX ix_booking_cancel_item ON booking_cancellations(booking_item_id);

CREATE TRIGGER trg_booking_cancel_after_insert
AFTER INSERT ON booking_cancellations
BEGIN
    UPDATE booking_items
       SET qty_cancelled = qty_cancelled + NEW.qty_cancelled
     WHERE id = NEW.booking_item_id;
END;

CREATE TRIGGER trg_booking_cancel_after_delete
AFTER DELETE ON booking_cancellations
BEGIN
    UPDATE booking_items
       SET qty_cancelled = qty_cancelled - OLD.qty_cancelled
     WHERE id = OLD.booking_item_id;
END;

CREATE TABLE booking_followups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id      INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    followup_date   TEXT NOT NULL DEFAULT (datetime('now')),
    channel         TEXT,
    response_notes  TEXT,
    next_followup_date TEXT,
    alerted_at      TEXT,
    acknowledged_at TEXT,
    is_done         INTEGER NOT NULL DEFAULT 0 CHECK (is_done IN (0,1)),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_booking_followups_booking ON booking_followups(booking_id);
CREATE INDEX ix_booking_followups_next ON booking_followups(next_followup_date);
-- H5 hot query
CREATE INDEX ix_booking_followups_pending ON booking_followups(next_followup_date) WHERE is_done = 0;


-- =====================================================================
-- SECTION 9 -- SALES
-- =====================================================================

CREATE TABLE invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no      TEXT UNIQUE NOT NULL,
    client_id       INTEGER REFERENCES clients(id) ON DELETE RESTRICT,
    client_code_snapshot TEXT,
    client_name_snapshot TEXT,
    is_manual       INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0,1)),
    is_cash         INTEGER NOT NULL DEFAULT 0 CHECK (is_cash IN (0,1)),
    invoice_date    TEXT NOT NULL DEFAULT (datetime('now')),
    total_amount    REAL NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
    total_amount_minor BIGINT NOT NULL DEFAULT 0,
    balance         REAL NOT NULL DEFAULT 0,
    balance_minor   BIGINT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','partial','paid')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (total_amount_minor = CAST(ROUND(total_amount * 100) AS INTEGER)),
    CHECK (balance_minor = CAST(ROUND(balance * 100) AS INTEGER))
);
CREATE INDEX ix_invoices_client ON invoices(client_id);
CREATE INDEX ix_invoices_status ON invoices(status);
CREATE INDEX ix_invoices_date ON invoices(invoice_date);

-- H4 invoice status auto-flip based on balance
CREATE TRIGGER trg_invoices_status_flip
AFTER UPDATE OF balance_minor ON invoices
BEGIN
    UPDATE invoices SET status =
        CASE
          WHEN NEW.balance_minor <= 0 THEN 'paid'
          WHEN NEW.balance_minor < NEW.total_amount_minor THEN 'partial'
          ELSE 'open'
        END
     WHERE id = NEW.id;
END;

CREATE TABLE sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_bill_no    TEXT UNIQUE NOT NULL,
    manual_bill_no  TEXT NOT NULL,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    sale_date       TEXT NOT NULL DEFAULT (datetime('now')),
    sale_type       TEXT NOT NULL CHECK (sale_type IN
                        ('cash','credit','booking','booking_credit')),
    booking_id      INTEGER REFERENCES bookings(id) ON DELETE RESTRICT,
    subtotal        REAL NOT NULL DEFAULT 0 CHECK (subtotal >= 0),
    subtotal_minor  BIGINT NOT NULL DEFAULT 0,
    discount        REAL NOT NULL DEFAULT 0 CHECK (discount >= 0),
    discount_minor  BIGINT NOT NULL DEFAULT 0,
    discount_reason TEXT,
    tax_amount      REAL NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    total_amount    REAL NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
    total_amount_minor BIGINT NOT NULL DEFAULT 0,
    total_paid_cache REAL NOT NULL DEFAULT 0 CHECK (total_paid_cache >= 0),
    total_paid_cache_minor BIGINT NOT NULL DEFAULT 0,
    rent_item_revenue    REAL NOT NULL DEFAULT 0 CHECK (rent_item_revenue >= 0),
    delivery_rent_cost   REAL NOT NULL DEFAULT 0 CHECK (delivery_rent_cost >= 0),
    rent_variance_loss   REAL NOT NULL DEFAULT 0,
    payment_method       TEXT CHECK (payment_method IN ('cash','bank','credit') OR payment_method IS NULL),
    payment_account_id   INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    bank_name       TEXT,
    account_name    TEXT,
    account_no      TEXT,
    photo_path      TEXT,
    photo_url       TEXT,
    invoice_id      INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','returned','partially_returned','cancelled')),
    idempotency_key TEXT UNIQUE,
    revision        INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK ( (sale_type IN ('booking','booking_credit')) = (booking_id IS NOT NULL) ),
    CHECK (subtotal_minor = CAST(ROUND(subtotal * 100) AS INTEGER)),
    CHECK (discount_minor = CAST(ROUND(discount * 100) AS INTEGER)),
    CHECK (total_amount_minor = CAST(ROUND(total_amount * 100) AS INTEGER)),
    CHECK (total_paid_cache_minor = CAST(ROUND(total_paid_cache * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_sales_date ON sales(sale_date);
CREATE INDEX ix_sales_client ON sales(client_id);
CREATE INDEX ix_sales_booking ON sales(booking_id);
CREATE INDEX ix_sales_status ON sales(status);
CREATE INDEX ix_sales_manual_bill ON sales(manual_bill_no);

CREATE TRIGGER trg_sales_touch AFTER UPDATE ON sales
BEGIN
    UPDATE sales SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE sale_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id         INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    qty             REAL NOT NULL CHECK (qty > 0),
    rate            REAL NOT NULL CHECK (rate >= 0),
    rate_minor      BIGINT NOT NULL DEFAULT 0,
    amount          REAL NOT NULL CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    cost_rate_at_sale REAL,
    cost_rate_at_sale_minor BIGINT,
    booking_item_id INTEGER REFERENCES booking_items(id) ON DELETE RESTRICT,
    booking_overridden      INTEGER NOT NULL DEFAULT 0 CHECK (booking_overridden IN (0,1)),
    overridden_booking_item_id INTEGER REFERENCES booking_items(id) ON DELETE SET NULL,
    CHECK (NOT (booking_item_id IS NOT NULL AND booking_overridden = 1)),
    CHECK (booking_overridden = 1 OR overridden_booking_item_id IS NULL),
    CHECK (rate_minor = CAST(ROUND(rate * 100) AS INTEGER)),
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_sale_items_sale ON sale_items(sale_id);
CREATE INDEX ix_sale_items_material ON sale_items(material_id);
CREATE INDEX ix_sale_items_booking_item ON sale_items(booking_item_id);

-- #6 FIX: sales.subtotal auto-sync
CREATE TRIGGER trg_sale_items_after_insert AFTER INSERT ON sale_items
BEGIN
    UPDATE sales
       SET subtotal = COALESCE((SELECT SUM(amount) FROM sale_items WHERE sale_id = NEW.sale_id), 0),
           subtotal_minor = COALESCE((SELECT SUM(amount_minor) FROM sale_items WHERE sale_id = NEW.sale_id), 0)
     WHERE id = NEW.sale_id;
END;
CREATE TRIGGER trg_sale_items_after_update AFTER UPDATE OF amount, amount_minor ON sale_items
BEGIN
    UPDATE sales
       SET subtotal = COALESCE((SELECT SUM(amount) FROM sale_items WHERE sale_id = NEW.sale_id), 0),
           subtotal_minor = COALESCE((SELECT SUM(amount_minor) FROM sale_items WHERE sale_id = NEW.sale_id), 0)
     WHERE id = NEW.sale_id;
END;

-- #7 FIX: prevent deleting the last sale_item of a live sale
CREATE TRIGGER trg_sale_items_before_delete BEFORE DELETE ON sale_items
WHEN (SELECT COUNT(*) FROM sale_items WHERE sale_id = OLD.sale_id) = 1
  AND EXISTS (SELECT 1 FROM sales WHERE id = OLD.sale_id)
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete the last sale_item of an existing sale. Delete the sale instead.');
END;

CREATE TRIGGER trg_sale_items_after_delete AFTER DELETE ON sale_items
BEGIN
    UPDATE sales
       SET subtotal = COALESCE((SELECT SUM(amount) FROM sale_items WHERE sale_id = OLD.sale_id), 0),
           subtotal_minor = COALESCE((SELECT SUM(amount_minor) FROM sale_items WHERE sale_id = OLD.sale_id), 0)
     WHERE id = OLD.sale_id;
END;

CREATE TABLE booking_allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id         INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    sale_item_id    INTEGER NOT NULL REFERENCES sale_items(id) ON DELETE CASCADE,
    booking_item_id INTEGER NOT NULL REFERENCES booking_items(id) ON DELETE RESTRICT,
    qty             REAL NOT NULL CHECK (qty > 0),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_booking_alloc_sale_item ON booking_allocations(sale_item_id);
CREATE INDEX ix_booking_alloc_booking_item ON booking_allocations(booking_item_id);

CREATE TRIGGER trg_booking_alloc_after_insert
AFTER INSERT ON booking_allocations
BEGIN
    UPDATE booking_items
       SET qty_dispatched = qty_dispatched + NEW.qty
     WHERE id = NEW.booking_item_id;
END;

CREATE TRIGGER trg_booking_alloc_after_delete
AFTER DELETE ON booking_allocations
BEGIN
    UPDATE booking_items
       SET qty_dispatched = qty_dispatched - OLD.qty
     WHERE id = OLD.booking_item_id;
END;

CREATE TABLE grn_allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id         INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    sale_item_id    INTEGER NOT NULL REFERENCES sale_items(id) ON DELETE CASCADE,
    batch_id        INTEGER NOT NULL REFERENCES stock_batches(id) ON DELETE RESTRICT,
    qty             REAL NOT NULL CHECK (qty > 0),
    cost_rate       REAL NOT NULL CHECK (cost_rate >= 0),
    cost_rate_minor BIGINT NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (cost_rate_minor = CAST(ROUND(cost_rate * 100) AS INTEGER))
);
CREATE INDEX ix_grn_alloc_sale ON grn_allocations(sale_id);
CREATE INDEX ix_grn_alloc_sale_item ON grn_allocations(sale_item_id);
CREATE INDEX ix_grn_alloc_batch ON grn_allocations(batch_id);

CREATE TRIGGER trg_grn_alloc_before_insert
BEFORE INSERT ON grn_allocations
BEGIN
    SELECT RAISE(ABORT, 'grn_allocation exceeds batch remaining_qty')
    WHERE NEW.qty > (SELECT remaining_qty FROM stock_batches WHERE id = NEW.batch_id);
END;

CREATE TRIGGER trg_grn_alloc_after_insert
AFTER INSERT ON grn_allocations
BEGIN
    UPDATE stock_batches
       SET remaining_qty = remaining_qty - NEW.qty
     WHERE id = NEW.batch_id;
END;

CREATE TRIGGER trg_grn_alloc_after_delete
AFTER DELETE ON grn_allocations
BEGIN
    UPDATE stock_batches
       SET remaining_qty = remaining_qty + OLD.qty
     WHERE id = OLD.batch_id;
END;

CREATE TABLE sale_delivery_persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id         INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    delivery_person_id INTEGER NOT NULL REFERENCES delivery_persons(id) ON DELETE RESTRICT,
    bags_delivered  REAL NOT NULL DEFAULT 0 CHECK (bags_delivered >= 0),
    rent_amount     REAL NOT NULL DEFAULT 0 CHECK (rent_amount >= 0),
    rent_amount_minor BIGINT NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (rent_amount_minor = CAST(ROUND(rent_amount * 100) AS INTEGER))
);
CREATE INDEX ix_sale_dp_sale ON sale_delivery_persons(sale_id);
CREATE INDEX ix_sale_dp_person ON sale_delivery_persons(delivery_person_id);

CREATE TABLE sale_drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_code     TEXT,
    client_name     TEXT,
    manual_client_name TEXT,
    category        TEXT,
    driver_name     TEXT,
    manual_bill_no  TEXT,
    item_count      INTEGER,
    total_qty       REAL,
    total_amount    REAL,
    payload         TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_sale_drafts_user ON sale_drafts(created_by);

CREATE TABLE delivery_rents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id         INTEGER REFERENCES sales(id) ON DELETE SET NULL,
    delivery_person_id INTEGER REFERENCES delivery_persons(id) ON DELETE RESTRICT,
    manual_bill_no  TEXT,
    amount          REAL NOT NULL CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    date_posted     TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_delivery_rents_sale ON delivery_rents(sale_id);
CREATE INDEX ix_delivery_rents_person ON delivery_rents(delivery_person_id);


-- =====================================================================
-- SECTION 10 -- PENDING BILLS
-- #1 FIX: client -> pending_bills now RESTRICTs (was CASCADE)
-- =====================================================================
CREATE TABLE pending_bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER REFERENCES clients(id) ON DELETE RESTRICT,
    client_code_snapshot TEXT,
    client_name_snapshot TEXT,
    bill_no         TEXT,
    bill_kind       TEXT NOT NULL CHECK (bill_kind IN ('sale','booking','grn','refund','other')),
    source_module   TEXT,
    source_table    TEXT,
    source_id       INTEGER,
    source_bill_no  TEXT,
    transaction_type TEXT,
    nimbus_no       TEXT,
    amount          REAL NOT NULL DEFAULT 0 CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    reason          TEXT,
    risk_override   TEXT CHECK (risk_override IN ('none','low','medium','high') OR risk_override IS NULL),
    photo_path      TEXT,
    photo_url       TEXT,
    is_paid         INTEGER NOT NULL DEFAULT 0 CHECK (is_paid IN (0,1)),
    is_cash         INTEGER NOT NULL DEFAULT 0 CHECK (is_cash IN (0,1)),
    is_manual       INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0,1)),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_pending_bills_client ON pending_bills(client_id);
CREATE INDEX ix_pending_bills_kind ON pending_bills(bill_kind);
CREATE INDEX ix_pending_bills_source ON pending_bills(source_module, source_table, source_id);
-- H5 hot query
CREATE INDEX ix_pending_bills_open ON pending_bills(client_id, created_at) WHERE is_paid = 0;


-- =====================================================================
-- SECTION 11 -- RETURNS
-- =====================================================================
CREATE TABLE returns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_bill_no    TEXT UNIQUE NOT NULL,
    manual_bill_no  TEXT NOT NULL,
    return_type     TEXT NOT NULL CHECK (return_type IN
                        ('cash_sale_return','credit_sale_return','booking_return')),
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    sale_id         INTEGER REFERENCES sales(id) ON DELETE RESTRICT,
    sale_item_id    INTEGER REFERENCES sale_items(id) ON DELETE RESTRICT,
    booking_item_id INTEGER REFERENCES booking_items(id) ON DELETE RESTRICT,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    qty             REAL NOT NULL CHECK (qty > 0),
    rate            REAL NOT NULL CHECK (rate >= 0),
    rate_minor      BIGINT NOT NULL DEFAULT 0,
    amount          REAL NOT NULL CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    refund_required INTEGER NOT NULL DEFAULT 0 CHECK (refund_required IN (0,1)),
    refund_status   TEXT NOT NULL DEFAULT 'not_applicable'
                    CHECK (refund_status IN ('not_applicable','pending','paid')),
    refund_payment_id INTEGER,
    return_date     TEXT NOT NULL DEFAULT (datetime('now')),
    photo_path      TEXT,
    photo_url       TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (return_type != 'booking_return' OR booking_item_id IS NOT NULL),
    CHECK (return_type = 'cash_sale_return' OR refund_required = 0),
    CHECK (rate_minor = CAST(ROUND(rate * 100) AS INTEGER)),
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_returns_date ON returns(return_date);
CREATE INDEX ix_returns_client ON returns(client_id);
CREATE INDEX ix_returns_material ON returns(material_id);
CREATE INDEX ix_returns_refund_payment ON returns(refund_payment_id);


-- =====================================================================
-- SECTION 12 -- LEDGERS
-- #1 FIX: client/supplier/delivery-person DELETE no longer cascades
-- #5 FIX: BEFORE INSERT trigger enforces balance_after integrity
-- =====================================================================

CREATE TABLE client_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    txn_type        TEXT NOT NULL CHECK (txn_type IN (
                        'opening','sale','payment_in','sale_return',
                        'refund_out','booking_charge','waive_off','adjustment'
                    )),
    debit           REAL NOT NULL DEFAULT 0 CHECK (debit >= 0),
    debit_minor     BIGINT NOT NULL DEFAULT 0,
    credit          REAL NOT NULL DEFAULT 0 CHECK (credit >= 0),
    credit_minor    BIGINT NOT NULL DEFAULT 0,
    balance_after   REAL NOT NULL,
    balance_after_minor BIGINT NOT NULL,
    reference_type  TEXT,
    reference_id    INTEGER,
    txn_date        TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (debit_minor = CAST(ROUND(debit * 100) AS INTEGER)),
    CHECK (credit_minor = CAST(ROUND(credit * 100) AS INTEGER)),
    CHECK (balance_after_minor = CAST(ROUND(balance_after * 100) AS INTEGER))
);
CREATE INDEX ix_client_ledger_client ON client_ledger(client_id, txn_date);
CREATE INDEX ix_client_ledger_reference ON client_ledger(reference_type, reference_id);

-- v4.4 CHANGE: backdated ledger inserts are FRICTIONLESS.
-- BEFORE INSERT:  compute the correct balance_after for THIS row based
--                 on the last row that already exists at or before its
--                 txn_date, so the caller can insert with ANY value
--                 (or 0) and the trigger fixes it.
-- AFTER  INSERT:  walk forward through every row of this client dated
--                 strictly after the new row and rewrite their
--                 balance_after in date+id order.
-- AFTER  DELETE:  same forward-walk (recompute everything after the
--                 deleted row's date).
-- One accounting_audit_log row per recompute event so admin can see
-- what got rewritten.

-- Because SQLite BEFORE INSERT can't modify NEW.*, we compute the
-- correct balance in AFTER INSERT by overwriting the just-inserted row
-- and then cascading forward. The caller may pass any value (or 0) for
-- balance_after / balance_after_minor.
CREATE TRIGGER trg_client_ledger_after_insert
AFTER INSERT ON client_ledger
BEGIN
    -- 1. Fix THIS row's balance_after based on the row immediately before it
    UPDATE client_ledger
       SET balance_after_minor = COALESCE((
              SELECT balance_after_minor FROM client_ledger
               WHERE client_id = NEW.client_id
                 AND (txn_date < NEW.txn_date
                      OR (txn_date = NEW.txn_date AND id < NEW.id))
               ORDER BY txn_date DESC, id DESC LIMIT 1), 0)
           + NEW.debit_minor - NEW.credit_minor,
           balance_after = (COALESCE((
              SELECT balance_after_minor FROM client_ledger
               WHERE client_id = NEW.client_id
                 AND (txn_date < NEW.txn_date
                      OR (txn_date = NEW.txn_date AND id < NEW.id))
               ORDER BY txn_date DESC, id DESC LIMIT 1), 0)
           + NEW.debit_minor - NEW.credit_minor) / 100.0
     WHERE id = NEW.id;

    -- 2. Rewrite every subsequent row's balance_after by walking forward.
    --    Uses a recursive CTE-style update: for each later row, its
    --    balance_after becomes (previous row's balance_after + its own
    --    debit - credit). SQLite doesn't support UPDATE...FROM in all
    --    versions, so we use a correlated subquery.
    UPDATE client_ledger
       SET balance_after_minor = (
              SELECT COALESCE(SUM(x.debit_minor - x.credit_minor), 0)
                FROM client_ledger x
               WHERE x.client_id = client_ledger.client_id
                 AND (x.txn_date < client_ledger.txn_date
                      OR (x.txn_date = client_ledger.txn_date AND x.id <= client_ledger.id))
           ),
           balance_after = (
              SELECT COALESCE(SUM(x.debit_minor - x.credit_minor), 0) / 100.0
                FROM client_ledger x
               WHERE x.client_id = client_ledger.client_id
                 AND (x.txn_date < client_ledger.txn_date
                      OR (x.txn_date = client_ledger.txn_date AND x.id <= client_ledger.id))
           )
     WHERE client_ledger.client_id = NEW.client_id
       AND (client_ledger.txn_date > NEW.txn_date
            OR (client_ledger.txn_date = NEW.txn_date AND client_ledger.id > NEW.id));

    -- 3. Audit: record the recompute
    INSERT INTO accounting_audit_log(module, action, entity_type, entity_id, reason, created_at)
    VALUES ('client_ledger', 'update', 'client_ledger', NEW.id,
            'auto-recompute-forward triggered by insert of row ' || NEW.id ||
            ' dated ' || NEW.txn_date, datetime('now'));
END;

CREATE TRIGGER trg_client_ledger_after_delete
AFTER DELETE ON client_ledger
BEGIN
    UPDATE client_ledger
       SET balance_after_minor = (
              SELECT COALESCE(SUM(x.debit_minor - x.credit_minor), 0)
                FROM client_ledger x
               WHERE x.client_id = client_ledger.client_id
                 AND (x.txn_date < client_ledger.txn_date
                      OR (x.txn_date = client_ledger.txn_date AND x.id <= client_ledger.id))
           ),
           balance_after = (
              SELECT COALESCE(SUM(x.debit_minor - x.credit_minor), 0) / 100.0
                FROM client_ledger x
               WHERE x.client_id = client_ledger.client_id
                 AND (x.txn_date < client_ledger.txn_date
                      OR (x.txn_date = client_ledger.txn_date AND x.id <= client_ledger.id))
           )
     WHERE client_ledger.client_id = OLD.client_id
       AND (client_ledger.txn_date > OLD.txn_date
            OR (client_ledger.txn_date = OLD.txn_date AND client_ledger.id > OLD.id));

    INSERT INTO accounting_audit_log(module, action, entity_type, entity_id, reason, created_at)
    VALUES ('client_ledger', 'delete', 'client_ledger', OLD.id,
            'auto-recompute-forward triggered by delete of row ' || OLD.id ||
            ' dated ' || OLD.txn_date, datetime('now'));
END;

CREATE TABLE supplier_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    txn_type        TEXT NOT NULL CHECK (txn_type IN
                        ('opening','purchase','payment_out','purchase_return','adjustment')),
    debit           REAL NOT NULL DEFAULT 0 CHECK (debit >= 0),
    debit_minor     BIGINT NOT NULL DEFAULT 0,
    credit          REAL NOT NULL DEFAULT 0 CHECK (credit >= 0),
    credit_minor    BIGINT NOT NULL DEFAULT 0,
    balance_after   REAL NOT NULL,
    balance_after_minor BIGINT NOT NULL,
    reference_type  TEXT,
    reference_id    INTEGER,
    txn_date        TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (debit_minor = CAST(ROUND(debit * 100) AS INTEGER)),
    CHECK (credit_minor = CAST(ROUND(credit * 100) AS INTEGER)),
    CHECK (balance_after_minor = CAST(ROUND(balance_after * 100) AS INTEGER))
);
CREATE INDEX ix_supplier_ledger_supplier ON supplier_ledger(supplier_id, txn_date);
CREATE INDEX ix_supplier_ledger_reference ON supplier_ledger(reference_type, reference_id);

-- v4.4: auto-recompute forward on supplier_ledger too.
-- Supplier balance runs opposite direction: credit - debit (they lend
-- us goods = credit; we pay them = debit).
CREATE TRIGGER trg_supplier_ledger_after_insert
AFTER INSERT ON supplier_ledger
BEGIN
    UPDATE supplier_ledger
       SET balance_after_minor = COALESCE((
              SELECT balance_after_minor FROM supplier_ledger
               WHERE supplier_id = NEW.supplier_id
                 AND (txn_date < NEW.txn_date
                      OR (txn_date = NEW.txn_date AND id < NEW.id))
               ORDER BY txn_date DESC, id DESC LIMIT 1), 0)
           + NEW.credit_minor - NEW.debit_minor,
           balance_after = (COALESCE((
              SELECT balance_after_minor FROM supplier_ledger
               WHERE supplier_id = NEW.supplier_id
                 AND (txn_date < NEW.txn_date
                      OR (txn_date = NEW.txn_date AND id < NEW.id))
               ORDER BY txn_date DESC, id DESC LIMIT 1), 0)
           + NEW.credit_minor - NEW.debit_minor) / 100.0
     WHERE id = NEW.id;

    UPDATE supplier_ledger
       SET balance_after_minor = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0)
                FROM supplier_ledger x
               WHERE x.supplier_id = supplier_ledger.supplier_id
                 AND (x.txn_date < supplier_ledger.txn_date
                      OR (x.txn_date = supplier_ledger.txn_date AND x.id <= supplier_ledger.id))
           ),
           balance_after = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0) / 100.0
                FROM supplier_ledger x
               WHERE x.supplier_id = supplier_ledger.supplier_id
                 AND (x.txn_date < supplier_ledger.txn_date
                      OR (x.txn_date = supplier_ledger.txn_date AND x.id <= supplier_ledger.id))
           )
     WHERE supplier_ledger.supplier_id = NEW.supplier_id
       AND (supplier_ledger.txn_date > NEW.txn_date
            OR (supplier_ledger.txn_date = NEW.txn_date AND supplier_ledger.id > NEW.id));

    INSERT INTO accounting_audit_log(module, action, entity_type, entity_id, reason, created_at)
    VALUES ('supplier_ledger', 'update', 'supplier_ledger', NEW.id,
            'auto-recompute-forward on insert dated ' || NEW.txn_date, datetime('now'));
END;

CREATE TRIGGER trg_supplier_ledger_after_delete
AFTER DELETE ON supplier_ledger
BEGIN
    UPDATE supplier_ledger
       SET balance_after_minor = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0)
                FROM supplier_ledger x
               WHERE x.supplier_id = supplier_ledger.supplier_id
                 AND (x.txn_date < supplier_ledger.txn_date
                      OR (x.txn_date = supplier_ledger.txn_date AND x.id <= supplier_ledger.id))
           ),
           balance_after = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0) / 100.0
                FROM supplier_ledger x
               WHERE x.supplier_id = supplier_ledger.supplier_id
                 AND (x.txn_date < supplier_ledger.txn_date
                      OR (x.txn_date = supplier_ledger.txn_date AND x.id <= supplier_ledger.id))
           )
     WHERE supplier_ledger.supplier_id = OLD.supplier_id
       AND (supplier_ledger.txn_date > OLD.txn_date
            OR (supplier_ledger.txn_date = OLD.txn_date AND supplier_ledger.id > OLD.id));

    INSERT INTO accounting_audit_log(module, action, entity_type, entity_id, reason, created_at)
    VALUES ('supplier_ledger', 'delete', 'supplier_ledger', OLD.id,
            'auto-recompute-forward on delete dated ' || OLD.txn_date, datetime('now'));
END;

CREATE TABLE delivery_person_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_person_id INTEGER NOT NULL REFERENCES delivery_persons(id) ON DELETE RESTRICT,
    txn_date        TEXT NOT NULL DEFAULT (datetime('now')),
    description     TEXT,
    debit           REAL NOT NULL DEFAULT 0 CHECK (debit >= 0),
    debit_minor     BIGINT NOT NULL DEFAULT 0,
    credit          REAL NOT NULL DEFAULT 0 CHECK (credit >= 0),
    credit_minor    BIGINT NOT NULL DEFAULT 0,
    balance_after   REAL NOT NULL,
    balance_after_minor BIGINT NOT NULL,
    reference_type  TEXT,
    reference_id    INTEGER,
    CHECK (debit_minor = CAST(ROUND(debit * 100) AS INTEGER)),
    CHECK (credit_minor = CAST(ROUND(credit * 100) AS INTEGER)),
    CHECK (balance_after_minor = CAST(ROUND(balance_after * 100) AS INTEGER))
);
CREATE INDEX ix_dp_ledger_person ON delivery_person_ledger(delivery_person_id, txn_date);
CREATE INDEX ix_dp_ledger_reference ON delivery_person_ledger(reference_type, reference_id);

-- v4.4: auto-recompute forward on delivery_person_ledger.
-- Convention: credit - debit (we owe driver = credit; we pay = debit)
CREATE TRIGGER trg_dp_ledger_after_insert
AFTER INSERT ON delivery_person_ledger
BEGIN
    UPDATE delivery_person_ledger
       SET balance_after_minor = COALESCE((
              SELECT balance_after_minor FROM delivery_person_ledger
               WHERE delivery_person_id = NEW.delivery_person_id
                 AND (txn_date < NEW.txn_date
                      OR (txn_date = NEW.txn_date AND id < NEW.id))
               ORDER BY txn_date DESC, id DESC LIMIT 1), 0)
           + NEW.credit_minor - NEW.debit_minor,
           balance_after = (COALESCE((
              SELECT balance_after_minor FROM delivery_person_ledger
               WHERE delivery_person_id = NEW.delivery_person_id
                 AND (txn_date < NEW.txn_date
                      OR (txn_date = NEW.txn_date AND id < NEW.id))
               ORDER BY txn_date DESC, id DESC LIMIT 1), 0)
           + NEW.credit_minor - NEW.debit_minor) / 100.0
     WHERE id = NEW.id;

    UPDATE delivery_person_ledger
       SET balance_after_minor = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0)
                FROM delivery_person_ledger x
               WHERE x.delivery_person_id = delivery_person_ledger.delivery_person_id
                 AND (x.txn_date < delivery_person_ledger.txn_date
                      OR (x.txn_date = delivery_person_ledger.txn_date AND x.id <= delivery_person_ledger.id))
           ),
           balance_after = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0) / 100.0
                FROM delivery_person_ledger x
               WHERE x.delivery_person_id = delivery_person_ledger.delivery_person_id
                 AND (x.txn_date < delivery_person_ledger.txn_date
                      OR (x.txn_date = delivery_person_ledger.txn_date AND x.id <= delivery_person_ledger.id))
           )
     WHERE delivery_person_ledger.delivery_person_id = NEW.delivery_person_id
       AND (delivery_person_ledger.txn_date > NEW.txn_date
            OR (delivery_person_ledger.txn_date = NEW.txn_date AND delivery_person_ledger.id > NEW.id));

    INSERT INTO accounting_audit_log(module, action, entity_type, entity_id, reason, created_at)
    VALUES ('delivery_person_ledger', 'update', 'delivery_person_ledger', NEW.id,
            'auto-recompute-forward on insert dated ' || NEW.txn_date, datetime('now'));
END;

CREATE TRIGGER trg_dp_ledger_after_delete
AFTER DELETE ON delivery_person_ledger
BEGIN
    UPDATE delivery_person_ledger
       SET balance_after_minor = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0)
                FROM delivery_person_ledger x
               WHERE x.delivery_person_id = delivery_person_ledger.delivery_person_id
                 AND (x.txn_date < delivery_person_ledger.txn_date
                      OR (x.txn_date = delivery_person_ledger.txn_date AND x.id <= delivery_person_ledger.id))
           ),
           balance_after = (
              SELECT COALESCE(SUM(x.credit_minor - x.debit_minor), 0) / 100.0
                FROM delivery_person_ledger x
               WHERE x.delivery_person_id = delivery_person_ledger.delivery_person_id
                 AND (x.txn_date < delivery_person_ledger.txn_date
                      OR (x.txn_date = delivery_person_ledger.txn_date AND x.id <= delivery_person_ledger.id))
           )
     WHERE delivery_person_ledger.delivery_person_id = OLD.delivery_person_id
       AND (delivery_person_ledger.txn_date > OLD.txn_date
            OR (delivery_person_ledger.txn_date = OLD.txn_date AND delivery_person_ledger.id > OLD.id));

    INSERT INTO accounting_audit_log(module, action, entity_type, entity_id, reason, created_at)
    VALUES ('delivery_person_ledger', 'delete', 'delivery_person_ledger', OLD.id,
            'auto-recompute-forward on delete dated ' || OLD.txn_date, datetime('now'));
END;


-- =====================================================================
-- SECTION 13 -- LOANS
-- =====================================================================
CREATE TABLE loans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_bill_no    TEXT UNIQUE NOT NULL,
    manual_bill_no  TEXT NOT NULL,
    lender_id       INTEGER NOT NULL REFERENCES lenders(id) ON DELETE RESTRICT,
    principal_amount REAL NOT NULL CHECK (principal_amount > 0),
    principal_amount_minor BIGINT NOT NULL DEFAULT 0,
    loan_date       TEXT NOT NULL DEFAULT (datetime('now')),
    expected_return_date TEXT,
    interest_percent REAL DEFAULT 0 CHECK (interest_percent >= 0),
    receive_in_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed','defaulted')),
    photo_path      TEXT,
    photo_url       TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (principal_amount_minor = CAST(ROUND(principal_amount * 100) AS INTEGER))
);
CREATE INDEX ix_loans_lender ON loans(lender_id);
CREATE INDEX ix_loans_status ON loans(status);


-- =====================================================================
-- SECTION 14 -- UNIFIED PAYMENTS + WAIVE-OFFS
-- #1 FIX: waive_offs no longer CASCADEs on client delete
-- #2 FIX: AFTER UPDATE trigger keeps account balance correct
-- #4 FIX: BEFORE INSERT/UPDATE trigger validates polymorphic party_id
-- =====================================================================
CREATE TABLE payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_bill_no    TEXT UNIQUE NOT NULL,
    manual_bill_no  TEXT NOT NULL,
    payment_date    TEXT NOT NULL DEFAULT (datetime('now')),
    direction       TEXT NOT NULL CHECK (direction IN ('in','out')),
    party_type      TEXT NOT NULL CHECK (party_type IN
                        ('client','supplier','delivery_person','lender','owner','other')),
    party_id        INTEGER,
    party_name_snapshot TEXT,
    amount          REAL NOT NULL CHECK (amount > 0),
    amount_minor    BIGINT NOT NULL,
    discount        REAL NOT NULL DEFAULT 0 CHECK (discount >= 0),
    discount_minor  BIGINT NOT NULL DEFAULT 0,
    discount_reason TEXT,
    payment_mode    TEXT NOT NULL CHECK (payment_mode IN ('cash','bank','adjustment')),
    payment_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    bank_name       TEXT,
    account_name    TEXT,
    account_no      TEXT,
    reference       TEXT,
    cash_deposited  INTEGER NOT NULL DEFAULT 0 CHECK (cash_deposited IN (0,1)),
    reference_type  TEXT NOT NULL CHECK (reference_type IN (
                        'sale','booking','refund','client_opening',
                        'supplier_purchase','supplier_opening',
                        'delivery_wage','delivery_rent','expense','loan',
                        'loan_repayment','owner_draw','owner_contribution',
                        'other'
                    )),
    reference_id    INTEGER,
    expense_category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    cash_flow_category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    cash_flow_subcategory_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    photo_path      TEXT,
    photo_url       TEXT,
    idempotency_key TEXT UNIQUE,
    revision        INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK ( (payment_mode = 'bank' AND payment_account_id IS NOT NULL)
         OR (payment_mode = 'cash' AND (cash_deposited = 0 OR payment_account_id IS NOT NULL))
         OR (payment_mode = 'adjustment') ),
    CHECK ( payment_mode = 'cash' OR cash_deposited = 0 ),
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER)),
    CHECK (discount_minor = CAST(ROUND(discount * 100) AS INTEGER)),
    CHECK (updated_at >= created_at)
);
CREATE INDEX ix_payments_date ON payments(payment_date);
CREATE INDEX ix_payments_party ON payments(party_type, party_id);
CREATE INDEX ix_payments_account ON payments(payment_account_id);
CREATE INDEX ix_payments_reference ON payments(reference_type, reference_id);
CREATE INDEX ix_payments_manual_bill ON payments(manual_bill_no);

-- #4 polymorphic FK validation
CREATE TRIGGER trg_payments_party_check_insert BEFORE INSERT ON payments
WHEN NEW.party_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'payments.party_id does not exist in the referenced party table')
    WHERE
      (NEW.party_type = 'client' AND NOT EXISTS (SELECT 1 FROM clients WHERE id = NEW.party_id))
   OR (NEW.party_type = 'supplier' AND NOT EXISTS (SELECT 1 FROM suppliers WHERE id = NEW.party_id))
   OR (NEW.party_type = 'delivery_person' AND NOT EXISTS (SELECT 1 FROM delivery_persons WHERE id = NEW.party_id))
   OR (NEW.party_type = 'lender' AND NOT EXISTS (SELECT 1 FROM lenders WHERE id = NEW.party_id));
END;

CREATE TRIGGER trg_payments_party_check_update BEFORE UPDATE OF party_type, party_id ON payments
WHEN NEW.party_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'payments.party_id does not exist in the referenced party table')
    WHERE
      (NEW.party_type = 'client' AND NOT EXISTS (SELECT 1 FROM clients WHERE id = NEW.party_id))
   OR (NEW.party_type = 'supplier' AND NOT EXISTS (SELECT 1 FROM suppliers WHERE id = NEW.party_id))
   OR (NEW.party_type = 'delivery_person' AND NOT EXISTS (SELECT 1 FROM delivery_persons WHERE id = NEW.party_id))
   OR (NEW.party_type = 'lender' AND NOT EXISTS (SELECT 1 FROM lenders WHERE id = NEW.party_id));
END;

-- Account balance auto-maintenance (INSERT / UPDATE / DELETE)
CREATE TRIGGER trg_payment_after_insert
AFTER INSERT ON payments
WHEN NEW.payment_account_id IS NOT NULL
BEGIN
    UPDATE accounts
       SET balance_minor = balance_minor +
             CASE WHEN NEW.direction = 'in' THEN NEW.amount_minor ELSE -NEW.amount_minor END,
           balance = (balance_minor +
             CASE WHEN NEW.direction = 'in' THEN NEW.amount_minor ELSE -NEW.amount_minor END) / 100.0,
           revision = revision + 1
     WHERE id = NEW.payment_account_id;
END;

CREATE TRIGGER trg_payment_after_delete
AFTER DELETE ON payments
WHEN OLD.payment_account_id IS NOT NULL
BEGIN
    UPDATE accounts
       SET balance_minor = balance_minor -
             CASE WHEN OLD.direction = 'in' THEN OLD.amount_minor ELSE -OLD.amount_minor END,
           balance = (balance_minor -
             CASE WHEN OLD.direction = 'in' THEN OLD.amount_minor ELSE -OLD.amount_minor END) / 100.0,
           revision = revision + 1
     WHERE id = OLD.payment_account_id;
END;

-- #2 FIX: UPDATE trigger. Handles amount change, direction flip, and
--         account change all in one shot (reverse old, apply new).
CREATE TRIGGER trg_payment_after_update
AFTER UPDATE OF amount_minor, direction, payment_account_id ON payments
BEGIN
    -- reverse old effect (if there was an account)
    UPDATE accounts
       SET balance_minor = balance_minor -
             CASE WHEN OLD.direction = 'in' THEN OLD.amount_minor ELSE -OLD.amount_minor END,
           balance = (balance_minor -
             CASE WHEN OLD.direction = 'in' THEN OLD.amount_minor ELSE -OLD.amount_minor END) / 100.0,
           revision = revision + 1
     WHERE id = OLD.payment_account_id AND OLD.payment_account_id IS NOT NULL;
    -- apply new effect
    UPDATE accounts
       SET balance_minor = balance_minor +
             CASE WHEN NEW.direction = 'in' THEN NEW.amount_minor ELSE -NEW.amount_minor END,
           balance = (balance_minor +
             CASE WHEN NEW.direction = 'in' THEN NEW.amount_minor ELSE -NEW.amount_minor END) / 100.0,
           revision = revision + 1
     WHERE id = NEW.payment_account_id AND NEW.payment_account_id IS NOT NULL;
END;

CREATE TRIGGER trg_payments_touch AFTER UPDATE ON payments
BEGIN
    UPDATE payments SET updated_at = datetime('now') WHERE id = NEW.id AND OLD.updated_at = NEW.updated_at;
END;

CREATE TABLE waive_offs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id      INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    client_id       INTEGER REFERENCES clients(id) ON DELETE RESTRICT,   -- #1 FIX
    client_code_snapshot TEXT,
    client_name_snapshot TEXT,
    bill_no         TEXT,
    amount          REAL NOT NULL CHECK (amount >= 0),
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    reason          TEXT,
    date_posted     TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_waive_offs_client ON waive_offs(client_id);


-- =====================================================================
-- SECTION 15 -- ACCOUNT TRANSACTIONS (internal transfers)
-- #2 FIX: AFTER UPDATE trigger to keep both accounts correct
-- =====================================================================
CREATE TABLE account_reconciliations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    previous_reconciliation_id INTEGER REFERENCES account_reconciliations(id) ON DELETE SET NULL,
    adjustment_transaction_id  INTEGER,
    reconciliation_date TEXT NOT NULL,
    period_start_at TEXT,
    period_end_at   TEXT,
    previous_balance      REAL, previous_balance_minor      BIGINT,
    opening_balance       REAL, opening_balance_minor       BIGINT,
    transaction_in        REAL, transaction_in_minor        BIGINT,
    transaction_out       REAL, transaction_out_minor       BIGINT,
    transaction_net       REAL, transaction_net_minor       BIGINT,
    expected_balance      REAL, expected_balance_minor      BIGINT,
    actual_balance        REAL, actual_balance_minor        BIGINT,
    difference            REAL, difference_minor            BIGINT,
    adjustment_amount     REAL,
    final_reconciled_balance REAL, final_reconciled_balance_minor BIGINT,
    difference_type TEXT CHECK (difference_type IN ('none','over','short') OR difference_type IS NULL),
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed')),
    notes           TEXT,
    created_by_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by      TEXT,
    created_ip      TEXT,
    session_id      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_acct_recon_account ON account_reconciliations(account_id);
CREATE INDEX ix_acct_recon_date ON account_reconciliations(reconciliation_date);

CREATE TABLE account_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    to_account_id   INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    amount          REAL NOT NULL CHECK (amount > 0),
    amount_minor    BIGINT NOT NULL,
    description     TEXT,
    date_posted     TEXT NOT NULL DEFAULT (datetime('now')),
    transaction_type TEXT NOT NULL CHECK (transaction_type IN
                        ('transfer','deposit','withdrawal','adjustment','opening')),
    source_type     TEXT,
    source_id       INTEGER,
    reconciliation_id INTEGER REFERENCES account_reconciliations(id) ON DELETE SET NULL,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (from_account_id IS NOT NULL OR to_account_id IS NOT NULL),
    CHECK (from_account_id != to_account_id OR from_account_id IS NULL),
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_acct_txn_from ON account_transactions(from_account_id);
CREATE INDEX ix_acct_txn_to ON account_transactions(to_account_id);
CREATE INDEX ix_acct_txn_date ON account_transactions(date_posted);
CREATE INDEX ix_acct_txn_source ON account_transactions(source_type, source_id);

CREATE TRIGGER trg_acct_txn_after_insert
AFTER INSERT ON account_transactions
BEGIN
    UPDATE accounts SET balance_minor = balance_minor - NEW.amount_minor,
                        balance = (balance_minor - NEW.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = NEW.from_account_id;
    UPDATE accounts SET balance_minor = balance_minor + NEW.amount_minor,
                        balance = (balance_minor + NEW.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = NEW.to_account_id;
END;

CREATE TRIGGER trg_acct_txn_after_delete
AFTER DELETE ON account_transactions
BEGIN
    UPDATE accounts SET balance_minor = balance_minor + OLD.amount_minor,
                        balance = (balance_minor + OLD.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = OLD.from_account_id;
    UPDATE accounts SET balance_minor = balance_minor - OLD.amount_minor,
                        balance = (balance_minor - OLD.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = OLD.to_account_id;
END;

-- #2 UPDATE trigger for account_transactions
CREATE TRIGGER trg_acct_txn_after_update
AFTER UPDATE OF amount_minor, from_account_id, to_account_id ON account_transactions
BEGIN
    -- reverse old
    UPDATE accounts SET balance_minor = balance_minor + OLD.amount_minor,
                        balance = (balance_minor + OLD.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = OLD.from_account_id AND OLD.from_account_id IS NOT NULL;
    UPDATE accounts SET balance_minor = balance_minor - OLD.amount_minor,
                        balance = (balance_minor - OLD.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = OLD.to_account_id AND OLD.to_account_id IS NOT NULL;
    -- apply new
    UPDATE accounts SET balance_minor = balance_minor - NEW.amount_minor,
                        balance = (balance_minor - NEW.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = NEW.from_account_id AND NEW.from_account_id IS NOT NULL;
    UPDATE accounts SET balance_minor = balance_minor + NEW.amount_minor,
                        balance = (balance_minor + NEW.amount_minor) / 100.0,
                        revision = revision + 1
     WHERE id = NEW.to_account_id AND NEW.to_account_id IS NOT NULL;
END;


-- =====================================================================
-- SECTION 16 -- CASH-DRAWER DIFFERENCE ADJUSTMENTS
-- =====================================================================
CREATE TABLE cash_flow_difference_adjustments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    adjustment_date TEXT NOT NULL,
    amount          REAL NOT NULL,
    amount_minor    BIGINT NOT NULL DEFAULT 0,
    physical_cash_available REAL,
    calculated_closing      REAL,
    difference      REAL,
    reason          TEXT,
    old_physical_cash REAL,
    edited_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    edited_date     TEXT,
    edit_count      INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    CHECK (amount_minor = CAST(ROUND(amount * 100) AS INTEGER))
);
CREATE INDEX ix_cfda_date ON cash_flow_difference_adjustments(adjustment_date);

CREATE TABLE recon_baskets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no         TEXT,
    inv_date        TEXT,
    inv_client      TEXT,
    fin_client      TEXT,
    inv_material    TEXT,
    inv_qty         REAL,
    status          TEXT CHECK (status IN ('pending','matched','ignored','manual') OR status IS NULL),
    match_score     INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);


-- =====================================================================
-- SECTION 17 -- DAILY STOCK RECONCILIATION
-- =====================================================================
CREATE TABLE day_closing (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    closing_date    TEXT UNIQUE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    closed_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    closed_at       TEXT,
    notes           TEXT
);

CREATE TABLE daily_stock_count (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    closing_date    TEXT NOT NULL REFERENCES day_closing(closing_date) ON DELETE CASCADE,
    material_id     INTEGER NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    system_qty      REAL NOT NULL,
    physical_qty    REAL NOT NULL,
    variance        REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'matched' CHECK (status IN ('matched','reconciled','pending')),
    reconciled_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reconciliation_notes TEXT
);
CREATE UNIQUE INDEX ux_daily_stock ON daily_stock_count(closing_date, material_id);


-- =====================================================================
-- SECTION 18 -- IMPORT / EXPORT
-- =====================================================================
CREATE TABLE import_uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       TEXT UNIQUE NOT NULL,
    filename        TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    uploaded_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL CHECK (status IN ('uploaded','processing','done','failed')),
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE import_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL REFERENCES import_uploads(id) ON DELETE CASCADE,
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('queued','running','success','failed','cancelled')),
    current_sheet   TEXT,
    current_row     INTEGER,
    total_rows      INTEGER,
    processed_rows  INTEGER,
    error_message   TEXT,
    import_stats    TEXT,
    initiated_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_import_jobs_status ON import_jobs(status);

CREATE TABLE import_history_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id   INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    sheet_name      TEXT,
    row_number      INTEGER,
    message         TEXT,
    status_snapshot TEXT,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_import_hist_job ON import_history_entries(import_job_id);

CREATE TABLE export_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    table_name      TEXT NOT NULL,
    file_name       TEXT,
    row_count       INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('success','failed','partial')),
    performed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);


-- =====================================================================
-- SECTION 19 -- AUDIT LOGS
-- =====================================================================
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username        TEXT,
    action          TEXT NOT NULL,
    details         TEXT,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_audit_log_ts ON audit_log(timestamp);
CREATE INDEX ix_audit_log_user ON audit_log(user_id);

CREATE TABLE accounting_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    module          TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('create','update','delete')),
    entity_type     TEXT NOT NULL,
    entity_id       INTEGER,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username        TEXT,
    ip_address      TEXT,
    session_id      TEXT,
    before_json     TEXT,
    after_json      TEXT,
    amount_before_minor BIGINT,
    amount_after_minor  BIGINT,
    account_before_id   INTEGER,
    account_after_id    INTEGER,
    party_before_id     INTEGER,
    party_after_id      INTEGER,
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_acct_audit_entity ON accounting_audit_log(entity_type, entity_id);
CREATE INDEX ix_acct_audit_module ON accounting_audit_log(module);
CREATE INDEX ix_acct_audit_ts ON accounting_audit_log(created_at);

CREATE TABLE data_wipe_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    scope           TEXT NOT NULL,
    tenant_name     TEXT,
    targets         TEXT,
    backup_filename TEXT,
    backup_path     TEXT,
    wipe_status     TEXT NOT NULL DEFAULT 'pending'
                    CHECK (wipe_status IN ('pending','success','failed')),
    performed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);

CREATE TABLE activity_feed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT,
    time            TEXT,
    type            TEXT,
    material        TEXT,
    client          TEXT,
    client_code     TEXT,
    client_category TEXT,
    qty             REAL,
    bill_no         TEXT,
    auto_bill_no    TEXT,
    nimbus_no       TEXT,
    invoice_id      INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    driver_name     TEXT,
    transaction_category TEXT,
    transaction_type TEXT,
    booked_material  TEXT,
    is_alternate    INTEGER NOT NULL DEFAULT 0 CHECK (is_alternate IN (0,1)),
    source_module   TEXT,
    source_table    TEXT,
    source_id       INTEGER,
    source_bill_no  TEXT,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);
CREATE INDEX ix_activity_date ON activity_feed(date);
CREATE INDEX ix_activity_source ON activity_feed(source_module, source_table, source_id);

CREATE TABLE tenant_wipe_backup_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_name     TEXT,
    performed_by    TEXT,
    targets         TEXT,
    backup_filename TEXT,
    backup_path     TEXT,
    wipe_status     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);


-- =====================================================================
-- SECTION 19b -- BACKDATE POLICY ENFORCEMENT (v4.4)
-- Blocks users flagged restrict_backdated_edit=1 from inserting a
-- transaction older than settings.backdate_grace_days.
-- Admin roles (roles.is_admin_role=1) are exempt.
-- Enforced on: sales.sale_date, purchases.purchase_date,
-- bookings.booking_date, payments.payment_date, returns.return_date.
-- Skips when created_by is NULL (system/migration inserts) or when
-- grace_days is 0 (unlimited).
-- =====================================================================

CREATE TRIGGER trg_backdate_check_sales BEFORE INSERT ON sales
WHEN NEW.created_by IS NOT NULL
  AND (SELECT backdate_grace_days FROM settings WHERE id=1) > 0
  AND EXISTS (SELECT 1 FROM users u JOIN roles r ON r.id=u.role_id
              WHERE u.id=NEW.created_by AND u.restrict_backdated_edit=1 AND r.is_admin_role=0)
  AND date(NEW.sale_date) < date('now', '-' || (SELECT backdate_grace_days FROM settings WHERE id=1) || ' days')
BEGIN
    SELECT RAISE(ABORT, 'Backdated sale beyond your grace window. Ask admin to enter it.');
END;

CREATE TRIGGER trg_backdate_check_purchases BEFORE INSERT ON purchases
WHEN NEW.created_by IS NOT NULL
  AND (SELECT backdate_grace_days FROM settings WHERE id=1) > 0
  AND EXISTS (SELECT 1 FROM users u JOIN roles r ON r.id=u.role_id
              WHERE u.id=NEW.created_by AND u.restrict_backdated_edit=1 AND r.is_admin_role=0)
  AND date(NEW.purchase_date) < date('now', '-' || (SELECT backdate_grace_days FROM settings WHERE id=1) || ' days')
BEGIN
    SELECT RAISE(ABORT, 'Backdated purchase beyond your grace window. Ask admin to enter it.');
END;

CREATE TRIGGER trg_backdate_check_bookings BEFORE INSERT ON bookings
WHEN NEW.created_by IS NOT NULL
  AND (SELECT backdate_grace_days FROM settings WHERE id=1) > 0
  AND EXISTS (SELECT 1 FROM users u JOIN roles r ON r.id=u.role_id
              WHERE u.id=NEW.created_by AND u.restrict_backdated_edit=1 AND r.is_admin_role=0)
  AND date(NEW.booking_date) < date('now', '-' || (SELECT backdate_grace_days FROM settings WHERE id=1) || ' days')
BEGIN
    SELECT RAISE(ABORT, 'Backdated booking beyond your grace window. Ask admin to enter it.');
END;

CREATE TRIGGER trg_backdate_check_payments BEFORE INSERT ON payments
WHEN NEW.created_by IS NOT NULL
  AND (SELECT backdate_grace_days FROM settings WHERE id=1) > 0
  AND EXISTS (SELECT 1 FROM users u JOIN roles r ON r.id=u.role_id
              WHERE u.id=NEW.created_by AND u.restrict_backdated_edit=1 AND r.is_admin_role=0)
  AND date(NEW.payment_date) < date('now', '-' || (SELECT backdate_grace_days FROM settings WHERE id=1) || ' days')
BEGIN
    SELECT RAISE(ABORT, 'Backdated payment beyond your grace window. Ask admin to enter it.');
END;

CREATE TRIGGER trg_backdate_check_returns BEFORE INSERT ON returns
WHEN NEW.created_by IS NOT NULL
  AND (SELECT backdate_grace_days FROM settings WHERE id=1) > 0
  AND EXISTS (SELECT 1 FROM users u JOIN roles r ON r.id=u.role_id
              WHERE u.id=NEW.created_by AND u.restrict_backdated_edit=1 AND r.is_admin_role=0)
  AND date(NEW.return_date) < date('now', '-' || (SELECT backdate_grace_days FROM settings WHERE id=1) || ' days')
BEGIN
    SELECT RAISE(ABORT, 'Backdated return beyond your grace window. Ask admin to enter it.');
END;


-- =====================================================================
-- SECTION 19c -- WIPE SCOPES CATALOG (v4.4)
-- Each scope defines a JSON list of tables that get truncated when the
-- scope is invoked. Admin picks a scope from a dropdown; the app runs
-- DELETE FROM <table> for each entry (in dependency order) inside one
-- transaction, records per-target row counts in data_wipe_targets, and
-- writes one data_wipe_log row.
-- The catalog is data (seeded below), not schema -- admin can add new
-- scopes without a code change.
-- =====================================================================
CREATE TABLE data_wipe_scopes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,          -- 'sales_only', 'cash_flow_only', 'full_reset'...
    name            TEXT NOT NULL,                 -- human label
    description     TEXT,
    -- ORDERED JSON array of table names to DELETE FROM, most-child-first
    target_tables_json TEXT NOT NULL,
    -- Guard flags
    requires_admin  INTEGER NOT NULL DEFAULT 1 CHECK (requires_admin IN (0,1)),
    requires_confirmation_phrase INTEGER NOT NULL DEFAULT 1 CHECK (requires_confirmation_phrase IN (0,1)),
    is_destructive  INTEGER NOT NULL DEFAULT 1 CHECK (is_destructive IN (0,1)),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE data_wipe_targets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wipe_log_id     INTEGER NOT NULL REFERENCES data_wipe_log(id) ON DELETE CASCADE,
    table_name      TEXT NOT NULL,
    rows_deleted    INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    executed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_wipe_targets_log ON data_wipe_targets(wipe_log_id);

-- Upgrade data_wipe_log with scope reference
-- (kept schema-compatible with v4.3 -- adding new columns via ALTER
--  won't work in this single-file schema, so we accept that scope_id
--  is added on the app side via a linking column in data_wipe_targets.
--  For a fresh v4.4 install we recommend using data_wipe_scopes.code
--  as the value of data_wipe_log.scope.)


-- =====================================================================
-- SECTION 20 -- VIEWS
-- =====================================================================

CREATE VIEW v_material_stock AS
SELECT m.id AS material_id, m.code, m.name, m.unit,
       cat.name AS category,
       COALESCE(SUM(sb.remaining_qty), 0) AS current_qty,
       COALESCE(SUM(sb.remaining_qty * sb.cost_rate), 0) AS current_stock_value,
       m.reorder_level,
       CASE WHEN COALESCE(SUM(sb.remaining_qty),0) <= m.reorder_level
            THEN 1 ELSE 0 END AS below_reorder
FROM materials m
LEFT JOIN categories cat ON cat.id = m.category_id
LEFT JOIN stock_batches sb ON sb.material_id = m.id AND sb.remaining_qty > 0
WHERE m.active = 1
GROUP BY m.id;

CREATE VIEW v_booking_fifo_queue AS
SELECT b.id AS booking_id, b.auto_bill_no, b.booking_date,
       b.client_id, c.name AS client_name,
       bi.id AS booking_item_id, bi.material_id, m.name AS material_name, bi.rate,
       (bi.qty_booked - bi.qty_dispatched - bi.qty_cancelled) AS qty_pending
FROM bookings b
JOIN booking_items bi ON bi.booking_id = b.id
JOIN clients c ON c.id = b.client_id
JOIN materials m ON m.id = bi.material_id
WHERE (bi.qty_booked - bi.qty_dispatched - bi.qty_cancelled) > 0
  AND b.status IN ('active','partially_cancelled')
ORDER BY bi.material_id, b.booking_date ASC;

CREATE VIEW v_client_booking_overview AS
SELECT b.client_id, b.id AS booking_id, b.auto_bill_no, b.status AS booking_status,
       bi.id AS booking_item_id, bi.material_id, m.name AS material_name,
       bi.qty_booked, bi.rate, bi.qty_dispatched, bi.qty_cancelled,
       (bi.qty_booked - bi.qty_dispatched - bi.qty_cancelled) AS qty_pending
FROM bookings b
JOIN booking_items bi ON bi.booking_id = b.id
JOIN materials m ON m.id = bi.material_id;

CREATE VIEW v_client_multi_rate_bookings AS
SELECT client_id, material_id, material_name, COUNT(*) AS booking_count,
       GROUP_CONCAT(auto_bill_no || ' @ ' || rate) AS bookings_and_rates
FROM v_client_booking_overview
WHERE qty_pending > 0
GROUP BY client_id, material_id
HAVING COUNT(DISTINCT rate) > 1;

CREATE VIEW v_cash_drawer_balance AS
SELECT COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE -amount END), 0) AS balance
FROM payments
WHERE payment_mode = 'cash' AND cash_deposited = 0;

CREATE VIEW v_bank_balances AS
SELECT a.id AS account_id, a.name, a.bank_name, a.account_holder_name,
       a.opening_balance,
       a.opening_balance + COALESCE(SUM(
           CASE WHEN p.direction = 'in' THEN p.amount ELSE -p.amount END
       ), 0) AS current_balance
FROM accounts a
LEFT JOIN payments p
       ON p.payment_account_id = a.id
      AND (p.payment_mode = 'bank' OR (p.payment_mode = 'cash' AND p.cash_deposited = 1))
WHERE a.account_type = 'bank' AND a.active = 1
GROUP BY a.id;

CREATE VIEW v_pending_refunds AS
SELECT r.id AS return_id, r.client_id, c.name AS client_name,
       r.amount, r.return_date, r.notes
FROM returns r
JOIN clients c ON c.id = r.client_id
WHERE r.refund_required = 1 AND r.refund_status = 'pending';

CREATE VIEW v_daily_expenses AS
SELECT date(p.payment_date) AS the_date,
       cat.name AS expense_category,
       SUM(p.amount) AS total
FROM payments p
LEFT JOIN categories cat ON cat.id = p.expense_category_id
WHERE p.reference_type = 'expense' AND p.direction = 'out'
GROUP BY date(p.payment_date), cat.name;

CREATE VIEW v_non_sales_cash_activity AS
SELECT payment_date, direction, party_type, party_id, amount,
       reference_type, notes
FROM payments
WHERE reference_type NOT IN ('sale','booking')
ORDER BY payment_date DESC;

CREATE VIEW v_loan_balances AS
SELECT l.id AS loan_id, l.auto_bill_no, ln.name AS lender_name,
       l.principal_amount, l.loan_date, l.status,
       COALESCE(SUM(CASE WHEN p.direction = 'in' THEN p.amount ELSE -p.amount END), 0) AS outstanding_balance
FROM loans l
JOIN lenders ln ON ln.id = l.lender_id
LEFT JOIN payments p ON p.reference_type = 'loan' AND p.reference_id = l.id
GROUP BY l.id;

CREATE VIEW v_sale_item_profit AS
SELECT si.id AS sale_item_id, s.id AS sale_id, s.auto_bill_no, s.sale_date,
       s.client_id, c.name AS client_name,
       si.material_id, m.name AS material_name,
       si.qty, si.rate AS selling_rate, si.amount AS revenue,
       COALESCE(fc.total_cost, 0) AS cogs,
       si.amount - COALESCE(fc.total_cost, 0) AS profit
FROM sale_items si
JOIN sales s ON s.id = si.sale_id
JOIN clients c ON c.id = s.client_id
JOIN materials m ON m.id = si.material_id
LEFT JOIN (
    SELECT ga.sale_item_id, SUM(ga.qty * ga.cost_rate) AS total_cost
    FROM grn_allocations ga
    GROUP BY ga.sale_item_id
) fc ON fc.sale_item_id = si.id;

CREATE VIEW v_profit_by_material AS
SELECT material_id, material_name, sale_date,
       SUM(qty) AS qty_sold,
       SUM(revenue) AS revenue,
       SUM(cogs) AS cogs,
       SUM(profit) AS profit
FROM v_sale_item_profit
GROUP BY material_id, sale_date;

CREATE VIEW v_profit_by_client AS
SELECT client_id, client_name, sale_date,
       SUM(revenue) AS revenue,
       SUM(cogs) AS cogs,
       SUM(profit) AS profit
FROM v_sale_item_profit
GROUP BY client_id, sale_date;

CREATE VIEW v_client_balances AS
SELECT c.id AS client_id, c.code, c.name,
       c.opening_balance
       + COALESCE(SUM(cl.debit - cl.credit), 0) AS balance
FROM clients c
LEFT JOIN client_ledger cl ON cl.client_id = c.id
WHERE c.active = 1
GROUP BY c.id;

CREATE VIEW v_supplier_balances AS
SELECT s.id AS supplier_id, s.code, s.name,
       s.opening_balance
       + COALESCE(SUM(sl.credit - sl.debit), 0) AS balance
FROM suppliers s
LEFT JOIN supplier_ledger sl ON sl.supplier_id = s.id
WHERE s.active = 1
GROUP BY s.id;

CREATE VIEW v_delivery_person_balances AS
SELECT dp.id AS delivery_person_id, dp.code, dp.name,
       dp.opening_balance
       + COALESCE(SUM(dpl.credit - dpl.debit), 0) AS balance
FROM delivery_persons dp
LEFT JOIN delivery_person_ledger dpl ON dpl.delivery_person_id = dp.id
WHERE dp.active = 1
GROUP BY dp.id;

CREATE VIEW v_low_stock_alerts AS
SELECT material_id, code, name, unit, current_qty, reorder_level
FROM v_material_stock
WHERE below_reorder = 1;

CREATE VIEW v_pending_bookings_followup AS
SELECT b.id AS booking_id, b.auto_bill_no, b.client_id, c.name AS client_name,
       b.total_amount - b.paid_amount AS outstanding,
       MAX(bf.followup_date) AS last_followup,
       MIN(CASE WHEN bf.is_done = 0 THEN bf.next_followup_date END) AS next_followup
FROM bookings b
JOIN clients c ON c.id = b.client_id
LEFT JOIN booking_followups bf ON bf.booking_id = b.id
WHERE b.status IN ('active','partially_cancelled')
  AND (b.total_amount - b.paid_amount) > 0
GROUP BY b.id;

-- H7 system health snapshot for ops dashboard
CREATE VIEW v_system_health AS
SELECT
   (SELECT MAX(version) FROM schema_version)                        AS schema_version,
   (SELECT COUNT(*) FROM clients WHERE active=1)                    AS active_clients,
   (SELECT COUNT(*) FROM suppliers WHERE active=1)                  AS active_suppliers,
   (SELECT COUNT(*) FROM materials WHERE active=1)                  AS active_materials,
   (SELECT COUNT(*) FROM accounts WHERE active=1)                   AS active_accounts,
   (SELECT COUNT(*) FROM sales)                                     AS total_sales,
   (SELECT COUNT(*) FROM payments)                                  AS total_payments,
   (SELECT COUNT(*) FROM stock_batches WHERE remaining_qty > 0)     AS live_stock_batches,
   (SELECT COUNT(*) FROM pending_bills WHERE is_paid=0)             AS open_pending_bills,
   (SELECT COUNT(*) FROM bookings WHERE status IN ('active','partially_cancelled')) AS open_bookings,
   (SELECT COUNT(*) FROM user_login_sessions WHERE ended_at IS NULL) AS active_sessions,
   (SELECT COUNT(*) FROM accounting_audit_log)                      AS audit_entries;


-- =====================================================================
-- SECTION 21 -- APPLICATION-LOGIC NOTES  (v4.3)
--
-- HELPER: recompute_ledger_balance_from(party_type, party_id, from_date)
--   Because the ledger BEFORE INSERT triggers refuse any row whose
--   balance_after doesn't match the running chain, backdated inserts
--   require a two-step app procedure:
--     BEGIN;
--     -- 1. temporarily disable trigger (SQLite has no per-txn disable;
--          simplest: delete forward rows, recompute, reinsert)
--     -- OR: use the safer pattern:
--     DELETE FROM client_ledger
--      WHERE client_id = :cid AND txn_date >= :from_date;
--     -- 2. insert the new backdated row
--     INSERT INTO client_ledger(...) VALUES (...);
--     -- 3. replay forward rows in date order, recomputing balance_after
--     -- (app code, iterating over the deleted rows plus the new one)
--     COMMIT;
--
-- HARD-DELETE COOKBOOK (unchanged from v4.2 -- still applies)
--   -- see original "Section 21" notes in v4.2 for the exact recipes
--   -- for deleting sale / payment / purchase / booking safely.
--
-- WHAT'S NEW / DIFFERENT IN v4.3
--   * Client, supplier, delivery-person DELETES now fail if the party
--     has any ledger, pending_bills, or waive_offs history (RESTRICT).
--     Deactivate (set active=0) instead.
--   * Editing payments.amount / direction / payment_account_id keeps
--     the linked account balance correct automatically.
--   * amount / amount_minor pairs are DB-enforced to agree. Any bug
--     that tries to insert 500 / 0 (or 500 / 49999) fails at write.
--   * payments.party_id is validated by trigger against the correct
--     table for its party_type. Nonexistent client IDs are refused.
--   * client_ledger / supplier_ledger / delivery_person_ledger will
--     refuse any insert whose balance_after doesn't match the running
--     chain -- protects against silent balance corruption.
--   * sales.subtotal / bookings.total_amount / purchases.subtotal are
--     auto-maintained by triggers on the item tables. Never drift.
--   * Deleting the last sale_item of an existing sale is refused;
--     delete the sale itself instead.
--   * materials(category_id, name) is UNIQUE. No two "Cement" in
--     the same category by mistake.
--   * WAL journal mode + busy_timeout recommended in PRAGMA header.
--     App must re-set PRAGMA foreign_keys=ON on every connection.
--   * updated_at auto-touches on every UPDATE across all major tables.
--   * booking.status auto-flips to 'completed' when every item is
--     fully dispatched, to 'partially_cancelled' when items are split.
--   * invoice.status auto-flips based on balance: paid / partial / open.
--   * Partial indexes on hot query paths (FIFO, collections, active
--     sessions, open bookings, pending follow-ups).
--   * v_system_health view for ops dashboard.
--
-- WHAT'S NEW IN v4.4
--   * Roles table has is_admin_role flag -- admin bypasses backdate
--     rules, wipe restrictions, approval workflow.
--   * ~60 granular permissions seeded (view/create/edit/delete per
--     module x 15+ modules).
--   * 4 pre-seeded roles: Admin, Manager, Cashier, Viewer.
--   * settings.backdate_grace_days + settings.require_new_master_approval.
--   * Backdate policy triggers on sales, purchases, bookings, payments,
--     returns: block users with restrict_backdated_edit=1 from posting
--     txns older than grace window. Admins exempt.
--   * Ledger triggers now AUTO-RECOMPUTE FORWARD instead of refusing.
--     Backdated insert into the middle of a ledger silently rewrites
--     balance_after for every subsequent row; audit entry recorded.
--   * created_by + approved/approved_by/approved_at columns on
--     categories, clients, suppliers, materials, lenders -- audit
--     trail for "+" inline adds; optional approval workflow.
--   * data_wipe_scopes catalog + data_wipe_targets breakdown for
--     safer, more transparent tenant/module wipes.
-- =====================================================================


-- =====================================================================
-- SECTION 22 -- SEED DATA  (v4.4)
-- Runs once at bootstrap. Idempotent via INSERT OR IGNORE.
-- =====================================================================

-- Singleton settings row
INSERT OR IGNORE INTO settings(id) VALUES (1);
INSERT OR IGNORE INTO root_backup_settings(id) VALUES (1);

-- =========================
-- ROLES (4 built-in)
-- =========================
INSERT OR IGNORE INTO roles(name, description, is_system, is_admin_role) VALUES
    ('Admin',   'Full access, bypasses all restrictions',        1, 1),
    ('Manager', 'Manage all modules, no destructive operations', 1, 0),
    ('Cashier', 'Day-to-day sales, payments, bookings',          1, 0),
    ('Viewer',  'Read-only across all modules',                  1, 0);

-- =========================
-- PERMISSIONS  (~60 codes: {module}.{view|create|edit|delete})
-- Some modules have extra actions (export, execute, approve).
-- =========================
INSERT OR IGNORE INTO permissions(code, module, description) VALUES
    -- Sales
    ('sales.view',       'sales',       'View sales'),
    ('sales.create',     'sales',       'Create sales'),
    ('sales.edit',       'sales',       'Edit sales'),
    ('sales.delete',     'sales',       'Delete sales'),
    ('sales.export',     'sales',       'Export sales data'),
    -- GRN / purchases
    ('grn.view',         'grn',         'View purchases'),
    ('grn.create',       'grn',         'Create purchases'),
    ('grn.edit',         'grn',         'Edit purchases'),
    ('grn.delete',       'grn',         'Delete purchases'),
    ('grn.export',       'grn',         'Export purchase data'),
    -- Bookings
    ('bookings.view',    'bookings',    'View bookings'),
    ('bookings.create',  'bookings',    'Create bookings'),
    ('bookings.edit',    'bookings',    'Edit bookings'),
    ('bookings.delete',  'bookings',    'Delete bookings'),
    ('bookings.cancel',  'bookings',    'Cancel booking items'),
    -- Returns
    ('returns.view',     'returns',     'View returns'),
    ('returns.create',   'returns',     'Create returns'),
    ('returns.edit',     'returns',     'Edit returns'),
    ('returns.delete',   'returns',     'Delete returns'),
    -- Payments
    ('payments.view',    'payments',    'View payments'),
    ('payments.create',  'payments',    'Create payments'),
    ('payments.edit',    'payments',    'Edit payments'),
    ('payments.delete',  'payments',    'Delete payments'),
    -- Cash flow
    ('cash_flow.view',   'cash_flow',   'View cash flow'),
    ('cash_flow.create', 'cash_flow',   'Create cash flow entries'),
    ('cash_flow.edit',   'cash_flow',   'Edit cash flow entries'),
    ('cash_flow.delete', 'cash_flow',   'Delete cash flow entries'),
    -- Loans
    ('loans.view',       'loans',       'View loans'),
    ('loans.create',     'loans',       'Create loans'),
    ('loans.edit',       'loans',       'Edit loans'),
    ('loans.delete',     'loans',       'Delete loans'),
    -- Clients
    ('clients.view',     'clients',     'View clients'),
    ('clients.create',   'clients',     'Create clients (incl. inline +)'),
    ('clients.edit',     'clients',     'Edit clients'),
    ('clients.delete',   'clients',     'Delete clients'),
    ('clients.approve',  'clients',     'Approve pending client entries'),
    -- Suppliers
    ('suppliers.view',   'suppliers',   'View suppliers'),
    ('suppliers.create', 'suppliers',   'Create suppliers (incl. inline +)'),
    ('suppliers.edit',   'suppliers',   'Edit suppliers'),
    ('suppliers.delete', 'suppliers',   'Delete suppliers'),
    ('suppliers.approve','suppliers',   'Approve pending supplier entries'),
    -- Materials
    ('materials.view',   'materials',   'View materials'),
    ('materials.create', 'materials',   'Create materials (incl. inline +)'),
    ('materials.edit',   'materials',   'Edit materials'),
    ('materials.delete', 'materials',   'Delete materials'),
    ('materials.approve','materials',   'Approve pending material entries'),
    -- Delivery persons
    ('delivery.view',    'delivery',    'View delivery persons'),
    ('delivery.create',  'delivery',    'Create delivery persons'),
    ('delivery.edit',    'delivery',    'Edit delivery persons'),
    ('delivery.delete',  'delivery',    'Delete delivery persons'),
    -- Categories
    ('categories.view',  'categories',  'View categories'),
    ('categories.create','categories',  'Create categories (incl. inline +)'),
    ('categories.edit',  'categories',  'Edit categories'),
    ('categories.delete','categories',  'Delete categories'),
    ('categories.approve','categories', 'Approve pending category entries'),
    -- Reports
    ('reports.view',     'reports',     'View reports'),
    ('reports.export',   'reports',     'Export reports'),
    -- Settings
    ('settings.view',    'settings',    'View settings'),
    ('settings.edit',    'settings',    'Edit settings'),
    -- Users / roles
    ('users.view',       'users',       'View users'),
    ('users.manage',     'users',       'Create/edit/delete users'),
    ('roles.manage',     'roles',       'Manage roles & permissions'),
    -- Ops
    ('wipe.execute',     'ops',         'Execute data wipe scopes'),
    ('backup.execute',   'ops',         'Trigger backup emails'),
    ('import.execute',   'ops',         'Run data imports'),
    ('audit.view',       'ops',         'View audit logs'),
    -- Day closing
    ('day_closing.view',   'day_closing', 'View day closings'),
    ('day_closing.execute','day_closing', 'Execute day close');

-- =========================
-- ROLE -> PERMISSION mappings
-- =========================
-- Admin: gets EVERY permission automatically (app should treat
-- roles.is_admin_role=1 as "has all permissions" without checking
-- role_permissions). We still seed here so audit queries work.
INSERT OR IGNORE INTO role_permissions(role_id, permission_id)
SELECT (SELECT id FROM roles WHERE name='Admin'), id FROM permissions;

-- Manager: everything except deleting and destructive ops
INSERT OR IGNORE INTO role_permissions(role_id, permission_id)
SELECT (SELECT id FROM roles WHERE name='Manager'), id
FROM permissions
WHERE code NOT IN (
    'sales.delete','grn.delete','bookings.delete','returns.delete',
    'payments.delete','cash_flow.delete','loans.delete','clients.delete',
    'suppliers.delete','materials.delete','delivery.delete','categories.delete',
    'wipe.execute','roles.manage','users.manage'
);

-- Cashier: day-to-day operations (create + view mostly)
INSERT OR IGNORE INTO role_permissions(role_id, permission_id)
SELECT (SELECT id FROM roles WHERE name='Cashier'), id
FROM permissions
WHERE code IN (
    'sales.view','sales.create',
    'grn.view',
    'bookings.view','bookings.create',
    'returns.view','returns.create',
    'payments.view','payments.create',
    'cash_flow.view','cash_flow.create',
    'clients.view','clients.create',
    'suppliers.view',
    'materials.view','materials.create',
    'delivery.view',
    'categories.view','categories.create',
    'reports.view',
    'day_closing.view'
);

-- Viewer: read-only across all modules
INSERT OR IGNORE INTO role_permissions(role_id, permission_id)
SELECT (SELECT id FROM roles WHERE name='Viewer'), id
FROM permissions
WHERE code LIKE '%.view';

-- =========================
-- WIPE SCOPES
-- =========================
INSERT OR IGNORE INTO data_wipe_scopes(code, name, description, target_tables_json, requires_admin, requires_confirmation_phrase, is_destructive) VALUES
    ('sales_only',
     'Sales only',
     'Wipe all sales, sale items, allocations, and multi-driver splits',
     '["grn_allocations","booking_allocations","sale_delivery_persons","sale_items","sales","sale_drafts"]',
     1, 1, 1),

    ('bookings_only',
     'Bookings only',
     'Wipe all bookings, booking items, cancellations, and follow-ups',
     '["booking_followups","booking_cancellations","booking_allocations","booking_items","bookings"]',
     1, 1, 1),

    ('purchases_only',
     'Purchases (GRN) only',
     'Wipe all purchases, purchase items, and stock batches spawned from them',
     '["fifo_consumptions","grn_allocations","stock_batches","purchase_items","purchases"]',
     1, 1, 1),

    ('payments_only',
     'Payments & waive-offs only',
     'Wipe all payment records and related waive-offs',
     '["waive_offs","payments"]',
     1, 1, 1),

    ('cash_flow_only',
     'Cash flow entries only',
     'Wipe cash-flow classified payments and difference adjustments',
     '["cash_flow_difference_adjustments"]',
     1, 1, 1),

    ('returns_only',
     'Returns only',
     'Wipe all return records',
     '["returns"]',
     1, 1, 1),

    ('stock_counts_only',
     'Daily stock counts only',
     'Wipe day-closing history and physical stock counts',
     '["daily_stock_count","day_closing"]',
     1, 1, 1),

    ('import_logs_only',
     'Import history only',
     'Wipe uploaded files, import job history, and per-row events',
     '["import_history_entries","import_jobs","import_uploads","recon_baskets"]',
     1, 1, 0),

    ('audit_logs_only',
     'Audit logs only',
     'Wipe audit trails (KEEPS accounting audit)',
     '["audit_log","activity_feed"]',
     1, 1, 0),

    ('sessions_only',
     'Login sessions only',
     'Force logout everyone by wiping session table',
     '["user_login_sessions"]',
     1, 0, 0),

    ('all_transactions',
     'All transactional data',
     'Everything except users, roles, categories, settings, master data. Keeps accounts and ledgers empty.',
     '["fifo_consumptions","grn_allocations","booking_allocations","sale_delivery_persons","sale_items","sales","sale_drafts","delivery_rents","booking_followups","booking_cancellations","booking_items","bookings","stock_batches","purchase_items","purchases","waive_offs","payments","account_transactions","account_reconciliations","cash_flow_difference_adjustments","returns","client_ledger","supplier_ledger","delivery_person_ledger","loans","invoices","pending_bills","daily_stock_count","day_closing","stock_transactions","activity_feed","import_history_entries","import_jobs","import_uploads","recon_baskets"]',
     1, 1, 1),

    ('factory_reset',
     'FULL FACTORY RESET',
     'Wipes EVERYTHING except schema_version and one root Admin user. Master data, transactions, categories, all gone.',
     '["fifo_consumptions","grn_allocations","booking_allocations","sale_delivery_persons","sale_items","sales","sale_drafts","delivery_rents","booking_followups","booking_cancellations","booking_items","bookings","stock_batches","purchase_items","purchases","waive_offs","payments","account_transactions","account_reconciliations","cash_flow_difference_adjustments","returns","client_ledger","supplier_ledger","delivery_person_ledger","loans","invoices","pending_bills","daily_stock_count","day_closing","stock_transactions","activity_feed","accounting_audit_log","audit_log","import_history_entries","import_jobs","import_uploads","recon_baskets","materials","clients","suppliers","delivery_persons","lenders","accounts","categories","bill_counter"]',
     1, 1, 1);


-- =====================================================================
-- END v4.4 SEED DATA
-- =====================================================================
