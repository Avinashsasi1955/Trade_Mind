import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple


GLOSSARY = {
    "SL": "Stop Loss — the predefined price where a losing trade exits.",
    "TP": "Take Profit — the planned price where profit is booked.",
    "OB": "Order Block — the final opposing candle before a strong displacement move.",
    "IMB": "Imbalance — an inefficiently traded price area created by aggressive one-sided flow.",
    "R:R": "Risk-to-Reward — expected loss compared with expected gain; e.g. 1:2.",
    "MS": "Market Structure — the sequence of swing highs and lows defining trend or range.",
    "MSS": "Market Structure Shift — an early break suggesting control may be changing sides.",
    "CHOCH": "Change of Character — the first meaningful break against the prevailing structure.",
    "BOS": "Break of Structure — continuation break beyond a confirmed swing point.",
    "EQH": "Equal Highs — clustered highs where buy-side liquidity may rest.",
    "EQL": "Equal Lows — clustered lows where sell-side liquidity may rest.",
    "FVG": "Fair Value Gap — a three-candle price gap often revisited for rebalancing.",
    "BSL": "Buy-Side Liquidity — stops and breakout orders resting above visible highs.",
    "SSL": "Sell-Side Liquidity — stops and breakdown orders resting below visible lows.",
    "SR": "Support/Resistance — areas where price previously reacted or stalled.",
    "POI": "Point of Interest — a confluence zone worth monitoring for confirmation.",
    "LTF": "Lower Timeframe — execution context such as 1m, 5m, or 15m.",
    "HTF": "Higher Timeframe — directional context such as 1h, 4h, or daily.",
    "PIP": "Minimum quoted price increment; NSE cash equities commonly use a ₹0.05 tick, subject to exchange rules.",
    "LOT": "The permitted order quantity unit. Cash equities may trade one share; derivatives use contract-specific lot sizes.",
}


def generate_candles(symbol: str, base_price: float, minutes: int = 240, interval: int = 1) -> List[Dict]:
    """Reproducible OHLCV history used until a real candle-feed adapter is connected."""
    seed = sum(ord(c) for c in symbol) * 1009 + datetime.now(timezone.utc).date().toordinal() + interval
    rng = random.Random(seed)
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=minutes * interval)
    price = base_price * (1 - rng.uniform(-.008, .012))
    candles = []
    drift = rng.uniform(-.00008, .00018)
    for idx in range(minutes):
        wave = math.sin(idx / (13 + interval)) * .00035
        shock = rng.gauss(0, .00065 * min(math.sqrt(interval), 8))
        open_price = price
        close = max(.05, open_price * (1 + drift + wave + shock))
        spread = abs(rng.gauss(.00045, .00018)) * open_price
        high = max(open_price, close) + spread
        low = min(open_price, close) - spread
        volume = int((80_000 + rng.randint(0, 120_000)) * (1 + abs(shock) * 280))
        candles.append({"time": (start + timedelta(minutes=idx * interval)).isoformat(), "open": round(open_price, 2), "high": round(high, 2), "low": round(low, 2), "close": round(close, 2), "volume": volume})
        price = close
    scale = base_price / candles[-1]["close"]
    for candle in candles:
        for key in ("open", "high", "low", "close"):
            candle[key] = round(candle[key] * scale, 2)
    return candles


def _atr(candles: List[Dict], period: int = 14) -> float:
    ranges = []
    for prior, current in zip(candles[-period-1:-1], candles[-period:]):
        ranges.append(max(current["high"] - current["low"], abs(current["high"] - prior["close"]), abs(current["low"] - prior["close"])))
    return sum(ranges) / max(1, len(ranges))


def _swings(candles: List[Dict], width: int = 3) -> Tuple[List[Dict], List[Dict]]:
    highs, lows = [], []
    for idx in range(width, len(candles) - width):
        window = candles[idx-width:idx+width+1]
        if candles[idx]["high"] == max(c["high"] for c in window):
            highs.append({"index": idx, "price": candles[idx]["high"], "time": candles[idx]["time"]})
        if candles[idx]["low"] == min(c["low"] for c in window):
            lows.append({"index": idx, "price": candles[idx]["low"], "time": candles[idx]["time"]})
    return highs, lows


def _gaps(candles: List[Dict]) -> List[Dict]:
    gaps = []
    for idx in range(2, len(candles)):
        first, third = candles[idx-2], candles[idx]
        if first["high"] < third["low"]:
            gaps.append({"type": "bullish", "low": first["high"], "high": third["low"], "time": third["time"]})
        elif first["low"] > third["high"]:
            gaps.append({"type": "bearish", "low": third["high"], "high": first["low"], "time": third["time"]})
    return gaps[-4:]


def _aggregate(candles: List[Dict], size: int = 5) -> List[Dict]:
    result = []
    for start in range(0, len(candles), size):
        group = candles[start:start + size]
        if not group:
            continue
        result.append({"time": group[-1]["time"], "open": group[0]["open"], "high": max(item["high"] for item in group),
                       "low": min(item["low"] for item in group), "close": group[-1]["close"], "volume": sum(item.get("volume", 0) for item in group)})
    return result


