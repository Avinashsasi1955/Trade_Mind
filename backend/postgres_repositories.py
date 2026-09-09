"""Native PostgreSQL runtime repositories for market history and ML research."""
from statistics import pstdev
from typing import Dict, Iterable, List

from .config import (DATABASE_POOL_MAX, DATABASE_POOL_MIN, DATABASE_URL, ML_MIN_COVERAGE_PCT,
                     ML_MIN_DAILY_BARS, ML_TRAINING_EXCHANGES)
from .postgres_compat import PostgresConnection


def _connect():
    return PostgresConnection(DATABASE_URL,DATABASE_POOL_MIN,DATABASE_POOL_MAX)


class PostgresHistoryStore:
    path=None
    def connect(self): return _connect()

    def _instrument(self,db,exchange,symbol,token=0):
        row=db.execute("SELECT id FROM instrument_master WHERE exchange=? AND symbol=?",(exchange.upper(),symbol.upper())).fetchone()
        if row: return row["id"]
        row=db.execute("""INSERT INTO instrument_master(instrument_token,exchange,symbol,instrument_type,lot_size,tick_size,is_active)
            VALUES(?,?,?,?,?,?,TRUE) ON CONFLICT(exchange,symbol) DO UPDATE SET instrument_token=COALESCE(EXCLUDED.instrument_token,instrument_master.instrument_token)
            RETURNING id""",(token or None,exchange.upper(),symbol.upper(),"EQ",1,.05)).fetchone()
        return row["id"]

    def save(self,exchange,symbol,token,interval,candles,updated_at,source="kite"):
        rows=list(candles)
        with self.connect() as db:
            instrument_id=self._instrument(db,exchange,symbol,token)
            db.executemany("""INSERT INTO live_market_bars(instrument_id,interval,bar_time,open_price,high_price,low_price,close_price,volume,open_interest,source,exchange_timestamp)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,interval,bar_time) DO UPDATE SET
                open_price=EXCLUDED.open_price,high_price=EXCLUDED.high_price,low_price=EXCLUDED.low_price,close_price=EXCLUDED.close_price,
                volume=EXCLUDED.volume,open_interest=EXCLUDED.open_interest,source=EXCLUDED.source,exchange_timestamp=EXCLUDED.exchange_timestamp""",
                [(instrument_id,interval,x["timestamp"],x["open"],x["high"],x["low"],x["close"],x.get("volume",0),x.get("oi") or None,source,x["timestamp"]) for x in rows])
            coverage=db.execute("SELECT MIN(bar_time) first,MAX(bar_time) last,COUNT(*) bars FROM live_market_bars WHERE instrument_id=? AND interval=?",(instrument_id,interval)).fetchone()
            db.execute("""INSERT INTO history_sync_coverage(instrument_id,interval,first_timestamp,last_timestamp,bars,status,error,updated_at)
                VALUES(?,?,?,?,?,'complete',NULL,?) ON CONFLICT(instrument_id,interval) DO UPDATE SET first_timestamp=EXCLUDED.first_timestamp,
                last_timestamp=EXCLUDED.last_timestamp,bars=EXCLUDED.bars,status='complete',error=NULL,updated_at=EXCLUDED.updated_at""",
                (instrument_id,interval,coverage["first"],coverage["last"],coverage["bars"],updated_at))
        return len(rows)

    def failure(self,exchange,symbol,token,interval,error,updated_at):
        with self.connect() as db:
            instrument_id=self._instrument(db,exchange,symbol,token)
            db.execute("""INSERT INTO history_sync_coverage(instrument_id,interval,bars,status,error,updated_at) VALUES(?,?,0,'failed',?,?)
                ON CONFLICT(instrument_id,interval) DO UPDATE SET status='failed',error=EXCLUDED.error,updated_at=EXCLUDED.updated_at""",
                (instrument_id,interval,error[:300],updated_at))

    def archive_imported(self,exchange,trade_date,source):
        with self.connect() as db: row=db.execute("SELECT 1 ok FROM history_ingestions WHERE exchange=? AND trade_date=? AND source=? AND status='complete'",(exchange.upper(),trade_date,source)).fetchone()
        return bool(row)

    def save_archive(self,exchange,trade_date,candles,source,url,sha256,byte_count,imported_at):
        grouped={}
        for item in candles: grouped.setdefault(item["symbol"],[]).append(item)
        total=0
        for symbol,rows in grouped.items(): total+=self.save(exchange,symbol,int(rows[0].get("instrument_token",0)),"day",rows,imported_at,source)
        with self.connect() as db:
            db.execute("""INSERT INTO history_ingestions(exchange,trade_date,source,url,sha256,byte_count,row_count,status,error,imported_at)
                VALUES(?,?,?,?,?,?,?,'complete',NULL,?) ON CONFLICT(exchange,trade_date,source) DO UPDATE SET url=EXCLUDED.url,sha256=EXCLUDED.sha256,
                byte_count=EXCLUDED.byte_count,row_count=EXCLUDED.row_count,status='complete',error=NULL,imported_at=EXCLUDED.imported_at""",
                (exchange.upper(),trade_date,source,url,sha256,byte_count,total,imported_at))
        return total

    def record_archive_failure(self,exchange,trade_date,source,url,error,imported_at):
        with self.connect() as db: db.execute("""INSERT INTO history_ingestions(exchange,trade_date,source,url,byte_count,row_count,status,error,imported_at)
            VALUES(?,?,?,?,0,0,'failed',?,?) ON CONFLICT(exchange,trade_date,source) DO UPDATE SET url=EXCLUDED.url,status='failed',error=EXCLUDED.error,imported_at=EXCLUDED.imported_at""",
            (exchange.upper(),trade_date,source,url,error[:300],imported_at))

    def rebuild_coverage(self,exchange,interval="day",updated_at=""):
        with self.connect() as db:
            rows=db.execute("""SELECT b.instrument_id,MIN(b.bar_time) first,MAX(b.bar_time) last,COUNT(*) bars FROM live_market_bars b
                JOIN instrument_master i ON i.id=b.instrument_id WHERE i.exchange=? AND b.interval=? GROUP BY b.instrument_id""",(exchange.upper(),interval)).fetchall()
            db.executemany("""INSERT INTO history_sync_coverage(instrument_id,interval,first_timestamp,last_timestamp,bars,status,error,updated_at)
                VALUES(?,?,?,?,?,'complete',NULL,?) ON CONFLICT(instrument_id,interval) DO UPDATE SET first_timestamp=EXCLUDED.first_timestamp,
                last_timestamp=EXCLUDED.last_timestamp,bars=EXCLUDED.bars,status='complete',error=NULL,updated_at=EXCLUDED.updated_at""",
                [(x["instrument_id"],interval,x["first"],x["last"],x["bars"],updated_at) for x in rows])
        return len(rows)

    def prune_symbols(self,exchange,valid_symbols):
        values=sorted({str(x).upper() for x in valid_symbols})
        with self.connect() as db:
            before=db.execute("SELECT COUNT(*) count FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id WHERE i.exchange=?",(exchange.upper(),)).fetchone()["count"]
            db.execute("CREATE TEMP TABLE valid_history_symbols(symbol TEXT PRIMARY KEY) ON COMMIT DROP")
            db.executemany("INSERT INTO valid_history_symbols(symbol) VALUES(?)",[(x,) for x in values])
            db.execute("DELETE FROM live_market_bars b USING instrument_master i WHERE b.instrument_id=i.id AND i.exchange=? AND i.symbol NOT IN (SELECT symbol FROM valid_history_symbols)",(exchange.upper(),))
            after=db.execute("SELECT COUNT(*) count FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id WHERE i.exchange=?",(exchange.upper(),)).fetchone()["count"]
        return before-after

    def stats(self):
        with self.connect() as db:
            coverage = db.execute("""SELECT COUNT(*) symbols_complete,
                MIN(first_timestamp) first_timestamp,MAX(last_timestamp) last_timestamp
                FROM history_sync_coverage WHERE status='complete'""").fetchone()
            bars_est = db.execute("SELECT COALESCE((SELECT reltuples::bigint FROM pg_class WHERE relname='live_market_bars'), 0) bars").fetchone()["bars"]
            failed = db.execute("SELECT COUNT(*) failures FROM history_sync_coverage WHERE status='failed'").fetchone()["failures"]
        return {
            "bars": int(bars_est),
            "symbols_complete": int(coverage["symbols_complete"] if coverage else 0),
            "failures": int(failed),
            "first_timestamp": coverage["first_timestamp"] if coverage else None,
            "last_timestamp": coverage["last_timestamp"] if coverage else None,
        }

    def candles(self,symbol,exchange="",interval="day",limit=500):
        clause="i.symbol=? AND b.interval=?"; params=[symbol.upper(),interval]
        if exchange: clause+=" AND i.exchange=?"; params.append(exchange.upper())
        params.append(max(1,min(5000,limit)))
        with self.connect() as db: rows=db.execute(f"""SELECT i.exchange,i.symbol,COALESCE(i.instrument_token,0) instrument_token,b.interval,b.bar_time timestamp,
            b.open_price open,b.high_price high,b.low_price low,b.close_price close,b.volume,COALESCE(b.open_interest,0) oi,b.source
            FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id WHERE {clause} ORDER BY b.bar_time DESC LIMIT ?""",params).fetchall()
        return [dict(x) for x in reversed(rows)]

    def coverage(self,symbol,exchange=""):
        clause="i.symbol=?"; params=[symbol.upper()]
        if exchange: clause+=" AND i.exchange=?"; params.append(exchange.upper())
        with self.connect() as db: row=db.execute(f"""SELECT i.exchange,i.symbol,COALESCE(i.instrument_token,0) instrument_token,c.interval,c.first_timestamp,c.last_timestamp,c.bars,c.status,c.error,c.updated_at
            FROM history_sync_coverage c JOIN instrument_master i ON i.id=c.instrument_id WHERE {clause} ORDER BY c.updated_at DESC LIMIT 1""",params).fetchone()
        return dict(row) if row else None

    def series(self,interval="day",minimum_bars=80,symbols=None):
        with self.connect() as db: keys=db.execute("""SELECT i.exchange,i.symbol,COUNT(*) bars FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
            WHERE b.interval=? GROUP BY i.exchange,i.symbol HAVING COUNT(*)>=? ORDER BY i.exchange,i.symbol""",(interval,minimum_bars)).fetchall()
        allowed=set(symbols) if symbols is not None else None
        for key in keys:
            if allowed is not None and (key["exchange"],key["symbol"]) not in allowed: continue
            yield {"exchange":key["exchange"],"symbol":key["symbol"],"bars":self.candles(key["symbol"],key["exchange"],interval,5000)}

    def symbol_keys(self,interval="day",minimum_bars=1):
        with self.connect() as db: rows=db.execute("""SELECT i.exchange,i.symbol FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
            WHERE b.interval=? GROUP BY i.exchange,i.symbol HAVING COUNT(*)>=?""",(interval,minimum_bars)).fetchall()
        return [(x["exchange"],x["symbol"]) for x in rows]

    def market_context(self,exchange="NSE",interval="day"):
        with self.connect() as db: rows=db.execute("""WITH daily AS (SELECT b.bar_time timestamp,b.close_price close,
            LAG(b.close_price) OVER(PARTITION BY b.instrument_id ORDER BY b.bar_time) previous_close FROM live_market_bars b
            JOIN instrument_master i ON i.id=b.instrument_id WHERE i.exchange=? AND b.interval=?), returns AS
            (SELECT timestamp,close/previous_close-1 return_1d FROM daily WHERE previous_close>0)
            SELECT timestamp,AVG(return_1d) market_return,AVG(CASE WHEN return_1d>0 THEN 1.0 ELSE 0.0 END) breadth,COUNT(*) observations
            FROM returns WHERE return_1d BETWEEN -0.5 AND 0.5 GROUP BY timestamp ORDER BY timestamp""",(exchange.upper(),interval)).fetchall()
        context={}; market_returns=[]
        for row in rows:
            value=float(row["market_return"] or 0); market_returns.append(value); window=market_returns[-20:]; compounded=1.0
            for item in window: compounded*=1+item
            context[row["timestamp"]]={"market_return_1d":value,"market_return_20d":compounded-1,"market_breadth":float(row["breadth"] or .5),
                "market_volatility_20d":pstdev(window) if len(window)>1 else 0.0,"market_observations":int(row["observations"])}
        return context

    def liquid_symbol_keys(self,minimum_average_value,sessions=20,interval="day"):
        exchanges=sorted(ML_TRAINING_EXCHANGES or {"NSE"})
        placeholders=",".join(["?"]*len(exchanges))
        with self.connect() as db: rows=db.execute(f"""
            WITH sessions AS (
              SELECT DISTINCT (bar_time AT TIME ZONE 'Asia/Kolkata')::date trade_date
              FROM live_market_bars WHERE interval=?
            ), coverage AS (
              SELECT i.id,i.exchange,i.symbol,
                MIN((b.bar_time AT TIME ZONE 'Asia/Kolkata')::date) first_date,
                MAX((b.bar_time AT TIME ZONE 'Asia/Kolkata')::date) last_date,
                COUNT(DISTINCT (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date) present_days
              FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
              WHERE b.interval=? AND i.exchange IN ({placeholders}) AND i.instrument_type='EQ'
              GROUP BY i.id,i.exchange,i.symbol
            ), expected AS (
              SELECT c.id,COUNT(*) expected_days
              FROM coverage c JOIN sessions s ON s.trade_date BETWEEN c.first_date AND c.last_date
              GROUP BY c.id
            ), recent AS (
              SELECT i.id,i.exchange,i.symbol,b.close_price*b.volume traded_value,
                ROW_NUMBER() OVER(PARTITION BY b.instrument_id ORDER BY b.bar_time DESC) rn
              FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
              WHERE b.interval=? AND i.exchange IN ({placeholders}) AND i.instrument_type='EQ'
            ), liquidity AS (
              SELECT id,exchange,symbol,AVG(traded_value) average_traded_value
              FROM recent WHERE rn<=? GROUP BY id,exchange,symbol
            )
            SELECT l.exchange,l.symbol FROM liquidity l
            JOIN coverage c ON c.id=l.id JOIN expected e ON e.id=l.id
            WHERE l.average_traded_value>=?
              AND c.present_days>=?
              AND (c.present_days::numeric/NULLIF(e.expected_days,0))*100>=?
        """,(interval,interval,*exchanges,interval,*exchanges,max(1,sessions),minimum_average_value,
             ML_MIN_DAILY_BARS,ML_MIN_COVERAGE_PCT)).fetchall()
        return {(x["exchange"],x["symbol"]) for x in rows}


