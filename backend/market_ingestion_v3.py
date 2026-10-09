import logging
import os
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from .candle_sanitizer import BarInvariantValidator, TickSanitizer
from .zerodha_adapter import ZerodhaAdapter

logger = logging.getLogger("nivesh.market_ingestion")



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
    def __init__(self, database_url: str, source: str = "zerodha_kite"):
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self.states: Dict[Tuple[int, str], Dict] = {}
        self.tokens = {}
        self.vix_tokens = set()
        self.source = source
        self.token_types = {}
        self.sanitizer = TickSanitizer()
        self.open_trades_cache = {}
        self.last_open_trades_sync = 0.0
        self.prev_cumulative_volume: Dict[int, Tuple[date, int]] = {}
        self.last_completed_bar_time: Dict[Tuple[int, str], datetime] = {}
        self.refresh_tokens()

    def refresh_tokens(self):
        with self.engine.connect() as connection:
            if self.source == "upstox_v3":
                rows = connection.execute(text("""SELECT i.id,k.provider_token instrument_token,i.exchange,i.symbol,i.instrument_type
                    FROM instrument_provider_keys k JOIN instrument_master i ON i.id=k.instrument_id
                    WHERE i.is_active AND k.is_active AND k.provider='upstox_v3'""")).fetchall()
            else:
                rows = connection.execute(text("SELECT id,instrument_token,exchange,symbol,instrument_type FROM instrument_master WHERE is_active AND instrument_token IS NOT NULL")).fetchall()
        self.tokens = {int(row.instrument_token): int(row.id) for row in rows}
        self.token_types = {int(row.instrument_token): str(row.instrument_type or "EQ").upper() for row in rows}
        self.vix_tokens = {int(row.instrument_token) for row in rows if row.symbol.replace(" ", "").upper() in {"INDIAVIX", "VIX"} and row.instrument_type == "INDEX"}

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
        except Exception as exc:
            logger.warning("Failed to refresh open trades cache: %s", exc)

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
                    from .position_manager import PositionManager
                    pm = PositionManager(database_url=str(self.engine.url))
                    pm.execute_exit(int(trade["id"]), price, exit_reason, exit_at=observed)
                except Exception as exc:
                    logger.error("Failed to record instant exit breach for trade %s: %s", trade.get("id"), exc, exc_info=True)
            else:
                remaining.append(trade)

        if remaining:
            self.open_trades_cache[instrument_id] = remaining
        else:
            self.open_trades_cache.pop(instrument_id, None)

    @staticmethod
    def _timestamp(tick: Dict) -> datetime:
        raw = tick.get("exchange_timestamp") or tick.get("last_trade_time")
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if isinstance(raw, (int, float)) and raw > 0:
            return datetime.fromtimestamp(raw, timezone.utc)
        if isinstance(raw, str):
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    @staticmethod
    def _bucket_seconds(value: datetime, seconds: int) -> datetime:
        if seconds <= 1:
            return value.replace(microsecond=0)
        epoch = int(value.timestamp())
        return datetime.fromtimestamp(epoch - (epoch % seconds), timezone.utc)

    def ingest(self, ticks: Iterable[Dict]):
        completed = []
        self.refresh_open_trades()
        for tick in ticks:
            token = int(tick.get("instrument_token") or 0)
            instrument = self.tokens.get(token)
            price = Decimal(str(tick.get("last_price") or 0))
            if not instrument or price <= 0:
                continue
            observed = self._timestamp(tick)
            if not _is_continuous_market_bar(observed):
                continue

            # 5-Gate Tick Sanitizer: reject freak jumps, non-positive ticks, or stale timestamps
            itype = self.token_types.get(token, "EQ")
            raw_vol = int(tick.get("volume") or 0)
            is_valid, _rejection = self.sanitizer.validate_tick(instrument, price, observed, raw_vol, itype)
            if not is_valid:
                continue

            if token in self.vix_tokens:
                self._save_vix(observed, price)

            # Sub-Second Trade Closer check on real-time tick price
            if instrument in self.open_trades_cache:
                self.check_instant_exit_breach(instrument, price, observed)

            for seconds, label in ((1, "1second"), (60, "1minute"), (300, "5minute")):
                bucket = self._bucket_seconds(observed, seconds)
                key = (instrument, label)
                last_completed = self.last_completed_bar_time.get(key)
                if last_completed is not None and bucket <= last_completed:
                    # Late tick arrived after bar was already closed and saved.
                    # Drop it from reopening/corrupting completed candles.
                    continue

                state = self.states.get(key)
                if state is not None and bucket < state["bar_time"]:
                    # Out-of-order tick older than current in-progress bar.
                    # Do not evict current bar; drop out-of-order tick.
                    continue

                ist_date = observed.astimezone(IST).date()
                if state is not None and state["bar_time"] != bucket:
                    # Rollover: previous bar complete
                    baseline = int(state.get("baseline_volume") if state.get("baseline_volume") is not None else state.get("first_volume", 0))
                    state["volume"] = max(0, int(state.get("last_volume") or 0) - baseline)
                    completed.append(state)
                    self.last_completed_bar_time[key] = state["bar_time"]
                    bar_date = state["bar_time"].astimezone(IST).date()
                    self.prev_cumulative_volume[instrument] = (bar_date, int(state.get("last_volume") or 0))
                    state = None

                if state is None:
                    prev_entry = self.prev_cumulative_volume.get(instrument)
                    if prev_entry is not None:
                        cached_date, cached_vol = prev_entry
                        if ist_date != cached_date or raw_vol < cached_vol:
                            # New session or exchange counter reset: baseline resets to raw_vol
                            baseline = raw_vol
                        else:
                            baseline = cached_vol
                    else:
                        baseline = raw_vol
                    cur_vol_record = max(cached_vol if (prev_entry and ist_date == prev_entry[0] and raw_vol >= prev_entry[1]) else 0, raw_vol)
                    self.prev_cumulative_volume[instrument] = (ist_date, cur_vol_record)
                    state = {
                        "instrument_id": instrument,
                        "interval": label,
                        "bar_time": bucket,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "baseline_volume": baseline,
                        "first_volume": raw_vol,
                        "last_volume": raw_vol,
                        "volume": max(0, raw_vol - baseline),
                        "first_oi": int(tick.get("oi") or 0),
                        "last_oi": int(tick.get("oi") or 0),
                        "exchange_timestamp": observed,
                    }
                    self.states[key] = state
                else:
                    state["high"] = max(state["high"], price)
                    state["low"] = min(state["low"], price)
                    state["close"] = price
                    state["last_volume"] = max(int(state.get("last_volume") or 0), raw_vol)
                    baseline = int(state.get("baseline_volume") if state.get("baseline_volume") is not None else state.get("first_volume", 0))
                    state["volume"] = max(0, int(state["last_volume"]) - baseline)
                    prev_val = self.prev_cumulative_volume.get(instrument, (ist_date, 0))[1]
                    self.prev_cumulative_volume[instrument] = (ist_date, max(prev_val, raw_vol))
                    state["last_oi"] = int(tick.get("oi") or state.get("last_oi") or 0)
                    state["exchange_timestamp"] = observed

        if completed:
            self._save_bars(completed)
        return len(completed)

    def discard_partial(self):
        """Never persist an in-progress candle as completed during shutdown."""
        count = len(self.states)
        self.states.clear()
        return count

    def flush_closed(self, watermark: datetime = None):
        """Persist states whose complete interval is at or before ``watermark``."""
        watermark = (watermark or datetime.now(timezone.utc)).astimezone(timezone.utc)
        completed = []
        for key, state in list(self.states.items()):
            seconds = {"1second": 1, "1minute": 60, "5minute": 300}.get(state["interval"], 60)
            if state["bar_time"] + timedelta(seconds=seconds) <= watermark:
                baseline = int(state.get("baseline_volume") if state.get("baseline_volume") is not None else state.get("first_volume", 0))
                state["volume"] = max(0, int(state.get("last_volume") or 0) - baseline)
                completed.append(state)
                self.last_completed_bar_time[key] = state["bar_time"]
                bar_date = state["bar_time"].astimezone(IST).date()
                prev_entry = self.prev_cumulative_volume.get(state["instrument_id"], (bar_date, 0))
                prev_vol = prev_entry[1] if isinstance(prev_entry, tuple) else prev_entry
                self.prev_cumulative_volume[state["instrument_id"]] = (
                    bar_date,
                    max(prev_vol, int(state.get("last_volume") or 0)),
                )
                self.states.pop(key, None)
        if completed:
            self._save_bars(completed)
        return len(completed)

    def partial_snapshots(self):
        """Return in-progress candle states for UI-only live chart rendering.

        These snapshots are intentionally not persisted as completed market bars,
        so ML training/inference still consumes closed candles only.
        """
        result = []
        for state in self.states.values():
            baseline = int(state.get("baseline_volume") if state.get("baseline_volume") is not None else state.get("first_volume", 0))
            last_vol = int(state.get("last_volume") or 0)
            result.append({
                "instrument_id": state["instrument_id"],
                "interval": state["interval"],
                "bar_time": state["bar_time"].isoformat(),
                "open": float(state["open"]),
                "high": float(state["high"]),
                "low": float(state["low"]),
                "close": float(state["close"]),
                "volume": max(0, last_vol - baseline),
                "open_interest": int(state.get("last_oi") or 0),
                "exchange_timestamp": state["exchange_timestamp"].isoformat(),
            })
        return result

    def _save_vix(self, observed: datetime, value: Decimal):
        if not _is_continuous_market_bar(observed):
            return
        with self.engine.begin() as connection:
            connection.execute(text("""INSERT INTO india_vix_history(observed_at,value,source) VALUES(:time,:value,:source)
            ON CONFLICT(observed_at) DO UPDATE SET value=EXCLUDED.value,source=EXCLUDED.source,received_at=CURRENT_TIMESTAMP"""), {"time": observed, "value": value, "source": self.source})

    def _save_bars(self, bars):
        with self.engine.begin() as connection:
            for bar in bars:
                if not _is_continuous_market_bar(bar["bar_time"]):
                    continue
                sanitized = BarInvariantValidator.sanitize_bar(bar)
                connection.execute(text("""
                  INSERT INTO live_market_bars(instrument_id,interval,bar_time,open_price,high_price,low_price,close_price,volume,open_interest,oi_change,source,exchange_timestamp)
                  VALUES(:instrument_id,CAST(:interval AS bar_interval),:bar_time,:open,:high,:low,:close,:volume,:oi,:oi_change,:source,:exchange_timestamp)
                  ON CONFLICT(instrument_id,interval,bar_time) DO UPDATE SET high_price=GREATEST(live_market_bars.high_price,EXCLUDED.high_price),
                  low_price=LEAST(live_market_bars.low_price,EXCLUDED.low_price),close_price=EXCLUDED.close_price,
                  volume=GREATEST(live_market_bars.volume,EXCLUDED.volume),
                  open_interest=EXCLUDED.open_interest,oi_change=EXCLUDED.oi_change,source=EXCLUDED.source,exchange_timestamp=EXCLUDED.exchange_timestamp,received_at=CURRENT_TIMESTAMP
                """), {**sanitized, "source": self.source})

