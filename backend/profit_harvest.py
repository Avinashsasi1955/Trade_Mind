"""Profit-Harvest Layer and Reversal Analysis Engine (Phase 2).

Evaluates trades that have reached a significant fraction of their profit target
(e.g. progress >= 0.83, representing 1.25R of a 1.5R target).
When chart exhibits exhaustion or structural reversal against the trade,
this layer tightens the stop loss to lock in accumulated profit or executes
an immediate protective market exit.

All stop updates are strictly monotonic (stops only tighten, never loosen).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nivesh.profit_harvest")


def calculate_progress(
    side: str,
    entry_price: Decimal,
    latest_price: Decimal,
    take_profit: Optional[Decimal],
) -> float:
    """Calculate normalized progress toward target: (price - entry) / (take_profit - entry).

    Returns 0.0 if take_profit is missing or equal to entry.
    """
    if take_profit is None or entry_price is None or latest_price is None:
        return 0.0

    side_upper = str(side).upper()
    if side_upper == "BUY":
        target_dist = take_profit - entry_price
        if target_dist <= Decimal("0"):
            return 0.0
        return float((latest_price - entry_price) / target_dist)
    else:  # SELL / SHORT
        target_dist = entry_price - take_profit
        if target_dist <= Decimal("0"):
            return 0.0
        return float((entry_price - latest_price) / target_dist)


def calculate_harvest_stop(
    side: str,
    entry_price: Decimal,
    latest_price: Decimal,
    current_sl: Optional[Decimal],
    lock_fraction: float = 0.65,
) -> Decimal:
    """Calculate tightened stop locking in lock_fraction of open profit.

    Guarantees monotonic behavior:
    - For BUY: new stop >= current_sl (never moves down).
    - For SELL: new stop <= current_sl (never moves up).
    """
    frac = Decimal(str(lock_fraction))
    side_upper = str(side).upper()

    if side_upper == "BUY":
        open_profit = max(Decimal("0"), latest_price - entry_price)
        proposed = round(entry_price + (open_profit * frac), 4)
        if current_sl is not None:
            return max(current_sl, proposed)
        return proposed
    else:
        open_profit = max(Decimal("0"), entry_price - latest_price)
        proposed = round(entry_price - (open_profit * frac), 4)
        if current_sl is not None:
            return min(current_sl, proposed)
        return proposed


def _calculate_rsi(closes: List[float], period: int = 14) -> List[float]:
    """Calculate standard Relative Strength Index."""
    if len(closes) < period + 1:
        return [50.0] * len(closes)

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_series = [50.0] * period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0.0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi_series.append(rsi)

    return rsi_series


def _calculate_ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    res = [values[0]]
    for v in values[1:]:
        res.append(v * alpha + res[-1] * (1.0 - alpha))
    return res


def compute_reversal_score(
    bars_5m: List[Dict[str, Any]],
    side: str = "BUY",
    bars_15m: Optional[List[Dict[str, Any]]] = None,
    vwap: Optional[float] = None,
    is_option: bool = False,
    underlying_bars: Optional[List[Dict[str, Any]]] = None,
    index_regime_against: bool = False,
    exit_threshold: float = 70.0,
    tighten_threshold: float = 40.0,
) -> Dict[str, Any]:
    """Compute deterministic reversal score (0 to 100) from closed candles.

    Components:
    1. Structure break (BOS / CHoCH against position): 25 pts
    2. Close back across VWAP or EMA9/EMA21 against position: 20 pts
    3. Rejection wick (wick > 2x body) on above-average volume: 20 pts
    4. RSI divergence / overbought rollover: 15 pts
    5. Multi-timeframe flip (15m trend against position): 10 pts
    6. Options IV/theta decay (premium falling while underlying stalls): 15 pts
    7. Index regime flip against trade: 10 pts

    Returns:
        Dict with reversal_score, action ('EXIT', 'TIGHTEN', 'HOLD'), components, and reasons.
    """
    side_upper = str(side).upper()
    total_score = 0.0
    reasons: List[str] = []
    components: Dict[str, Dict[str, Any]] = {}

    if not bars_5m or len(bars_5m) < 3:
        return {
            "reversal_score": 0.0,
            "action": "HOLD",
            "components": {},
            "reasons": ["insufficient_bars"],
        }

    closes = [float(b.get("close") or b.get("close_price") or 0.0) for b in bars_5m]
    highs = [float(b.get("high") or b.get("high_price") or 0.0) for b in bars_5m]
    lows = [float(b.get("low") or b.get("low_price") or 0.0) for b in bars_5m]
    opens = [float(b.get("open") or b.get("open_price") or 0.0) for b in bars_5m]
    volumes = [max(0.0, float(b.get("volume") or 0.0)) for b in bars_5m]

    latest_close = closes[-1]
    latest_open = opens[-1]
    latest_high = highs[-1]
    latest_low = lows[-1]
    latest_vol = volumes[-1]

    # 1. Structure Break (BOS / CHoCH against position) - 25 pts
    struct_active = False
    struct_pts = 0.0
    struct_desc = ""
    try:
        from .strategies.chart_gate import _structure_analysis
        struct_res = _structure_analysis(bars_5m)
        bias = str(struct_res.get("bias", "neutral")).lower()
        if side_upper == "BUY" and bias in ("put", "bear"):
            struct_active = True
            struct_pts = 25.0
            struct_desc = f"bearish structure break ({struct_res.get('reason') or 'CHoCH/BOS'})"
            reasons.append(struct_desc)
        elif side_upper == "SELL" and bias in ("call", "bull"):
            struct_active = True
            struct_pts = 25.0
            struct_desc = f"bullish structure break ({struct_res.get('reason') or 'CHoCH/BOS'})"
            reasons.append(struct_desc)
    except Exception as e:
        logger.debug(f"Structure analysis check bypassed: {e}")

    # Fallback structure check if fewer than 8 bars or chart_gate returned neutral
    if not struct_active and len(closes) >= 3:
        if side_upper == "BUY" and latest_close < min(lows[-3:-1]):
            struct_active = True
            struct_pts = 20.0
            struct_desc = f"close {latest_close:.2f} broke below prior swing low {min(lows[-3:-1]):.2f}"
            reasons.append(struct_desc)
        elif side_upper == "SELL" and latest_close > max(highs[-3:-1]):
            struct_active = True
            struct_pts = 20.0
            struct_desc = f"close {latest_close:.2f} broke above prior swing high {max(highs[-3:-1]):.2f}"
            reasons.append(struct_desc)

    components["structure_break"] = {"active": struct_active, "score": struct_pts, "detail": struct_desc}
    total_score += struct_pts

    # 2. Close Back Across VWAP or EMA9/EMA21 - 20 pts
    vwap_ema_active = False
    vwap_ema_pts = 0.0
    vwap_desc = []

    if vwap is not None and vwap > 0:
        if side_upper == "BUY" and latest_close < vwap:
            vwap_ema_active = True
            vwap_desc.append(f"close {latest_close:.2f} crossed below VWAP {vwap:.2f}")
        elif side_upper == "SELL" and latest_close > vwap:
            vwap_ema_active = True
            vwap_desc.append(f"close {latest_close:.2f} crossed above VWAP {vwap:.2f}")

    if len(closes) >= 9:
        ema9 = _calculate_ema(closes, 9)[-1]
        ema21 = _calculate_ema(closes, 21 if len(closes) >= 21 else len(closes))[-1]
        if side_upper == "BUY" and (latest_close < ema9 or ema9 < ema21):
            vwap_ema_active = True
            vwap_desc.append(f"bearish EMA9/21 cross or close below EMA9 ({latest_close:.2f} < {ema9:.2f})")
        elif side_upper == "SELL" and (latest_close > ema9 or ema9 > ema21):
            vwap_ema_active = True
            vwap_desc.append(f"bullish EMA9/21 cross or close above EMA9 ({latest_close:.2f} > {ema9:.2f})")

    if vwap_ema_active:
        vwap_ema_pts = 20.0
        desc_str = "; ".join(vwap_desc)
        reasons.append(desc_str)
        components["vwap_ema_cross"] = {"active": True, "score": vwap_ema_pts, "detail": desc_str}
        total_score += vwap_ema_pts
    else:
        components["vwap_ema_cross"] = {"active": False, "score": 0.0, "detail": "price holding above VWAP/EMAs"}

    # 3. Rejection Wick (wick > 2x body) on Above-Average Volume - 20 pts
    wick_active = False
    wick_pts = 0.0
    wick_desc = ""

    body = abs(latest_close - latest_open)
    avg_vol = sum(volumes[-10:-1]) / max(1, len(volumes[-10:-1])) if len(volumes) >= 2 else latest_vol
    vol_elevated = latest_vol >= (avg_vol * 1.15) if avg_vol > 0 else True

    if side_upper == "BUY":
        upper_wick = latest_high - max(latest_open, latest_close)
        if upper_wick >= (2.0 * max(0.01, body)) and vol_elevated:
            wick_active = True
            wick_pts = 20.0
            wick_desc = f"bearish rejection upper wick {upper_wick:.2f} > 2x body {body:.2f} on {latest_vol:.0f} volume"
            reasons.append(wick_desc)
    else:
        lower_wick = min(latest_open, latest_close) - latest_low
        if lower_wick >= (2.0 * max(0.01, body)) and vol_elevated:
            wick_active = True
            wick_pts = 20.0
            wick_desc = f"bullish rejection lower wick {lower_wick:.2f} > 2x body {body:.2f} on {latest_vol:.0f} volume"
            reasons.append(wick_desc)

    components["rejection_wick"] = {"active": wick_active, "score": wick_pts, "detail": wick_desc}
    total_score += wick_pts

    # 4. RSI Divergence / Overbought Exhaustion - 15 pts
    rsi_active = False
    rsi_pts = 0.0
    rsi_desc = ""
    rsi_series = _calculate_rsi(closes)
    if len(rsi_series) >= 2:
        curr_rsi = rsi_series[-1]
        prev_rsi = rsi_series[-2]
        if side_upper == "BUY":
            # Bearish divergence or overbought rollover (RSI falling from > 70)
            if (curr_rsi > 70.0 and curr_rsi < prev_rsi) or (curr_rsi < prev_rsi and latest_close > closes[-2]):
                rsi_active = True
                rsi_pts = 15.0
                rsi_desc = f"RSI bearish divergence/rollover ({prev_rsi:.1f} -> {curr_rsi:.1f})"
                reasons.append(rsi_desc)
        else:
            # Bullish divergence or oversold rollover (RSI rising from < 30)
            if (curr_rsi < 30.0 and curr_rsi > prev_rsi) or (curr_rsi > prev_rsi and latest_close < closes[-2]):
                rsi_active = True
                rsi_pts = 15.0
                rsi_desc = f"RSI bullish divergence/bounce ({prev_rsi:.1f} -> {curr_rsi:.1f})"
                reasons.append(rsi_desc)

    components["rsi_divergence"] = {"active": rsi_active, "score": rsi_pts, "detail": rsi_desc}
    total_score += rsi_pts

    # 5. Multi-Timeframe Flip (15m Structure against trade) - 10 pts
    mtf_active = False
    mtf_pts = 0.0
    mtf_desc = ""
    if bars_15m and len(bars_15m) >= 3:
        m15_closes = [float(b.get("close") or b.get("close_price") or 0.0) for b in bars_15m]
        m15_ema = _calculate_ema(m15_closes, 9)[-1]
        if side_upper == "BUY" and m15_closes[-1] < m15_ema:
            mtf_active = True
            mtf_pts = 10.0
            mtf_desc = f"15m candle flipped below EMA9 ({m15_closes[-1]:.2f} < {m15_ema:.2f})"
            reasons.append(mtf_desc)
        elif side_upper == "SELL" and m15_closes[-1] > m15_ema:
            mtf_active = True
            mtf_pts = 10.0
            mtf_desc = f"15m candle flipped above EMA9 ({m15_closes[-1]:.2f} > {m15_ema:.2f})"
            reasons.append(mtf_desc)

    components["mtf_flip"] = {"active": mtf_active, "score": mtf_pts, "detail": mtf_desc}
    total_score += mtf_pts

    # 6. Options IV / Theta Decay (Premium drops while underlying stalls) - 15 pts
    opt_active = False
    opt_pts = 0.0
    opt_desc = ""
    if is_option and underlying_bars and len(underlying_bars) >= 2 and len(closes) >= 2:
        und_prev = float(underlying_bars[-2].get("close") or underlying_bars[-2].get("close_price") or 0.0)
        und_curr = float(underlying_bars[-1].get("close") or underlying_bars[-1].get("close_price") or 0.0)
        opt_ret = (latest_close / closes[-2] - 1.0) if closes[-2] > 0 else 0.0

        if side_upper == "BUY":
            # For Call option: underlying is flat or rising, but option premium dropped
            und_ret = (und_curr / und_prev - 1.0) if und_prev > 0 else 0.0
            if und_ret >= -0.001 and opt_ret < -0.02:
                opt_active = True
                opt_pts = 15.0
                opt_desc = f"option premium fell {opt_ret:.1%} while underlying stalled ({und_ret:+.1%})"
                reasons.append(opt_desc)

    components["option_iv_decay"] = {"active": opt_active, "score": opt_pts, "detail": opt_desc}
    total_score += opt_pts

    # 7. Index Regime Flip Against Trade - 10 pts
    reg_pts = 10.0 if index_regime_against else 0.0
    if index_regime_against:
        reasons.append("broad market index regime flipped against position")
    components["regime_flip"] = {
        "active": index_regime_against,
        "score": reg_pts,
        "detail": "index regime conflict" if index_regime_against else "aligned",
    }
    total_score += reg_pts

    final_score = min(100.0, round(total_score, 1))

    if final_score >= exit_threshold:
        action = "EXIT"
    elif final_score >= tighten_threshold:
        action = "TIGHTEN"
    else:
        action = "HOLD"

    return {
        "reversal_score": final_score,
        "action": action,
        "components": components,
        "reasons": reasons,
    }
