import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .config import DATABASE_PATH, DEFAULT_CAPITAL
from .security import hash_password


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolios (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  starting_capital REAL NOT NULL,
  cash REAL NOT NULL,
  realised_pnl REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  risk_profile TEXT NOT NULL DEFAULT 'balanced',
  max_position_pct REAL NOT NULL DEFAULT 5,
  max_daily_loss_pct REAL NOT NULL DEFAULT 2,
  auto_scan INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS holdings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  average_price REAL NOT NULL,
  opened_at TEXT NOT NULL,
  UNIQUE(user_id, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
  quantity INTEGER NOT NULL,
  price REAL NOT NULL,
  realised_pnl REAL NOT NULL DEFAULT 0,
  confidence REAL NOT NULL,
  strategy TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'FILLED',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  stocks_scanned INTEGER NOT NULL,
  opportunities INTEGER NOT NULL,
  trades_placed INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  price REAL NOT NULL,
  change_pct REAL NOT NULL,
  confidence REAL NOT NULL,
  reasoning TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  bias TEXT NOT NULL,
  confidence REAL NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_user_symbol ON stock_analyses(user_id, symbol, id DESC);
CREATE TABLE IF NOT EXISTS sentiment_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  score REAL NOT NULL,
  label TEXT NOT NULL,
  confidence REAL NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sentiment_user_symbol ON sentiment_snapshots(user_id, symbol, id DESC);
CREATE TABLE IF NOT EXISTS ai_gateway_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  request_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  cached INTEGER NOT NULL DEFAULT 0,
  response_text TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gateway_user_created ON ai_gateway_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gateway_hash ON ai_gateway_logs(user_id, request_hash, id DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def create_user(db: sqlite3.Connection, name: str, email: str, password: str) -> int:
    cursor = db.execute(
        "INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",
        (name.strip(), email.strip().lower(), hash_password(password), now_iso()),
    )
    user_id = cursor.lastrowid
    db.execute("INSERT INTO portfolios(user_id,starting_capital,cash,updated_at) VALUES(?,?,?,?)", (user_id, DEFAULT_CAPITAL, DEFAULT_CAPITAL, now_iso()))
    db.execute("INSERT INTO settings(user_id) VALUES(?)", (user_id,))
    db.commit()
    return user_id


def initialize() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        user = db.execute("SELECT id FROM users WHERE email=?", ("arjun@example.com",)).fetchone()
        if not user:
            user_id = create_user(db, "Arjun Kapoor", "arjun@example.com", "nivesh123")
            seed_demo_portfolio(db, user_id)


def seed_demo_portfolio(db: sqlite3.Connection, user_id: int) -> None:
    holdings = [
        ("RELIANCE", "Reliance Industries", 12, 2948.20), ("HDFCBANK", "HDFC Bank", 18, 1682.10),
        ("INFY", "Infosys", 20, 1487.65), ("LT", "Larsen & Toubro", 8, 3584.50),
        ("SUNPHARMA", "Sun Pharma", 15, 1496.40), ("MARUTI", "Maruti Suzuki", 2, 12340.00),
    ]
    stamp = now_iso()
    invested = 0.0
    for symbol, name, qty, price in holdings:
        db.execute("INSERT OR IGNORE INTO holdings(user_id,symbol,name,quantity,average_price,opened_at) VALUES(?,?,?,?,?,?)", (user_id, symbol, name, qty, price, stamp))
        invested += qty * price
    db.execute("UPDATE portfolios SET cash=?, updated_at=? WHERE user_id=?", (DEFAULT_CAPITAL - invested, stamp, user_id))
    demo_trades = [
        ("RELIANCE", "Reliance Industries", "BUY", 12, 2948.20, 0, 86, "Breakout + Momentum", "Volume expanded above the 20-day average with a clean breakout and constructive sector breadth."),
        ("TATAMOTORS", "Tata Motors", "SELL", 25, 1024.50, 862.50, 81, "Momentum exit", "The position reached its target while RSI entered an overbought zone and auto-sector breadth weakened."),
        ("HDFCBANK", "HDFC Bank", "BUY", 18, 1682.10, 0, 78, "Mean Reversion", "Price reclaimed VWAP near support with a favourable reward-to-risk ratio and a defined stop."),
    ]
    for row in demo_trades:
        db.execute("INSERT INTO trades(user_id,symbol,name,action,quantity,price,realised_pnl,confidence,strategy,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (user_id, *row, stamp))
    db.commit()


def find_user_by_email(db: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
