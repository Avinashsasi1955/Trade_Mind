import math
from datetime import datetime, timezone
from typing import Dict, List

from .market import market_snapshot
from .technical_analysis import generate_candles


TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "1H": 60, "1D": 1440}


def _ema(values: List[float], period: int) -> List[float]:
    alpha = 2 / (period + 1)
    result, current = [], values[0]
    for value in values:
        current = value * alpha + current * (1 - alpha)
        result.append(round(current, 2))
    return result


def _sma(values: List[float], period: int) -> List[float]:
    return [round(sum(values[max(0, i-period+1):i+1]) / len(values[max(0, i-period+1):i+1]), 2) for i in range(len(values))]


def _bollinger(values: List[float], period: int = 20) -> Dict[str, List[float]]:
    middle = _sma(values, period)
    upper, lower = [], []
    for idx, mean in enumerate(middle):
        window = values[max(0, idx-period+1):idx+1]
        variance = sum((value - mean) ** 2 for value in window) / len(window)
        deviation = math.sqrt(variance)
        upper.append(round(mean + deviation * 2, 2))
        lower.append(round(mean - deviation * 2, 2))
    return {"upper": upper, "middle": middle, "lower": lower}


def _rsi(values: List[float], period: int = 14) -> List[float]:
    result = [50.0]
    gains, losses = [], []
    for idx in range(1, len(values)):
        change = values[idx] - values[idx-1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]
        avg_gain = sum(recent_gains) / len(recent_gains)
        avg_loss = sum(recent_losses) / len(recent_losses)
        value = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        result.append(round(value, 2))
    return result


def chart_data(symbol: str, timeframe: str = "5m") -> Dict:
    timeframe = timeframe if timeframe in TIMEFRAMES else "5m"
    stock = next((item for item in market_snapshot() if item["symbol"] == symbol.upper()), None)
    if not stock:
        raise ValueError("Unknown NSE symbol")
    candles = generate_candles(stock["symbol"], stock["price"], 180, TIMEFRAMES[timeframe])
    closes = [item["close"] for item in candles]
    volumes = [item["volume"] for item in candles]
    change = closes[-1] - closes[-2]
    return {
        "symbol": stock["symbol"], "name": stock["name"], "exchange": "NSE", "timeframe": timeframe,
        "price": closes[-1], "change": round(change, 2), "change_pct": round(change / closes[-2] * 100, 2),
        "candles": candles, "indicators": {"ema20": _ema(closes, 20), "ema50": _ema(closes, 50), "bollinger": _bollinger(closes), "rsi14": _rsi(closes), "volume_sma20": _sma(volumes, 20)},
        "data_mode": "simulated", "updated_at": datetime.now(timezone.utc).isoformat(),
    }
