"""Accumulative Swing Index (ASI) and NSE Trap Detection Engine.

Implements Welles Wilder's Accumulative Swing Index to filter out single-candle
wicks and detect institutional liquidity traps (Bull Traps / Bear Traps).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def calculate_asi(candles: List[Dict[str, Any]], limit_move: Optional[float] = None) -> List[Dict[str, Any]]:
    """Calculate Wilder's Swing Index (SI) and Accumulative Swing Index (ASI).

    Expected candle format: Dict with 'open', 'high', 'low', 'close', 'time'.
    """
    if not candles or len(candles) < 2:
        return []

    # Estimate limit move L from average true range if not provided
    if not limit_move or limit_move <= 0:
        first_p = float(candles[0].get("close", 100))
        limit_move = max(0.5, first_p * 0.05)  # 5% default circuit proxy

    asi_series: List[Dict[str, Any]] = []
    accumulated_asi = 0.0

    # Seed the first bar
    asi_series.append({
        "time": candles[0].get("time"),
        "si": 0.0,
        "asi": 0.0,
        "close": float(candles[0]["close"]),
        "high": float(candles[0]["high"]),
        "low": float(candles[0]["low"]),
    })

    for i in range(1, len(candles)):
        c0 = float(candles[i - 1]["close"])
        o0 = float(candles[i - 1]["open"])
        c1 = float(candles[i]["close"])
        o1 = float(candles[i]["open"])
        h1 = float(candles[i]["high"])
        l1 = float(candles[i]["low"])

        a = abs(h1 - c0)
        b = abs(l1 - c0)
        c_diff = abs(h1 - l1)
        d = abs(c0 - o0)

        # Calculate Wilder's R
        if a >= b and a >= c_diff:
            r = a - 0.5 * b + 0.25 * d
        elif b >= a and b >= c_diff:
            r = b - 0.5 * a + 0.25 * d
        else:
            r = c_diff + 0.25 * d

        if r <= 0.00001:
            r = 0.00001

        k = max(a, b)

        # Swing Index calculation
        numerator = (c1 - c0) + 0.5 * (c1 - o1) + 0.25 * (c0 - o0)
        si = 50.0 * (numerator / r) * (k / limit_move)
        accumulated_asi += si

        asi_series.append({
            "time": candles[i].get("time"),
            "si": round(si, 4),
            "asi": round(accumulated_asi, 4),
            "close": c1,
            "high": h1,
            "low": l1,
        })

    return asi_series


def detect_nse_trap(
    candles: List[Dict[str, Any]],
    asi_series: Optional[List[Dict[str, Any]]] = None,
    lookback: int = 15,
) -> Dict[str, Any]:
    """Analyze price vs. ASI swing divergence to detect institutional traps.

    Returns:
        dict with:
            is_trap: bool
            trap_type: Optional[str] ('BULL_TRAP', 'BEAR_TRAP', or None)
            verdict: str ('BULL_TRAP', 'BEAR_TRAP', 'CONFIRMED_BREAKOUT', 'NO_TRAP')
            action: str (e.g. 'VETO_CALL_BUY', 'VETO_PUT_BUY', 'SAFE_TO_EXECUTE')
            details: dict
    """
    if len(candles) < lookback + 2:
        return {"is_trap": False, "trap_type": None, "verdict": "NO_TRAP", "action": "SAFE_TO_EXECUTE"}

    if not asi_series:
        asi_series = calculate_asi(candles)

    if len(asi_series) < lookback + 2:
        return {"is_trap": False, "trap_type": None, "verdict": "NO_TRAP", "action": "SAFE_TO_EXECUTE"}

    recent_candles = candles[-lookback:]
    recent_asi = asi_series[-lookback:]

    curr_p = float(recent_candles[-1]["close"])
    curr_high = float(recent_candles[-1]["high"])
    curr_low = float(recent_candles[-1]["low"])
    curr_asi = recent_asi[-1]["asi"]

    prior_candles = recent_candles[:-2]
    prior_asi = recent_asi[:-2]

    prior_max_p = max(float(c["high"]) for c in prior_candles)
    prior_min_p = min(float(c["low"]) for c in prior_candles)
    prior_max_asi = max(a["asi"] for a in prior_asi)
    prior_min_asi = min(a["asi"] for a in prior_asi)

    # 1. BULL TRAP Check (Liquidity Sweep at Resistance):
    # Price makes higher high or breaks resistance, but ASI fails to confirm (Bearish Divergence)
    if curr_high >= prior_max_p and curr_asi < prior_max_asi:
        asi_divergence = prior_max_asi - curr_asi
        return {
            "is_trap": True,
            "trap_type": "BULL_TRAP",
            "verdict": "BULL_TRAP",
            "action": "VETO_CALL_BUY",
            "reason": f"Price broke high {curr_high:.2f} >= {prior_max_p:.2f} but ASI diverged ({curr_asi:.1f} < {prior_max_asi:.1f})",
            "asi_divergence": round(asi_divergence, 2),
            "current_asi": curr_asi,
            "current_price": curr_p,
        }

    # 2. BEAR TRAP Check (Stop Hunt at Support):
    # Price makes lower low or breaks support, but ASI prints a higher low (Bullish Divergence)
    if curr_low <= prior_min_p and curr_asi > prior_min_asi:
        asi_divergence = curr_asi - prior_min_asi
        return {
            "is_trap": True,
            "trap_type": "BEAR_TRAP",
            "verdict": "BEAR_TRAP",
            "action": "VETO_PUT_BUY",
            "reason": f"Price broke low {curr_low:.2f} <= {prior_min_p:.2f} but ASI held higher low ({curr_asi:.1f} > {prior_min_asi:.1f})",
            "asi_divergence": round(asi_divergence, 2),
            "current_asi": curr_asi,
            "current_price": curr_p,
        }

    # 3. CONFIRMED BREAKOUT Check:
    if curr_high > prior_max_p and curr_asi > prior_max_asi:
        return {
            "is_trap": False,
            "trap_type": None,
            "verdict": "CONFIRMED_BULLISH_BREAKOUT",
            "action": "SAFE_TO_EXECUTE",
            "reason": "Both price and ASI broke resistance cleanly.",
            "current_asi": curr_asi,
            "current_price": curr_p,
        }

    if curr_low < prior_min_p and curr_asi < prior_min_asi:
        return {
            "is_trap": False,
            "trap_type": None,
            "verdict": "CONFIRMED_BEARISH_BREAKOUT",
            "action": "SAFE_TO_EXECUTE",
            "reason": "Both price and ASI broke support cleanly.",
            "current_asi": curr_asi,
            "current_price": curr_p,
        }

    return {
        "is_trap": False,
        "trap_type": None,
        "verdict": "NO_TRAP",
        "action": "SAFE_TO_EXECUTE",
        "current_asi": curr_asi,
        "current_price": curr_p,
    }
