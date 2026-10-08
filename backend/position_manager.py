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
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
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
            if (now_utc - wm_utc).total_seconds() > 10.0:
                is_historical = True

        # Try redis tick cache first if available and not historical/bypassed
        if self.redis and not bypass_cache and not is_historical:
            try:
                cached_price = self.redis.hget("nivesh:ticks:latest", str(instrument_id))
                if cached_price:
                    val = Decimal(str(cached_price))
                    return (val, "market") if return_source else val
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
                val = Decimal(str(row["close_price"]))
                return (val, "market") if return_source else val

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
                            SELECT close_price FROM live_market_bars
                            WHERE instrument_id = :und_id
                              AND interval IN ('1second', '1minute', '5minute', 'day')
                              AND bar_time <= :watermark
                              AND bar_time >= :watermark - INTERVAL '15 minutes'
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

                        # Calibrate model mark against entry fill ratio
                        scale_ratio = 1.0
                        if entry_price and float(entry_price) > 0 and signal_at:
                            entry_spot_row = conn.execute(
                                text("""
                                    SELECT close_price FROM live_market_bars
                                    WHERE instrument_id = :und_id
                                      AND interval IN ('1second', '1minute', '5minute', 'day')
                                      AND bar_time <= :sig_at
                                      AND source = ANY(:sources)
                                    ORDER BY bar_time DESC LIMIT 1
                                """),
                                {"und_id": und_id, "sig_at": signal_at, "sources": list(TRADE_BAR_SOURCES)}
                            ).mappings().one_or_none()
                            if entry_spot_row and entry_spot_row["close_price"]:
                                sig_date = (signal_at.astimezone(IST) if getattr(signal_at, "tzinfo", None) else signal_at).date() if hasattr(signal_at, "date") else signal_at
                                sig_dte = max(0.5, float((opt_meta["expiry"] - sig_date).days)) if opt_meta.get("expiry") else 4.0
                                entry_bs = calculate_black_scholes_greeks(float(entry_spot_row["close_price"]), strike, sig_dte, iv=0.145, option_type=opt_meta["instrument_type"])["price"]
                                if entry_bs > 0.05:
                                    scale_ratio = max(0.2, min(5.0, float(entry_price) / entry_bs))
                        val = Decimal(str(max(0.05, round(greeks["price"] * scale_ratio, 2))))
                        return (val, "synthetic") if return_source else val
        return (None, None) if return_source else None

    def get_exit_bars(self, instrument_id: int, signal_at: datetime, watermark: datetime,
                      entry_price: Optional[Decimal] = None, is_multi_day: bool = False) -> List[Dict]:
        """Fetch historical bars since trade entry for high-precision exit checks."""
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
                if rows:
                    def _ts(v):
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
                            text("""
                                SELECT bar_time, high_price, low_price, close_price, volume
                                FROM live_market_bars
                                WHERE instrument_id = :und_id
                                  AND interval = :interval
                                  AND source = ANY(:sources)
                                  AND bar_time >= :signal_at AND bar_time <= :watermark
                                ORDER BY bar_time ASC
                            """),
                            {"und_id": und_id, "interval": und_interval, "sources": list(TRADE_BAR_SOURCES), "signal_at": signal_at, "watermark": watermark}
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
                        g_high = calculate_black_scholes_greeks(float(ub["high_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_low = calculate_black_scholes_greeks(float(ub["low_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        g_close = calculate_black_scholes_greeks(float(ub["close_price"]), strike, dte, iv=0.145, option_type=opt_meta["instrument_type"])
                        h_p = max(0.05, round(max(g_high["price"], g_low["price"]) * scale_ratio, 2))
                        l_p = max(0.05, round(min(g_high["price"], g_low["price"]) * scale_ratio, 2))
                        c_p = max(0.05, round(g_close["price"] * scale_ratio, 2))
                        opt_bars.append({
                            "bar_time": ub["bar_time"],
                            "high_price": h_p,
                            "low_price": l_p,
                            "close_price": c_p,
                            "volume": ub["volume"],
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
        except Exception:
            pass

        entry = Decimal(str(pos["theoretical_fill_price"]))
        signal_at = pos["signal_at"]
        if signal_at.tzinfo is None:
            signal_at = signal_at.replace(tzinfo=timezone.utc)
        if watermark.tzinfo is None:
            watermark = watermark.replace(tzinfo=timezone.utc)

        price_res = self.get_latest_price(pos["instrument_id"], watermark, entry_price=entry, signal_at=signal_at, return_source=True)
        if isinstance(price_res, tuple):
            latest_price, price_source = price_res
        else:
            latest_price, price_source = price_res, "market"
        if latest_price is None or latest_price <= 0:
            return None

        side = str(pos["side"]).upper()
        trade_mode = str(pos.get("trade_mode") or "INTRADAY").upper()
        quantity = Decimal(str(int(pos.get("quantity") or 1)))
        fees = Decimal(str(pos.get("estimated_fees") or 0))

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
                except Exception:
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
                except Exception:
                    pass

        # Recover trailed stop from separate storage if unparsed
        if self.redis and not note_dict.get("trailed_stop_price"):
            try:
                sep_val = self.redis.get(f"nivesh:trailed_stop:{pos['id']}")
                if sep_val:
                    note_dict["trailed_stop_price"] = float(sep_val)
            except Exception:
                pass

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

        # Multi-day holding invariant: swing trades stay open and mature
        is_multi_day = trade_mode in ("SWING", "POSITIONAL")

        # 3. Bar history for excursion analysis
        bars = self.get_exit_bars(pos["instrument_id"], signal_at, watermark, entry_price=entry, is_multi_day=is_multi_day)
        best_favourable = entry
        worst_adverse = entry

        exit_price: Optional[Decimal] = None
        exit_reason: Optional[str] = None
        exit_bar_time: Optional[datetime] = None

        # If this is a paired hedge leg, verify whether primary leg has already closed
        if is_hedge_leg and paired_primary_audit_id:
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
                            except Exception:
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
        # Chronological Bar-by-Bar Replay (Guarantees Perfect Execution Even After Offline/Reboots)
        # -------------------------------------------------------------
        best_favourable = entry
        worst_adverse = entry
        running_sl = initial_sl or stop_loss

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

                    # 1. Take Profit hit on this bar
                    if take_profit and h >= take_profit:
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

                    # 3. Stop Loss or Trailed Stop hit on this bar
                    if running_sl and l <= running_sl:
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

                    # 1. Take Profit hit on this bar
                    if take_profit and l <= take_profit:
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

                    # 3. Stop Loss or Trailed Stop hit on this bar
                    if running_sl and h >= running_sl:
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
                    reason_tag = "PROFIT_TRAILING_LOCK" if (trailed_sl > entry if side == "BUY" else trailed_sl < entry) else "TRAILING_STOP_ACTIVE"
                    note_dict["risk_manager"] = {
                        "old_sl": old_sl_flt,
                        "new_sl": sl_flt,
                        "reasons": [reason_tag],
                        "latest": round(float(latest_price), 2),
                    }
                    if self.redis:
                        try:
                            self.redis.set(f"nivesh:trailed_stop:{pos['id']}", str(sl_flt), ex=86400)
                        except Exception:
                            pass
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
                    if side == "BUY":
                        if trailing_stop_price and latest_price <= trailing_stop_price:
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
                        if trailing_stop_price and latest_price >= trailing_stop_price:
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
            if is_multi_day:
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
                        except Exception:
                            pass
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
                            except Exception:
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
                                    except Exception:
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
                                    except Exception:
                                        pass

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
                                except Exception:
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
