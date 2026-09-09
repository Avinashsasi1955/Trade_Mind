"""Kite instrument synchronization and deterministic PostgreSQL bar aggregation."""
import os
import time as time_module
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from .zerodha_adapter import ZerodhaAdapter


IST = ZoneInfo("Asia/Kolkata")


def _session_time(value: str, fallback: time) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except Exception:
        return fallback


def _is_continuous_market_bar(timestamp: datetime) -> bool:
    """True only for normal NSE/BSE continuous-market buckets.

    Upstox can keep sending reconnect/status or delayed ticks after 15:30 IST.
    Those are useful diagnostics but must not become completed ML/chart candles.
    """
    local = timestamp.astimezone(IST)
    start = _session_time(os.getenv("NIVESH_MARKET_SESSION_START_IST", "09:15"), time(9, 15))
    end = _session_time(os.getenv("NIVESH_MARKET_SESSION_END_IST", "15:30"), time(15, 30))
    return local.weekday() < 5 and start <= local.time() < end


def _kind(row: Dict) -> str:
    value=str(row.get("instrument_type") or "").upper()
    if value in {"FUT","CE","PE"}: return value
    if value in {"EQ","BE","BZ"}: return "EQ"
    return "INDEX"


def sync_instrument_master(database_url: str,api_key: str,access_token: str) -> Dict:
    adapter=ZerodhaAdapter(api_key,access_token); rows=adapter.instruments(); accepted=[]
    for row in rows:
        exchange=str(row.get("exchange") or "").upper()
        if exchange not in {"NSE","BSE","NFO","BFO"}: continue
        kind=_kind(row); symbol=str(row.get("tradingsymbol") or "").upper()
        if not symbol: continue
        expiry=row.get("expiry") or None; strike=Decimal(str(row.get("strike") or 0)) if kind in {"CE","PE"} else None
        accepted.append({"token":int(row["instrument_token"]),"exchange":exchange,"symbol":symbol,
                         "underlying":str(row.get("name") or symbol).upper(),"kind":kind,"expiry":expiry,
                         "strike":strike,"lot":int(row.get("lot_size") or 1),"tick":Decimal(str(row.get("tick_size") or .05)),
                         "fno":exchange in {"NFO","BFO"}})
    engine=create_engine(database_url,pool_pre_ping=True,future=True)
    with engine.begin() as connection:
        for row in accepted:
            connection.execute(text("""
              INSERT INTO instrument_master(instrument_token,exchange,symbol,underlying_symbol,instrument_type,expiry,strike,lot_size,tick_size,is_fno_eligible,is_active)
              VALUES(:token,:exchange,:symbol,:underlying,:kind,:expiry,:strike,:lot,:tick,:fno,TRUE)
              ON CONFLICT(exchange,symbol) DO UPDATE SET instrument_token=EXCLUDED.instrument_token,underlying_symbol=EXCLUDED.underlying_symbol,
              instrument_type=EXCLUDED.instrument_type,expiry=EXCLUDED.expiry,strike=EXCLUDED.strike,lot_size=EXCLUDED.lot_size,
              tick_size=EXCLUDED.tick_size,is_fno_eligible=EXCLUDED.is_fno_eligible,is_active=TRUE,system_recorded_at=CURRENT_TIMESTAMP
            """),row)
        connection.execute(text("""UPDATE instrument_master equity SET is_fno_eligible=EXISTS(
            SELECT 1 FROM instrument_master derivative WHERE derivative.exchange IN ('NFO','BFO')
            AND derivative.instrument_type IN ('FUT','CE','PE') AND derivative.is_active
            AND derivative.underlying_symbol=equity.symbol)
            WHERE equity.exchange IN ('NSE','BSE') AND equity.instrument_type='EQ'"""))
    return {"received":len(rows),"accepted":len(accepted),"source":"zerodha_kite","updated_at":datetime.now(timezone.utc).isoformat()}


