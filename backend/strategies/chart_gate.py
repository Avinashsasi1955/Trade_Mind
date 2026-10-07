"""Deterministic chart gates and strategy pattern evaluators.

Separates strategy recognition (Trend, Pullback, Range Bounce, Trap Reversal)
from execution orchestration in LivePaperInference.
"""
import math
import sys
from typing import Dict, List, Optional, Tuple, Any

from backend.asi_indicator import detect_nse_trap
from backend.volume_profile import calculate_volume_profile, evaluate_volume_profile_verdict


def _detect_trap(bars: List[Dict]) -> Dict:
    li = sys.modules.get("backend.live_inference")
    fn = getattr(li, "detect_nse_trap", None) if li else None
    if fn:
        return fn(bars)
    return detect_nse_trap(bars)


def _calc_volume_profile(bars: List[Dict]) -> Dict:
    li = sys.modules.get("backend.live_inference")
    fn = getattr(li, "calculate_volume_profile", None) if li else None
    if fn:
        return fn(bars)
    return calculate_volume_profile(bars)


def _eval_volume_profile(last: float, signal: int, vp_data: Dict) -> Dict:
    li = sys.modules.get("backend.live_inference")
    fn = getattr(li, "evaluate_volume_profile_verdict", None) if li else None
    if fn:
        return fn(last, signal, vp_data)
    return evaluate_volume_profile_verdict(last, signal, vp_data)


