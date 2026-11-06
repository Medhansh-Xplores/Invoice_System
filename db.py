"""SQLite storage: vendors, POs, approved-invoice ledger, runs, settings."""
import os, sqlite3, json

DATA = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
for d in ("", "uploads", "cache"):
    os.makedirs(os.path.join(DATA, d), exist_ok=True)
DB = os.path.join(DATA, "desk.db")

DEFAULT_SETTINGS = {
    "tol_approve_pct": 2.0,     # up to this % over remaining PO balance -> approve
    "tol_review_pct": 10.0,     # above approve, up to this % -> review; beyond -> reject
    "dup_inv_edit": 2,          # max edit distance between invoice numbers to count as similar
    "dup_amount_pct": 1.0,      # amounts within this % count as the same
    "dup_days": 30,             # invoice dates within this many days count as close
    "dup_score_threshold": 0.75,
    "po_match_min_conf": 0.7,   # minimum confidence to accept an inferred PO
    "math_tol_pct": 0.5,        # subtotal + tax vs total tolerance
}

VENDORS = [
    (1, "Northwind Supplies", "NW-4471-0092"),
    (2, "Apex Logistics", "AL-8820-1135"),
    (3, "Brightline Software", "BS-3305-7741"),
    (4, "Cedar Office Co", "CO-1190-6628"),
    (5, "Harbor Print & Packaging", "HP-5521-0307"),
]
POS = [
    ("PO-7001", 1, 10000.00, "Office furniture and delivery"),
    ("PO-7002", 2, 50000.00, "Freight services Q4 2026"),
    ("PO-7003", 3, 5000.00, "Annual software licence and support"),
    ("PO-7004", 4, 6200.00, "Office chairs and desks"),
    ("PO-7005", 5, 7700.00, "Printed packaging boxes"),
    ("PO-7007", 4, 900.00, "Stationery supplies"),
]
LEDGER = [  # history that already exists before the demo
    (2, "INV-A100", "2026-10-12", 20000.00, "PO-7002"),
    (4, "INV-554", "2026-10-20", 2400.00, "PO-7004"),
    (5, "INV-1042", "2026-11-01", 3850.00, "PO-7005"),
]

def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init(reset=False):
    c = conn()
    if reset:
        for t in ("runs", "ledger", "pos", "vendors", "settings"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS vendors(id INTEGER PRIMARY KEY, name TEXT, bank_account TEXT, status TEXT DEFAULT 'approved');
    CREATE TABLE IF NOT EXISTS pos(po_number TEXT PRIMARY KEY, vendor_id INTEGER, amount REAL, description TEXT, currency TEXT DEFAULT 'USD');
    CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER, invoice_number TEXT, invoice_date TEXT, amount REAL, po_number TEXT, source TEXT, run_id INTEGER);
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, filename TEXT, sha TEXT, decision TEXT, vendor TEXT, invoice_number TEXT, total REAL, po_number TEXT, rationale TEXT, flags TEXT, audit TEXT, fields TEXT, email TEXT, duration_ms INTEGER);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value REAL);
    """)
    if not c.execute("SELECT 1 FROM vendors").fetchone():
        c.executemany("INSERT INTO vendors(id,name,bank_account) VALUES(?,?,?)", VENDORS)
        c.executemany("INSERT INTO pos(po_number,vendor_id,amount,description) VALUES(?,?,?,?)", POS)
        c.executemany("INSERT INTO ledger(vendor_id,invoice_number,invoice_date,amount,po_number,source) VALUES(?,?,?,?,?,'seed')", LEDGER)
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    c.commit(); c.close()

def get_settings(c):
    s = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
    return {**DEFAULT_SETTINGS, **s}

def reset_data():
    init(reset=True)
