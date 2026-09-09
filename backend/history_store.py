import sqlite3
from pathlib import Path
from statistics import pstdev
from typing import Dict, Iterable, List

from .config import DATABASE_URL, MARKET_HISTORY_PATH, ML_MIN_COVERAGE_PCT, ML_MIN_DAILY_BARS, ML_TRAINING_EXCHANGES


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS candles (
  exchange TEXT NOT NULL,
  symbol TEXT NOT NULL,
  instrument_token INTEGER NOT NULL,
  interval TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume INTEGER NOT NULL DEFAULT 0, oi INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'kite',
  PRIMARY KEY(exchange,symbol,interval,timestamp)
);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_time ON candles(exchange,symbol,interval,timestamp);
CREATE TABLE IF NOT EXISTS sync_coverage (
  exchange TEXT NOT NULL, symbol TEXT NOT NULL, instrument_token INTEGER NOT NULL,
  interval TEXT NOT NULL, first_timestamp TEXT, last_timestamp TEXT,
  bars INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY(exchange,symbol,interval)
);
CREATE TABLE IF NOT EXISTS history_ingestions (
  exchange TEXT NOT NULL, trade_date TEXT NOT NULL, source TEXT NOT NULL,
  url TEXT NOT NULL, sha256 TEXT, byte_count INTEGER NOT NULL DEFAULT 0,
  row_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, error TEXT,
  imported_at TEXT NOT NULL,
  PRIMARY KEY(exchange,trade_date,source)
);
"""


class HistoryStore:
    def __new__(cls,path: Path = MARKET_HISTORY_PATH):
        if cls is HistoryStore and DATABASE_URL and Path(path)==Path(MARKET_HISTORY_PATH):
            from .postgres_repositories import PostgresHistoryStore
            return PostgresHistoryStore()
        return super().__new__(cls)

    def __init__(self, path: Path = MARKET_HISTORY_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def save(self, exchange: str, symbol: str, token: int, interval: str, candles: Iterable[Dict], updated_at: str, source: str = "kite") -> int:
        rows = list(candles)
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO candles(exchange,symbol,instrument_token,interval,timestamp,open,high,low,close,volume,oi,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           [(exchange, symbol, token, interval, item["timestamp"], item["open"], item["high"], item["low"], item["close"], item.get("volume", 0), item.get("oi", 0), source) for item in rows])
            first = rows[0]["timestamp"] if rows else None
            last = rows[-1]["timestamp"] if rows else None
            count = db.execute("SELECT COUNT(*) FROM candles WHERE exchange=? AND symbol=? AND interval=?", (exchange, symbol, interval)).fetchone()[0]
            db.execute("INSERT OR REPLACE INTO sync_coverage VALUES(?,?,?,?,?,?,?,?,?,?)", (exchange, symbol, token, interval, first, last, count, "complete", None, updated_at))
        return len(rows)

    def failure(self, exchange: str, symbol: str, token: int, interval: str, error: str, updated_at: str):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO sync_coverage VALUES(?,?,?,?,?,?,?,?,?,?)", (exchange, symbol, token, interval, None, None, 0, "failed", error[:300], updated_at))

    def archive_imported(self, exchange: str, trade_date: str, source: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM history_ingestions WHERE exchange=? AND trade_date=? AND source=? AND status='complete'",
                (exchange.upper(), trade_date, source),
            ).fetchone()
        return bool(row)

    def save_archive(self, exchange: str, trade_date: str, candles: List[Dict], source: str,
                     url: str, sha256: str, byte_count: int, imported_at: str) -> int:
        """Atomically import one official daily file and record its provenance."""
        exchange = exchange.upper()
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO candles(exchange,symbol,instrument_token,interval,timestamp,open,high,low,close,volume,oi,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(exchange, item["symbol"], int(item.get("instrument_token", 0)), "day", item["timestamp"],
                  item["open"], item["high"], item["low"], item["close"], int(item.get("volume", 0)),
                  int(item.get("oi", 0)), source) for item in candles],
            )
            db.execute(
                "INSERT OR REPLACE INTO history_ingestions(exchange,trade_date,source,url,sha256,byte_count,row_count,status,error,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (exchange, trade_date, source, url, sha256, byte_count, len(candles), "complete", None, imported_at),
            )
        return len(candles)

    def record_archive_failure(self, exchange: str, trade_date: str, source: str, url: str,
                               error: str, imported_at: str):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO history_ingestions(exchange,trade_date,source,url,sha256,byte_count,row_count,status,error,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (exchange.upper(), trade_date, source, url, None, 0, 0, "failed", error[:300], imported_at),
            )

    def rebuild_coverage(self, exchange: str, interval: str = "day", updated_at: str = "") -> int:
        """Recompute coverage once after a bulk archive import."""
        exchange = exchange.upper()
        with self.connect() as db:
            rows = db.execute(
                "SELECT symbol,MAX(instrument_token),MIN(timestamp),MAX(timestamp),COUNT(*) FROM candles WHERE exchange=? AND interval=? GROUP BY symbol",
                (exchange, interval),
            ).fetchall()
            db.executemany(
                "INSERT OR REPLACE INTO sync_coverage VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(exchange, symbol, token, interval, first, last, bars, "complete", None, updated_at)
                 for symbol, token, first, last, bars in rows],
            )
        return len(rows)

    def prune_symbols(self, exchange: str, valid_symbols: Iterable[str]) -> int:
        """Remove non-equity archive rows using an explicit authoritative master."""
        exchange = exchange.upper()
        values = sorted({str(item).upper() for item in valid_symbols})
        with self.connect() as db:
            before = db.execute("SELECT COUNT(*) FROM candles WHERE exchange=?", (exchange,)).fetchone()[0]
            db.execute("CREATE TEMP TABLE valid_history_symbols(symbol TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO valid_history_symbols(symbol) VALUES(?)", [(item,) for item in values])
            db.execute("DELETE FROM candles WHERE exchange=? AND symbol NOT IN (SELECT symbol FROM valid_history_symbols)", (exchange,))
            db.execute("DELETE FROM sync_coverage WHERE exchange=? AND symbol NOT IN (SELECT symbol FROM valid_history_symbols)", (exchange,))
            after = db.execute("SELECT COUNT(*) FROM candles WHERE exchange=?", (exchange,)).fetchone()[0]
        return before - after

    def stats(self) -> Dict:
        with self.connect() as db:
            bars = db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            symbols = db.execute("SELECT COUNT(*) FROM sync_coverage WHERE status='complete'").fetchone()[0]
            failures = db.execute("SELECT COUNT(*) FROM sync_coverage WHERE status='failed'").fetchone()[0]
            bounds = db.execute("SELECT MIN(timestamp),MAX(timestamp) FROM candles").fetchone()
        return {"bars": bars, "symbols_complete": symbols, "failures": failures, "first_timestamp": bounds[0], "last_timestamp": bounds[1]}

    def candles(self, symbol: str, exchange: str = "", interval: str = "day", limit: int = 500):
        clause, params = "symbol=? AND interval=?", [symbol.upper(), interval]
        if exchange:
            clause += " AND exchange=?"
            params.append(exchange.upper())
        params.append(max(1, min(5000, limit)))
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(f"SELECT * FROM candles WHERE {clause} ORDER BY timestamp DESC LIMIT ?", params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def coverage(self, symbol: str, exchange: str = ""):
        clause, params = "symbol=?", [symbol.upper()]
        if exchange:
            clause += " AND exchange=?"
            params.append(exchange.upper())
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(f"SELECT * FROM sync_coverage WHERE {clause} ORDER BY updated_at DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None

    def series(self, interval: str = "day", minimum_bars: int = 80, symbols=None):
        """Return stored real series grouped by exchange/symbol for research jobs."""
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            keys = db.execute("SELECT exchange,symbol,COUNT(*) bars FROM candles WHERE interval=? GROUP BY exchange,symbol HAVING COUNT(*)>=? ORDER BY exchange,symbol", (interval, minimum_bars)).fetchall()
            allowed=set(symbols) if symbols is not None else None
            for key in keys:
                if allowed is not None and (key["exchange"],key["symbol"]) not in allowed: continue
                rows = db.execute("SELECT * FROM candles WHERE exchange=? AND symbol=? AND interval=? ORDER BY timestamp", (key["exchange"], key["symbol"], interval)).fetchall()
                yield {"exchange":key["exchange"],"symbol":key["symbol"],"bars":[dict(row) for row in rows]}

    def symbol_keys(self, interval: str = "day", minimum_bars: int = 1):
        with self.connect() as db:
            return [(row[0], row[1]) for row in db.execute(
                "SELECT exchange,symbol FROM candles WHERE interval=? GROUP BY exchange,symbol HAVING COUNT(*)>=?",
                (interval, minimum_bars),
            ).fetchall()]

    def market_context(self, exchange: str = "NSE", interval: str = "day") -> Dict[str, Dict]:
        """Build same-day/lagged broad-market context without future observations."""
        with self.connect() as db:
            rows = db.execute("""
              WITH daily AS (
                SELECT timestamp,close,
                  LAG(close) OVER (PARTITION BY exchange,symbol ORDER BY timestamp) previous_close
                FROM candles WHERE exchange=? AND interval=?
              ), returns AS (
                SELECT timestamp,close/previous_close-1 return_1d
                FROM daily WHERE previous_close>0
              )
              SELECT timestamp,AVG(return_1d),AVG(CASE WHEN return_1d>0 THEN 1.0 ELSE 0.0 END),COUNT(*)
              FROM returns WHERE return_1d BETWEEN -0.5 AND 0.5
              GROUP BY timestamp ORDER BY timestamp
            """, (exchange.upper(), interval)).fetchall()
        context={}; market_returns=[]
        for timestamp,market_return,breadth,count in rows:
            market_returns.append(float(market_return or 0))
            window=market_returns[-20:]
            compounded=1.0
            for value in window: compounded*=1+value
            volatility=pstdev(window) if len(window)>1 else 0.0
            context[timestamp]={"market_return_1d":float(market_return or 0),"market_return_20d":compounded-1,
                                "market_breadth":float(breadth or .5),"market_volatility_20d":volatility,
                                "market_observations":int(count)}
        return context

    def liquid_symbol_keys(self, minimum_average_value: float, sessions: int = 20, interval: str = "day"):
        exchanges=sorted(ML_TRAINING_EXCHANGES or {"NSE"})
        placeholders=",".join(["?"]*len(exchanges))
        with self.connect() as db:
            rows=db.execute(f"""
              WITH sessions AS (
                SELECT DISTINCT date(timestamp) trade_date FROM candles WHERE interval=?
              ), coverage AS (
                SELECT exchange,symbol,MIN(date(timestamp)) first_date,MAX(date(timestamp)) last_date,
                  COUNT(DISTINCT date(timestamp)) present_days
                FROM candles WHERE interval=? AND exchange IN ({placeholders}) GROUP BY exchange,symbol
              ), expected AS (
                SELECT c.exchange,c.symbol,COUNT(*) expected_days
                FROM coverage c JOIN sessions s ON s.trade_date BETWEEN c.first_date AND c.last_date
                GROUP BY c.exchange,c.symbol
              ), recent AS (
                SELECT exchange,symbol,close*volume traded_value,
                  ROW_NUMBER() OVER(PARTITION BY exchange,symbol ORDER BY timestamp DESC) rn
                FROM candles WHERE interval=? AND exchange IN ({placeholders})
              ), liquidity AS (
                SELECT exchange,symbol,AVG(traded_value) average_traded_value
                FROM recent WHERE rn<=? GROUP BY exchange,symbol
              )
              SELECT l.exchange,l.symbol FROM liquidity l
              JOIN coverage c ON c.exchange=l.exchange AND c.symbol=l.symbol
              JOIN expected e ON e.exchange=l.exchange AND e.symbol=l.symbol
              WHERE l.average_traded_value>=?
                AND c.present_days>=?
                AND (CAST(c.present_days AS REAL)/NULLIF(e.expected_days,0))*100>=?
            """,(interval,interval,*exchanges,interval,*exchanges,max(1,sessions),minimum_average_value,
                 ML_MIN_DAILY_BARS,ML_MIN_COVERAGE_PCT)).fetchall()
        return {(row[0],row[1]) for row in rows}
