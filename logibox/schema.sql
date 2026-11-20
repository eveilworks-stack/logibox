PRAGMA foreign_keys = ON;

CREATE TABLE company (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL, address TEXT, invoice_no TEXT, bank_info TEXT
);

CREATE TABLE subcontractors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL, invoice_no TEXT, bank_info TEXT,
  default_fee_rate_pct REAL NOT NULL DEFAULT 2
);

CREATE TABLE suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE, invoice_no TEXT
);
INSERT INTO suppliers(name) VALUES ('エムエスト'), ('エムロジ');

CREATE TABLE mgmt_counters (
  year INTEGER PRIMARY KEY,
  last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE cases (
  id INTEGER PRIMARY KEY,
  mgmt_no TEXT NOT NULL UNIQUE,
  case_no TEXT, name TEXT NOT NULL,
  start_date TEXT, due_date TEXT,
  planned_rate_pct REAL NOT NULL,
  maker TEXT, purchase_limit_tax_in INTEGER,
  deleted_at TEXT  -- NULLでない=削除ボックス内。完全削除してもmgmt_noは再利用しない
);

CREATE TABLE purchases (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  amount_tax_in INTEGER NOT NULL CHECK (amount_tax_in >= 0),
  invoice_date TEXT, due_date TEXT,
  paid_amount INTEGER NOT NULL DEFAULT 0, paid_date TEXT,
  invoice_file TEXT, receipt_file TEXT,
  items_text TEXT
);

CREATE TABLE case_subs (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  sub_id INTEGER NOT NULL REFERENCES subcontractors(id),
  slot INTEGER NOT NULL CHECK (slot IN (1, 2)),
  principal_tax_in INTEGER NOT NULL CHECK (principal_tax_in >= 0),
  fee_rate_pct REAL NOT NULL DEFAULT 2,
  tax_mode TEXT NOT NULL DEFAULT 'consignment' CHECK (tax_mode IN ('deposit','sale','consignment')),
  principal_received_date TEXT,
  settle_date TEXT,
  UNIQUE (case_id, slot), UNIQUE (case_id, sub_id)
);

CREATE TABLE haak_billings (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL UNIQUE REFERENCES cases(id) ON DELETE CASCADE,
  confirmed_amount_tax_in INTEGER,
  confirmed_checked INTEGER NOT NULL DEFAULT 0,
  billing_date TEXT, due_date TEXT, paid_date TEXT,
  items_text TEXT, note TEXT
);

CREATE TABLE issued_documents (
  id INTEGER PRIMARY KEY,
  doc_type TEXT NOT NULL CHECK (doc_type IN ('principal_invoice','receipt','haak_invoice')),
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  sub_id INTEGER REFERENCES subcontractors(id),
  revision INTEGER NOT NULL DEFAULT 1,
  issued_date TEXT NOT NULL,
  amount_tax_in INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,
  file TEXT,
  sent_date TEXT,
  updated_at TEXT
);

CREATE TABLE issued_document_history (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES issued_documents(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  saved_at TEXT NOT NULL,
  amount_tax_in INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,
  file TEXT
);

CREATE TABLE settlements (
  id INTEGER PRIMARY KEY,
  sub_id INTEGER NOT NULL REFERENCES subcontractors(id),
  period TEXT NOT NULL,
  pay_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'not_requested'
    CHECK (status IN ('not_requested','requested','received','mismatch','paid')),
  UNIQUE (sub_id, period)
);

CREATE TABLE settlement_lines (
  id INTEGER PRIMARY KEY,
  settlement_id INTEGER NOT NULL REFERENCES settlements(id),
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
  principal INTEGER NOT NULL,
  sub_profit INTEGER NOT NULL,
  carry_over INTEGER NOT NULL DEFAULT 0,
  carry_to_case_id INTEGER REFERENCES cases(id),
  UNIQUE (settlement_id, case_id)
);

CREATE TABLE sub_invoices (
  id INTEGER PRIMARY KEY,
  settlement_id INTEGER NOT NULL REFERENCES settlements(id),
  received_date TEXT, invoice_no TEXT,
  amount_tax_in INTEGER NOT NULL, file TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
  purchase_id INTEGER REFERENCES purchases(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK(kind IN ('purchase_invoice','purchase_receipt','other')),
  original_name TEXT NOT NULL,
  stored_name TEXT NOT NULL UNIQUE,
  mime TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
