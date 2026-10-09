"""Dedicated High-Frequency Positional Trade Manager & Real-Time Risk Guard.

Runs an autonomous, non-blocking 1-second evaluation loop exclusively for open trades
(WHERE net_pnl IS NULL), decoupled from heavy batch ML candidate generation.

Enforces:
1. Exact elapsed-seconds clock:
   - 0-90s: Fast Adverse Cut at -0.4R if no upward expansion (cuts fakeouts early).
   - 90s-300s (5m): Tighten stop loss to -0.35R if unrealized gain < +0.2R (scratch protection).
   - 15m: STAGNATION_GUARD closes flat intraday trades (curing INFY/ULTRACEMCO bleed).
   - Swing trades exempt up to 15 market sessions.
2. Sub-second fee-padded breakeven (+0.7R), profit lock (+1.2R), and trailing stop (+1.4R).
3. Real-time telemetry publication to Redis (`nivesh:positions:live`) with 1-second TTL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import create_engine, text

from .config import (
    DATABASE_URL,
    REDIS_URL,
    PRICE_MAX_AGE_SECONDS,
    PRICE_MAX_AGE_SWING_SECONDS,
    STALE_DATA_EXIT_MINUTES,
    PROFIT_HARVEST_ENABLED,
    HARVEST_TRIGGER,
    HARVEST_EXIT_THRESHOLD,
    HARVEST_TIGHTEN_THRESHOLD,
    HARVEST_LOCK_FRACTION,
)
from .profit_harvest import (
    calculate_progress,
    calculate_harvest_stop,
    compute_reversal_score,
)
from .ml.validation_engine import record_shadow_exit

logger = logging.getLogger("nivesh.position_manager")
IST = ZoneInfo("Asia/Kolkata")
TRADE_BAR_SOURCES = ("zerodha_kite", "kite_gap_backfill", "upstox_v3", "upstox_rest_5m")


ADVERSE_CUT_WINDOW_SECONDS: float = 90.0
ADVERSE_CUT_THRESHOLD_R: Decimal = Decimal("0.40")
STAGNATION_TIGHTEN_SECONDS: float = 300.0
STAGNATION_TIGHTEN_R: Decimal = Decimal("0.35")
STAGNATION_SCRATCH_SECONDS: float = 900.0
STAGNATION_MIN_EXPANSION_R: Decimal = Decimal("0.25")
FORCE_FLAT_TIME: str = "15:15"


class PriceResult(tuple):
    """2-tuple (price, source) with an optional bar_time attribute for staleness checking."""

    def __new__(cls, price: Optional[Decimal], source: Optional[str], bar_time: Optional[datetime] = None):
        inst = super().__new__(cls, (price, source))
        inst.bar_time = bar_time
        return inst


from datetime import time as dt_time


def _parse_time(val: Any) -> Optional[dt_time]:
    if val is None:
        return None
    if isinstance(val, dt_time):
        return val
    if isinstance(val, str):
        val = val.strip()
        parts = val.split(":")
        if len(parts) >= 2:
            return dt_time(int(parts[0]), int(parts[1]), int(parts[2].split(".")[0]) if len(parts) > 2 else 0)
    return None


import threading

# Calendar session cache: date -> (dict of session row or None)
_CALENDAR_CACHE: Dict[date, Optional[Dict[str, Any]]] = {}
_MISSING_CALENDAR_WARNED_DATES: set[date] = set()
_CALENDAR_LOCK = threading.Lock()


def clear_calendar_cache() -> None:
    """Clear calendar cache and warning tracking for tests and resets."""
    with _CALENDAR_LOCK:
        _CALENDAR_CACHE.clear()
        _MISSING_CALENDAR_WARNED_DATES.clear()


def get_calendar_session(target_date: date, engine: Any = None) -> Optional[Dict[str, Any]]:
    """Retrieve and cache exchange_trading_calendar row for target_date.

    Caches per IST date to avoid database hits per second per position.
    Logs a missing-calendar warning once per date.
    """
    with _CALENDAR_LOCK:
        if target_date in _CALENDAR_CACHE:
            return _CALENDAR_CACHE[target_date]

    session_row = None
    if engine is not None:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT session_status, opens_at, closes_at, source
                        FROM exchange_trading_calendar
                        WHERE exchange = 'NSE' AND session_date = :day
                    """),
                    {"day": target_date}
                ).mappings().one_or_none()
                if row:
                    session_row = dict(row)
        except Exception as e:
            logger.debug(f"exchange_trading_calendar lookup failed for {target_date}: {e}")

    with _CALENDAR_LOCK:
        _CALENDAR_CACHE[target_date] = session_row
        if session_row is None and target_date not in _MISSING_CALENDAR_WARNED_DATES:
            logger.warning(
                f"No exchange_trading_calendar row for NSE on {target_date}; "
                f"falling back to weekday 09:15-15:30 IST"
            )
            _MISSING_CALENDAR_WARNED_DATES.add(target_date)

    return session_row


def is_market_hours(dt: Optional[datetime] = None, engine: Any = None) -> bool:
    """Return True if given datetime falls in regular NSE market hours using exchange_trading_calendar.

    Falls back to weekday plus 09:15-15:30 IST only if no calendar row exists, logging a warning once per date.
    """
    target = dt or datetime.now(timezone.utc)
    target_ist = target.astimezone(IST) if getattr(target, "tzinfo", None) else target.replace(tzinfo=timezone.utc).astimezone(IST)
    target_date = target_ist.date()
    cur_time = target_ist.time()

    session_row = get_calendar_session(target_date, engine)
    if session_row is not None:
        status = str(session_row.get("session_status") or "").upper()
        if status == "CLOSED":
            return False
        opens = _parse_time(session_row.get("opens_at")) or dt_time(9, 15)
        closes = _parse_time(session_row.get("closes_at")) or dt_time(15, 30)
        return opens <= cur_time <= closes

    # Fallback to weekday check if calendar row is missing
    if target_ist.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    return dt_time(9, 15) <= cur_time <= dt_time(15, 30)


def get_market_open_time(dt: datetime, engine: Any = None) -> dt_time:
    """Get NSE market open time for date of given datetime, checking calendar."""
    target_ist = dt.astimezone(IST) if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=timezone.utc).astimezone(IST)
    session_row = get_calendar_session(target_ist.date(), engine)
    if session_row is not None:
        opens = _parse_time(session_row.get("opens_at"))
        if opens:
            return opens
    return dt_time(9, 15)




def execute_exit(
    engine,
    audit_id: int,
    price: Any = None,
    reason: str = "",
    exit_at: Optional[datetime] = None,
    is_synthetic: bool = False,
    exit_price: Any = None,
    exit_reason: str = "",
) -> bool:
    """Module-level atomic exit writer: takes an existing engine to prevent per-exit connection allocations and password masking issues."""
    final_price = exit_price if exit_price is not None else price
    final_reason = exit_reason if exit_reason else reason
    if final_price is None:
        raise ValueError("price is required")
    exit_time = exit_at or datetime.now(timezone.utc)
    record_shadow_exit(
        engine,
        int(audit_id),
        Decimal(str(final_price)) if not isinstance(final_price, Decimal) else final_price,
        str(final_reason),
        exit_at=exit_time,
        is_synthetic=is_synthetic,
    )
    return True


