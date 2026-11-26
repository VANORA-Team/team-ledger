import json
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
from . import config
from .locks import data_guard

authenticated_session = ContextVar('ledger_authenticated_session', default=None)

DEFAULTS = {
    'team_name': '一起记',
    'default_currency': 'CNY',
    'reference_currency': 'CNY',
    'currencies': [
        {'code': 'CNY', 'name': '人民币', 'symbol': '¥', 'decimals': 2},
        {'code': 'USD', 'name': '美元', 'symbol': '$', 'decimals': 2},
        {'code': 'EUR', 'name': '欧元', 'symbol': '€', 'decimals': 2},
        {'code': 'HKD', 'name': '港币', 'symbol': 'HK$', 'decimals': 2},
        {'code': 'JPY', 'name': '日元', 'symbol': 'JP¥', 'decimals': 0},
    ],
    'telegram_allowed_chats': [],
    'feishu_allowed_chats': [],
}

USER_ROLES = ('owner', 'finance', 'member')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 name TEXT NOT NULL, password_hash TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
 oidc_issuer TEXT NOT NULL DEFAULT '', oidc_sub TEXT, oidc_email TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires INTEGER NOT NULL, oidc_sid TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS oidc_flows (
 state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, nonce TEXT NOT NULL,
 code_verifier TEXT NOT NULL, team_name TEXT NOT NULL DEFAULT '',
 return_hash TEXT NOT NULL DEFAULT '', expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oidc_revoked_jti (
 jti_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, expense_type TEXT NOT NULL DEFAULT 'advance',
 income_type TEXT NOT NULL DEFAULT '',
 amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 gross_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(gross_amount_minor>=0),
 fee_mode TEXT NOT NULL DEFAULT 'none',
 fee_rate TEXT NOT NULL DEFAULT '',
 fee_minor INTEGER NOT NULL DEFAULT 0 CHECK(fee_minor>=0),
 currency TEXT NOT NULL, decimals INTEGER NOT NULL,
 fx_rate TEXT NOT NULL DEFAULT '', fx_currency TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL,
 category TEXT NOT NULL DEFAULT '', date TEXT NOT NULL, payer_id INTEGER NOT NULL REFERENCES users(id),
 created_by INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL,
 source TEXT NOT NULL, source_key TEXT UNIQUE, receipt TEXT, receipt_name TEXT,
 note TEXT NOT NULL DEFAULT '', review_note TEXT NOT NULL DEFAULT '',
 paid_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS entries_status ON entries(status, date);
CREATE INDEX IF NOT EXISTS entries_payer ON entries(payer_id);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES entries(id),
 user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES entries(id),
 user_id INTEGER NOT NULL REFERENCES users(id), amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 currency TEXT NOT NULL, decimals INTEGER NOT NULL, note TEXT NOT NULL,
 entry_amount_minor INTEGER NOT NULL, fx_rate TEXT NOT NULL, fx_date TEXT,
 quote_id TEXT, source_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS payments_entry ON payments(entry_id,id);
CREATE TABLE IF NOT EXISTS payment_quotes (
 id TEXT PRIMARY KEY, entry_id INTEGER NOT NULL REFERENCES entries(id), user_id INTEGER NOT NULL REFERENCES users(id),
 version INTEGER NOT NULL, currency TEXT NOT NULL, decimals INTEGER NOT NULL,
 rate TEXT NOT NULL, rate_date TEXT, remaining_minor INTEGER NOT NULL, maximum_minor INTEGER NOT NULL,
 expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_links (
 telegram_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id));
CREATE TABLE IF NOT EXISTS link_codes (
 code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_links (
 open_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id));
CREATE TABLE IF NOT EXISTS feishu_config (
 id INTEGER PRIMARY KEY CHECK(id=1), app_id TEXT NOT NULL, app_secret TEXT NOT NULL,
 version INTEGER NOT NULL, domain TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS feishu_link_codes (
 code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_inbox (
 message_id TEXT PRIMARY KEY, open_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_outbox (
 id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, open_id TEXT NOT NULL,
 user_id INTEGER REFERENCES users(id), text TEXT NOT NULL,
 message_uuid TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS feishu_pending (
 open_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 plan_id TEXT NOT NULL, confirmation TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fsui_identities (
 id INTEGER PRIMARY KEY, open_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS fsui_links (
 telegram_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id));
CREATE TABLE IF NOT EXISTS fsui_updates (update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fsui_drafts (
 token TEXT PRIMARY KEY, telegram_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id), step TEXT NOT NULL,
 payload TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fsui_outbox (
 id INTEGER PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fsui_prompts (
 chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, telegram_id TEXT NOT NULL,
 token TEXT NOT NULL, step TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS fsui_messages (
 id INTEGER PRIMARY KEY, feishu_id TEXT NOT NULL UNIQUE, chat_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fsui_actions (
 token TEXT PRIMARY KEY, telegram_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 action TEXT NOT NULL, expires INTEGER NOT NULL, group_mode INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_updates (update_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tg_drafts (
 token TEXT PRIMARY KEY, telegram_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 user_id INTEGER NOT NULL REFERENCES users(id), step TEXT NOT NULL,
 payload TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_outbox (
 id INTEGER PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tg_prompts (
 chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, telegram_id TEXT NOT NULL,
 token TEXT NOT NULL, step TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS fx_cache (
 base TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ai_plans (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), source TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL, expires INTEGER NOT NULL,
 executed_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS ai_config (
 id INTEGER PRIMARY KEY CHECK(id=1),
 enabled INTEGER NOT NULL DEFAULT 1,
 base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
 api_key TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL DEFAULT '',
 response_format TEXT NOT NULL DEFAULT 'json_object',
 timeout INTEGER NOT NULL DEFAULT 40,
 context_entries INTEGER NOT NULL DEFAULT 120,
 context_chars INTEGER NOT NULL DEFAULT 60000,
 max_actions INTEGER NOT NULL DEFAULT 20,
 updated_at TEXT NOT NULL DEFAULT '');
'''


@contextmanager
def db(write=False):
    with data_guard():
        with _connection(write) as conn:
            yield conn


@contextmanager
def _connection(write=False):
    config.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    try:
        conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        token_hash = authenticated_session.get()
        if token_hash is not None and not conn.execute(
                'SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id '
                'WHERE s.token_hash=? AND s.expires>? AND u.active=1',
                (token_hash, int(time.time()))).fetchone():
            from fastapi import HTTPException
            raise HTTPException(401, '登录已失效，请重新登录')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with data_guard():
        _init_db()


def _init_db():
    config.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    try:
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(SCHEMA)
        # Both processes can initialize concurrently; serialize the additive migration.
        conn.execute('BEGIN IMMEDIATE')
        entry_cols = {r[1] for r in conn.execute('PRAGMA table_info(entries)')}
        if 'reimbursed_minor' not in entry_cols:
            conn.execute('ALTER TABLE entries ADD COLUMN reimbursed_minor INTEGER NOT NULL DEFAULT 0 CHECK(reimbursed_minor>=0 AND reimbursed_minor<=amount_minor)')
        if 'expense_type' not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN expense_type TEXT NOT NULL DEFAULT 'advance'")
        if 'income_type' not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN income_type TEXT NOT NULL DEFAULT ''")
        if 'gross_amount_minor' not in entry_cols:
            conn.execute('ALTER TABLE entries ADD COLUMN gross_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK(gross_amount_minor>=0)')
        if 'fee_mode' not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN fee_mode TEXT NOT NULL DEFAULT 'none'")
        if 'fee_rate' not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN fee_rate TEXT NOT NULL DEFAULT ''")
        if 'fee_minor' not in entry_cols:
            conn.execute('ALTER TABLE entries ADD COLUMN fee_minor INTEGER NOT NULL DEFAULT 0 CHECK(fee_minor>=0)')
        if 'fx_rate' not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN fx_rate TEXT NOT NULL DEFAULT ''")
        if 'fx_currency' not in entry_cols:
            conn.execute("ALTER TABLE entries ADD COLUMN fx_currency TEXT NOT NULL DEFAULT ''")
        if 'domain' not in {r[1] for r in conn.execute('PRAGMA table_info(feishu_config)')}:
            conn.execute("ALTER TABLE feishu_config ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
        if 'attempts' not in {r[1] for r in conn.execute('PRAGMA table_info(feishu_outbox)')}:
            conn.execute('ALTER TABLE feishu_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
        user_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
        if 'oidc_issuer' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN oidc_issuer TEXT NOT NULL DEFAULT ''")
        if 'oidc_sub' not in user_cols:
            conn.execute('ALTER TABLE users ADD COLUMN oidc_sub TEXT')
        if 'oidc_email' not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN oidc_email TEXT NOT NULL DEFAULT ''")
        if 'oidc_sid' not in {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}:
            conn.execute("ALTER TABLE sessions ADD COLUMN oidc_sid TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_identity "
            "ON users(oidc_issuer, oidc_sub) WHERE oidc_sub IS NOT NULL AND oidc_sub<>''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS sessions_oidc_sid ON sessions(oidc_sid) WHERE oidc_sid<>''")
        conn.execute("UPDATE entries SET expense_type='' WHERE kind='income' AND expense_type<>''")
        conn.execute("UPDATE entries SET income_type='operating' WHERE kind='income' AND (income_type IS NULL OR income_type='')")
        conn.execute("UPDATE entries SET income_type='' WHERE kind='expense' AND income_type<>''")
        conn.execute('UPDATE entries SET gross_amount_minor=amount_minor WHERE gross_amount_minor=0')
        conn.execute("UPDATE entries SET fee_mode='none', fee_rate='', fee_minor=0 WHERE fee_mode IS NULL OR fee_mode=''")
        conn.execute("UPDATE entries SET fx_rate='', fx_currency='' WHERE fx_rate IS NULL OR fx_rate='' OR fx_currency IS NULL OR fx_currency=''")
        # Earlier versions only supported full payments. Preserve their dates and notes,
        # and create exactly one payment record without rewriting their audit history.
        conn.execute("""INSERT OR IGNORE INTO payments(entry_id,user_id,amount_minor,currency,decimals,note,entry_amount_minor,fx_rate,fx_date,source_key,created_at)
            SELECT e.id,COALESCE((SELECT user_id FROM events WHERE entry_id=e.id AND action='pay' ORDER BY id DESC LIMIT 1),e.created_by),
                e.amount_minor,e.currency,e.decimals,e.review_note,e.amount_minor,'1',NULL,'legacy:pay:'||e.id,COALESCE(e.paid_at,e.updated_at)
            FROM entries e WHERE e.kind='expense' AND e.status='reimbursed' AND e.reimbursed_minor=0""")
        conn.execute("UPDATE entries SET reimbursed_minor=amount_minor WHERE kind='expense' AND status='reimbursed' AND reimbursed_minor=0")
        migrate_public_expenses(conn)
        conn.execute("UPDATE entries SET status='posted' WHERE kind='income' AND status='distributed'")
        conn.execute("DELETE FROM settings WHERE key='categories'")
        # Retire the approval workflow without changing amounts, payment data or history.
        conn.execute("UPDATE entries SET status='pending' WHERE kind='expense' AND status IN ('submitted','approved')")
        conn.execute("UPDATE entries SET status='draft' WHERE kind='expense' AND status='rejected'")
        for key, value in DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def migrate_public_expenses(conn):
    """Retire distributions while preserving their cash effect and audit metadata."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = []
    if 'dividends' in tables:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(dividends)')]
        legacy = [dict(zip(columns, row)) for row in conn.execute('SELECT * FROM dividends ORDER BY id')]
    keys = {row['source_key'] for row in legacy}
    for row in conn.execute("SELECT id,amount_minor,currency,decimals,created_by,review_note,updated_at FROM entries WHERE kind='income' AND status='distributed'").fetchall():
        entry_id, amount, code, decimals, creator, note, timestamp = row
        key = f'legacy:dividend:{entry_id}'
        if key not in keys:
            actor = conn.execute("SELECT user_id FROM events WHERE entry_id=? AND action='distribute' ORDER BY id DESC LIMIT 1", (entry_id,)).fetchone()
            legacy.append(dict(user_id=actor[0] if actor else creator, amount_minor=amount,
                               currency=code, decimals=decimals, note=note, created_at=timestamp, source_key=key))
    for row in legacy:
        key = 'migration:public:' + row['source_key']
        if conn.execute('SELECT 1 FROM entries WHERE source_key=?', (key,)).fetchone():
            continue
        stamp, actor = row['created_at'], row['user_id']
        cursor = conn.execute("""INSERT INTO entries
            (kind,expense_type,amount_minor,gross_amount_minor,fee_mode,fee_rate,fee_minor,currency,decimals,description,date,payer_id,created_by,status,source,source_key,note,paid_at,created_at,updated_at)
            VALUES ('expense','public',?,?,'none','',0,?,?,'历史公账支出',?,?,?,'posted','migration',?,?,?,?,?)""",
            (row['amount_minor'], row['amount_minor'], row['currency'], row['decimals'], stamp[:10], actor, actor,
             key, row['note'], stamp, stamp, stamp))
        conn.execute("INSERT INTO events(entry_id,user_id,action,detail,created_at) VALUES (?,?,'migrated',?,?)",
                     (cursor.lastrowid, actor, json.dumps(row, ensure_ascii=False), stamp))
    conn.execute('DROP TABLE IF EXISTS dividend_quotes')
    conn.execute('DROP TABLE IF EXISTS dividends')
    for table in ('tg_drafts', 'fsui_drafts', 'tg_prompts', 'fsui_prompts'):
        conn.execute(f"DELETE FROM {table} WHERE step LIKE 'dividend_%'")


def settings(conn):
    return {r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM settings')}


def set_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))


def remove_receipt_file(name):
    """A restore may have made this receipt live again after the caller committed."""
    filename = Path(name).name
    with data_guard():
        with db() as conn:
            referenced = conn.execute('SELECT 1 FROM entries WHERE receipt=? LIMIT 1', (filename,)).fetchone()
        if not referenced:
            (config.DATA / 'receipts' / filename).unlink(missing_ok=True)
