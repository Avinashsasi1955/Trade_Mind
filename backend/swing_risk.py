"""Institutional Swing Risk Model.

Computes swing-grade wide stops, gap-buffered structural invalidation levels,
fixed risk-% sizing with position-value caps, and underlying-level invalidation stops for options.
Fails closed: rejects setups with insufficient data or excessive stop distance.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nivesh.swing_risk")

SWING_REJECTED_THIN_HISTORY = "SWING_REJECTED_THIN_HISTORY"
SWING_REJECTED_ATR_CALCULATION = "SWING_REJECTED_ATR_CALCULATION"
SWING_REJECTED_STOP_CAP_EXCEEDED = "SWING_REJECTED_STOP_CAP_EXCEEDED"
SWING_REJECTED_ZERO_QUANTITY = "SWING_REJECTED_ZERO_QUANTITY"


def calculate_daily_atr(candles: List[Dict[str, Any]], period: int = 14) -> Optional[Decimal]:
    """Calculate true Average True Range over daily candles.

    Fails closed: returns None if fewer than 15 daily candles are available.
    """
    if not candles or len(candles) < 15:
        return None

    trs: List[Decimal] = []
    for i in range(1, len(candles)):
        h = Decimal(str(candles[i].get("high") or candles[i].get("high_price") or 0))
        l = Decimal(str(candles[i].get("low") or candles[i].get("low_price") or 0))
        c_prev = Decimal(str(candles[i - 1].get("close") or candles[i - 1].get("close_price") or 0))

        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)

    subset = trs[-period:] if len(trs) >= period else trs
    if not subset:
        return None
    return sum(subset) / Decimal(str(len(subset)))


def compute_swing_risk_parameters(
    symbol: str,
    side: str,
    entry_price: float | Decimal,
    daily_candles: Optional[List[Dict[str, Any]]] = None,
    atr14: Optional[float | Decimal] = None,
    k: float = 1.5,
    gap_buffer_pct: float = 0.005,
    capital: Optional[float] = None,
    risk_pct: Optional[float] = None,
    is_option: bool = False,
    underlying_daily_candles: Optional[List[Dict[str, Any]]] = None,
    option_type: Optional[str] = None,
    delta: Optional[float | Decimal] = None,
    lot_size: int = 1,
    max_stop_atr: Optional[float] = None,
    max_position_pct: Optional[float] = None,
    return_reason: bool = False,
) -> Any:
    """Recomputes swing SL/TP from daily structure and ATR(14) with gap buffers and sizing.

    Rules:
    1. Stop = max(structure-based swing low/high, k * daily ATR(14)) + gap buffer.
    2. Capped at NIVESH_SWING_MAX_STOP_ATR * ATR (default 3x). If structural stop is wider,
       rejects the swing setup (returns None).
    3. Target from capped stop with minimum 1.5 R:R (does not push to 20-day high).
    4. Sizing: fixed risk-% of capital, capped by NIVESH_SWING_MAX_POSITION_PCT (default 20%).
       Respects option lot size and margin.
    5. Options: computed from underlying's daily ATR and structure, converted via delta,
       and enforces underlying invalidation level.
    6. Minimum 15 daily candles required. Returns None if history is insufficient.
    """
    side = side.upper().strip()
    entry = Decimal(str(entry_price))
    if entry <= 0:
        raise ValueError("Entry price must be positive")

    # Load configuration defaults
    if max_stop_atr is None:
        max_stop_atr = float(os.getenv("NIVESH_SWING_MAX_STOP_ATR", "3.0"))
    if max_position_pct is None:
        max_position_pct = float(os.getenv("NIVESH_SWING_MAX_POSITION_PCT", "0.20"))
    if max_position_pct > 1.0:
        max_position_pct = max_position_pct / 100.0

    if capital is None:
        from .config import DEFAULT_CAPITAL
        capital = float(os.getenv("NIVESH_SWING_CAPITAL", os.getenv("NIVESH_PAPER_NOTIONAL", str(DEFAULT_CAPITAL))))

    if risk_pct is None:
        risk_pct = float(os.getenv("NIVESH_SWING_RISK_PCT", "0.01"))
    if risk_pct > 1.0:
        risk_pct = risk_pct / 100.0

    k_dec = Decimal(str(k))
    gap_pct_dec = Decimal(str(gap_buffer_pct))
    max_stop_atr_dec = Decimal(str(max_stop_atr))

    und_invalidation: Optional[float] = None

    def _reject(reason: str):
        return (None, reason) if return_reason else None

    if is_option:
        # Option contracts: must have at least 15 daily candles on the underlying
        if not underlying_daily_candles or len(underlying_daily_candles) < 15:
            logger.warning(
                f"Rejecting SWING option setup for {symbol}: insufficient underlying daily history "
                f"({len(underlying_daily_candles) if underlying_daily_candles else 0} < 15 candles)"
            )
            return _reject(SWING_REJECTED_THIN_HISTORY)

        und_atr = calculate_daily_atr(underlying_daily_candles, 14)
        if und_atr is None:
            logger.warning(f"Rejecting SWING option setup for {symbol}: unable to compute underlying daily ATR")
            return _reject(SWING_REJECTED_ATR_CALCULATION)

        und_lookback = underlying_daily_candles[-20:] if len(underlying_daily_candles) >= 20 else underlying_daily_candles
        und_spot = Decimal(str(underlying_daily_candles[-1].get("close") or underlying_daily_candles[-1].get("close_price") or entry))
        opt_t = (option_type or ("CE" if side == "BUY" else "PE")).upper()

        if opt_t == "CE":
            u_low = min(Decimal(str(c.get("low") or c.get("low_price") or und_spot)) for c in und_lookback)
            und_inval_dec = round(u_low * (Decimal("1.0") - gap_pct_dec), 2)
            und_struct_dist = max(Decimal("0.0"), und_spot - und_inval_dec)
            und_atr_dist = k_dec * und_atr
            und_raw_stop = max(und_atr_dist, und_struct_dist)
        else:  # PE
            u_high = max(Decimal(str(c.get("high") or c.get("high_price") or und_spot)) for c in und_lookback)
            und_inval_dec = round(u_high * (Decimal("1.0") + gap_pct_dec), 2)
            und_struct_dist = max(Decimal("0.0"), und_inval_dec - und_spot)
            und_atr_dist = k_dec * und_atr
            und_raw_stop = max(und_atr_dist, und_struct_dist)

        und_invalidation = float(und_inval_dec)

        # Cap stop distance at max_stop_atr * ATR on underlying
        max_allowed_und_stop = max_stop_atr_dec * und_atr
        if und_raw_stop > max_allowed_und_stop:
            logger.warning(
                f"Rejecting SWING option setup for {symbol}: underlying structural stop {und_raw_stop:.2f} "
                f"exceeds max allowed {max_allowed_und_stop:.2f} ({max_stop_atr}x ATR)"
            )
            return _reject(SWING_REJECTED_STOP_CAP_EXCEEDED)

        # Convert underlying stop distance to option premium via delta
        approx_delta = abs(Decimal(str(delta))) if delta is not None and float(delta) > 0 else Decimal("0.50")
        opt_stop_dist = max(Decimal("0.05"), und_raw_stop * approx_delta)

        final_sl = max(Decimal("0.05"), round(entry - opt_stop_dist, 2))
        stop_distance = max(Decimal("0.05"), entry - final_sl)
        final_tp = round(entry + (Decimal("1.5") * stop_distance), 2)
        effective_atr = und_atr

    else:
        # Non-options (Equity / Futures): requires at least 15 daily candles
        if not daily_candles or len(daily_candles) < 15:
            logger.warning(
                f"Rejecting SWING setup for {symbol}: insufficient daily history "
                f"({len(daily_candles) if daily_candles else 0} < 15 candles)"
            )
            return _reject(SWING_REJECTED_THIN_HISTORY)

        atr = Decimal(str(atr14)) if atr14 is not None else calculate_daily_atr(daily_candles, 14)
        if atr is None:
            logger.warning(f"Rejecting SWING setup for {symbol}: unable to compute daily ATR")
            return _reject(SWING_REJECTED_ATR_CALCULATION)

        atr = max(Decimal("0.10"), atr)
        effective_atr = atr
        max_allowed_stop = max_stop_atr_dec * atr
        atr_stop_distance = k_dec * atr
        gap_buffer = entry * gap_pct_dec

        lookback_bars = daily_candles[-20:] if len(daily_candles) >= 20 else daily_candles
        swing_low = min(Decimal(str(c.get("low") or c.get("low_price") or entry)) for c in lookback_bars)
        swing_high = max(Decimal(str(c.get("high") or c.get("high_price") or entry)) for c in lookback_bars)

        if side == "BUY":
            raw_stop = min(entry - atr_stop_distance, swing_low)
            raw_stop_dist = entry - (raw_stop - gap_buffer)
            if raw_stop_dist > max_allowed_stop:
                logger.warning(
                    f"Rejecting SWING setup for {symbol}: structural stop distance {raw_stop_dist:.2f} "
                    f"exceeds max allowed {max_allowed_stop:.2f} ({max_stop_atr}x ATR)"
                )
                return _reject(SWING_REJECTED_STOP_CAP_EXCEEDED)
            final_sl = max(Decimal("0.05"), round(entry - raw_stop_dist, 2))
            stop_distance = max(Decimal("0.05"), entry - final_sl)
            final_tp = round(entry + (Decimal("1.5") * stop_distance), 2)
        else:  # SELL
            raw_stop = max(entry + atr_stop_distance, swing_high)
            raw_stop_dist = (raw_stop + gap_buffer) - entry
            if raw_stop_dist > max_allowed_stop:
                logger.warning(
                    f"Rejecting SWING setup for {symbol}: structural stop distance {raw_stop_dist:.2f} "
                    f"exceeds max allowed {max_allowed_stop:.2f} ({max_stop_atr}x ATR)"
                )
                return _reject(SWING_REJECTED_STOP_CAP_EXCEEDED)
            final_sl = round(entry + raw_stop_dist, 2)
            stop_distance = max(Decimal("0.05"), final_sl - entry)
            final_tp = max(Decimal("0.05"), round(entry - (Decimal("1.5") * stop_distance), 2))

    # Sizing: Fixed risk % of capital, capped by NIVESH_SWING_MAX_POSITION_PCT
    cap_dec = Decimal(str(capital))
    risk_budget = cap_dec * Decimal(str(risk_pct))
    max_position_val = cap_dec * Decimal(str(max_position_pct))

    risk_qty = int(risk_budget / stop_distance)
    pos_cap_qty = int(max_position_val / entry)
    target_qty = min(risk_qty, pos_cap_qty)

    lot = max(1, int(lot_size or 1))
    if is_option:
        lots = target_qty // lot
        final_qty = lots * lot
        if final_qty < lot:
            logger.warning(
                f"Rejecting SWING option setup for {symbol}: sizing ({target_qty}) is less than 1 lot ({lot}). "
                f"Risk budget: {risk_budget:.2f}, Max position value: {max_position_val:.2f}"
            )
            return _reject(SWING_REJECTED_ZERO_QUANTITY)
    else:
        final_qty = target_qty
        if final_qty <= 0:
            logger.warning(
                f"Rejecting SWING setup for {symbol}: sizing yielded 0 quantity. "
                f"Risk budget: {risk_budget:.2f}, Max position value: {max_position_val:.2f}"
            )
            return _reject(SWING_REJECTED_ZERO_QUANTITY)

    rr_ratio = round(float(abs(final_tp - entry) / stop_distance), 2)

    res = {
        "symbol": symbol,
        "trade_mode": "SWING",
        "side": side,
        "entry_price": float(entry),
        "stop_loss_price": float(final_sl),
        "take_profit_price": float(final_tp),
        "quantity": final_qty,
        "risk_reward": rr_ratio,
        "stop_distance": float(stop_distance),
        "atr14": float(effective_atr),
        "underlying_invalidation_level": und_invalidation,
    }
    return (res, None) if return_reason else res