class PostgresResearchStore:
    path=None
    is_postgres=True
    def connect(self): return _connect()
    def add_action(self,exchange,symbol,effective_date,action_type,ratio_from=1,ratio_to=1,cash_amount=0,source="manual"):
        if action_type not in {"split","bonus","dividend"}: raise ValueError("Unsupported corporate action")
        from .database import now_iso
        with self.connect() as db: db.execute("""INSERT INTO corporate_actions(exchange,symbol,effective_date,action_type,ratio_from,ratio_to,cash_amount,source,created_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange,symbol,effective_date,action_type) DO UPDATE SET ratio_from=EXCLUDED.ratio_from,ratio_to=EXCLUDED.ratio_to,
            cash_amount=EXCLUDED.cash_amount,source=EXCLUDED.source,created_at=EXCLUDED.created_at""",(exchange.upper(),symbol.upper(),effective_date,action_type,ratio_from,ratio_to,cash_amount,source,now_iso()))
    def actions(self,exchange,symbol):
        with self.connect() as db: return [dict(x) for x in db.execute("SELECT * FROM corporate_actions WHERE exchange=? AND symbol=? ORDER BY effective_date",(exchange,symbol)).fetchall()]
    def status(self):
        import json
        tables=("corporate_actions","corporate_action_ingestions","universe_membership","feature_rows","model_versions","validation_runs","shadow_predictions","drift_reports")
        counts = {}
        with self.connect() as db:
            for table in tables:
                if table in ("feature_rows", "shadow_predictions"):
                    row = db.execute(f"SELECT COALESCE((SELECT reltuples::bigint FROM pg_class WHERE relname='{table}'), 0) count").fetchone()
                else:
                    row = db.execute(f"SELECT COUNT(*) count FROM {table}").fetchone()
                counts[table] = int(row["count"] if row else 0)
            latest=db.execute("SELECT version,status,metrics,created_at FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            experiment=db.execute("SELECT version,status,metrics,created_at FROM model_versions ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            value=latest["metrics"]; metrics=value if isinstance(value,dict) else json.loads(value)
            latest={**dict(latest),"metrics":metrics}
        if experiment:
            value=experiment["metrics"]; experiment={**dict(experiment),"metrics":value if isinstance(value,dict) else json.loads(value)}
        return {"counts":counts,"latest_model":latest,"latest_experiment":experiment}