class PositionManager:
    def __init__(self, database_url: Optional[str] = None, redis_url: Optional[str] = None, redis_client=None):
        self.database_url = database_url or DATABASE_URL or os.getenv("DATABASE_URL", "")
        self.redis_url = redis_url or REDIS_URL or os.getenv("REDIS_URL", "")
        
        # Dedicated engine with pool pre-ping
        engine_kwargs = {"pool_pre_ping": True, "future": True}
        if not self.database_url.startswith("sqlite"):
            engine_kwargs["pool_size"] = 3
            engine_kwargs["max_overflow"] = 2
        self.engine = create_engine(self.database_url, **engine_kwargs)
        if redis_client is not None:
            self.redis = redis_client
        else:
            self.redis = (
                redis.Redis.from_url(self.redis_url, decode_responses=True, socket_timeout=3)
                if self.redis_url
                else None
            )

        # Risk parameters
        self.breakeven_trigger_r = Decimal(os.getenv("NIVESH_SHADOW_BREAKEVEN_TRIGGER_R", "1.10"))
        self.profit_lock_trigger_r = Decimal(os.getenv("NIVESH_SHADOW_PROFIT_LOCK_TRIGGER_R", "1.60"))
        self.profit_lock_guaranteed_r = Decimal(os.getenv("NIVESH_SHADOW_PROFIT_LOCK_GUARANTEED_R", "1.00"))
        self.trailing_trigger_r = Decimal(os.getenv("NIVESH_SHADOW_TRAILING_TRIGGER_R", "1.50"))
        self.trailing_giveback_r = Decimal(os.getenv("NIVESH_SHADOW_TRAILING_GIVEBACK_R", "0.40"))
        
        self.adverse_cut_window_seconds = ADVERSE_CUT_WINDOW_SECONDS
        self.adverse_cut_threshold_r = ADVERSE_CUT_THRESHOLD_R
        
        self.stagnation_tighten_seconds = STAGNATION_TIGHTEN_SECONDS
        self.stagnation_tighten_r = STAGNATION_TIGHTEN_R
        self.stagnation_scratch_seconds = STAGNATION_SCRATCH_SECONDS
        self.stagnation_min_expansion_r = STAGNATION_MIN_EXPANSION_R
        self.force_flat_time = os.getenv("NIVESH_SHADOW_FORCE_FLAT_IST", FORCE_FLAT_TIME)
        self.price_max_age_seconds = int(os.getenv("NIVESH_PRICE_MAX_AGE_SECONDS", str(PRICE_MAX_AGE_SECONDS)))
        self.price_max_age_swing_seconds = int(os.getenv("NIVESH_PRICE_MAX_AGE_SWING_SECONDS", str(PRICE_MAX_AGE_SWING_SECONDS)))
        self.stale_data_exit_minutes = int(os.getenv("NIVESH_STALE_DATA_EXIT_MINUTES", str(STALE_DATA_EXIT_MINUTES)))
        self.stale_open_grace_seconds = int(os.getenv("NIVESH_STALE_OPEN_GRACE_SECONDS", "120"))
        self.stale_positions_tracker: Dict[int, datetime] = {}

        # Profit-Harvest Layer (Phase 2): Target 1.5R, Reached 1.25R (0.83), Chart Reverses
        self.profit_harvest_enabled = os.getenv("NIVESH_PROFIT_HARVEST_ENABLED", "1" if PROFIT_HARVEST_ENABLED else "0") == "1"
        self.harvest_trigger = float(os.getenv("NIVESH_HARVEST_TRIGGER", str(HARVEST_TRIGGER)))
        self.harvest_exit_threshold = float(os.getenv("NIVESH_HARVEST_EXIT_THRESHOLD", str(HARVEST_EXIT_THRESHOLD)))
        self.harvest_tighten_threshold = float(os.getenv("NIVESH_HARVEST_TIGHTEN_THRESHOLD", str(HARVEST_TIGHTEN_THRESHOLD)))
        self.harvest_lock_fraction = float(os.getenv("NIVESH_HARVEST_LOCK_FRACTION", str(HARVEST_LOCK_FRACTION)))
        self._harvest_cache: Dict[Tuple[int, datetime], Dict[str, Any]] = {}

    def _get_recent_5m_bars(self, instrument_id: int, watermark: datetime, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch or derive recent CLOSED 5m bars for instrument up to watermark.

        Drops in-progress candles: a 5m bar starting at bar_time is only closed when
        watermark >= bar_time + 5 minutes.
        """
        closed_cutoff = watermark - timedelta(minutes=5)
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT open_price, high_price, low_price, close_price, volume, bar_time
                        FROM live_market_bars
                        WHERE instrument_id = :inst_id
                          AND interval IN ('5minute', '5m')
                          AND bar_time <= :wm
                        ORDER BY bar_time DESC
                        LIMIT :limit
                    """),
                    {"inst_id": instrument_id, "wm": closed_cutoff, "limit": limit}
                ).mappings().all()
                if rows and len(rows) >= 3:
                    return [
                        {
                            "open": float(r["open_price"]),
                            "high": float(r["high_price"]),
                            "low": float(r["low_price"]),
                            "close": float(r["close_price"]),
                            "volume": float(r["volume"] or 0),
                            "bar_time": r["bar_time"],
                        }
                        for r in reversed(rows)
                    ]

                # Derive 5m bars from 1m bars if 5m bars not directly written
                m1_rows = conn.execute(
                    text("""
                        SELECT open_price, high_price, low_price, close_price, volume, bar_time
                        FROM live_market_bars
                        WHERE instrument_id = :inst_id
                          AND interval IN ('1minute', '1m')
                          AND bar_time <= :wm
                        ORDER BY bar_time DESC
                        LIMIT :limit
                    """),
                    {"inst_id": instrument_id, "wm": watermark, "limit": limit * 5}
                ).mappings().all()
                if m1_rows:
                    bars_1m = [
                        {
                            "open": float(r["open_price"]),
                            "high": float(r["high_price"]),
                            "low": float(r["low_price"]),
                            "close": float(r["close_price"]),
                            "volume": float(r["volume"] or 0),
                            "bar_time": r["bar_time"],
                        }
                        for r in reversed(m1_rows)
                    ]
                    bars_5m = []
                    current_bucket = None
                    for b in bars_1m:
                        b_time = b["bar_time"]
                        if isinstance(b_time, str):
                            b_time = datetime.fromisoformat(b_time)
                        bucket_time = b_time.replace(minute=(b_time.minute // 5) * 5, second=0, microsecond=0)
                        if not current_bucket or current_bucket["bar_time"] != bucket_time:
                            # Only append previously completed bucket if fully closed
                            if current_bucket and (current_bucket["bar_time"] + timedelta(minutes=5) <= watermark):
                                bars_5m.append(current_bucket)
                            current_bucket = {
                                "open": b["open"],
                                "high": b["high"],
                                "low": b["low"],
                                "close": b["close"],
                                "volume": b["volume"],
                                "bar_time": bucket_time,
                            }
                        else:
                            current_bucket["high"] = max(current_bucket["high"], b["high"])
                            current_bucket["low"] = min(current_bucket["low"], b["low"])
                            current_bucket["close"] = b["close"]
                            current_bucket["volume"] += b["volume"]
                    # Check if final bucket is closed
                    if current_bucket and (current_bucket["bar_time"] + timedelta(minutes=5) <= watermark):
                        bars_5m.append(current_bucket)
                    return bars_5m[-limit:]
        except Exception as e:
            logger.debug(f"Failed to fetch 5m bars for instrument #{instrument_id}: {e}")
        return []

    def _get_recent_15m_bars(self, instrument_id: int, watermark: datetime, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch or derive recent CLOSED 15m bars up to watermark."""
        closed_15m_cutoff = watermark - timedelta(minutes=15)
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT open_price, high_price, low_price, close_price, volume, bar_time
                        FROM live_market_bars
                        WHERE instrument_id = :inst_id
                          AND interval IN ('15minute', '15m')
                          AND bar_time <= :wm
                        ORDER BY bar_time DESC
                        LIMIT :limit
                    """),
                    {"inst_id": instrument_id, "wm": closed_15m_cutoff, "limit": limit}
                ).mappings().all()
                if rows and len(rows) >= 3:
                    return [
                        {
                            "open": float(r["open_price"]),
                            "high": float(r["high_price"]),
                            "low": float(r["low_price"]),
                            "close": float(r["close_price"]),
                            "volume": float(r["volume"] or 0),
                            "bar_time": r["bar_time"],
                        }
                        for r in reversed(rows)
                    ]
        except Exception:
            pass

        # Derive from closed 5m bars
        bars_5m = self._get_recent_5m_bars(instrument_id, watermark, limit=limit * 3)
        if not bars_5m:
            return []
        bars_15m = []
        current_bucket = None
        for b in bars_5m:
            b_time = b.get("bar_time") or watermark
            if isinstance(b_time, str):
                b_time = datetime.fromisoformat(b_time)
            bucket_time = b_time.replace(minute=(b_time.minute // 15) * 15, second=0, microsecond=0)
            if not current_bucket or current_bucket["bar_time"] != bucket_time:
                if current_bucket and (current_bucket["bar_time"] + timedelta(minutes=15) <= watermark):
                    bars_15m.append(current_bucket)
                current_bucket = {
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b["volume"],
                    "bar_time": bucket_time,
                }
            else:
                current_bucket["high"] = max(current_bucket["high"], b["high"])
                current_bucket["low"] = min(current_bucket["low"], b["low"])
                current_bucket["close"] = b["close"]
                current_bucket["volume"] += b["volume"]
        if current_bucket and (current_bucket["bar_time"] + timedelta(minutes=15) <= watermark):
            bars_15m.append(current_bucket)
        return bars_15m[-limit:]

    def _get_session_vwap(self, instrument_id: int, watermark: datetime) -> Optional[float]:
        """Compute session VWAP from the day's 1m bars from 09:15 IST up to watermark."""
        try:
            wm_ist = watermark.astimezone(IST) if getattr(watermark, "tzinfo", None) else watermark.replace(tzinfo=timezone.utc).astimezone(IST)
            sess_start_ist = datetime.combine(wm_ist.date(), dt_time(9, 15), tzinfo=IST)
            sess_start_utc = sess_start_ist.astimezone(timezone.utc)
            with self.engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT high_price, low_price, close_price, volume
                        FROM live_market_bars
                        WHERE instrument_id = :inst_id
                          AND interval IN ('1minute', '1m')
                          AND bar_time >= :start_time AND bar_time <= :wm
                        ORDER BY bar_time ASC
                    """),
                    {"inst_id": instrument_id, "start_time": sess_start_utc, "wm": watermark}
                ).mappings().all()
                if not rows:
                    return None
                total_vp = 0.0
                total_vol = 0.0
                for r in rows:
                    v = float(r["volume"] or 0.0)
                    if v <= 0:
                        continue
                    typ_p = (float(r["high_price"]) + float(r["low_price"]) + float(r["close_price"])) / 3.0
                    total_vp += typ_p * v
                    total_vol += v
                if total_vol > 0:
                    return round(total_vp / total_vol, 4)
        except Exception as e:
            logger.debug(f"Failed to compute session VWAP for #{instrument_id}: {e}")
        return None

    def _get_index_regime_against(self, side: str, watermark: datetime) -> bool:
        """Check whether benchmark index (NIFTY 50) closed 5m candle is flipped against position."""
        try:
            with self.engine.connect() as conn:
                idx_id = conn.execute(
                    text("""
                        SELECT id FROM instrument_master 
                        WHERE symbol IN ('NIFTY', 'NIFTY 50', 'NIFTY 50 INDEX') 
                           OR underlying_symbol IN ('NIFTY', 'NIFTY 50')
                        ORDER BY id ASC LIMIT 1
                    """)
                ).scalar_one_or_none()
                if not idx_id:
                    return False
                bars_5m = self._get_recent_5m_bars(idx_id, watermark, limit=10)
                if not bars_5m or len(bars_5m) < 3:
                    return False
                closes = [b["close"] for b in bars_5m]
                alpha = 2.0 / (9.0 + 1.0)
                ema9 = closes[0]
                for c in closes[1:]:
                    ema9 = c * alpha + ema9 * (1.0 - alpha)

                side_upper = str(side).upper()
                if side_upper == "BUY" and closes[-1] < ema9:
                    return True
                elif side_upper == "SELL" and closes[-1] > ema9:
                    return True
        except Exception as e:
            logger.debug(f"Failed to check index regime: {e}")
        return False

    def _get_recent_underlying_bars(self, symbol: str, watermark: datetime, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch underlying equity bars for an option contract."""
        try:
            with self.engine.connect() as conn:
                und_sym = symbol.split()[0] if " " in symbol else symbol
                rows = conn.execute(
                    text("""
                        SELECT b.open_price, b.high_price, b.low_price, b.close_price, b.volume, b.bar_time
                        FROM live_market_bars b
                        JOIN instrument_master i ON i.id = b.instrument_id
                        WHERE (i.symbol = :sym OR i.underlying_symbol = :sym)
                          AND i.instrument_type IN ('EQ', 'INDEX')
                          AND b.interval IN ('5minute', '5m', '1minute', '1m')
                          AND b.bar_time <= :wm
                        ORDER BY b.bar_time DESC
                        LIMIT :limit
                    """),
                    {"sym": und_sym, "wm": watermark, "limit": limit}
                ).mappings().all()
                if rows:
                    return [
                        {
                            "open": float(r["open_price"]),
                            "high": float(r["high_price"]),
                            "low": float(r["low_price"]),
                            "close": float(r["close_price"]),
                            "volume": float(r["volume"] or 0),
                            "bar_time": r["bar_time"],
                        }
                        for r in reversed(rows)
                    ]
        except Exception as e:
            logger.debug(f"Failed to fetch underlying bars for {symbol}: {e}")
        return []

    def execute_exit(
        self,
        audit_id: int,
        exit_price: Decimal,
        exit_reason: str,
        exit_at: Optional[datetime] = None,
        is_synthetic: bool = False,
    ) -> bool:
        """Atomic exit writer: executes exit through the single exit authority."""
        return execute_exit(
            self.engine,
            audit_id=audit_id,
            price=exit_price,
            reason=exit_reason,
            exit_at=exit_at,
            is_synthetic=is_synthetic,
        )

    def is_force_flat_time(self, current_dt: datetime) -> bool:
        """Returns True if current time is at or past 15:15 IST (avoiding Zerodha RMS penalty)."""
        dt_ist = current_dt.astimezone(IST) if current_dt.tzinfo else current_dt.replace(tzinfo=timezone.utc).astimezone(IST)
        cutoff_hour, cutoff_min = [int(x) for x in self.force_flat_time.split(":")]
        return (dt_ist.hour > cutoff_hour) or (dt_ist.hour == cutoff_hour and dt_ist.minute >= cutoff_min)

    def get_open_positions(self) -> List[Dict]:
        """Fetch all currently open reconciled shadow paper trades."""
        with self.engine.connect() as conn:
            query = text("""
                SELECT 
                    a.id, a.instrument_id, a.side, a.signal_at, 
                    a.stop_loss_price, a.take_profit_price,
                    a.theoretical_fill_price, a.quantity, a.estimated_fees,
                    COALESCE(a.trade_mode, 'INTRADAY') AS trade_mode,
                    COALESCE(a.holding_days, 0) AS holding_days,
                    COALESCE(a.max_holding_days, 15) AS max_holding_days,
                    a.improvement_note, a.reasoning_chain,
                    i.symbol, i.exchange, i.instrument_type, i.expiry,
                    COALESCE(i.underlying_symbol, i.symbol) AS underlying_symbol
                FROM shadow_execution_audits a
                JOIN instrument_master i ON i.id = a.instrument_id
                WHERE a.audit_status = 'RECONCILED' AND a.net_pnl IS NULL
                ORDER BY a.signal_at ASC;
            """)
            rows = conn.execute(query).mappings().all()
            return [dict(r) for r in rows]

    def get_latest_price(self, instrument_id: int, watermark: datetime,
                         entry_price: Optional[Decimal] = None,
                         signal_at: Optional[datetime] = None,
                         return_source: bool = False,
                         bypass_cache: bool = False) -> Any:
        """Fetch current LTP for an instrument from sub-second or 1-minute market bars."""
        # Historical timestamps bypass live tick cache
        is_historical = False
        if watermark:
            now_utc = datetime.now(timezone.utc)
            wm_utc = watermark.astimezone(timezone.utc) if getattr(watermark, "tzinfo", None) else watermark.replace(tzinfo=timezone.utc)
            if abs((now_utc - wm_utc).total_seconds()) > 10.0:
                is_historical = True

        # Try redis tick cache first if available and not historical/bypassed
        if self.redis and not bypass_cache and not is_historical:
            try:
                cached_price = self.redis.hget("nivesh:ticks:latest", str(instrument_id))
                if cached_price:
                    val = Decimal(str(cached_price))
                    cached_ts_raw = self.redis.hget("nivesh:ticks:timestamp", str(instrument_id))
                    cached_bar_time = None
                    if cached_ts_raw:
                        if isinstance(cached_ts_raw, bytes):
                            cached_ts_raw = cached_ts_raw.decode("utf-8")
                        try:
                            cached_bar_time = datetime.fromisoformat(cached_ts_raw)
                        except Exception:
                            try:
                                cached_bar_time = datetime.strptime(cached_ts_raw.split(".")[0], "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                try:
                                    cached_bar_time = datetime.fromtimestamp(float(cached_ts_raw), tz=timezone.utc)
                                except Exception:
                                    pass
                        if cached_bar_time and cached_bar_time.tzinfo is None:
                            cached_bar_time = cached_bar_time.replace(tzinfo=timezone.utc)
                    return PriceResult(val, "market", bar_time=cached_bar_time or watermark) if return_source else val
            except Exception as e:
                logger.debug(f"Redis tick cache lookup failed for instrument {instrument_id}: {e}")

        with self.engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT close_price, bar_time FROM live_market_bars
                    WHERE instrument_id = :instrument_id
                      AND interval IN ('1second', '1minute', '5minute')
                      AND bar_time <= :watermark
                      AND source IN ('zerodha_kite', 'kite_gap_backfill', 'upstox_v3', 'upstox_rest_5m')
                    ORDER BY bar_time DESC,
                             CASE interval WHEN '1second' THEN 0 WHEN '1minute' THEN 1 ELSE 2 END
                    LIMIT 1
                """),
                {
                    "instrument_id": instrument_id,
                    "watermark": watermark,
                },
            ).mappings().one_or_none()
            if row and row["close_price"] is not None:
                val = Decimal(str(row["close_price"]))
                b_time = row.get("bar_time")
                if b_time is not None:
                    if isinstance(b_time, str):
                        try:
                            b_time = datetime.fromisoformat(b_time)
                        except Exception:
                            b_time = datetime.strptime(b_time.split(".")[0], "%Y-%m-%d %H:%M:%S")
                    if getattr(b_time, "tzinfo", None) is None:
                        b_time = b_time.replace(tzinfo=timezone.utc)
                return PriceResult(val, "market", bar_time=b_time) if return_source else val

            # Option contract mark-to-market derivation from underlying spot
            opt_meta = conn.execute(
                text("""
                    SELECT symbol, underlying_symbol, strike, expiry, instrument_type
                    FROM instrument_master WHERE id = :id
                """),
                {"id": instrument_id}
            ).mappings().one_or_none()
            if opt_meta and opt_meta.get("instrument_type") in ("CE", "PE") and opt_meta.get("strike"):
                und = opt_meta.get("underlying_symbol") or opt_meta["symbol"].split()[0]
                und_id = conn.execute(
                    text("SELECT id FROM instrument_master WHERE (symbol = :und OR underlying_symbol = :und) AND instrument_type IN ('EQ', 'INDEX') LIMIT 1"),
                    {"und": und}
                ).scalar_one_or_none()
                if und_id:
                    und_row = conn.execute(
                        text("""
                            SELECT close_price, bar_time FROM live_market_bars
                            WHERE instrument_id = :und_id
                              AND interval IN ('1second', '1minute', '5minute', 'day')
                              AND bar_time <= :watermark
                              AND bar_time >= :watermark - INTERVAL '15 minutes'
                              AND source IN ('zerodha_kite', 'kite_gap_backfill', 'upstox_v3', 'upstox_rest_5m')
                            ORDER BY bar_time DESC LIMIT 1
                        """),
                        {"und_id": und_id, "watermark": watermark}
                    ).mappings().one_or_none()
                    if und_row and und_row["close_price"]:
                        spot = float(und_row["close_price"])
                        strike = float(opt_meta["strike"])
                        wm_date = (watermark.astimezone(IST) if getattr(watermark, "tzinfo", None) else watermark).date() if hasattr(watermark, "date") else watermark
                        dte = max(0.5, float((opt_meta["expiry"] - wm_date).days)) if opt_meta.get("expiry") else 4.0
                        from backend.greeks_engine import calculate_black_scholes_greeks
                        greeks = calculate_black_scholes_greeks(spot, strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])

                        # Calibrate model mark against entry fill ratio
                        scale_ratio = 1.0
                        if entry_price and float(entry_price) > 0 and signal_at:
                            entry_spot_row = conn.execute(
                                text("""
                                    SELECT close_price FROM live_market_bars
                                    WHERE instrument_id = :und_id
                                      AND interval IN ('1second', '1minute', '5minute', 'day')
                                      AND bar_time <= :sig_at
                                      AND source IN ('zerodha_kite', 'kite_gap_backfill', 'upstox_v3', 'upstox_rest_5m')
                                    ORDER BY bar_time DESC LIMIT 1
                                """),
                                {"und_id": und_id, "sig_at": signal_at}
                            ).mappings().one_or_none()
                            if entry_spot_row and entry_spot_row["close_price"]:
                                sig_date = (signal_at.astimezone(IST) if getattr(signal_at, "tzinfo", None) else signal_at).date() if hasattr(signal_at, "date") else signal_at
                                sig_dte = max(0.5, float((opt_meta["expiry"] - sig_date).days)) if opt_meta.get("expiry") else 4.0
                                entry_bs = calculate_black_scholes_greeks(float(entry_spot_row["close_price"]), strike, sig_dte, iv=0.145, option_type=opt_meta["instrument_type"])["price"]
                                if entry_bs > 0.05:
                                    scale_ratio = max(0.2, min(5.0, float(entry_price) / entry_bs))
                        val = Decimal(str(max(0.05, round(greeks["price"] * scale_ratio, 2))))
                        und_b_time = und_row.get("bar_time")
                        if und_b_time is not None:
                            if isinstance(und_b_time, str):
                                try:
                                    und_b_time = datetime.fromisoformat(und_b_time)
                                except Exception:
                                    und_b_time = datetime.strptime(und_b_time.split(".")[0], "%Y-%m-%d %H:%M:%S")
                            if getattr(und_b_time, "tzinfo", None) is None:
                                und_b_time = und_b_time.replace(tzinfo=timezone.utc)
                        return PriceResult(val, "synthetic", bar_time=und_b_time) if return_source else val
        return PriceResult(None, None, bar_time=None) if return_source else None


    def get_exit_bars(self, instrument_id: int, signal_at: datetime, watermark: datetime,
                      entry_price: Optional[Decimal] = None, is_multi_day: bool = False,
                      exclusive_start: bool = False) -> List[Dict]:
        """Fetch historical bars since trade entry for high-precision exit checks."""
        op_start = ">" if exclusive_start else ">="
        with self.engine.connect() as conn:
            # Check real option / equity market bars first: prefer highest resolution that has complete coverage
            max_start_gap = {"1second": 5, "1minute": 120, "5minute": 600}
            t_sig = signal_at.astimezone(timezone.utc).timestamp() if getattr(signal_at, "tzinfo", None) else signal_at.replace(tzinfo=timezone.utc).timestamp()
            t_wm = watermark.astimezone(timezone.utc).timestamp() if getattr(watermark, "tzinfo", None) else watermark.replace(tzinfo=timezone.utc).timestamp()
            best_real: List[Dict] = []
            best_gap = float("inf")
            target_intervals = ("1minute", "5minute") if is_multi_day else ("1second", "1minute", "5minute")
            for interval in target_intervals:
                rows = conn.execute(
                    text(f"""
                        SELECT bar_time, open_price, high_price, low_price, close_price, volume
                        FROM live_market_bars
                        WHERE instrument_id = :instrument
                          AND interval = :interval
                          AND source IN ('zerodha_kite', 'kite_gap_backfill', 'upstox_v3', 'upstox_rest_5m')
                          AND bar_time {op_start} :signal_at AND bar_time <= :watermark
                        ORDER BY bar_time ASC
                    """),
                    {
                        "instrument": instrument_id,
                        "interval": interval,
                        "signal_at": signal_at,
                        "watermark": watermark,
                    },
                ).mappings().all()
                if rows:
                    def _ts(v):
                        if isinstance(v, str):
                            try:
                                v = datetime.fromisoformat(v)
                            except Exception:
                                v = datetime.strptime(v.split(".")[0], "%Y-%m-%d %H:%M:%S")
                        return v.astimezone(timezone.utc).timestamp() if getattr(v, "tzinfo", None) else v.replace(tzinfo=timezone.utc).timestamp()
                    start_gap = abs(_ts(rows[0]["bar_time"]) - t_sig)
                    start_ok = start_gap <= max_start_gap[interval]
                    end_ok = (t_wm - _ts(rows[-1]["bar_time"])) <= max_start_gap[interval] * 2
                    candidate = [dict(r, interval=interval) for r in rows]
                    if start_ok and end_ok:
                        return candidate
                    if not best_real or start_gap < best_gap:
                        best_real = candidate
                        best_gap = start_gap

            # Check instrument metadata before deciding fallback
            opt_meta = conn.execute(
                text("SELECT symbol, underlying_symbol, strike, expiry, instrument_type FROM instrument_master WHERE id = :id"),
                {"id": instrument_id}
            ).mappings().one_or_none()
            is_option = bool(opt_meta and opt_meta.get("instrument_type") in ("CE", "PE") and opt_meta.get("strike"))

            # Prefer real market bars (equity, futures, or options) over the synthetic model
            if best_real:
                return best_real

            # Fallback: synthesize option excursion bars from underlying stock movement
            if is_option:
                und = opt_meta.get("underlying_symbol") or opt_meta["symbol"].split()[0]
                und_id = conn.execute(
                    text("SELECT id FROM instrument_master WHERE (symbol = :und OR underlying_symbol = :und) AND instrument_type IN ('EQ', 'INDEX') LIMIT 1"),
                    {"und": und}
                ).scalar_one_or_none()
                und_bars = []
                if und_id:
                    for und_interval in ("1minute", "5minute"):
                        und_bars = conn.execute(
                            text(f"""
                                SELECT bar_time, open_price, high_price, low_price, close_price, volume
                                FROM live_market_bars
                                WHERE instrument_id = :und_id
                                  AND interval = :interval
                                  AND source IN ('zerodha_kite', 'kite_gap_backfill', 'upstox_v3', 'upstox_rest_5m')
                                  AND bar_time {op_start} :signal_at AND bar_time <= :watermark
                                ORDER BY bar_time ASC
                            """),
                            {"und_id": und_id, "interval": und_interval, "signal_at": signal_at, "watermark": watermark}
                        ).mappings().all()
                        if und_bars:
                            break
                if und_bars:
                    from backend.greeks_engine import calculate_black_scholes_greeks
                    strike = float(opt_meta["strike"])
                    exp_date = opt_meta.get("expiry")

                    # Calibrate model price with entry fill ratio so model equals entry at entry time
                    scale_ratio = 1.0
                    if entry_price and float(entry_price) > 0:
                        first_ub = und_bars[0]
                        f_date = (first_ub["bar_time"].astimezone(IST) if getattr(first_ub["bar_time"], "tzinfo", None) else first_ub["bar_time"]).date() if hasattr(first_ub["bar_time"], "date") else first_ub["bar_time"]
                        f_dte = max(0.5, float((exp_date - f_date).days)) if exp_date else 4.0
                        entry_bs = calculate_black_scholes_greeks(float(first_ub["close_price"]), strike, f_dte, iv=0.145, option_type=opt_meta["instrument_type"])["price"]
                        if entry_bs > 0.05:
                            scale_ratio = max(0.2, min(5.0, float(entry_price) / entry_bs))

                    opt_bars = []
                    for ub in und_bars:
                        b_time = ub["bar_time"]
                        b_date = (b_time.astimezone(IST) if getattr(b_time, "tzinfo", None) else b_time).date() if hasattr(b_time, "date") else b_time
                        dte = max(0.5, float((exp_date - b_date).days)) if exp_date else 4.0
                        u_open = float(ub.get("open_price") or ub["close_price"])
                        g_open = calculate_black_scholes_greeks(u_open, strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_high = calculate_black_scholes_greeks(float(ub["high_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_low = calculate_black_scholes_greeks(float(ub["low_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_close = calculate_black_scholes_greeks(float(ub["close_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        o_p = max(0.05, round(g_open["price"] * scale_ratio, 2))
                        h_p = max(0.05, round(max(g_high["price"], g_low["price"]) * scale_ratio, 2))
                        l_p = max(0.05, round(min(g_high["price"], g_low["price"]) * scale_ratio, 2))
                        c_p = max(0.05, round(g_close["price"] * scale_ratio, 2))
                        opt_bars.append({
                            "bar_time": ub["bar_time"],
                            "open_price": o_p,
                            "high_price": h_p,
                            "low_price": l_p,
                            "close_price": c_p,
                            "volume": ub.get("volume", 0),
                            "interval": "synthesized_option"
                        })
                    return opt_bars
        return best_real


    def evaluate_position(self, pos: Dict, watermark: datetime) -> Optional[Dict]:
        """Sub-second evaluation of a single open trade against institutional risk rules."""
        # Ensure position hasn't already been closed (e.g., by an atomic basket exit)
        try:
            with self.engine.connect() as chk_conn:
                is_closed = chk_conn.execute(
                    text("SELECT net_pnl FROM shadow_execution_audits WHERE id = :id"),
                    {"id": int(pos["id"])}
                ).scalar_one_or_none()
                if is_closed is not None:
                    return None
        except Exception as e:
            logger.debug(f"Closed status check failed for audit #{pos.get('id')}: {e}")

        entry = Decimal(str(pos["theoretical_fill_price"]))
        signal_at = pos["signal_at"]
        if isinstance(signal_at, str):
            try:
                signal_at = datetime.fromisoformat(signal_at)
            except Exception:
                signal_at = datetime.strptime(signal_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
        if getattr(signal_at, "tzinfo", None) is None:
            signal_at = signal_at.replace(tzinfo=timezone.utc)

        if isinstance(watermark, str):
            try:
                watermark = datetime.fromisoformat(watermark)
            except Exception:
                watermark = datetime.strptime(watermark.split(".")[0], "%Y-%m-%d %H:%M:%S")
        if getattr(watermark, "tzinfo", None) is None:
            watermark = watermark.replace(tzinfo=timezone.utc)

        side = str(pos["side"]).upper()
        trade_mode = str(pos.get("trade_mode") or "INTRADAY").upper()
        quantity = Decimal(str(int(pos.get("quantity") or 1)))
        fees = Decimal(str(pos.get("estimated_fees") or 0))
        instrument_type = str(pos.get("instrument_type") or "").upper()
        is_multi_day = trade_mode in ("SWING", "POSITIONAL")

        price_res = self.get_latest_price(pos["instrument_id"], watermark, entry_price=entry, signal_at=signal_at, return_source=True)
        if isinstance(price_res, tuple):
            latest_price, price_source = price_res
        else:
            latest_price, price_source = price_res, "market"
        if latest_price is None or latest_price <= 0:
            return None

        # -------------------------------------------------------------
        # Price Freshness Guard: detect stale price during market hours
        # -------------------------------------------------------------
        exit_price: Optional[Decimal] = None
        exit_reason: Optional[str] = None
        exit_bar_time: Optional[datetime] = None

        price_bar_time = getattr(price_res, "bar_time", None)
        max_age = self.price_max_age_swing_seconds if is_multi_day else self.price_max_age_seconds
        is_stale_price = False

        market_open_now = is_market_hours(watermark, engine=self.engine)
        if market_open_now and price_bar_time is not None:
            wm_ist = watermark.astimezone(IST) if watermark.tzinfo else watermark.replace(tzinfo=timezone.utc).astimezone(IST)
            open_time = get_market_open_time(watermark, engine=self.engine)
            session_open_dt = datetime.combine(wm_ist.date(), open_time, tzinfo=IST)
            seconds_since_open = (wm_ist - session_open_dt).total_seconds()

            if seconds_since_open >= self.stale_open_grace_seconds:
                price_age = max(0.0, (watermark - price_bar_time).total_seconds())
                if price_age > max_age:
                    is_stale_price = True
                    logger.critical(
                        f"CRITICAL: STALE_PRICE detected for audit #{pos['id']} ({pos.get('symbol')}): "
                        f"price age {price_age:.1f}s exceeds max {max_age}s"
                    )
                    if pos["id"] not in self.stale_positions_tracker:
                        self.stale_positions_tracker[pos["id"]] = watermark
                    stale_duration = (watermark - self.stale_positions_tracker[pos["id"]]).total_seconds()
                    if stale_duration >= self.stale_data_exit_minutes * 60.0:
                        session_row = get_calendar_session(wm_ist.date(), self.engine)
                        if session_row is None:
                            logger.critical(
                                f"CRITICAL: Stale data duration ({stale_duration:.1f}s) exceeded exit threshold for audit #{pos['id']} ({pos.get('symbol')}), "
                                f"but no exchange_trading_calendar row exists for {wm_ist.date()}. "
                                f"Suppressing STALE_DATA_EXIT to prevent false liquidation on unrecorded holiday/session."
                            )
                        else:
                            exit_price = latest_price
                            exit_reason = "STALE_DATA_EXIT"
                            exit_bar_time = watermark
                else:
                    if pos["id"] in self.stale_positions_tracker:
                        del self.stale_positions_tracker[pos["id"]]
            else:
                # Within open grace period: suppress staleness check
                if pos["id"] in self.stale_positions_tracker:
                    del self.stale_positions_tracker[pos["id"]]
        else:
            # Outside market hours or exchange holiday: suppress staleness check and clear tracker
            if pos["id"] in self.stale_positions_tracker:
                del self.stale_positions_tracker[pos["id"]]

        elapsed_seconds = max(0.0, (watermark - signal_at).total_seconds())

        stop_loss = Decimal(str(pos["stop_loss_price"])) if pos.get("stop_loss_price") else None
        take_profit = Decimal(str(pos["take_profit_price"])) if pos.get("take_profit_price") else None

        # Institutional Short Options Discipline (CBOE & tastytrade benchmark):
        # 1. 50% Max Profit Target: Holding short options past 50% decay yields severe negative gamma risk.
        #    Take profit when premium reaches 50% of credit received (LTP <= entry * 0.50).
        # 2. 1.5x Hard Loss Ceiling: Cap short option loss at 1.5x entry price to prevent naked tail risk.
        if side == "SELL" and instrument_type in ("CE", "PE"):
            opt_tp_50 = entry * Decimal("0.50")
            take_profit = max(take_profit, opt_tp_50) if take_profit else opt_tp_50
            opt_sl_150 = entry * Decimal("1.50")
            stop_loss = min(stop_loss, opt_sl_150) if stop_loss else opt_sl_150

        # 1. Calculate baseline risk (R-points) based on INITIAL stop loss
        raw_note = pos.get("improvement_note")
        note_dict = {}
        raw_note_parse_failed = False
        if raw_note:
            if isinstance(raw_note, dict):
                note_dict = dict(raw_note)
            elif isinstance(raw_note, str):
                try:
                    note_dict = json.loads(raw_note)
                except Exception as e:
                    logger.debug(f"Failed to parse improvement_note JSON for audit #{pos.get('id')}: {e}")
                    raw_note_parse_failed = True
                    note_dict = {}
                    # Attempt regex recovery for key metadata if JSON was truncated/malformed
                    m_syn = re.search(r'"synthetic_entry":\s*(true|false)', raw_note, re.IGNORECASE)
                    if m_syn:
                        note_dict["synthetic_entry"] = (m_syn.group(1).lower() == "true")
                    m_basket = re.search(r'"spread_basket_id":\s*"([^"]+)"', raw_note)
                    if m_basket:
                        note_dict["spread_basket_id"] = m_basket.group(1)
                    m_primary = re.search(r'"paired_primary_audit_id":\s*(\d+)', raw_note)
                    if m_primary:
                        note_dict["paired_primary_audit_id"] = int(m_primary.group(1))
                    m_init_sl = re.search(r'"initial_stop_loss_price":\s*([0-9.]+)', raw_note)
                    if m_init_sl:
                        note_dict["initial_stop_loss_price"] = float(m_init_sl.group(1))
                    m_trail = re.search(r'"trailed_stop_price":\s*([0-9.]+)', raw_note)
                    if m_trail:
                        note_dict["trailed_stop_price"] = float(m_trail.group(1))

        # Check dedicated columns and fields if metadata was unparsed or omitted
        if not note_dict.get("spread_basket_id") or not note_dict.get("paired_primary_audit_id"):
            raw_rc = pos.get("reasoning_chain")
            if raw_rc:
                try:
                    rc_data = json.loads(raw_rc) if isinstance(raw_rc, str) and raw_rc.startswith("{") else (dict(raw_rc) if isinstance(raw_rc, dict) else {})
                    if not note_dict.get("spread_basket_id"):
                        note_dict["spread_basket_id"] = rc_data.get("spread_basket_id") or rc_data.get("strategy_group")
                    if not note_dict.get("paired_primary_audit_id"):
                        note_dict["paired_primary_audit_id"] = rc_data.get("paired_primary_audit_id")
                except Exception as e:
                    logger.debug(f"Failed to parse reasoning_chain for audit #{pos.get('id')}: {e}")

        # Recover trailed stop from separate storage if unparsed
        if self.redis and not note_dict.get("trailed_stop_price"):
            try:
                sep_val = self.redis.get(f"nivesh:trailed_stop:{pos['id']}")
                if sep_val:
                    note_dict["trailed_stop_price"] = float(sep_val)
            except Exception as e:
                logger.debug(f"Failed to get trailed stop from Redis for audit #{pos.get('id')}: {e}")

        spread_basket_id = note_dict.get("spread_basket_id")
        paired_primary_audit_id = note_dict.get("paired_primary_audit_id")
        is_hedge_leg = bool(paired_primary_audit_id or note_dict.get("engine") == "multi_leg_hedge")

        initial_stop_raw = note_dict.get("initial_stop_loss_price")
        if initial_stop_raw is not None:
            initial_sl = Decimal(str(initial_stop_raw))
        elif stop_loss is not None:
            initial_sl = stop_loss
            note_dict["initial_stop_loss_price"] = float(initial_sl)
        else:
            initial_sl = None

        fallback_risk = max(Decimal("0.50"), entry * Decimal("0.006"))
        if side == "BUY":
            r_points = (entry - initial_sl) if (initial_sl and entry > initial_sl) else fallback_risk
        else:
            r_points = (initial_sl - entry) if (initial_sl and initial_sl > entry) else fallback_risk
        r_points = max(Decimal("0.05"), r_points)

        # 2. Fee-padded breakeven buffer (covers 2.5x roundtrip fees + 2 ticks spread slippage)
        tick_size = Decimal("0.05")
        fee_buffer_per_share = ((fees * Decimal("2.5")) + (Decimal("2") * tick_size * quantity)) / max(Decimal("1"), quantity)

        # If this is a paired hedge leg, verify whether primary leg has already closed
        if not exit_reason and is_hedge_leg and paired_primary_audit_id:
            try:
                with self.engine.connect() as prim_conn:
                    p_res = prim_conn.execute(
                        text("SELECT net_pnl, exit_reason, exit_at FROM shadow_execution_audits WHERE id = :pid"),
                        {"pid": int(paired_primary_audit_id)}
                    ).mappings().one_or_none()
                    if p_res and p_res["net_pnl"] is not None:
                        p_exit_at = p_res.get("exit_at")
                        if isinstance(p_exit_at, str):
                            try:
                                p_exit_at = datetime.fromisoformat(p_exit_at)
                            except Exception as e:
                                logger.debug(f"Failed to parse p_exit_at ISO datetime: {e}")
                                p_exit_at = None
                        if isinstance(p_exit_at, datetime) and p_exit_at.tzinfo is None:
                            p_exit_at = p_exit_at.replace(tzinfo=timezone.utc)
                        exit_bar_time = p_exit_at or watermark
                        exit_reason = f"BASKET_EXIT:{p_res.get('exit_reason') or 'PRIMARY_CLOSED'}"

                        hist_price_res = self.get_latest_price(
                            pos["instrument_id"],
                            exit_bar_time,
                            entry_price=entry,
                            signal_at=signal_at,
                            return_source=True,
                            bypass_cache=True,
                        )
                        if isinstance(hist_price_res, tuple):
                            p_hist_price, p_hist_src = hist_price_res
                        else:
                            p_hist_price, p_hist_src = hist_price_res, "market"

                        if p_hist_price and p_hist_price > 0:
                            exit_price = p_hist_price
                            price_source = p_hist_src
                        else:
                            exit_price = latest_price
            except Exception as prim_err:
                logger.debug(f"Could not check primary leg #{paired_primary_audit_id}: {prim_err}")

        # -------------------------------------------------------------
        # Incremental Bar Evaluation: O(new bars) Replay
        # -------------------------------------------------------------
        last_proc_raw = note_dict.get("last_processed_bar_time")
        last_processed_dt = None
        if last_proc_raw:
            try:
                last_processed_dt = datetime.fromisoformat(last_proc_raw)
                if last_processed_dt.tzinfo is None:
                    last_processed_dt = last_processed_dt.replace(tzinfo=timezone.utc)
            except Exception as e:
                logger.debug(f"Failed to parse last_processed_bar_time: {e}")
                last_processed_dt = None

        if last_processed_dt and last_processed_dt >= signal_at:
            best_favourable = Decimal(str(note_dict["best_favourable"])) if note_dict.get("best_favourable") is not None else entry
            worst_adverse = Decimal(str(note_dict["worst_adverse"])) if note_dict.get("worst_adverse") is not None else entry
            running_sl = Decimal(str(note_dict["running_sl"])) if note_dict.get("running_sl") is not None else (initial_sl or stop_loss)
            bars = self.get_exit_bars(pos["instrument_id"], last_processed_dt, watermark, entry_price=entry, is_multi_day=is_multi_day, exclusive_start=True)
        else:
            best_favourable = entry
            worst_adverse = entry
            running_sl = initial_sl or stop_loss
            bars = self.get_exit_bars(pos["instrument_id"], signal_at, watermark, entry_price=entry, is_multi_day=is_multi_day, exclusive_start=False)

        # Ensure excursion R metrics are always initialized
        favorable_r = Decimal("0.0")
        current_r = Decimal("0.0")

        if not exit_reason:
            for bar in bars:
                o = Decimal(str(bar.get("open_price") if bar.get("open_price") is not None else (bar.get("close_price") or entry)))
                h = Decimal(str(bar.get("high_price") or entry))
                l = Decimal(str(bar.get("low_price") or entry))
                c = Decimal(str(bar.get("close_price") or bar.get("high_price") or entry))
                b_time = bar.get("bar_time")

                if side == "BUY":
                    best_favourable = max(best_favourable, h)
                    worst_adverse = min(worst_adverse, l)
                    fav_r = (best_favourable - entry) / r_points
                    favorable_r = max(favorable_r, fav_r)

                    if not is_hedge_leg:
                        # Chronological intra-replay trailing stop progression
                        if fav_r >= self.breakeven_trigger_r:
                            be_p = entry + fee_buffer_per_share
                            running_sl = max(running_sl, be_p) if running_sl else be_p
                        if fav_r >= self.profit_lock_trigger_r:
                            pl_p = entry + self.profit_lock_guaranteed_r * r_points
                            running_sl = max(running_sl, pl_p) if running_sl else pl_p
                        if fav_r >= self.trailing_trigger_r:
                            ts_p = best_favourable - self.trailing_giveback_r * r_points
                            running_sl = max(running_sl, ts_p) if running_sl else ts_p

                        # 1. Take Profit hit on this bar (gap-aware open check)
                        if take_profit and o >= take_profit:
                            exit_price = o
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_time = b_time
                            break
                        elif take_profit and h >= take_profit:
                            exit_price = take_profit
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_time = b_time
                            break

                        # 2. +2.5R Spike Profit Capture hit on this bar
                        if fav_r >= Decimal("2.50"):
                            exit_price = entry + Decimal("2.5") * r_points
                            exit_reason = "PROFIT_CAPTURE"
                            exit_bar_time = b_time
                            break

                        # 3. Stop Loss or Trailed Stop hit on this bar (Gap-Aware Fills)
                        if running_sl:
                            if o <= running_sl:
                                exit_price = o
                                exit_reason = "GAP_DOWN_STOP"
                                exit_bar_time = b_time
                                break
                            elif l <= running_sl:
                                exit_price = running_sl
                                if running_sl >= entry + self.profit_lock_guaranteed_r * r_points:
                                    exit_reason = "TRAILING_STOP"
                                elif running_sl >= entry:
                                    exit_reason = "BREAKEVEN_STOP"
                                else:
                                    exit_reason = "STOP_LOSS"
                                exit_bar_time = b_time
                                break
                else:  # SELL
                    best_favourable = min(best_favourable, l)
                    worst_adverse = max(worst_adverse, h)
                    fav_r = (entry - best_favourable) / r_points
                    favorable_r = max(favorable_r, fav_r)

                    if not is_hedge_leg:
                        # Chronological intra-replay trailing stop progression
                        if fav_r >= self.breakeven_trigger_r:
                            be_p = entry - fee_buffer_per_share
                            running_sl = min(running_sl, be_p) if running_sl else be_p
                        if fav_r >= self.profit_lock_trigger_r:
                            pl_p = entry - self.profit_lock_guaranteed_r * r_points
                            running_sl = min(running_sl, pl_p) if running_sl else pl_p
                        if fav_r >= self.trailing_trigger_r:
                            ts_p = best_favourable + self.trailing_giveback_r * r_points
                            running_sl = min(running_sl, ts_p) if running_sl else ts_p

                        # 1. Take Profit hit on this bar (gap-aware open check)
                        if take_profit and o <= take_profit:
                            exit_price = o
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_time = b_time
                            break
                        elif take_profit and l <= take_profit:
                            exit_price = take_profit
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_time = b_time
                            break

                        # 2. +2.5R Spike Profit Capture hit on this bar
                        if fav_r >= Decimal("2.50"):
                            exit_price = max(Decimal("0.05"), entry - Decimal("2.5") * r_points)
                            exit_reason = "PROFIT_CAPTURE"
                            exit_bar_time = b_time
                            break

                        # 3. Stop Loss or Trailed Stop hit on this bar (Gap-Aware Fills)
                        if running_sl:
                            if o >= running_sl:
                                exit_price = o
                                exit_reason = "GAP_UP_STOP"
                                exit_bar_time = b_time
                                break
                            elif h >= running_sl:
                                exit_price = running_sl
                                if running_sl <= entry - self.profit_lock_guaranteed_r * r_points:
                                    exit_reason = "TRAILING_STOP"
                                elif running_sl <= entry:
                                    exit_reason = "BREAKEVEN_STOP"
                                else:
                                    exit_reason = "STOP_LOSS"
                                exit_bar_time = b_time
                                break

        persisted_trail_raw = note_dict.get("trailed_stop_price")
        if not exit_reason and not is_hedge_leg:
            if persisted_trail_raw is not None:
                persisted_trail = Decimal(str(persisted_trail_raw))
                if side == "BUY":
                    running_sl = max(running_sl, persisted_trail) if running_sl else persisted_trail
                else:
                    running_sl = min(running_sl, persisted_trail) if running_sl else persisted_trail
            if stop_loss is not None:
                if side == "BUY":
                    running_sl = max(running_sl, stop_loss) if running_sl else stop_loss
                else:
                    running_sl = min(running_sl, stop_loss) if running_sl else stop_loss

        # Also incorporate latest live price into excursion if no historical exit triggered
        if not exit_reason:
            if side == "BUY":
                best_favourable = max(best_favourable, latest_price)
                worst_adverse = min(worst_adverse, latest_price)
                favorable_r = max(favorable_r, (best_favourable - entry) / r_points)
                current_r = (latest_price - entry) / r_points
            else:
                best_favourable = min(best_favourable, latest_price)
                worst_adverse = max(worst_adverse, latest_price)
                favorable_r = max(favorable_r, (entry - best_favourable) / r_points)
                current_r = (entry - latest_price) / r_points

            # -------------------------------------------------------------
            # Intraday-Only Protections (Bypassed For Multi-Day / Swing Setups & Hedge Legs)
            # -------------------------------------------------------------
            if not is_multi_day and not is_hedge_leg:
                # RULE 1: Immediate Adverse Cut (0 to 90 seconds)
                if elapsed_seconds <= self.adverse_cut_window_seconds:
                    if current_r <= -self.adverse_cut_threshold_r and favorable_r <= Decimal("0.10"):
                        exit_price = latest_price
                        exit_reason = "EARLY_ADVERSE_CUT"

                # RULE 2: Dynamic Stop Tightening at 5m
                if not exit_reason and elapsed_seconds >= self.stagnation_tighten_seconds and favorable_r < Decimal("0.20"):
                    if side == "BUY":
                        tightened_sl = entry - self.stagnation_tighten_r * r_points
                        running_sl = max(running_sl, tightened_sl) if running_sl else tightened_sl
                    else:
                        tightened_sl = entry + self.stagnation_tighten_r * r_points
                        running_sl = min(running_sl, tightened_sl) if running_sl else tightened_sl

                # RULE 3: 15-Minute STAGNATION_GUARD
                if not exit_reason and elapsed_seconds >= self.stagnation_scratch_seconds:
                    if favorable_r < self.stagnation_min_expansion_r:
                        exit_price = latest_price
                        exit_reason = "STAGNATION_GUARD"

            # -------------------------------------------------------------
            # Trailing Stops, Profit Locks & Breakeven Stops (Bypassed For Hedge Legs)
            # -------------------------------------------------------------
            if not is_hedge_leg:
                profit_lock_price = None
                if favorable_r >= self.profit_lock_trigger_r:
                    profit_lock_price = (entry + self.profit_lock_guaranteed_r * r_points) if side == "BUY" else (entry - self.profit_lock_guaranteed_r * r_points)

                breakeven_price = None
                if favorable_r >= self.breakeven_trigger_r:
                    breakeven_price = (entry + fee_buffer_per_share) if side == "BUY" else (entry - fee_buffer_per_share)

                # Profit-Harvest Layer (Phase 2): Target 1.5R, Progress >= 0.83, Reversal Analysis
                harvest_stop_price = None
                harvest_exit_requested = False
                if self.profit_harvest_enabled and take_profit is not None:
                    target_progress = calculate_progress(side, entry, latest_price, take_profit)

                    # 50% target progress breakeven stop ONLY when profit_harvest_enabled is True!
                    if target_progress >= 0.50 and breakeven_price is None:
                        breakeven_price = (entry + fee_buffer_per_share) if side == "BUY" else (entry - fee_buffer_per_share)

                    if target_progress >= self.harvest_trigger:
                        # Cache key per closed 5m candle
                        wm_utc = watermark.astimezone(timezone.utc) if getattr(watermark, "tzinfo", None) else watermark.replace(tzinfo=timezone.utc)
                        closed_5m_t = (wm_utc - timedelta(minutes=5)).replace(second=0, microsecond=0)
                        closed_5m_t = closed_5m_t.replace(minute=(closed_5m_t.minute // 5) * 5)
                        cache_key = (int(pos["instrument_id"]), closed_5m_t)

                        if not hasattr(self, "_harvest_cache") or self._harvest_cache is None:
                            self._harvest_cache = {}
                        harvest_res = self._harvest_cache.get(cache_key)
                        if harvest_res is None:
                            bars_5m = self._get_recent_5m_bars(pos["instrument_id"], watermark, limit=20)
                            bars_15m = self._get_recent_15m_bars(pos["instrument_id"], watermark, limit=10)
                            sess_vwap = self._get_session_vwap(pos["instrument_id"], watermark)
                            is_option = str(pos.get("option_type") or "").upper() in ("CE", "PE", "CALL", "PUT") or (" " in str(pos.get("symbol", "")))
                            und_bars = self._get_recent_underlying_bars(pos["symbol"], watermark, limit=20) if is_option else None
                            idx_regime = self._get_index_regime_against(side, watermark)

                            harvest_res = compute_reversal_score(
                                bars_5m=bars_5m,
                                side=side,
                                bars_15m=bars_15m,
                                vwap=sess_vwap or (float(pos.get("vwap") or 0.0) if pos.get("vwap") else None),
                                is_option=is_option,
                                underlying_bars=und_bars,
                                index_regime_against=idx_regime,
                                exit_threshold=self.harvest_exit_threshold,
                                tighten_threshold=self.harvest_tighten_threshold,
                            )
                            if len(self._harvest_cache) > 200:
                                self._harvest_cache.clear()
                            self._harvest_cache[cache_key] = harvest_res

                        note_dict["profit_harvest"] = {
                            "progress": round(target_progress, 4),
                            "reversal_score": harvest_res["reversal_score"],
                            "action": harvest_res["action"],
                            "active_count": harvest_res.get("active_count", 0),
                            "reasons": harvest_res["reasons"],
                            "enabled": True,
                            "evaluated_at": watermark.isoformat(),
                        }

                        if harvest_res["action"] == "EXIT":
                            harvest_exit_requested = True
                        elif harvest_res["action"] == "TIGHTEN":
                            harvest_stop_price = calculate_harvest_stop(
                                side=side,
                                entry_price=entry,
                                latest_price=latest_price,
                                current_sl=running_sl,
                                lock_fraction=self.harvest_lock_fraction,
                            )

                trailing_stop_price = None
                if favorable_r >= self.trailing_trigger_r:
                    if side == "BUY":
                        trailing_stop_price = best_favourable - self.trailing_giveback_r * r_points
                    else:
                        trailing_stop_price = best_favourable + self.trailing_giveback_r * r_points

                # Trailing Stop & Profit Lock Level Update & Database Persistence
                trailed_sl = running_sl
                if side == "BUY":
                    candidates = [p for p in (running_sl, profit_lock_price, breakeven_price, trailing_stop_price, harvest_stop_price) if p is not None]
                    if candidates:
                        trailed_sl = max(candidates)
                else:
                    candidates = [p for p in (running_sl, profit_lock_price, breakeven_price, trailing_stop_price, harvest_stop_price) if p is not None]
                    if candidates:
                        trailed_sl = min(candidates)

                # Persist trailed stop loss to improvement_note without overwriting initial stop_loss_price
                orig_trailed = Decimal(str(note_dict.get("trailed_stop_price"))) if note_dict.get("trailed_stop_price") else initial_sl
                improved = False
                if trailed_sl is not None:
                    if orig_trailed is None:
                        improved = True
                    elif side == "BUY" and trailed_sl > orig_trailed:
                        improved = True
                    elif side == "SELL" and trailed_sl < orig_trailed:
                        improved = True

                if improved:
                    running_sl = trailed_sl
                    sl_flt = round(float(trailed_sl), 4)
                    old_sl_flt = round(float(orig_trailed), 4) if orig_trailed else (round(float(initial_sl), 4) if initial_sl else sl_flt)
                    note_dict["trailed_stop_price"] = sl_flt
                    note_dict["initial_stop_loss_price"] = round(float(initial_sl), 4) if initial_sl else None
                    if harvest_stop_price is not None and trailed_sl == harvest_stop_price:
                        reason_tag = "PROFIT_HARVEST_STOP"
                    elif (trailed_sl > entry if side == "BUY" else trailed_sl < entry):
                        reason_tag = "PROFIT_TRAILING_LOCK"
                    else:
                        reason_tag = "TRAILING_STOP_ACTIVE"

                    note_dict["risk_manager"] = {
                        "old_sl": old_sl_flt,
                        "new_sl": sl_flt,
                        "reasons": [reason_tag],
                        "latest": round(float(latest_price), 2),
                    }
                    if self.redis:
                        try:
                            self.redis.set(f"nivesh:trailed_stop:{pos['id']}", str(sl_flt), ex=86400)
                        except Exception as e:
                            logger.debug(f"Failed to set trailed stop in Redis for audit #{pos['id']}: {e}")
                    try:
                        with self.engine.begin() as conn:
                            if raw_note_parse_failed:
                                compact_note = {
                                    "original_raw_note": raw_note,
                                    "trailed_stop_price": sl_flt,
                                    "initial_stop_loss_price": round(float(initial_sl), 4) if initial_sl else None,
                                    "synthetic_entry": note_dict.get("synthetic_entry"),
                                    "spread_basket_id": spread_basket_id,
                                    "paired_primary_audit_id": paired_primary_audit_id,
                                    "risk_manager": note_dict["risk_manager"],
                                    "profit_harvest": note_dict.get("profit_harvest"),
                                }
                                conn.execute(
                                    text("UPDATE shadow_execution_audits SET stop_loss_price = :sl, improvement_note = :note WHERE id = :id AND net_pnl IS NULL"),
                                    {"sl": sl_flt, "note": json.dumps(compact_note, default=str), "id": int(pos["id"])}
                                )
                            else:
                                conn.execute(
                                    text("UPDATE shadow_execution_audits SET stop_loss_price = :sl, improvement_note = :note WHERE id = :id AND net_pnl IS NULL"),
                                    {"sl": sl_flt, "note": json.dumps(note_dict, default=str), "id": int(pos["id"])}
                                )
                        logger.info(f"PositionManager Trailed SL for #{pos['id']} {pos['symbol']}: {orig_trailed} -> {sl_flt}")
                    except Exception as upd_err:
                        logger.warning(f"Could not persist trailed stop for #{pos['id']}: {upd_err}")

                # Evaluate Stops & Targets Against Current Live Price
                if not exit_reason:
                    if harvest_exit_requested:
                        exit_price = latest_price
                        exit_reason = "PROFIT_HARVEST_REVERSAL_EXIT"
                    elif side == "BUY":
                        if harvest_stop_price and latest_price <= harvest_stop_price:
                            exit_price = min(harvest_stop_price, latest_price)
                            exit_reason = "PROFIT_HARVEST_STOP"
                        elif trailing_stop_price and latest_price <= trailing_stop_price:
                            stop_level = max(trailing_stop_price, profit_lock_price or breakeven_price or trailing_stop_price)
                            exit_price = min(stop_level, latest_price)
                            exit_reason = "TRAILING_STOP"
                        elif profit_lock_price and latest_price <= profit_lock_price:
                            exit_price = min(profit_lock_price, latest_price)
                            exit_reason = "PROFIT_LOCK_STOP"
                        elif breakeven_price and latest_price <= breakeven_price:
                            exit_price = min(breakeven_price, latest_price)
                            exit_reason = "BREAKEVEN_STOP"
                        elif running_sl and latest_price <= running_sl:
                            exit_price = min(running_sl, latest_price)
                            if running_sl >= entry + self.profit_lock_guaranteed_r * r_points:
                                exit_reason = "TRAILING_STOP"
                            elif running_sl >= entry:
                                exit_reason = "BREAKEVEN_STOP"
                            else:
                                exit_reason = "STOP_LOSS"
                        elif take_profit and latest_price >= take_profit:
                            exit_price = take_profit
                            exit_reason = "TAKE_PROFIT"
                    else:
                        if harvest_stop_price and latest_price >= harvest_stop_price:
                            exit_price = max(harvest_stop_price, latest_price)
                            exit_reason = "PROFIT_HARVEST_STOP"
                        elif trailing_stop_price and latest_price >= trailing_stop_price:
                            stop_level = min(trailing_stop_price, profit_lock_price or breakeven_price or trailing_stop_price)
                            exit_price = max(stop_level, latest_price)
                            exit_reason = "TRAILING_STOP"
                        elif profit_lock_price and latest_price >= profit_lock_price:
                            exit_price = max(profit_lock_price, latest_price)
                            exit_reason = "PROFIT_LOCK_STOP"
                        elif breakeven_price and latest_price >= breakeven_price:
                            exit_price = max(breakeven_price, latest_price)
                            exit_reason = "BREAKEVEN_STOP"
                        elif running_sl and latest_price >= running_sl:
                            exit_price = max(running_sl, latest_price)
                            if running_sl <= entry - self.profit_lock_guaranteed_r * r_points:
                                exit_reason = "TRAILING_STOP"
                            elif running_sl <= entry:
                                exit_reason = "BREAKEVEN_STOP"
                            else:
                                exit_reason = "STOP_LOSS"
                        elif take_profit and latest_price <= take_profit:
                            exit_price = take_profit
                            exit_reason = "TAKE_PROFIT"
        else:
            # If historical exit triggered, calculate current_r and favorable_r accurately
            if side == "BUY":
                favorable_r = max(favorable_r, (best_favourable - entry) / r_points)
                current_r = ((exit_price or latest_price) - entry) / r_points
            else:
                favorable_r = max(favorable_r, (entry - best_favourable) / r_points)
                current_r = (entry - (exit_price or latest_price)) / r_points

        # -------------------------------------------------------------
        # Mode-Specific Exits: Swing Multi-Day vs Intraday 15:15 RMS Cutoff
        # -------------------------------------------------------------
        if not exit_reason:
            # Check Option Underlying Invalidation Stop if specified
            und_inval = note_dict.get("underlying_invalidation_level")
            if und_inval is not None and instrument_type in ("CE", "PE"):
                try:
                    und_inval_dec = Decimal(str(und_inval))
                    und_sym = pos.get("underlying_symbol") or (pos.get("symbol") or "").split()[0]
                    with self.engine.connect() as u_conn:
                        u_row = u_conn.execute(
                            text("""
                                SELECT close_price FROM live_market_bars b
                                JOIN instrument_master i ON i.id = b.instrument_id
                                WHERE (i.symbol = :sym OR i.underlying_symbol = :sym)
                                  AND i.instrument_type IN ('EQ', 'INDEX')
                                  AND b.interval IN ('1second', '1minute', '5minute')
                                  AND b.bar_time <= :wm
                                  AND b.source IN ('zerodha_kite', 'kite_gap_backfill', 'upstox_v3', 'upstox_rest_5m')
                                ORDER BY b.bar_time DESC LIMIT 1
                            """),
                            {"sym": und_sym, "wm": watermark}
                        ).mappings().one_or_none()
                        if u_row and u_row["close_price"]:
                            u_spot = Decimal(str(u_row["close_price"]))
                            if (instrument_type == "CE" and u_spot <= und_inval_dec) or (instrument_type == "PE" and u_spot >= und_inval_dec):
                                exit_price = latest_price
                                exit_reason = "UNDERLYING_STOP"
                                exit_bar_time = watermark
                except Exception as und_err:
                    logger.debug(f"Underlying invalidation check failed for #{pos.get('id')}: {und_err}")

            if not exit_reason and is_multi_day:
                signal_date = signal_at.astimezone(IST).date()
                current_date = watermark.astimezone(IST).date()
                expiry_val = pos.get("expiry")
                expiry_date = None
                if expiry_val:
                    if isinstance(expiry_val, datetime):
                        expiry_date = expiry_val.astimezone(IST).date() if expiry_val.tzinfo else expiry_val.date()
                    elif isinstance(expiry_val, date):
                        expiry_date = expiry_val
                    elif isinstance(expiry_val, str):
                        try:
                            expiry_date = date.fromisoformat(expiry_val.split("T")[0])
                        except Exception as e:
                            logger.debug(f"Failed to parse expiry date: {e}")
                    elif hasattr(expiry_val, "date"):
                        expiry_date = expiry_val.date()
                if instrument_type in {"CE", "PE", "FUT"} and expiry_date:
                    if current_date > expiry_date or (current_date == expiry_date and self.is_force_flat_time(watermark)):
                        exit_price = latest_price
                        exit_reason = "EXPIRY_FORCE_FLAT"

                if not exit_reason:
                    days_held = (current_date - signal_date).days
                    if days_held >= int(pos.get("max_holding_days") or 15):
                        exit_price = latest_price
                        exit_reason = "SWING_MAX_DAYS_EXPIRED"
            elif not exit_reason:
                if self.is_force_flat_time(watermark):
                    exit_price = latest_price
                    exit_reason = "SESSION_FORCE_FLAT"

        # Persist incremental evaluation state when position remains open
        if not exit_reason:
            note_dict["running_sl"] = float(running_sl) if running_sl is not None else None
            note_dict["best_favourable"] = float(best_favourable)
            note_dict["worst_adverse"] = float(worst_adverse)
            if bars:
                last_b_time = bars[-1].get("bar_time")
                note_dict["last_processed_bar_time"] = last_b_time.isoformat() if hasattr(last_b_time, "isoformat") else str(last_b_time)
            try:
                with self.engine.begin() as upd_conn:
                    upd_conn.execute(
                        text("""
                            UPDATE shadow_execution_audits
                            SET improvement_note = :note,
                                stop_loss_price = :sl
                            WHERE id = :id AND net_pnl IS NULL
                        """),
                        {
                            "id": int(pos["id"]),
                            "note": json.dumps(note_dict, default=str),
                            "sl": float(running_sl) if running_sl is not None else None,
                        }
                    )
            except Exception as e:
                logger.debug(f"Incremental evaluation update failed for audit #{pos.get('id')}: {e}")

        # Position Telemetry Object
        unrealized_pnl = ((latest_price - entry) * quantity - fees) if side == "BUY" else ((entry - latest_price) * quantity - fees)
        telemetry = {
            "id": pos["id"],
            "symbol": pos["symbol"],
            "side": side,
            "trade_mode": trade_mode,
            "entry_price": float(entry),
            "latest_price": float(latest_price),
            "stop_loss_price": float(running_sl) if running_sl is not None else (float(initial_sl) if initial_sl else None),
            "take_profit_price": float(take_profit) if take_profit else None,
            "holding_days": max(0, (watermark.astimezone(IST).date() - signal_at.astimezone(IST).date()).days) if is_multi_day else 0,
            "unrealized_pnl": round(float(unrealized_pnl), 2),
            "current_r": round(float(current_r), 2),
            "favorable_r": round(float(favorable_r), 2),
            "elapsed_seconds": int(elapsed_seconds),
            "status": "CLOSED" if exit_reason else "OPEN",
            "exit_reason": exit_reason,
            "exit_price": float(exit_price) if exit_price else None,
            "stale_price": is_stale_price,
            "profit_harvest": note_dict.get("profit_harvest"),
            "updated_at": watermark.isoformat(),
        }

        # If exit condition triggered, record shadow exit immediately with non-blocking call
        if exit_reason and exit_price:
            has_synth_bars = any(b.get("interval") == "synthesized_option" for b in bars) if bars else False
            is_synthetic_exit = bool(price_source == "synthetic" or has_synth_bars or note_dict.get("synthetic_entry"))
            logger.info(f"PositionManager Triggered Exit: #{pos['id']} {pos['symbol']} via {exit_reason} at {exit_price} (synthetic={is_synthetic_exit})")
            record_shadow_exit(self.engine, int(pos["id"]), exit_price, exit_reason, exit_at=exit_bar_time or watermark, is_synthetic=is_synthetic_exit)
            telemetry["closed"] = True
            telemetry["is_synthetic"] = is_synthetic_exit

            # Atomic Multi-Leg Spread Basket Exit: Close all open sibling legs sharing the basket
            sibling_closed_ids = []
            if spread_basket_id or paired_primary_audit_id:
                try:
                    patterns = []
                    params = {"curr_id": int(pos["id"])}
                    if spread_basket_id:
                        patterns.append("improvement_note LIKE :basket_pattern")
                        params["basket_pattern"] = f'%"{spread_basket_id}"%'
                        patterns.append("improvement_note LIKE :as_primary_pattern")
                        params["as_primary_pattern"] = f'%"paired_primary_audit_id": {pos["id"]}%'
                    if paired_primary_audit_id:
                        patterns.append("id = :paired_primary_id")
                        params["paired_primary_id"] = int(paired_primary_audit_id)

                    if patterns:
                        where_clause = " OR ".join(patterns)
                        with self.engine.connect() as sib_conn:
                            try:
                                sib_rows = sib_conn.execute(
                                    text(f"""
                                        SELECT id, instrument_id, theoretical_fill_price, side, quantity, signal_at, improvement_note, reasoning_chain
                                        FROM shadow_execution_audits
                                        WHERE audit_status = 'RECONCILED'
                                          AND net_pnl IS NULL
                                          AND id != :curr_id
                                          AND ({where_clause})
                                    """),
                                    params
                                ).mappings().all()
                            except Exception as e:
                                logger.debug(f"Query without reasoning_chain fallback: {e}")
                                sib_rows = sib_conn.execute(
                                    text(f"""
                                        SELECT id, instrument_id, theoretical_fill_price, side, quantity, signal_at, improvement_note
                                        FROM shadow_execution_audits
                                        WHERE audit_status = 'RECONCILED'
                                          AND net_pnl IS NULL
                                          AND id != :curr_id
                                          AND ({where_clause})
                                    """),
                                    params
                                ).mappings().all()

                        for sib in sib_rows:
                            sib_note_raw = sib.get("improvement_note")
                            sib_note = {}
                            if sib_note_raw:
                                if isinstance(sib_note_raw, dict):
                                    sib_note = dict(sib_note_raw)
                                elif isinstance(sib_note_raw, str):
                                    try:
                                        sib_note = json.loads(sib_note_raw)
                                    except Exception as e:
                                        logger.debug(f"Failed to parse sibling improvement_note: {e}")
                                        sib_note = {}
                                        # Regex recovery for truncated notes
                                        m_basket = re.search(r'"spread_basket_id":\s*"([^"]+)"', sib_note_raw)
                                        if m_basket:
                                            sib_note["spread_basket_id"] = m_basket.group(1)
                                        m_primary = re.search(r'"paired_primary_audit_id":\s*(\d+)', sib_note_raw)
                                        if m_primary:
                                            sib_note["paired_primary_audit_id"] = int(m_primary.group(1))

                            # Fallback to reasoning_chain if absent in sib_note
                            if not sib_note.get("spread_basket_id") or not sib_note.get("paired_primary_audit_id"):
                                raw_sib_rc = sib.get("reasoning_chain")
                                if raw_sib_rc:
                                    try:
                                        sib_rc_data = json.loads(raw_sib_rc) if isinstance(raw_sib_rc, str) and raw_sib_rc.startswith("{") else (dict(raw_sib_rc) if isinstance(raw_sib_rc, dict) else {})
                                        if not sib_note.get("spread_basket_id"):
                                            sib_note["spread_basket_id"] = sib_rc_data.get("spread_basket_id") or sib_rc_data.get("strategy_group")
                                        if not sib_note.get("paired_primary_audit_id"):
                                            sib_note["paired_primary_audit_id"] = sib_rc_data.get("paired_primary_audit_id")
                                    except Exception as e:
                                        logger.debug(f"Failed to parse sibling reasoning_chain: {e}")

                            # Strict exact membership verification:
                            matches_basket = bool(spread_basket_id and sib_note.get("spread_basket_id") == spread_basket_id)
                            matches_as_hedge = bool(paired_primary_audit_id and sib["id"] == int(paired_primary_audit_id))
                            matches_as_primary = bool(sib_note.get("paired_primary_audit_id") == int(pos["id"]))
                            if not (matches_basket or matches_as_hedge or matches_as_primary):
                                continue

                            sib_id = int(sib["id"])
                            sib_inst_id = int(sib["instrument_id"])
                            sib_entry = Decimal(str(sib["theoretical_fill_price"]))
                            sib_sig_at = sib.get("signal_at")
                            if isinstance(sib_sig_at, str):
                                try:
                                    sib_sig_at = datetime.fromisoformat(sib_sig_at)
                                except Exception as e:
                                    logger.debug(f"Failed to parse sibling signal_at: {e}")
                                    sib_sig_at = None

                            if isinstance(sib_sig_at, datetime) and sib_sig_at.tzinfo is None:
                                sib_sig_at = sib_sig_at.replace(tzinfo=timezone.utc)
                            pricing_wm = exit_bar_time or watermark
                            sib_price_res = self.get_latest_price(
                                sib_inst_id,
                                pricing_wm,
                                entry_price=sib_entry,
                                signal_at=sib_sig_at,
                                return_source=True
                            )
                            if isinstance(sib_price_res, tuple):
                                sib_price, sib_src = sib_price_res
                            else:
                                sib_price, sib_src = sib_price_res, "market"

                            if sib_price is None or sib_price <= 0:
                                sib_price = sib_entry
                                sib_is_synth = True
                            else:
                                sib_is_synth = (sib_src == "synthetic")

                            sib_exit_reason = f"BASKET_EXIT:{exit_reason}"
                            logger.info(f"PositionManager Atomic Basket Exit: #{sib_id} (basket={spread_basket_id or paired_primary_audit_id}) closed via {sib_exit_reason} at {sib_price}")
                            record_shadow_exit(
                                self.engine,
                                sib_id,
                                sib_price,
                                sib_exit_reason,
                                exit_at=exit_bar_time or watermark,
                                is_synthetic=bool(is_synthetic_exit or sib_is_synth)
                            )
                            sibling_closed_ids.append(sib_id)
                except Exception as sib_err:
                    logger.warning(f"PositionManager failed to close basket siblings for #{pos['id']}: {sib_err}")

            if sibling_closed_ids:
                telemetry["sibling_closed_ids"] = sibling_closed_ids

        return telemetry

    def emit_telemetry(self, telemetries: List[Dict]) -> None:
        """Publish real-time telemetry to Redis for zero-lag dashboard updates."""
        if not self.redis:
            return
        try:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "open_count": len([t for t in telemetries if not t.get("closed")]),
                "positions": telemetries,
            }
            self.redis.set("nivesh:positions:live", json.dumps(payload), ex=10)
            self.redis.publish("nivesh:stream:positions", json.dumps(payload))
        except Exception as exc:
            logger.warning(f"Failed to publish position telemetry: {exc}")

    def run_once(self, watermark: Optional[datetime] = None) -> Dict:
        """Single 1-second pass over all open positions."""
        now = watermark or datetime.now(timezone.utc)
        positions = self.get_open_positions()
        telemetries = []
        closed_count = 0
        closed_ids = set()
        for pos in positions:
            if pos["id"] in closed_ids:
                continue
            res = self.evaluate_position(pos, now)
            if res:
                telemetries.append(res)
                if res.get("closed"):
                    closed_count += 1
                    closed_ids.add(pos["id"])
                    if res.get("sibling_closed_ids"):
                        closed_ids.update(res["sibling_closed_ids"])
                        closed_count += len(res["sibling_closed_ids"])
        self.emit_telemetry(telemetries)
        return {
            "status": "ok",
            "evaluated": len(positions),
            "closed": closed_count,
            "telemetries": telemetries,
        }

    def run_forever(self, interval_seconds: float = 1.0) -> None:
        """Continuous high-frequency loop running every 1 second."""
        logger.info(f"Starting PositionManager 1-second high-frequency loop (interval={interval_seconds}s)")
        while True:
            start = time.time()
            try:
                self.run_once()
            except Exception as exc:
                logger.error(f"Error in PositionManager loop: {exc}", exc_info=True)
            elapsed = time.time() - start
            time.sleep(max(0.05, interval_seconds - elapsed))


def replay_trade_walk_forward(
    trade: Dict[str, Any],
    bars: List[Dict[str, Any]],
    *,
    harvest_enabled: bool = False,
    harvest_trigger: float = 0.83,
    exit_threshold: float = 70.0,
    tighten_threshold: float = 40.0,
    lock_fraction: float = 0.65,
    tick_size: float = 0.05,
    breakeven_trigger_r: float = 1.10,
    profit_lock_trigger_r: float = 1.60,
    profit_lock_guaranteed_r: float = 1.00,
    trailing_trigger_r: float = 1.50,
    trailing_giveback_r: float = 0.40,
    bars_15m: Optional[List[Dict[str, Any]]] = None,
    vwap: Optional[float] = None,
    index_regime_against: Optional[bool] = None,
) -> Dict[str, Any]:
    """Walk a historical shadow trade forward across price bars with PositionManager exit logic.

    Phase 2d Specifications:
    - Exits evaluated on 1-minute bars (or 1s if present).
    - Reversal score evaluated ONLY on CLOSED 5-minute bars built from 1m bars (excluding partial edge buckets).
    - Models stagnation scratch (900s), adverse cut (90s / 0.40R), stagnation tighten (300s), and force-flat (15:15 IST).
    - If no exit is triggered by the end of the walk, returns exit_reason = 'FELL_THROUGH_TO_RECORDED'.
    - Protects running_sl is None safely against all type errors.
    """
    if not bars:
        entry_val = float(trade.get("theoretical_fill_price") or trade.get("entry") or 0.0)
        exit_val = float(trade.get("realised_exit_price") or trade.get("exit_price") or entry_val)
        sl_val = float(trade.get("stop_loss_price") or trade.get("initial_sl") or (entry_val * 0.99))
        r_dist = abs(entry_val - sl_val) or 1.0
        side_u = str(trade.get("side") or "BUY").upper()
        rel_r = (exit_val - entry_val) / r_dist if side_u == "BUY" else (entry_val - exit_val) / r_dist
        return {
            "exit_price": exit_val,
            "exit_reason": "NO_BARS",
            "net_pnl": 0.0,
            "realized_r": round(rel_r, 4),
            "max_favorable": exit_val,
        }

    entry = Decimal(str(trade.get("theoretical_fill_price") or trade.get("entry") or 0.0))
    initial_sl = Decimal(str(trade["stop_loss_price"])) if trade.get("stop_loss_price") else (
        Decimal(str(trade["initial_sl"])) if trade.get("initial_sl") else None
    )
    take_profit = Decimal(str(trade["take_profit_price"])) if trade.get("take_profit_price") else (
        Decimal(str(trade["target_price"])) if trade.get("target_price") else None
    )
    side = str(trade.get("side") or "BUY").upper()
    quantity = Decimal(str(trade.get("quantity") or 1))
    fees = Decimal(str(trade.get("estimated_fees") or 0))

    fallback_risk = max(Decimal("0.50"), entry * Decimal("0.006"))
    if side == "BUY":
        r_points = (entry - initial_sl) if (initial_sl and entry > initial_sl) else fallback_risk
        running_sl = initial_sl if initial_sl is not None else (entry - fallback_risk)
    else:
        r_points = (initial_sl - entry) if (initial_sl and initial_sl > entry) else fallback_risk
        running_sl = initial_sl if initial_sl is not None else (entry + fallback_risk)
    r_points = max(Decimal("0.05"), r_points)

    target_dist = abs(take_profit - entry) if take_profit else (r_points * Decimal("1.5"))
    fee_buffer = ((fees * Decimal("2.5")) + (Decimal("2") * Decimal(str(tick_size)) * quantity)) / max(Decimal("1"), quantity)

    running_sl_reason = "STOP_LOSS"
    pending_market_exit: Optional[str] = None
    best_favourable = entry
    exit_price: Optional[Decimal] = None
    exit_reason: Optional[str] = None
    harvest_stop: Optional[Decimal] = None

    # Determine start timestamp for elapsed seconds
    start_time: Optional[datetime] = None
    sig_val = trade.get("signal_at")
    if sig_val:
        try:
            start_time = datetime.fromisoformat(sig_val) if isinstance(sig_val, str) else sig_val
            if start_time and getattr(start_time, "tzinfo", None) is None:
                start_time = start_time.replace(tzinfo=IST)
            elif start_time and getattr(start_time, "tzinfo", None) is not None:
                start_time = start_time.astimezone(IST)
        except Exception:
            start_time = None
    if start_time is None and bars and bars[0].get("bar_time"):
        try:
            b0_t = bars[0]["bar_time"]
            start_time = datetime.fromisoformat(b0_t) if isinstance(b0_t, str) else b0_t
            if start_time and getattr(start_time, "tzinfo", None) is None:
                start_time = start_time.replace(tzinfo=IST)
            elif start_time and getattr(start_time, "tzinfo", None) is not None:
                start_time = start_time.astimezone(IST)
        except Exception:
            start_time = None

    # Track 5m closed candles and bucket aggregation
    closed_5m_bars: List[Dict[str, Any]] = []
    current_5m_bucket_bars: List[Dict[str, Any]] = []
    current_bucket_min: Optional[int] = None
    is_5m_input = False
    if bars and any(b.get("interval") in ("5minute", "5m", "15minute", "15m", "30minute", "30m", "60minute", "60m", "day") for b in bars[:2]):
        is_5m_input = True
    elif len(bars) >= 2 and (bars[0].get("bar_time") or bars[0].get("timestamp")) and (bars[1].get("bar_time") or bars[1].get("timestamp")):
        try:
            bt0 = bars[0].get("bar_time") or bars[0].get("timestamp")
            bt1 = bars[1].get("bar_time") or bars[1].get("timestamp")
            t0 = datetime.fromisoformat(bt0) if isinstance(bt0, str) else bt0
            t1 = datetime.fromisoformat(bt1) if isinstance(bt1, str) else bt1
            if abs((t1 - t0).total_seconds()) > 60:
                is_5m_input = True
        except Exception:
            pass

    trade_mode = str(trade.get("trade_mode") or "INTRADAY").upper()

    for i, b in enumerate(bars):
        b_open = Decimal(str(b.get("open_price") if b.get("open_price") is not None else b.get("open", entry)))
        b_high = Decimal(str(b.get("high_price") if b.get("high_price") is not None else b.get("high", entry)))
        b_low = Decimal(str(b.get("low_price") if b.get("low_price") is not None else b.get("low", entry)))
        b_close = Decimal(str(b.get("close_price") if b.get("close_price") is not None else b.get("close", entry)))
        b_vol = float(b.get("volume") or 0)
        b_time = b.get("bar_time")

        b_dt: Optional[datetime] = None
        if b_time:
            try:
                b_dt = datetime.fromisoformat(b_time) if isinstance(b_time, str) else b_time
                if b_dt and getattr(b_dt, "tzinfo", None) is None:
                    b_dt = b_dt.replace(tzinfo=IST)
                elif b_dt and getattr(b_dt, "tzinfo", None) is not None:
                    b_dt = b_dt.astimezone(IST)
            except Exception:
                b_dt = None

        elapsed_seconds = (b_dt - start_time).total_seconds() if (b_dt and start_time) else (i * 60.0)

        # -------------------------------------------------------------
        # STEP 1: IN-BAR CHECK OF PRIOR STOPS & TARGETS (BEFORE THIS BAR)
        # -------------------------------------------------------------
        if pending_market_exit is not None:
            exit_price = b_open
            exit_reason = pending_market_exit
            break

        # Check existing stop loss and take profit against current bar's range
        if side == "BUY":
            if running_sl is not None and b_open <= running_sl:
                exit_price = b_open
                exit_reason = "GAP_DOWN_STOP"
                break
            elif running_sl is not None and b_low <= running_sl:
                exit_price = running_sl
                exit_reason = running_sl_reason
                break
            elif take_profit and b_open >= take_profit:
                exit_price = b_open
                exit_reason = "TAKE_PROFIT"
                break
            elif take_profit and b_high >= take_profit:
                exit_price = take_profit
                exit_reason = "TAKE_PROFIT"
                break
        else:  # SELL
            if running_sl is not None and b_open >= running_sl:
                exit_price = b_open
                exit_reason = "GAP_UP_STOP"
                break
            elif running_sl is not None and b_high >= running_sl:
                exit_price = running_sl
                exit_reason = running_sl_reason
                break
            elif take_profit and b_open <= take_profit:
                exit_price = b_open
                exit_reason = "TAKE_PROFIT"
                break
            elif take_profit and b_low <= take_profit:
                exit_price = take_profit
                exit_reason = "TAKE_PROFIT"
                break

        # Check modeled intraday exits (adverse cut, stagnation scratch, force-flat)
        if trade_mode not in ("SWING", "POSITIONAL"):
            current_r = ((b_close - entry) / r_points) if side == "BUY" else ((entry - b_close) / r_points)
            favorable_r_now = ((best_favourable - entry) / r_points) if side == "BUY" else ((entry - best_favourable) / r_points)

            # RULE 1: Immediate Adverse Cut (0 to 90 seconds) - skip if bar interval > 1m
            if not is_5m_input and elapsed_seconds <= ADVERSE_CUT_WINDOW_SECONDS:
                if current_r <= -ADVERSE_CUT_THRESHOLD_R and favorable_r_now <= Decimal("0.10"):
                    exit_price = b_close
                    exit_reason = "EARLY_ADVERSE_CUT"
                    break

            # RULE 2: 15-Minute STAGNATION_GUARD (900 seconds) - skip if bar interval > 1m
            if not is_5m_input and elapsed_seconds >= STAGNATION_SCRATCH_SECONDS:
                if favorable_r_now < STAGNATION_MIN_EXPANSION_R:
                    exit_price = b_close
                    exit_reason = "STAGNATION_GUARD"
                    break

            # Modeled Intraday Force-Flat Exit at 15:15 IST
            if b_dt:
                if b_dt.hour > 15 or (b_dt.hour == 15 and b_dt.minute >= 15):
                    exit_price = b_close
                    exit_reason = "FORCE_FLAT"
                    break

        # -------------------------------------------------------------
        # STEP 2: BAR SURVIVED -> COMPUTE EXCURSION & RATCHETS AT CLOSE FOR NEXT BAR
        # -------------------------------------------------------------
        if side == "BUY":
            best_favourable = max(best_favourable, b_high)
            favorable_r = (best_favourable - entry) / r_points
        else:
            best_favourable = min(best_favourable, b_low)
            favorable_r = (entry - best_favourable) / r_points

        profit_lock_price = None
        if favorable_r >= Decimal(str(profit_lock_trigger_r)):
            profit_lock_price = (entry + Decimal(str(profit_lock_guaranteed_r)) * r_points) if side == "BUY" else (entry - Decimal(str(profit_lock_guaranteed_r)) * r_points)

        breakeven_price = None
        if favorable_r >= Decimal(str(breakeven_trigger_r)):
            breakeven_price = (entry + fee_buffer) if side == "BUY" else (entry - fee_buffer)

        trailing_stop_price = None
        if favorable_r >= Decimal(str(trailing_trigger_r)):
            trailing_stop_price = (best_favourable - Decimal(str(trailing_giveback_r)) * r_points) if side == "BUY" else (best_favourable + Decimal(str(trailing_giveback_r)) * r_points)

        # 5-minute Stagnation Tighten (300 seconds) - skip if bar interval > 1m
        if not is_5m_input and trade_mode not in ("SWING", "POSITIONAL") and elapsed_seconds >= STAGNATION_TIGHTEN_SECONDS and favorable_r < Decimal("0.20"):
            stag_sl = (entry - STAGNATION_TIGHTEN_R * r_points) if side == "BUY" else (entry + STAGNATION_TIGHTEN_R * r_points)
            if side == "BUY" and (running_sl is None or stag_sl > running_sl):
                running_sl = stag_sl
                running_sl_reason = "STAGNATION_TIGHTEN"
            elif side == "SELL" and (running_sl is None or stag_sl < running_sl):
                running_sl = stag_sl
                running_sl_reason = "STAGNATION_TIGHTEN"

        # Accumulate closed 5-minute candles
        new_closed_5m = False
        if is_5m_input:
            closed_5m_bars.append({
                "open": float(b_open),
                "high": float(b_high),
                "low": float(b_low),
                "close": float(b_close),
                "volume": b_vol,
                "bar_time": b_time,
            })
            new_closed_5m = True
        else:
            b_min = b_dt.minute if b_dt else (i % 60)
            bucket_idx = (b_min // 5) * 5
            if current_bucket_min is not None and bucket_idx != current_bucket_min:
                if len(current_5m_bucket_bars) >= 5:
                    closed_5m_bars.append({
                        "open": float(current_5m_bucket_bars[0]["open"]),
                        "high": float(max(x["high"] for x in current_5m_bucket_bars)),
                        "low": float(min(x["low"] for x in current_5m_bucket_bars)),
                        "close": float(current_5m_bucket_bars[-1]["close"]),
                        "volume": float(sum(x["volume"] for x in current_5m_bucket_bars)),
                        "bar_time": current_5m_bucket_bars[-1]["bar_time"],
                    })
                    new_closed_5m = True
                current_5m_bucket_bars = []

            current_bucket_min = bucket_idx
            current_5m_bucket_bars.append({
                "open": b_open,
                "high": b_high,
                "low": b_low,
                "close": b_close,
                "volume": b_vol,
                "bar_time": b_time,
            })

        # Profit-Harvest Layer: evaluated strictly at 5-minute candle close
        if new_closed_5m and harvest_enabled and take_profit is not None:
            prog = float((b_close - entry) / target_dist) if side == "BUY" else float((entry - b_close) / target_dist)
            best_prog = float((best_favourable - entry) / target_dist) if side == "BUY" else float((entry - best_favourable) / target_dist)

            if (prog >= 0.50 or best_prog >= 0.50) and breakeven_price is None:
                breakeven_price = (entry + fee_buffer) if side == "BUY" else (entry - fee_buffer)

            if prog >= harvest_trigger or best_prog >= harvest_trigger:
                if len(closed_5m_bars) >= 3:
                    calc_vwap = vwap
                    if calc_vwap is None:
                        tot_v = sum(float(x.get("volume") or 1) for x in closed_5m_bars)
                        tot_pv = sum(float(x.get("close") or entry) * float(x.get("volume") or 1) for x in closed_5m_bars)
                        calc_vwap = (tot_pv / tot_v) if tot_v > 0 else float(entry)

                    rev_res = compute_reversal_score(
                        bars_5m=closed_5m_bars[-20:],
                        side=side,
                        bars_15m=bars_15m,
                        vwap=calc_vwap,
                        index_regime_against=index_regime_against or False,
                        exit_threshold=exit_threshold,
                        tighten_threshold=tighten_threshold,
                    )
                    if rev_res["action"] == "EXIT":
                        pending_market_exit = "PROFIT_HARVEST_REVERSAL_EXIT"
                    elif rev_res["action"] == "TIGHTEN":
                        harvest_stop = calculate_harvest_stop(
                            side=side,
                            entry_price=entry,
                            latest_price=b_close,
                            current_sl=running_sl,
                            lock_fraction=lock_fraction,
                        )

        # Ratchet trailing stop loss for NEXT bar
        candidates = [p for p in (running_sl, profit_lock_price, breakeven_price, trailing_stop_price, harvest_stop if harvest_enabled else None) if p is not None]
        if candidates:
            next_sl = max(candidates) if side == "BUY" else min(candidates)
            if side == "BUY" and (running_sl is None or next_sl > running_sl):
                running_sl = next_sl
                if harvest_stop and running_sl == harvest_stop:
                    running_sl_reason = "PROFIT_HARVEST_STOP"
                elif profit_lock_price and running_sl >= profit_lock_price:
                    running_sl_reason = "TRAILING_STOP"
                elif breakeven_price and running_sl >= breakeven_price:
                    running_sl_reason = "BREAKEVEN_STOP"
            elif side == "SELL" and (running_sl is None or next_sl < running_sl):
                running_sl = next_sl
                if harvest_stop and running_sl == harvest_stop:
                    running_sl_reason = "PROFIT_HARVEST_STOP"
                elif profit_lock_price and running_sl <= profit_lock_price:
                    running_sl_reason = "TRAILING_STOP"
                elif breakeven_price and running_sl <= breakeven_price:
                    running_sl_reason = "BREAKEVEN_STOP"

    exit_bar_time = None
    if exit_price is None:
        last_b = bars[-1]
        exit_bar_time = last_b.get("bar_time") or last_b.get("timestamp")
        if pending_market_exit is not None:
            exit_price = Decimal(str(last_b.get("close_price") if last_b.get("close_price") is not None else last_b.get("close", entry)))
            exit_reason = pending_market_exit
        else:
            exit_price = Decimal(str(trade.get("realised_exit_price") or last_b.get("close_price") or last_b.get("close") or entry))
            exit_reason = "FELL_THROUGH_TO_RECORDED"
    else:
        exit_bar_time = b_time

    # Apply fees and 2-tick slippage
    slippage_per_share = Decimal(str(tick_size)) * Decimal("2")
    net_exit = (exit_price - slippage_per_share) if side == "BUY" else (exit_price + slippage_per_share)
    gross_pnl = ((net_exit - entry) * quantity) if side == "BUY" else ((entry - net_exit) * quantity)
    net_pnl = gross_pnl - fees
    realized_r = float(((net_exit - entry) - (fees / quantity)) / r_points) if side == "BUY" else float(((entry - net_exit) - (fees / quantity)) / r_points)

    exit_date_str = None
    if exit_bar_time:
        try:
            edt = datetime.fromisoformat(str(exit_bar_time))
            exit_date_str = (edt.astimezone(IST).date() if edt.tzinfo else edt.date()).isoformat()
        except Exception:
            pass

    return {
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "net_pnl": float(net_pnl),
        "realized_r": round(realized_r, 4),
        "max_favorable": float(best_favourable),
        "exit_time": str(exit_bar_time) if exit_bar_time else None,
        "exit_date": exit_date_str,
    }

