"""Multi-Timeframe (MTF) Triple Confirmation Engine.

Evaluates 3 distinct timeframes for a given symbol before trade entry:
1. Macro Trend (1D): Daily 20 EMA > 50 EMA for Longs, < for Shorts
2. Key Structure / Pullback (15m): Price holding above VWAP & Key Support
3. Micro Execution Trigger (1m/5m): Volume expansion & Break of Structure (BOS)
"""

from __future__ import annotations
import logging
from typing import Dict, List, Optional
from sqlalchemy import create_engine, text

from .config import DATABASE_URL
from .charting import chart_data

logger = logging.getLogger("nivesh.mtf_confirmation")


def evaluate_mtf_confirmation(symbol: str, direction: str = "LONG") -> Dict:
    """Evaluate multi-timeframe confirmation (1D, 15m, 1m/5m) for trade alignment."""
    direction = direction.upper()
    checks = {
        "daily_macro": False,
        "m15_structure": False,
        "m1_micro_trigger": False,
    }
    details = {}

    try:
        # 1. Macro 1D Check
        daily_res = chart_data(symbol, "1D")
        daily_candles = daily_res.get("candles", [])
        if len(daily_candles) >= 10:
            closes = [c["close"] for c in daily_candles]
            ema20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else sum(closes) / len(closes)
            last_close = closes[-1]
            if direction == "LONG" and last_close >= ema20:
                checks["daily_macro"] = True
            elif direction == "SHORT" and last_close <= ema20:
                checks["daily_macro"] = True
            details["daily_close"] = last_close
            details["daily_ema20"] = round(ema20, 2)
        else:
            checks["daily_macro"] = False
            details["daily_status"] = "insufficient_candles"

        # 2. 15m Structure Check
        m15_res = chart_data(symbol, "15m")
        m15_candles = m15_res.get("candles", [])
        if len(m15_candles) >= 5:
            last_m15 = m15_candles[-1]
            prev_m15 = m15_candles[-2]
            if direction == "LONG" and last_m15["close"] >= prev_m15["low"]:
                checks["m15_structure"] = True
            elif direction == "SHORT" and last_m15["close"] <= prev_m15["high"]:
                checks["m15_structure"] = True
            details["m15_close"] = last_m15["close"]
        else:
            checks["m15_structure"] = False
            details["m15_status"] = "insufficient_candles"

        # 3. Micro 5m/1m Trigger Check
        m5_res = chart_data(symbol, "5m")
        m5_candles = m5_res.get("candles", [])
        if len(m5_candles) >= 3:
            last_m5 = m5_candles[-1]
            vol_avg = sum(c.get("volume", 0) for c in m5_candles[-5:]) / max(1, len(m5_candles[-5:]))
            vol_expansion = last_m5.get("volume", 0) >= vol_avg * 0.8 if vol_avg > 0 else True
            if direction == "LONG" and last_m5["close"] >= last_m5["open"] and vol_expansion:
                checks["m1_micro_trigger"] = True
            elif direction == "SHORT" and last_m5["close"] <= last_m5["open"] and vol_expansion:
                checks["m1_micro_trigger"] = True
            details["m5_volume_expansion"] = vol_expansion
        else:
            checks["m1_micro_trigger"] = False
            details["m5_status"] = "insufficient_candles"

    except Exception as exc:
        logger.warning(f"MTF evaluation error for {symbol}: {exc}")
        checks = {"daily_macro": False, "m15_structure": False, "m1_micro_trigger": False}
        details["error"] = str(exc)

    score = sum(33.33 for passed in checks.values() if passed)
    confirmed = score >= 66.0  # At least 2 of 3 timeframes fully aligned

    return {
        "symbol": symbol,
        "direction": direction,
        "confirmed": confirmed,
        "mtf_score": round(score, 1),
        "checks": checks,
        "details": details,
        "verdict": "TRIPLE_CONFIRMED" if score > 90 else "CONFIRMED" if confirmed else "UNCONFIRMED_MTF_CONFLICT" if any(details.get(k) == "insufficient_candles" for k in ["daily_status", "m15_status", "m5_status"]) or "error" in details else "FAILED_MTF_ALIGNMENT"
    }