def analyse_structure(symbol: str, name: str, price: float, candles: List[Dict] = None) -> Dict:
    historical = bool(candles)
    ltf = candles[-500:] if historical else generate_candles(symbol, price, 240, 1)
    htf = _aggregate(ltf, 5) if historical else generate_candles(symbol, price, 160, 15)
    if len(ltf) < 80 or len(htf) < 30:
        raise ValueError("At least 80 daily candles are required for market-structure analysis")
    highs, lows = _swings(ltf)
    htf_highs, htf_lows = _swings(htf)
    close = ltf[-1]["close"]
    atr = _atr(ltf)
    recent_high = highs[-1]["price"] if highs else max(c["high"] for c in ltf[-30:])
    recent_low = lows[-1]["price"] if lows else min(c["low"] for c in ltf[-30:])
    htf_slope = htf[-1]["close"] - htf[-30]["close"]
    ltf_slope = ltf[-1]["close"] - ltf[-30]["close"]
    htf_bias = "bullish" if htf_slope > 0 else "bearish"
    ltf_bias = "bullish" if ltf_slope > 0 else "bearish"
    bias = htf_bias if htf_bias == ltf_bias else "neutral"
    bos = "bullish" if close > recent_high else "bearish" if close < recent_low else "none"
    choch = htf_bias != ltf_bias
    mss = choch and abs(ltf_slope) > atr * 1.5
    tolerance = close * .0015
    eqh = len(highs) >= 2 and abs(highs[-1]["price"] - highs[-2]["price"]) <= tolerance
    eql = len(lows) >= 2 and abs(lows[-1]["price"] - lows[-2]["price"]) <= tolerance
    gaps = _gaps(ltf)
    displacement_idx = max(range(len(ltf)-25, len(ltf)), key=lambda i: abs(ltf[i]["close"] - ltf[i]["open"]))
    displacement = ltf[displacement_idx]
    ob_source = ltf[max(0, displacement_idx - 1)]
    ob_type = "bullish" if displacement["close"] > displacement["open"] else "bearish"
    order_block = {"type": ob_type, "low": min(ob_source["open"], ob_source["close"]), "high": max(ob_source["open"], ob_source["close"]), "time": ob_source["time"]}
    bsl = round(max([h["price"] for h in highs[-3:]] or [recent_high]), 2)
    ssl = round(min([l["price"] for l in lows[-3:]] or [recent_low]), 2)
    support = round(max([l["price"] for l in lows if l["price"] < close][-3:] or [recent_low]), 2)
    resistance = round(min([h["price"] for h in highs if h["price"] > close][-3:] or [recent_high]), 2)
    poi = gaps[-1] if gaps else order_block
    direction = 1 if bias == "bullish" else -1 if bias == "bearish" else (1 if ltf_bias == "bullish" else -1)
    sl = round(min(close - atr, min(support, order_block["low"]) - atr * .25) if direction > 0 else max(close + atr, max(resistance, order_block["high"]) + atr * .25), 2)
    risk = abs(close - sl)
    tp = round(close + direction * risk * 2, 2)
    rr = round(abs(tp-close) / max(.01, risk), 2)
    confluence = sum([htf_bias == ltf_bias, bos != "none", bool(gaps), mss, eqh or eql])
    confidence = min(94, 55 + confluence * 7)
    structure = "higher highs / higher lows" if bias == "bullish" else "lower highs / lower lows" if bias == "bearish" else "mixed / range"
    narrative = f"{symbol} is {bias} across the analysed LTF/HTF stack with {structure}. "
    narrative += f"{'A market-structure shift is active. ' if mss else ''}{'CHOCH is present; wait for confirmation. ' if choch else ''}"
    narrative += f"Liquidity is mapped near ₹{bsl:,.2f} BSL and ₹{ssl:,.2f} SSL. The primary POI spans ₹{poi['low']:,.2f}–₹{poi['high']:,.2f}. "
    narrative += f"A model setup uses SL ₹{sl:,.2f}, TP ₹{tp:,.2f}, and R:R 1:{rr:.1f}; these are research levels, not advice."
    return {
        "symbol": symbol, "name": name, "price": close, "bias": bias, "confidence": confidence,
        "timeframes": {"ltf": "1D" if historical else "1m", "htf": "1W" if historical else "15m", "ltf_bias": ltf_bias, "htf_bias": htf_bias},
        "market_structure": {"state": structure, "bos": bos, "mss": mss, "choch": choch, "eqh": eqh, "eql": eql},
        "liquidity": {"bsl": bsl, "ssl": ssl}, "support_resistance": {"support": support, "resistance": resistance},
        "order_block": {**order_block, "low": round(order_block["low"],2), "high": round(order_block["high"],2)},
        "fvg": [{**gap, "low": round(gap["low"],2), "high": round(gap["high"],2)} for gap in gaps],
        "poi": {**poi, "low": round(poi["low"],2), "high": round(poi["high"],2)},
        "trade_plan": {"direction": "LONG" if direction > 0 else "SHORT", "entry": close, "sl": sl, "tp": tp, "risk_reward": rr, "tick_size": .05, "lot_size": 1},
        "narrative": narrative, "candles": ltf[-80:], "data_mode": "kite_historical" if historical else "simulated", "updated_at": datetime.now(timezone.utc).isoformat(),
    }