def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def _structure_analysis(bars: List[Dict]) -> Dict:
    """Backend equivalent of the chart Structure overlay.

    The frontend draws BOS/CHoCH, swing highs/lows and CALL/PUT zones.  The
    Senior layer needs the same information as data, so this deterministic
    parser converts completed candles into auditable structure features.
    """
    if len(bars) < 8:
        return {"accepted":False,"bias":"neutral","reason":"insufficient candles for BOS/CHoCH structure"}
    highs=[float(bar["high"]) for bar in bars]
    lows=[float(bar["low"]) for bar in bars]
    closes=[float(bar["close"]) for bar in bars]
    volumes=[max(0,int(bar.get("volume") or 0)) for bar in bars]
    pivots=[]
    for idx in range(2,len(bars)-2):
        if all(highs[idx]>=highs[j] for j in (idx-2,idx-1)) and all(highs[idx]>highs[j] for j in (idx+1,idx+2)):
            pivots.append({"type":"H","index":idx,"price":highs[idx]})
        if all(lows[idx]<=lows[j] for j in (idx-2,idx-1)) and all(lows[idx]<lows[j] for j in (idx+1,idx+2)):
            pivots.append({"type":"L","index":idx,"price":lows[idx]})
    last_high=last_low=None
    trend=0
    events=[]
    for idx,close in enumerate(closes):
        for pivot in [p for p in pivots if p["index"]==idx]:
            if pivot["type"]=="H": last_high=pivot
            else: last_low=pivot
        if last_high and idx>last_high["index"]+1 and close>last_high["price"]:
            events.append({"kind":"CHoCH" if trend<0 else "BOS","side":"bull","index":idx,"level":last_high["price"]})
            trend=1; last_high=None
        if last_low and idx>last_low["index"]+1 and close<last_low["price"]:
            events.append({"kind":"CHoCH" if trend>0 else "BOS","side":"bear","index":idx,"level":last_low["price"]})
            trend=-1; last_low=None
    recent_event=events[-1] if events else None
    ema_fast=_ema(closes,5)[-1]
    ema_mid=_ema(closes,13 if len(closes)>=13 else max(6,len(closes)//2))[-1]
    recent_base=closes[-4] if len(closes)>=4 else closes[0]
    recent_move=(closes[-1]/recent_base-1) if recent_base else 0
    avg_volume=sum(volumes[-12:-1])/max(1,len(volumes[-12:-1]))
    volume_ratio=(volumes[-1]/avg_volume) if avg_volume else 1.0
    bias="neutral"
    direction=0
    reasons=[]
    if recent_event and recent_event["side"]=="bull":
        bias="call"; direction=1; reasons.append(f"{recent_event['kind']} bullish structure break")
    elif recent_event and recent_event["side"]=="bear":
        bias="put"; direction=-1; reasons.append(f"{recent_event['kind']} bearish structure break")
    elif closes[-1]>=ema_fast>=ema_mid and recent_move>0:
        bias="call"; direction=1; reasons.append("bullish EMA structure with positive recent move")
    elif closes[-1]<=ema_fast<=ema_mid and recent_move<0:
        bias="put"; direction=-1; reasons.append("bearish EMA structure with negative recent move")
    if volume_ratio<0.55:
        reasons.append("weak structure volume")
    recent_highs=[p for p in pivots if p["type"]=="H"][-2:]
    recent_lows=[p for p in pivots if p["type"]=="L"][-2:]
    confidence=45
    if recent_event: confidence+=22
    if abs(recent_move)>=0.001: confidence+=10
    if volume_ratio>=0.9: confidence+=10
    if (direction>0 and closes[-1]>=ema_fast>=ema_mid) or (direction<0 and closes[-1]<=ema_fast<=ema_mid): confidence+=13
    confidence=max(0,min(100,confidence if direction else 35))
    return {"accepted":direction!=0 and confidence>=60,"bias":bias,"direction":direction,
            "confidence":round(confidence,2),"recent_event":recent_event,
            "events":events[-5:],"swing_highs":recent_highs,"swing_lows":recent_lows,
            "volume_ratio":round(volume_ratio,3),"recent_move_pct":round(recent_move*100,4),
            "call_zone":bias=="call","put_zone":bias=="put",
            "reason":"; ".join(reasons) if reasons else "no clear BOS/CHoCH or trend structure"}


def _timeframe_direction(bars: List[Dict], min_bars: int) -> Dict:
    if len(bars) < min_bars:
        return {"status": "insufficient", "accepted": False, "direction": 0,
                "bias": "neutral", "confidence": 0, "bars": len(bars),
                "reason": f"needs {min_bars} bars"}
    if len(bars) >= 8:
        structure = _structure_analysis(bars)
        return {**structure, "status": "ready", "bars": len(bars)}
    closes = [float(bar["close"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    first = float(bars[0].get("open") or closes[0])
    last = closes[-1]
    move = (last / first - 1) if first else 0
    range_pct = ((max(highs) - min(lows)) / last) if last else 0
    direction = 1 if move > 0.0008 else -1 if move < -0.0008 else 0
    confidence = min(100, 42 + abs(move) * 10000 + range_pct * 1200) if direction else 35
    return {"status": "ready", "accepted": direction != 0 and confidence >= 52,
            "direction": direction, "bias": "call" if direction > 0 else "put" if direction < 0 else "neutral",
            "confidence": round(confidence, 2), "bars": len(bars),
            "recent_move_pct": round(move * 100, 4),
            "reason": "compact timeframe directional move" if direction else "compact timeframe neutral"}


def _instrument_local_direction(item: Dict) -> Dict:
    """Resolve CALL/PUT bias from this instrument's own candles.

    This is intentionally independent from the broad session case and from a
    weak model probability.  When the model is neutral, the Senior/learning
    layers must not turn every stock into the same BUY/PUT just because the
    active model is clustered around 0.50.  Each stock gets its own direction
    from BOS/CHoCH, EMA trend, breakout/pullback and recent candle behaviour.
    """
    bars = item.get("intraday") or []
    if len(bars) < 4:
        return {"direction": 0, "bias": "neutral", "confidence": 0,
                "reason": "not enough local candles"}
    structure = _structure_analysis(bars)
    if structure.get("accepted") and int(structure.get("direction") or 0):
        return {"direction": int(structure["direction"]), "bias": structure.get("bias"),
                "confidence": float(structure.get("confidence") or 0),
                "reason": "local BOS/CHoCH structure", "structure": structure}
    closes = [float(bar["close"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    volumes = [max(0, int(bar.get("volume") or 0)) for bar in bars]
    last = closes[-1]
    previous = closes[-2]
    ema_fast = _ema(closes, 5)[-1]
    ema_mid = _ema(closes, 10 if len(closes) >= 10 else max(4, len(closes)//2))[-1]
    lookback = min(10, len(bars))
    prior_high = max(highs[-lookback:-1]) if lookback > 1 else highs[-1]
    prior_low = min(lows[-lookback:-1]) if lookback > 1 else lows[-1]
    recent_base = closes[-4] if len(closes) >= 4 else closes[0]
    recent_move = (last / recent_base - 1) if recent_base else 0
    avg_volume = sum(volumes[-lookback:-1]) / max(1, len(volumes[-lookback:-1]))
    volume_ratio = (volumes[-1] / avg_volume) if avg_volume else 1.0
    trend_up = last >= ema_fast >= ema_mid and recent_move >= 0
    trend_down = last <= ema_fast <= ema_mid and recent_move <= 0
    breakout_up = last > prior_high and volume_ratio >= 0.7
    breakout_down = last < prior_low and volume_ratio >= 0.7
    pullback_up = trend_up and lows[-1] <= ema_fast <= last and last >= previous
    pullback_down = trend_down and highs[-1] >= ema_fast >= last and last <= previous
    range_mid = (prior_high + prior_low) / 2.0
    range_bounce_up = (last <= range_mid) and (last <= prior_low * 1.004 or (lows[-1] <= prior_low and last >= lows[-1])) and last >= previous
    range_reject_down = (last >= range_mid) and (last >= prior_high * 0.996 or (highs[-1] >= prior_high and last <= highs[-1])) and last <= previous
    direction = 1 if (breakout_up or pullback_up or (trend_up and recent_move > 0.0006) or range_bounce_up) else -1 if (breakout_down or pullback_down or (trend_down and recent_move < -0.0006) or range_reject_down) else 0
    confidence = 0
    reasons = []
    if range_bounce_up and direction > 0:
        confidence = 50
        reasons.append("local range support bounce")
    elif range_reject_down and direction < 0:
        confidence = 50
        reasons.append("local range resistance reject")
    elif direction:
        confidence = 44
        confidence += 14 if abs(recent_move) >= 0.001 else 7
        confidence += 12 if volume_ratio >= 0.9 else 5
        confidence += 12 if breakout_up or breakout_down else 9 if pullback_up or pullback_down else 5
        confidence += 8 if (direction > 0 and trend_up) or (direction < 0 and trend_down) else 0
        reasons.append("local breakout/pullback/trend direction")
    else:
        reasons.append("local chart neutral")
    return {"direction": direction, "bias": "call" if direction > 0 else "put" if direction < 0 else "neutral",
            "confidence": round(min(100, confidence), 2), "recent_move_pct": round(recent_move * 100, 4),
            "volume_ratio": round(volume_ratio, 3), "reason": "; ".join(reasons),
            "structure": structure}


def _chart_strategy_gate(item: Dict, signal: int) -> Dict:
    """Deterministic paper-trading chart gate.

    The ML model supplies direction/edge. This gate asks whether the live chart
    is actually tradeable: trend, breakout/reversion context, volume, and a
    minimum risk/reward profile. Adapts strategy to market regime (Trend, Range/Chop,
    and Institutional Trap Reversals) instead of blocking when conditions oscillate.
    """
    bars = item.get("intraday") or []
    if len(bars) < 12:
        return {"accepted": False, "reason": "less than 12 completed 5m live candles", "strategy": "NO_TRADE"}
    local_direction = _instrument_local_direction(item)
    if not signal:
        signal = int(local_direction.get("direction") or 0)
    if not signal:
        return {"accepted": False, "reason": "no instrument-local CALL/PUT direction", "strategy": "NO_TRADE",
                "local_direction": local_direction}
    closes = [float(bar["close"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    volumes = [max(0, int(bar.get("volume") or 0)) for bar in bars]
    ema_fast = _ema(closes, 8)
    ema_slow = _ema(closes, 21 if len(closes) >= 21 else max(9, len(closes)//2))
    last = closes[-1]
    previous = closes[-2]
    prior_high = max(highs[-10:-1])
    prior_low = min(lows[-10:-1])
    avg_volume = sum(volumes[-10:-1]) / max(1, len(volumes[-10:-1]))
    volume_ok = volumes[-1] >= avg_volume * .75 if avg_volume else True
    atr = sum((high - low) for high, low in zip(highs[-10:], lows[-10:])) / 10
    if atr <= 0:
        return {"accepted": False, "reason": "zero intraday range", "strategy": "NO_TRADE"}

    # True Range & Directional Movement (ADX proxy)
    dx_vals = []
    for i in range(1, len(bars)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        if tr > 0:
            dx = abs(plus_dm - minus_dm) / tr * 100.0
            dx_vals.append(dx)
    adx = (sum(dx_vals[-7:]) / len(dx_vals[-7:])) if dx_vals else 20.0
    is_choppy = adx < 18.0

    structure = _structure_analysis(bars)
    has_structure_break = bool(structure.get("recent_event"))
    if not is_choppy and has_structure_break and structure.get("accepted") and signal and structure.get("direction") and structure["direction"] != signal:
        return {"accepted": False, "reason": "ML direction conflicts with BOS/CHoCH structure",
                "strategy": "NO_TRADE", "structure": structure}

    # Regime & Market Structure Analytics: Intraday VWAP & ADX Trend Strength
    total_pv = sum(((float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0) * max(1, int(b.get("volume") or 1)) for b in bars)
    total_vol = sum(max(1, int(b.get("volume") or 1)) for b in bars)
    vwap = total_pv / total_vol if total_vol > 0 else last

    # Wyckoff Intra-Bar Volume Authenticity Indicator (True vs Fake Candles)
    rvol = volumes[-1] / max(1, avg_volume) if avg_volume else 1.0
    candle_spread = highs[-1] - lows[-1]
    is_wide_spread = candle_spread >= atr * 1.5
    if is_wide_spread and rvol < 0.85:
        if closes[-1] > (highs[-1] + lows[-1]) / 2 and signal > 0:
            return {"accepted": False, "reason": f"Wyckoff Effort-vs-Result divergence: wide bullish spread with low volume (RVOL {rvol:.2f}x < 0.85x), fake candle exhaustion trap",
                    "strategy": "NO_TRADE", "local_direction": local_direction}
        elif closes[-1] < (highs[-1] + lows[-1]) / 2 and signal < 0:
            return {"accepted": False, "reason": f"Wyckoff Effort-vs-Result divergence: wide bearish spread with low volume (RVOL {rvol:.2f}x < 0.85x), fake breakdown exhaustion trap",
                    "strategy": "NO_TRADE", "local_direction": local_direction}

    # Anti-Chasing Overextension Guard: Volatility-adaptive threshold
    vwap_distance_pct = (last - vwap) / vwap
    multiplier = 2.0 if (not is_choppy and rvol >= 1.2) else 1.5
    max_vwap_dist = max(0.008, min(0.035, (atr / max(1e-9, last)) * multiplier))
    if signal > 0 and vwap_distance_pct > max_vwap_dist:
        return {"accepted": False, "reason": f"overextended above VWAP (+{vwap_distance_pct*100:.2f}% > +{max_vwap_dist*100:.2f}%), high probability pullback trap",
                "strategy": "NO_TRADE", "local_direction": local_direction}
    if signal < 0 and vwap_distance_pct < -max_vwap_dist:
        return {"accepted": False, "reason": f"overextended below VWAP ({vwap_distance_pct*100:.2f}% < -{max_vwap_dist*100:.2f}%), oversold bounce trap",
                "strategy": "NO_TRADE", "local_direction": local_direction}

    # Pattern Recognition for Trend, Pullbacks, Ranges, and Mean Reversions
    trend_up = ema_fast[-1] > ema_slow[-1] and last >= ema_fast[-1]
    trend_down = ema_fast[-1] < ema_slow[-1] and last <= ema_fast[-1]
    breakout_volume_ok = rvol >= 1.20 and volume_ok and not is_choppy
    breakout_up = last > prior_high and breakout_volume_ok
    breakout_down = last < prior_low and breakout_volume_ok
    pullback_buy = trend_up and (lows[-1] <= ema_fast[-1] * 1.002 or last >= ema_fast[-1] >= lows[-2]) and last >= previous and not is_choppy
    pullback_sell = trend_down and (highs[-1] >= ema_fast[-1] * 0.998 or last <= ema_fast[-1] <= highs[-2]) and last <= previous and not is_choppy
    momentum_up = trend_up and last > previous and ema_fast[-1] > ema_fast[-2] and not is_choppy
    momentum_down = trend_down and last < previous and ema_fast[-1] < ema_fast[-2] and not is_choppy


    # ASI Trap Analysis
    asi_trap = _detect_trap(bars)
    trap_type = asi_trap.get("trap_type") if asi_trap.get("is_trap") else None
    if asi_trap.get("is_trap"):
        if signal > 0 and trap_type == "BULL_TRAP":
            return {"accepted": False, "reason": f"trade blocked by ASI Bull Trap detection ({asi_trap.get('reason')})",
                    "strategy": "NO_TRADE", "trap_details": asi_trap}
        elif signal < 0 and trap_type == "BEAR_TRAP":
            return {"accepted": False, "reason": f"trade blocked by ASI Bear Trap detection ({asi_trap.get('reason')})",
                    "strategy": "NO_TRADE", "trap_details": asi_trap}

    failed_breakout_buy = signal > 0 and trap_type == "BEAR_TRAP"
    failed_breakout_sell = signal < 0 and trap_type == "BULL_TRAP"

    # Range and Mean-Reversion Patterns (Active in Choppy / Consolidation Regimes)
    range_mid = (prior_high + prior_low) / 2.0
    is_bounce = last >= previous or last > float(bars[-1].get("open", last))
    is_reject = last <= previous or last < float(bars[-1].get("open", last))
    range_support_bounce = (last <= range_mid) and (last <= prior_low + (atr * 0.4) or (lows[-1] <= prior_low and last >= lows[-1])) and is_bounce and signal > 0
    range_resist_reject = (last >= range_mid) and (last >= prior_high - (atr * 0.4) or (highs[-1] >= prior_high and last <= highs[-1])) and is_reject and signal < 0
    vwap_reversion_buy = signal > 0 and vwap_distance_pct <= -max(0.005, max_vwap_dist * 0.6) and is_bounce
    vwap_reversion_sell = signal < 0 and vwap_distance_pct >= max(0.005, max_vwap_dist * 0.6) and is_reject

    # Strategy Selection Matrix (Senior Trader: Defined-Risk Option Buying / Spreads)
    if signal > 0 and failed_breakout_buy:
        strategy = "FAILED_BREAKOUT_REVERSAL_CALL_BUY"
        route = ("CE", "BUY")
    elif signal > 0 and breakout_up:
        strategy = "BREAKOUT_CALL_BUY"
        route = ("CE", "BUY")
    elif signal > 0 and pullback_buy:
        strategy = "TREND_PULLBACK_CALL_BUY"
        route = ("CE", "BUY")
    elif signal > 0 and vwap_reversion_buy:
        strategy = "VWAP_MEAN_REVERSION_CALL_BUY"
        route = ("CE", "BUY")
    elif signal > 0 and range_support_bounce:
        strategy = "RANGE_SUPPORT_CALL_BUY"
        route = ("CE", "BUY")
    elif signal > 0 and momentum_up:
        strategy = "MOMENTUM_CALL_BUY"
        route = ("CE", "BUY")
    elif signal < 0 and failed_breakout_sell:
        strategy = "FAILED_BREAKOUT_REVERSAL_PUT_BUY"
        route = ("PE", "BUY")
    elif signal < 0 and breakout_down:
        strategy = "BREAKDOWN_PUT_BUY"
        route = ("PE", "BUY")
    elif signal < 0 and pullback_sell:
        strategy = "TREND_PULLBACK_PUT_BUY"
        route = ("PE", "BUY")
    elif signal < 0 and vwap_reversion_sell:
        strategy = "VWAP_MEAN_REVERSION_PUT_BUY"
        route = ("PE", "BUY")
    elif signal < 0 and range_resist_reject:
        strategy = "RANGE_RESISTANCE_PUT_BUY"
        route = ("PE", "BUY")
    elif signal < 0 and momentum_down:
        strategy = "MOMENTUM_PUT_BUY"
        route = ("PE", "BUY")
    else:
        rej_reason = "breakout blocked during choppy consolidation (ADX < 18)" if (is_choppy and (last > prior_high or last < prior_low)) else "trade direction not confirmed by this stock's breakout/pullback/momentum chart structure"
        return {"accepted": False, "reason": rej_reason, "strategy": "NO_TRADE", "local_direction": local_direction}

    # Calibrate Risk and Reward by Strategy Type
    if strategy.startswith("RANGE_") or "VWAP_MEAN_REVERSION" in strategy:
        risk = max(atr * 1.0, last * 0.0035)
        dist_vwap = abs(last - vwap)
        reward = max(atr * 2.0, dist_vwap if dist_vwap > atr else last * 0.007)
    elif "FAILED_BREAKOUT" in strategy:
        risk = max(atr * 0.8, last * 0.0030)
        range_span = abs(prior_high - prior_low)
        reward = max(atr * 2.5, range_span * 0.70 if range_span > 0 else last * 0.010)
    elif signal > 0:
        risk = max(atr * 1.5, last * .006)
        reward = max(atr * 3.0, last * .012)
    else:
        risk = max(atr * 1.2, last * .005)
        reward = max(atr * 3.0, last * .012)
    rr = reward / risk if risk else 0

    # Adverse Institutional Option Wall (PCR Filter)
    atm_pcr = float(item.get("atm_pcr", 1.0) or 1.0)
    if signal > 0 and atm_pcr < 0.50:
        return {"accepted": False, "reason": f"Long trade blocked by adverse extreme Call writing wall (ATM PCR {atm_pcr:.2f} < 0.50)",
                "strategy": "NO_TRADE"}
    if signal < 0 and atm_pcr > 1.50:
        return {"accepted": False, "reason": f"Short trade blocked by adverse extreme Put writing wall (ATM PCR {atm_pcr:.2f} > 1.50)",
                "strategy": "NO_TRADE"}

    # Volume Profile Value Area Exhaustion Filter
    vp_data = _calc_volume_profile(bars)
    vp_eval = _eval_volume_profile(last, signal, vp_data)
    item["_volume_profile"] = vp_eval
    is_strong_trend = (not is_choppy) and (adx >= 22.0)
    is_reversal = "FAILED_BREAKOUT" in strategy or strategy.startswith("RANGE_") or "VWAP_MEAN_REVERSION" in strategy
    if not is_strong_trend and not is_reversal:
        if vp_eval.get("verdict") == "CAUTION_VAH_EXTENSION" and rvol < 1.3:
            return {"accepted": False, "reason": f"Long trade blocked by Volume Profile VAH exhaustion without expansion volume (POC: {vp_data.get('poc')}, VAH: {vp_data.get('vah')})",
                    "strategy": "NO_TRADE", "volume_profile": vp_eval}
        if vp_eval.get("verdict") == "CAUTION_VAL_EXTENSION" and rvol < 1.3:
            return {"accepted": False, "reason": f"Short trade blocked by Volume Profile VAL exhaustion without expansion volume (POC: {vp_data.get('poc')}, VAL: {vp_data.get('val')})",
                    "strategy": "NO_TRADE", "volume_profile": vp_eval}

    return {"accepted": True, "reason": "fresh live feed, chart structure, BOS/CHoCH, volume and risk/reward passed",
            "strategy": strategy, "option_type": route[0], "side": route[1], "rr": round(rr, 2),
            "signal": signal, "structure": structure, "local_direction": local_direction,
            "directional_intent": "BULLISH" if signal > 0 else "BEARISH",
            "stop_loss": round(last - risk, 4) if signal > 0 else round(last + risk, 4),
            "take_profit": round(last + reward, 4) if signal > 0 else round(last - reward, 4)}


def _learning_strategy_gate(item: Dict, signal: int) -> Dict:
    """Looser paper-only gate for collecting live P&L samples.

    This is deliberately not promotion evidence.  It still requires real live
    candles and non-zero movement, but it allows small exploratory option-paper
    trades so the user can evaluate P&L, exits, and mistakes during a session.
    """
    bars=item.get("intraday") or []
    if len(bars)<6:
        return {"accepted":False,"reason":"less than 6 completed 5m live candles","strategy":"NO_TRADE"}
    local_direction = _instrument_local_direction(item)
    if not signal:
        signal = int(local_direction.get("direction") or 0)
    if not signal:
        return {"accepted":False,"reason":"learning trade rejected: no instrument-local CALL/PUT direction",
                "strategy":"NO_TRADE","local_direction":local_direction}
    closes=[float(bar["close"]) for bar in bars]
    highs=[float(bar["high"]) for bar in bars]
    lows=[float(bar["low"]) for bar in bars]
    volumes=[max(0,int(bar.get("volume") or 0)) for bar in bars]
    last=closes[-1]
    previous=closes[-2]
    atr=sum((high-low) for high,low in zip(highs[-6:],lows[-6:]))/6
    if atr<=0 or last<=0:
        return {"accepted":False,"reason":"zero live range","strategy":"NO_TRADE"}

    # Regime & Market Structure Analytics (ADX Proxy)
    dx_vals = []
    for i in range(1, len(bars)):
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        if tr > 0:
            dx = abs(plus_dm - minus_dm) / tr * 100.0
            dx_vals.append(dx)
    adx = (sum(dx_vals[-7:]) / len(dx_vals[-7:])) if dx_vals else 20.0
    is_choppy = adx < 18.0
    if is_choppy:
        return {"accepted":False,"reason":f"learning trade blocked during choppy consolidation (ADX {adx:.1f} < 18)",
                "strategy":"NO_TRADE","local_direction":local_direction}

    # Wyckoff Effort-vs-Result Volume Authenticity Indicator
    avg_volume = sum(volumes[-10:-1]) / max(1, len(volumes[-10:-1])) if len(volumes) >= 2 else 0
    rvol = volumes[-1] / max(1, avg_volume) if avg_volume else 1.0
    candle_spread = highs[-1] - lows[-1]
    is_wide_spread = candle_spread >= atr * 1.5
    if is_wide_spread and rvol < 0.90:
        return {"accepted":False,"reason":f"learning trade rejected by Wyckoff divergence (RVOL {rvol:.2f}x < 0.90x on wide spread)",
                "strategy":"NO_TRADE","local_direction":local_direction}

    # Anti-Chasing VWAP Overextension Guard
    total_pv = sum(((float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0) * max(1, int(b.get("volume") or 1)) for b in bars)
    total_vol = sum(max(1, int(b.get("volume") or 1)) for b in bars)
    vwap = total_pv / total_vol if total_vol > 0 else last
    vwap_distance_pct = (last - vwap) / vwap
    if signal > 0 and vwap_distance_pct > 0.008:
        return {"accepted":False,"reason":f"learning trade overextended above VWAP (+{vwap_distance_pct*100:.2f}% > +0.8%)",
                "strategy":"NO_TRADE","local_direction":local_direction}
    if signal < 0 and vwap_distance_pct < -0.008:
        return {"accepted":False,"reason":f"learning trade overextended below VWAP ({vwap_distance_pct*100:.2f}% < -0.8%)",
                "strategy":"NO_TRADE","local_direction":local_direction}

    structure=_structure_analysis(bars)
    ema_fast=_ema(closes,5)
    ema_slow=_ema(closes,10 if len(closes)>=10 else 6)
    momentum_up=last>=previous and ema_fast[-1]>=ema_slow[-1]
    momentum_down=last<=previous and ema_fast[-1]<=ema_slow[-1]
    if signal>0 and momentum_up:
        strategy="LEARNING_MOMENTUM_CALL_BUY"; route=("CE","BUY")
    elif signal>0:
        strategy="LEARNING_RANGE_PUT_SELL"; route=("PE","SELL")
    elif signal<0 and momentum_down:
        strategy="LEARNING_MOMENTUM_PUT_BUY"; route=("PE","BUY")
    else:
        strategy="LEARNING_RANGE_CALL_SELL"; route=("CE","SELL")
    if signal > 0:
        risk = max(atr * 1.5, last * .006)
    else:
        risk = max(atr * 1.2, last * .005)
    reward = max(atr * 3.0, last * .012)
    rr = reward / risk if risk else 0
    if structure.get("accepted") and signal and structure.get("direction") and structure["direction"] != signal:
        return {"accepted": False, "reason": "learning trade rejected because BOS/CHoCH structure conflicts with signal",
                "strategy": "NO_TRADE", "structure": structure}

    # 1. Welles Wilder's ASI False Breakout / Trap Filter
    asi_trap = _detect_trap(bars)
    if asi_trap.get("is_trap"):
        trap_type = asi_trap.get("trap_type")
        if signal > 0 and trap_type == "BULL_TRAP":
            return {"accepted": False, "reason": f"learning trade blocked by ASI Bull Trap detection ({asi_trap.get('reason')})",
                    "strategy": "NO_TRADE", "trap_details": asi_trap}
        elif signal < 0 and trap_type == "BEAR_TRAP":
            return {"accepted": False, "reason": f"learning trade blocked by ASI Bear Trap detection ({asi_trap.get('reason')})",
                    "strategy": "NO_TRADE", "trap_details": asi_trap}

    # 2. Adverse Institutional Option Wall (PCR Filter)
    atm_pcr = float(item.get("atm_pcr", 1.0) or 1.0)
    if signal > 0 and atm_pcr < 0.50:
        return {"accepted": False, "reason": f"learning Long trade blocked by adverse extreme Call writing wall (ATM PCR {atm_pcr:.2f} < 0.50)",
                "strategy": "NO_TRADE"}
    if signal < 0 and atm_pcr > 1.50:
        return {"accepted": False, "reason": f"learning Short trade blocked by adverse extreme Put writing wall (ATM PCR {atm_pcr:.2f} > 1.50)",
                "strategy": "NO_TRADE"}

    return {"accepted": True, "reason": "learning paper trade: live candles, BOS/CHoCH context and non-zero range passed; not promotion evidence",
            "strategy": strategy, "option_type": route[0], "side": route[1], "rr": round(rr, 2),
            "signal": signal, "learning_mode": True, "structure": structure, "local_direction": local_direction,
            "directional_intent": "BULLISH" if signal > 0 else "BEARISH",
            "stop_loss": round(last - risk, 4) if signal > 0 else round(last + risk, 4),
            "take_profit": round(last + reward, 4) if signal > 0 else round(last - reward, 4)}

