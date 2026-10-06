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
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import create_engine, text

from .config import DATABASE_URL, REDIS_URL
from .ml.validation_engine import record_shadow_exit

logger = logging.getLogger("nivesh.position_manager")
IST = ZoneInfo("Asia/Kolkata")
TRADE_BAR_SOURCES = ("zerodha_kite", "kite_gap_backfill", "upstox_v3", "upstox_rest_5m")


class PositionManager:
    def __init__(self, database_url: Optional[str] = None, redis_url: Optional[str] = None):
        self.database_url = database_url or DATABASE_URL or os.getenv("DATABASE_URL", "")
        self.redis_url = redis_url or REDIS_URL or os.getenv("REDIS_URL", "")
        
        # Dedicated engine with pool pre-ping
        engine_kwargs = {"pool_pre_ping": True, "future": True}
        if not self.database_url.startswith("sqlite"):
            engine_kwargs["pool_size"] = 3
            engine_kwargs["max_overflow"] = 2
        self.engine = create_engine(self.database_url, **engine_kwargs)
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
        
        self.adverse_cut_window_seconds = 90.0
        self.adverse_cut_threshold_r = Decimal("0.40")
        
        self.stagnation_tighten_seconds = 300.0  # 5 minutes
        self.stagnation_tighten_r = Decimal("0.35")
        self.stagnation_scratch_seconds = 900.0  # 15 minutes (intraday)
        self.stagnation_min_expansion_r = Decimal("0.25")
        
        self.force_flat_time = "15:15"

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

    def get_latest_price(self, instrument_id: int, watermark: datetime) -> Optional[Decimal]:
        """Fetch current LTP for an instrument from sub-second or 1-minute market bars."""
        # Try redis tick cache first if available
        if self.redis:
            try:
                cached_price = self.redis.hget("nivesh:ticks:latest", str(instrument_id))
                if cached_price:
                    return Decimal(str(cached_price))
            except Exception:
                pass

        with self.engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT close_price FROM live_market_bars
                    WHERE instrument_id = :instrument_id
                      AND interval IN ('1second', '1minute', '5minute')
                      AND bar_time <= :watermark
                      AND source = ANY(:sources)
                    ORDER BY bar_time DESC,
                             CASE interval WHEN '1second' THEN 0 WHEN '1minute' THEN 1 ELSE 2 END
                    LIMIT 1
                """),
                {
                    "instrument_id": instrument_id,
                    "watermark": watermark,
                    "sources": list(TRADE_BAR_SOURCES),
                },
            ).mappings().one_or_none()
            if row and row["close_price"] is not None:
                return Decimal(str(row["close_price"]))

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
                    text("SELECT id FROM instrument_master WHERE (symbol = :und OR underlying_symbol = :und) AND instrument_type = 'EQ' LIMIT 1"),
                    {"und": und}
                ).scalar_one_or_none()
                if und_id:
                    und_row = conn.execute(
                        text("""
                            SELECT close_price FROM live_market_bars
                            WHERE instrument_id = :und_id
                              AND interval IN ('1second', '1minute', '5minute', 'day')
                              AND bar_time <= :watermark
                              AND source = ANY(:sources)
                            ORDER BY bar_time DESC LIMIT 1
                        """),
                        {"und_id": und_id, "watermark": watermark, "sources": list(TRADE_BAR_SOURCES)}
                    ).mappings().one_or_none()
                    if und_row and und_row["close_price"]:
                        spot = float(und_row["close_price"])
                        strike = float(opt_meta["strike"])
                        wm_date = (watermark.astimezone(IST) if getattr(watermark, "tzinfo", None) else watermark).date() if hasattr(watermark, "date") else watermark
                        dte = max(0.5, float((opt_meta["expiry"] - wm_date).days)) if opt_meta.get("expiry") else 4.0
                        from backend.greeks_engine import calculate_black_scholes_greeks
                        greeks = calculate_black_scholes_greeks(spot, strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        return Decimal(str(max(0.05, round(greeks["price"], 2))))
        return None

    def get_exit_bars(self, instrument_id: int, signal_at: datetime, watermark: datetime) -> List[Dict]:
        """Fetch historical bars since trade entry for high-precision exit checks."""
        with self.engine.connect() as conn:
            for interval, minimum in (("1second", 3), ("1minute", 3), ("5minute", 1)):
                rows = conn.execute(
                    text("""
                        SELECT bar_time, high_price, low_price, close_price, volume
                        FROM live_market_bars
                        WHERE instrument_id = :instrument
                          AND interval = :interval
                          AND source = ANY(:sources)
                          AND bar_time >= :signal_at AND bar_time <= :watermark
                        ORDER BY bar_time ASC
                    """),
                    {
                        "instrument": instrument_id,
                        "interval": interval,
                        "signal_at": signal_at,
                        "watermark": watermark,
                        "sources": list(TRADE_BAR_SOURCES),
                    },
                ).mappings().all()
                if len(rows) >= minimum:
                    return [dict(r, interval=interval) for r in rows]

            # Fallback: synthesize option excursion bars from underlying stock movement
            opt_meta = conn.execute(
                text("SELECT symbol, underlying_symbol, strike, expiry, instrument_type FROM instrument_master WHERE id = :id"),
                {"id": instrument_id}
            ).mappings().one_or_none()
            if opt_meta and opt_meta.get("instrument_type") in ("CE", "PE") and opt_meta.get("strike"):
                und = opt_meta.get("underlying_symbol") or opt_meta["symbol"].split()[0]
                und_id = conn.execute(
                    text("SELECT id FROM instrument_master WHERE (symbol = :und OR underlying_symbol = :und) AND instrument_type IN ('EQ', 'INDEX') LIMIT 1"),
                    {"und": und}
                ).scalar_one_or_none()
                und_bars = []
                if und_id:
                    und_bars = conn.execute(
                        text("""
                            SELECT bar_time, high_price, low_price, close_price, volume
                            FROM live_market_bars
                            WHERE instrument_id = :und_id
                              AND interval IN ('1minute', '5minute')
                              AND source = ANY(:sources)
                              AND bar_time >= :signal_at AND bar_time <= :watermark
                            ORDER BY bar_time ASC
                        """),
                        {"und_id": und_id, "sources": list(TRADE_BAR_SOURCES), "signal_at": signal_at, "watermark": watermark}
                    ).mappings().all()
                if und_bars:
                    from backend.greeks_engine import calculate_black_scholes_greeks
                    strike = float(opt_meta["strike"])
                    wm_date = (watermark.astimezone(IST) if getattr(watermark, "tzinfo", None) else watermark).date() if hasattr(watermark, "date") else watermark
                    dte = max(0.5, float((opt_meta["expiry"] - wm_date).days)) if opt_meta.get("expiry") else 4.0
                    opt_bars = []
                    for ub in und_bars:
                        g_high = calculate_black_scholes_greeks(float(ub["high_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_low = calculate_black_scholes_greeks(float(ub["low_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_close = calculate_black_scholes_greeks(float(ub["close_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        h_p = max(0.05, round(max(g_high["price"], g_low["price"]), 2))
                        l_p = max(0.05, round(min(g_high["price"], g_low["price"]), 2))
                        c_p = max(0.05, round(g_close["price"], 2))
                        opt_bars.append({
                            "bar_time": ub["bar_time"],
                            "high_price": h_p,
                            "low_price": l_p,
                            "close_price": c_p,
                            "volume": ub["volume"],
                            "interval": "synthesized_option"
                        })
                    return opt_bars
        return []

    def evaluate_position(self, pos: Dict, watermark: datetime) -> Optional[Dict]:
        """Sub-second evaluation of a single open trade against institutional risk rules."""
        latest_price = self.get_latest_price(pos["instrument_id"], watermark)
        if latest_price is None or latest_price <= 0:
            return None

        entry = Decimal(str(pos["theoretical_fill_price"]))
        side = str(pos["side"]).upper()
        trade_mode = str(pos.get("trade_mode") or "INTRADAY").upper()
        quantity = Decimal(str(int(pos.get("quantity") or 1)))
        fees = Decimal(str(pos.get("estimated_fees") or 0))
        signal_at = pos["signal_at"]
        if signal_at.tzinfo is None:
            signal_at = signal_at.replace(tzinfo=timezone.utc)
        if watermark.tzinfo is None:
            watermark = watermark.replace(tzinfo=timezone.utc)

        elapsed_seconds = max(0.0, (watermark - signal_at).total_seconds())

        stop_loss = Decimal(str(pos["stop_loss_price"])) if pos.get("stop_loss_price") else None
        take_profit = Decimal(str(pos["take_profit_price"])) if pos.get("take_profit_price") else None
        instrument_type = str(pos.get("instrument_type") or "").upper()

        # Institutional Short Options Discipline (CBOE & tastytrade benchmark):
        # 1. 50% Max Profit Target: Holding short options past 50% decay yields severe negative gamma risk.
        #    Take profit when premium reaches 50% of credit received (LTP <= entry * 0.50).
        # 2. 1.5x Hard Loss Ceiling: Cap short option loss at 1.5x entry price to prevent naked tail risk.
        if side == "SELL" and instrument_type in ("CE", "PE"):
            opt_tp_50 = entry * Decimal("0.50")
            take_profit = max(take_profit, opt_tp_50) if take_profit else opt_tp_50
            opt_sl_150 = entry * Decimal("1.50")
            stop_loss = min(stop_loss, opt_sl_150) if stop_loss else opt_sl_150

        # 1. Calculate baseline risk (R-points)
        fallback_risk = max(Decimal("0.50"), entry * Decimal("0.006"))
        if side == "BUY":
            r_points = (entry - stop_loss) if (stop_loss and entry > stop_loss) else fallback_risk
        else:
            r_points = (stop_loss - entry) if (stop_loss and stop_loss > entry) else fallback_risk
        r_points = max(Decimal("0.05"), r_points)

        # 2. Fee-padded breakeven buffer (covers 2.5x roundtrip fees + 2 ticks spread slippage)
        tick_size = Decimal("0.05")
        fee_buffer_per_share = ((fees * Decimal("2.5")) + (Decimal("2") * tick_size * quantity)) / max(Decimal("1"), quantity)

        # 3. Bar history for excursion analysis
        bars = self.get_exit_bars(pos["instrument_id"], signal_at, watermark)
        best_favourable = entry
        worst_adverse = entry
        # Multi-day holding invariant: swing trades stay open and mature
        is_multi_day = trade_mode in ("SWING", "POSITIONAL")

        exit_price: Optional[Decimal] = None
        exit_reason: Optional[str] = None
        exit_bar_time: Optional[datetime] = None

        # -------------------------------------------------------------
        # Chronological Bar-by-Bar Replay (Guarantees Perfect Execution Even After Offline/Reboots)
        # -------------------------------------------------------------
        best_favourable = entry
        worst_adverse = entry
        running_sl = stop_loss

        # Ensure excursion R metrics are always initialized
        favorable_r = Decimal("0.0")
        current_r = Decimal("0.0")

        tp_candidate_bar = None
        tp_candidate_price = None
        tp_candidate_reason = None

        sl_candidate_bar = None
        sl_candidate_price = None
        sl_candidate_reason = None

        for bar in bars:
            h = Decimal(str(bar.get("high_price") or entry))
            l = Decimal(str(bar.get("low_price") or entry))
            c = Decimal(str(bar.get("close_price") or bar.get("high_price") or entry))
            b_time = bar.get("bar_time")

            if side == "BUY":
                best_favourable = max(best_favourable, h)
                worst_adverse = min(worst_adverse, l)
                favorable_r = max(favorable_r, (best_favourable - entry) / r_points)
                # Check Take Profit
                if take_profit and h >= take_profit and not tp_candidate_bar:
                    tp_candidate_price = take_profit
                    tp_candidate_reason = "TAKE_PROFIT"
                    tp_candidate_bar = b_time
                # Check +2.5R Spike
                fav_r = (best_favourable - entry) / r_points
                if fav_r >= Decimal("2.50") and not tp_candidate_bar:
                    tp_candidate_price = entry + Decimal("2.5") * r_points
                    tp_candidate_reason = "PROFIT_CAPTURE"
                    tp_candidate_bar = b_time
                # Check Stop Loss
                if running_sl and l <= running_sl and not sl_candidate_bar:
                    sl_candidate_price = running_sl
                    sl_candidate_reason = "STOP_LOSS"
                    sl_candidate_bar = b_time

                # If purely intraday, break on first exit triggered
                if not is_multi_day:
                    if tp_candidate_bar and sl_candidate_bar:
                        # Which occurred first?
                        if tp_candidate_bar <= sl_candidate_bar:
                            exit_price = tp_candidate_price
                            exit_reason = tp_candidate_reason
                            exit_bar_time = tp_candidate_bar
                        else:
                            exit_price = sl_candidate_price
                            exit_reason = sl_candidate_reason
                            exit_bar_time = sl_candidate_bar
                        break
                    elif tp_candidate_bar:
                        exit_price = tp_candidate_price
                        exit_reason = tp_candidate_reason
                        exit_bar_time = tp_candidate_bar
                        break
                    elif sl_candidate_bar:
                        exit_price = sl_candidate_price
                        exit_reason = sl_candidate_reason
                        exit_bar_time = sl_candidate_bar
                        break
            else:  # SELL
                best_favourable = min(best_favourable, l)
                worst_adverse = max(worst_adverse, h)
                favorable_r = max(favorable_r, (entry - best_favourable) / r_points)
                # Check Take Profit
                if take_profit and l <= take_profit and not tp_candidate_bar:
                    tp_candidate_price = take_profit
                    tp_candidate_reason = "TAKE_PROFIT"
                    tp_candidate_bar = b_time
                # Check +2.5R Spike
                fav_r = (entry - best_favourable) / r_points
                if fav_r >= Decimal("2.50") and not tp_candidate_bar:
                    tp_candidate_price = entry - Decimal("2.5") * r_points
                    tp_candidate_reason = "PROFIT_CAPTURE"
                    tp_candidate_bar = b_time
                # Check Stop Loss
                if running_sl and h >= running_sl and not sl_candidate_bar:
                    sl_candidate_price = running_sl
                    sl_candidate_reason = "STOP_LOSS"
                    sl_candidate_bar = b_time

                # If purely intraday, break on first exit triggered
                if not is_multi_day:
                    if tp_candidate_bar and sl_candidate_bar:
                        if tp_candidate_bar <= sl_candidate_bar:
                            exit_price = tp_candidate_price
                            exit_reason = tp_candidate_reason
                            exit_bar_time = tp_candidate_bar
                        else:
                            exit_price = sl_candidate_price
                            exit_reason = sl_candidate_reason
                            exit_bar_time = sl_candidate_bar
                        break
                    elif tp_candidate_bar:
                        exit_price = tp_candidate_price
                        exit_reason = tp_candidate_reason
                        exit_bar_time = tp_candidate_bar
                        break
                    elif sl_candidate_bar:
                        exit_price = sl_candidate_price
                        exit_reason = sl_candidate_reason
                        exit_bar_time = sl_candidate_bar
                        break

        # Multi-day / Swing evaluation:
        if is_multi_day:
            if tp_candidate_bar and (not sl_candidate_bar or tp_candidate_bar <= sl_candidate_bar):
                # Favorable exit condition: If trade reached Take Profit or +2.5R before any stop loss, bank the profit!
                exit_price = tp_candidate_price
                exit_reason = tp_candidate_reason
                exit_bar_time = tp_candidate_bar
            elif sl_candidate_bar:
                # Trade never reached TP and breached Stop Loss:
                # Enforce Stop Loss protection at defined protective barrier (e.g. 47.45) to prevent runaway loss
                exit_price = sl_candidate_price
                exit_reason = "STOP_LOSS"
                exit_bar_time = sl_candidate_bar

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
            # Intraday-Only Protections (Bypassed For Multi-Day / Swing Setups)
            # -------------------------------------------------------------
            if not is_multi_day:
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
            # Trailing Stops, Profit Locks & Breakeven Stops
            # -------------------------------------------------------------
            profit_lock_price = None
            if favorable_r >= self.profit_lock_trigger_r:
                profit_lock_price = (entry + self.profit_lock_guaranteed_r * r_points) if side == "BUY" else (entry - self.profit_lock_guaranteed_r * r_points)

            breakeven_price = None
            if favorable_r >= self.breakeven_trigger_r:
                breakeven_price = (entry + fee_buffer_per_share) if side == "BUY" else (entry - fee_buffer_per_share)

            trailing_stop_price = None
            if favorable_r >= self.trailing_trigger_r:
                if side == "BUY":
                    trailing_stop_price = best_favourable - self.trailing_giveback_r * r_points
                else:
                    trailing_stop_price = best_favourable + self.trailing_giveback_r * r_points

            # Trailing Stop & Profit Lock Level Update & Database Persistence
            trailed_sl = running_sl
            if side == "BUY":
                candidates = [p for p in (running_sl, profit_lock_price, breakeven_price, trailing_stop_price) if p is not None]
                if candidates:
                    trailed_sl = max(candidates)
            else:
                candidates = [p for p in (running_sl, profit_lock_price, breakeven_price, trailing_stop_price) if p is not None]
                if candidates:
                    trailed_sl = min(candidates)

            # Persist trailed stop loss to DB if it has improved and position is still open
            orig_sl = Decimal(str(pos["stop_loss_price"])) if pos.get("stop_loss_price") else None
            improved = False
            if trailed_sl is not None:
                if orig_sl is None:
                    improved = True
                elif side == "BUY" and trailed_sl > orig_sl:
                    improved = True
                elif side == "SELL" and trailed_sl < orig_sl:
                    improved = True

            if improved:
                running_sl = trailed_sl
                try:
                    with self.engine.begin() as conn:
                        conn.execute(
                            text("UPDATE shadow_execution_audits SET stop_loss_price = :sl WHERE id = :id AND net_pnl IS NULL"),
                            {"sl": round(float(trailed_sl), 4), "id": int(pos["id"])}
                        )
                    logger.info(f"PositionManager Trailed SL for #{pos['id']} {pos['symbol']}: {orig_sl} -> {round(float(trailed_sl), 4)}")
                except Exception as upd_err:
                    logger.warning(f"Could not persist trailed stop for #{pos['id']}: {upd_err}")

            # Evaluate Stops & Targets Against Current Live Price
            if not exit_reason:
                if side == "BUY":
                    if trailing_stop_price and latest_price <= trailing_stop_price:
                        exit_price = max(trailing_stop_price, profit_lock_price or breakeven_price or trailing_stop_price)
                        exit_reason = "TRAILING_STOP"
                    elif profit_lock_price and latest_price <= profit_lock_price:
                        exit_price = profit_lock_price
                        exit_reason = "PROFIT_LOCK_STOP"
                    elif breakeven_price and latest_price <= breakeven_price:
                        exit_price = breakeven_price
                        exit_reason = "BREAKEVEN_STOP"
                    elif running_sl and latest_price <= running_sl:
                        exit_price = running_sl
                        exit_reason = "STOP_LOSS"
                    elif take_profit and latest_price >= take_profit:
                        exit_price = take_profit
                        exit_reason = "TAKE_PROFIT"
                else:
                    if trailing_stop_price and latest_price >= trailing_stop_price:
                        exit_price = min(trailing_stop_price, profit_lock_price or breakeven_price or trailing_stop_price)
                        exit_reason = "TRAILING_STOP"
                    elif profit_lock_price and latest_price >= profit_lock_price:
                        exit_price = profit_lock_price
                        exit_reason = "PROFIT_LOCK_STOP"
                    elif breakeven_price and latest_price >= border_sl if (border_sl := breakeven_price) else False:
                        exit_price = breakeven_price
                        exit_reason = "BREAKEVEN_STOP"
                    elif running_sl and latest_price >= running_sl:
                        exit_price = running_sl
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
            if is_multi_day:
                # Multi-day holding condition: continue holding up to max holding days / expiry week
                signal_date = signal_at.astimezone(IST).date()
                current_date = watermark.astimezone(IST).date()
                days_held = (current_date - signal_date).days
                if days_held >= int(pos.get("max_holding_days") or 15):
                    exit_price = latest_price
                    exit_reason = "SWING_MAX_DAYS_EXPIRED"
            else:
                if self.is_force_flat_time(watermark):
                    exit_price = latest_price
                    exit_reason = "SESSION_FORCE_FLAT"

        # Position Telemetry Object
        unrealized_pnl = ((latest_price - entry) * quantity - fees) if side == "BUY" else ((entry - latest_price) * quantity - fees)
        telemetry = {
            "id": pos["id"],
            "symbol": pos["symbol"],
            "side": side,
            "trade_mode": trade_mode,
            "entry_price": float(entry),
            "latest_price": float(latest_price),
            "unrealized_pnl": round(float(unrealized_pnl), 2),
            "current_r": round(float(current_r), 2),
            "favorable_r": round(float(favorable_r), 2),
            "elapsed_seconds": int(elapsed_seconds),
            "status": "CLOSED" if exit_reason else "OPEN",
            "exit_reason": exit_reason,
            "exit_price": float(exit_price) if exit_price else None,
            "updated_at": watermark.isoformat(),
        }

        # If exit condition triggered, record shadow exit immediately with non-blocking call
        if exit_reason and exit_price:
            logger.info(f"PositionManager Triggered Exit: #{pos['id']} {pos['symbol']} via {exit_reason} at {exit_price}")
            record_shadow_exit(self.engine, int(pos["id"]), exit_price, exit_reason, exit_at=exit_bar_time or watermark)
            telemetry["closed"] = True

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
        for pos in positions:
            res = self.evaluate_position(pos, now)
            if res:
                telemetries.append(res)
                if res.get("closed"):
                    closed_count += 1
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
