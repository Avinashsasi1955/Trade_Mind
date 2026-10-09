"""Institutional Swing Risk Model.

Computes swing-grade wide stops, gap-buffered structural invalidation levels,
fixed risk-% sizing, and underlying-level invalidation stops for options.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nivesh.swing_risk")


def calculate_daily_atr(candles: List[Dict[str, Any]], period: int = 14) -> Decimal:
    """Calculate true Average True Range over daily candles."""
    if not candles or len(candles) < 2:
        return Decimal("1.0")

    trs: List[Decimal] = []
    for i in range(1, len(candles)):
        h = Decimal(str(candles[i].get("high") or candles[i].get("high_price") or 0))
        l = Decimal(str(candles[i].get("low") or candles[i].get("low_price") or 0))
        c_prev = Decimal(str(candles[i - 1].get("close") or candles[i - 1].get("close_price") or 0))

        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)

    subset = trs[-period:] if len(trs) >= period else trs
    if not subset:
        return Decimal("1.0")
    return sum(subset) / Decimal(str(len(subset)))


def compute_swing_risk_parameters(
    symbol: str,
    side: str,
    entry_price: float | Decimal,
    daily_candles: List[Dict[str, Any]],
    atr14: Optional[float | Decimal] = None,
    k: float = 1.5,
    gap_buffer_pct: float = 0.005,
    capital: Optional[float] = 1_000_000.0,
    risk_pct: float = 0.01,
    is_option: bool = False,
    underlying_daily_candles: Optional[List[Dict[str, Any]]] = None,
    option_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Recomputes swing SL/TP from daily structure and ATR(14) with gap buffers and sizing.

    Rules:
    1. Stop = max(structure-based swing low/high, k * daily ATR(14)) + gap buffer.
    2. Sizing = fixed risk-% of capital using the wider stop distance.
    3. Option Invalidation Stop = underlying level where structural thesis breaks.
    4. Default TP = structure-aligned with minimum R:R >= 1.5.
    """
    side = side.upper().strip()
    entry = Decimal(str(entry_price))
    if entry <= 0:
        raise ValueError("Entry price must be positive")

    atr = Decimal(str(atr14)) if atr14 is not None else calculate_daily_atr(daily_candles, 14)
    atr = max(Decimal("0.10"), atr)

    k_dec = Decimal(str(k))
    gap_pct_dec = Decimal(str(gap_buffer_pct))
    atr_stop_distance = k_dec * atr
    gap_buffer = entry * gap_pct_dec

    lookback_bars = daily_candles[-20:] if len(daily_candles) >= 20 else daily_candles
    if not lookback_bars:
        # Fallback if no history provided
        swing_low = entry - atr_stop_distance
        swing_high = entry + atr_stop_distance
    else:
        swing_low = min(Decimal(str(c.get("low") or c.get("low_price") or entry)) for c in lookback_bars)
        swing_high = max(Decimal(str(c.get("high") or c.get("high_price") or entry)) for c in lookback_bars)

    if side == "BUY":
        # Wider stop of structure low or ATR stop, padded with overnight gap buffer
        raw_stop = min(entry - atr_stop_distance, swing_low)
        final_sl = max(Decimal("0.05"), round(raw_stop - gap_buffer, 2))
        stop_distance = max(Decimal("0.05"), entry - final_sl)

        # Structure TP with minimum 1.5 R:R
        structure_tp = swing_high if swing_high > entry else entry + (Decimal("1.5") * stop_distance)
        final_tp = round(max(entry + (Decimal("1.5") * stop_distance), structure_tp), 2)
    else:  # SELL
        raw_stop = max(entry + atr_stop_distance, swing_high)
        final_sl = round(raw_stop + gap_buffer, 2)
        stop_distance = max(Decimal("0.05"), final_sl - entry)

        structure_tp = swing_low if swing_low < entry else entry - (Decimal("1.5") * stop_distance)
        final_tp = max(Decimal("0.05"), round(min(entry - (Decimal("1.5") * stop_distance), structure_tp), 2))

    # Fixed risk capital position sizing
    cap_dec = Decimal(str(capital or 1_000_000.0))
    risk_dec = Decimal(str(risk_pct))
    risk_budget = cap_dec * risk_dec
    calculated_qty = max(1, int(risk_budget / stop_distance))

    # Underlying invalidation level for options
    und_invalidation: Optional[float] = None
    if is_option and underlying_daily_candles:
        und_lookback = underlying_daily_candles[-20:] if len(underlying_daily_candles) >= 20 else underlying_daily_candles
        opt_t = (option_type or ("CE" if side == "BUY" else "PE")).upper()
        if opt_t == "CE":
            # Call option invalidated if underlying breaks below swing low
            u_low = min(Decimal(str(c.get("low") or c.get("low_price") or 0)) for c in und_lookback)
            und_invalidation = float(round(u_low * (Decimal("1.0") - gap_pct_dec), 2))
        else:
            # Put option invalidated if underlying breaks above swing high
            u_high = max(Decimal(str(c.get("high") or c.get("high_price") or 0)) for c in und_lookback)
            und_invalidation = float(round(u_high * (Decimal("1.0") + gap_pct_dec), 2))

    rr_ratio = round(float(abs(final_tp - entry) / stop_distance), 2)

    return {
        "symbol": symbol,
        "trade_mode": "SWING",
        "side": side,
        "entry_price": float(entry),
        "stop_loss_price": float(final_sl),
        "take_profit_price": float(final_tp),
        "quantity": calculated_qty,
        "risk_reward": rr_ratio,
        "stop_distance": float(stop_distance),
        "atr14": float(atr),
        "gap_buffer": float(gap_buffer),
        "underlying_invalidation_level": und_invalidation,
    }