class PostgresBarAggregator:
    def __init__(self,database_url: str,source: str="zerodha_kite"):
        self.engine=create_engine(database_url,pool_pre_ping=True,future=True)
        self.states={}; self.tokens={}; self.vix_tokens=set(); self.source=source
        self.open_trades_cache={}
        self.last_open_trades_sync=0.0
        self.refresh_tokens()

    def refresh_tokens(self):
        with self.engine.connect() as connection:
            if self.source=="upstox_v3":
                rows=connection.execute(text("""SELECT i.id,k.provider_token instrument_token,i.exchange,i.symbol,i.instrument_type
                    FROM instrument_provider_keys k JOIN instrument_master i ON i.id=k.instrument_id
                    WHERE i.is_active AND k.is_active AND k.provider='upstox_v3'""")).fetchall()
            else:
                rows=connection.execute(text("SELECT id,instrument_token,exchange,symbol,instrument_type FROM instrument_master WHERE is_active AND instrument_token IS NOT NULL")).fetchall()
        self.tokens={int(row.instrument_token):int(row.id) for row in rows}
        self.vix_tokens={int(row.instrument_token) for row in rows if row.symbol.replace(" ","").upper() in {"INDIAVIX","VIX"} and row.instrument_type=="INDEX"}

    def refresh_open_trades(self, force: bool = False):
        """Periodically sync active open trades cache for sub-second exit matching."""
        now_ts = time_module.time()
        if not force and (now_ts - self.last_open_trades_sync) < 3.0:
            return
        self.last_open_trades_sync = now_ts
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(text("""
                    SELECT a.id, a.instrument_id, a.side, a.theoretical_fill_price, a.stop_loss_price,
                           a.take_profit_price, a.quantity, a.estimated_fees, a.signal_at, i.instrument_type
                    FROM shadow_execution_audits a
                    JOIN instrument_master i ON i.id = a.instrument_id
                    WHERE a.audit_status = 'RECONCILED' AND a.net_pnl IS NULL
                """)).mappings().all()
                cache = {}
                for r in rows:
                    instr = int(r["instrument_id"])
                    cache.setdefault(instr, []).append(dict(r))
                self.open_trades_cache = cache
        except Exception:
            pass

    def check_instant_exit_breach(self, instrument_id: int, price: Decimal, observed: datetime):
        """Sub-second Autonomous Trade Closer Hook: checks incoming ticks against open trade SL/TP."""
        trades = self.open_trades_cache.get(instrument_id, [])
        if not trades:
            return

        remaining = []
        for trade in trades:
            sl = Decimal(str(trade["stop_loss_price"])) if trade["stop_loss_price"] is not None else None
            tp = Decimal(str(trade["take_profit_price"])) if trade["take_profit_price"] is not None else None
            side = str(trade["side"])
            exit_reason = None

            if side == "BUY":
                if sl is not None and price <= sl:
                    exit_reason = "STOP_LOSS"
                elif tp is not None and price >= tp:
                    exit_reason = "TAKE_PROFIT"
            else:
                if sl is not None and price >= sl:
                    exit_reason = "STOP_LOSS"
                elif tp is not None and price <= tp:
                    exit_reason = "TAKE_PROFIT"

            if exit_reason:
                try:
                    from .ml.validation_engine import record_shadow_exit
                    record_shadow_exit(self.engine, int(trade["id"]), price, exit_reason, observed)
                except Exception:
                    pass
            else:
                remaining.append(trade)

        if remaining:
            self.open_trades_cache[instrument_id] = remaining
        else:
            self.open_trades_cache.pop(instrument_id, None)

    @staticmethod
    def _timestamp(tick: Dict) -> datetime:
        raw=tick.get("exchange_timestamp") or tick.get("last_trade_time")
        if isinstance(raw,(int,float)) and raw>0: return datetime.fromtimestamp(raw,timezone.utc)
        if isinstance(raw,str): return datetime.fromisoformat(raw.replace("Z","+00:00")).astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    @staticmethod
    def _bucket_seconds(value: datetime, seconds: int) -> datetime:
        if seconds <= 1:
            return value.replace(microsecond=0)
        epoch = int(value.timestamp())
        return datetime.fromtimestamp(epoch - (epoch % seconds), timezone.utc)

    def ingest(self,ticks: Iterable[Dict]):
        completed=[]
        self.refresh_open_trades()
        for tick in ticks:
            token=int(tick.get("instrument_token") or 0); instrument=self.tokens.get(token); price=Decimal(str(tick.get("last_price") or 0))
            if not instrument or price<=0: continue
            observed=self._timestamp(tick)
            if not _is_continuous_market_bar(observed):
                continue
            if token in self.vix_tokens: self._save_vix(observed,price)

            # Sub-Second Trade Closer check on real-time tick price
            if instrument in self.open_trades_cache:
                self.check_instant_exit_breach(instrument, price, observed)

            for seconds,label in ((1,"1second"),(60,"1minute"),(300,"5minute")):
                bucket=self._bucket_seconds(observed,seconds); key=(instrument,label); state=self.states.get(key)
                if state and state["bar_time"]!=bucket: completed.append(state); state=None
                if state is None:
                    state={"instrument_id":instrument,"interval":label,"bar_time":bucket,"open":price,"high":price,"low":price,"close":price,
                           "first_volume":int(tick.get("volume") or 0),"last_volume":int(tick.get("volume") or 0),
                           "first_oi":int(tick.get("oi") or 0),"last_oi":int(tick.get("oi") or 0),"exchange_timestamp":observed}
                    self.states[key]=state
                else:
                    state["high"]=max(state["high"],price); state["low"]=min(state["low"],price); state["close"]=price
                    state["last_volume"]=int(tick.get("volume") or state["last_volume"]); state["last_oi"]=int(tick.get("oi") or state["last_oi"]); state["exchange_timestamp"]=observed
        if completed: self._save_bars(completed)
        return len(completed)

    def discard_partial(self):
        """Never persist an in-progress candle as completed during shutdown."""
        count=len(self.states); self.states.clear(); return count

    def flush_closed(self,watermark: datetime = None):
        """Persist states whose complete interval is at or before ``watermark``."""
        watermark=(watermark or datetime.now(timezone.utc)).astimezone(timezone.utc)
        completed=[]
        for key,state in list(self.states.items()):
            seconds={"1second":1,"1minute":60,"5minute":300}.get(state["interval"],60)
            if state["bar_time"]+timedelta(seconds=seconds)<=watermark:
                completed.append(state); self.states.pop(key,None)
        if completed: self._save_bars(completed)
        return len(completed)

    def partial_snapshots(self):
        """Return in-progress candle states for UI-only live chart rendering.

        These snapshots are intentionally not persisted as completed market bars,
        so ML training/inference still consumes closed candles only.
        """
        result=[]
        for state in self.states.values():
            result.append({
                "instrument_id": state["instrument_id"],
                "interval": state["interval"],
                "bar_time": state["bar_time"].isoformat(),
                "open": float(state["open"]),
                "high": float(state["high"]),
                "low": float(state["low"]),
                "close": float(state["close"]),
                "volume": max(0,int(state.get("last_volume") or 0)-int(state.get("first_volume") or 0)),
                "open_interest": int(state.get("last_oi") or 0),
                "exchange_timestamp": state["exchange_timestamp"].isoformat(),
            })
        return result

    def _save_vix(self,observed: datetime,value: Decimal):
        if not _is_continuous_market_bar(observed):
            return
        with self.engine.begin() as connection:
            connection.execute(text("""INSERT INTO india_vix_history(observed_at,value,source) VALUES(:time,:value,:source)
            ON CONFLICT(observed_at) DO UPDATE SET value=EXCLUDED.value,source=EXCLUDED.source,received_at=CURRENT_TIMESTAMP"""),{"time":observed,"value":value,"source":self.source})

    def _save_bars(self,bars):
        with self.engine.begin() as connection:
            for bar in bars:
                if not _is_continuous_market_bar(bar["bar_time"]):
                    continue
                volume=max(0,bar["last_volume"]-bar["first_volume"]); oi=bar["last_oi"] or None; oi_change=(bar["last_oi"]-bar["first_oi"]) if oi is not None else None
                connection.execute(text("""
                  INSERT INTO live_market_bars(instrument_id,interval,bar_time,open_price,high_price,low_price,close_price,volume,open_interest,oi_change,source,exchange_timestamp)
                  VALUES(:instrument_id,CAST(:interval AS bar_interval),:bar_time,:open,:high,:low,:close,:volume,:oi,:oi_change,:source,:exchange_timestamp)
                  ON CONFLICT(instrument_id,interval,bar_time) DO UPDATE SET high_price=GREATEST(live_market_bars.high_price,EXCLUDED.high_price),
                  low_price=LEAST(live_market_bars.low_price,EXCLUDED.low_price),close_price=EXCLUDED.close_price,volume=EXCLUDED.volume,
                  open_interest=EXCLUDED.open_interest,oi_change=EXCLUDED.oi_change,source=EXCLUDED.source,exchange_timestamp=EXCLUDED.exchange_timestamp,received_at=CURRENT_TIMESTAMP
                """),{**bar,"volume":volume,"oi":oi,"oi_change":oi_change,"source":self.source})
