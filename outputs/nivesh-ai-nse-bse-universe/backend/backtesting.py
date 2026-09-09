"""Deterministic, cost-aware backtesting for NSE research.

Signals are calculated on a completed daily bar and executed at the next day's
open.  This intentionally prevents the most common look-ahead error.
"""

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List

from .config import ROOT
from .market import BASE_MARKET


HISTORY_DIR = ROOT / "data" / "historical"
STRATEGIES = {"ensemble", "momentum", "mean_reversion", "breakout"}


def _sma(values: List[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / max(1, len(window))


def _rsi(values: List[float], period: int = 14) -> float:
    changes = [b - a for a, b in zip(values[-period-1:-1], values[-period:])]
    gains = sum(max(0, value) for value in changes) / max(1, len(changes))
    losses = sum(max(0, -value) for value in changes) / max(1, len(changes))
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def _simulated_history(symbol: str, sessions: int = 300) -> List[Dict]:
    stock = next((row for row in BASE_MARKET if row[0] == symbol), None)
    if not stock:
        raise ValueError("Unknown NSE symbol")
    rng = random.Random(sum(ord(char) for char in symbol) * 9173)
    end = date.today()
    start_price = stock[2] * .82
    price, bars = start_price, []
    day = end - timedelta(days=sessions * 7 // 5 + 30)
    while day <= end:
        if day.weekday() < 5:
            cycle = math.sin(len(bars) / 31) * .0025
            overnight = rng.gauss(.0002, .006)
            open_price = max(.05, price * (1 + overnight))
            close = max(.05, open_price * (1 + .00035 + cycle + rng.gauss(0, .012)))
            spread = abs(rng.gauss(.009, .004)) * open_price
            bars.append({"date": day.isoformat(), "open": round(open_price, 2),
                         "high": round(max(open_price, close) + spread, 2),
                         "low": round(max(.05, min(open_price, close) - spread), 2),
                         "close": round(close, 2), "volume": rng.randint(900_000, 8_500_000)})
            price = close
        day += timedelta(days=1)
    return bars[-sessions:]


def load_history(symbol: str) -> Dict:
    symbol = symbol.upper().replace(".NS", "")
    target = HISTORY_DIR / f"{symbol}.json"
    if target.is_file():
        payload = json.loads(target.read_text())
        bars = payload.get("candles", payload if isinstance(payload, list) else [])
        source = payload.get("source", "imported historical data") if isinstance(payload, dict) else "imported historical data"
        if len(bars) >= 80:
            return {"symbol": symbol, "source": source, "data_mode": "historical", "candles": bars}
    return {"symbol": symbol, "source": "deterministic demo series", "data_mode": "simulated", "candles": _simulated_history(symbol)}


def _signal(bars: List[Dict], index: int, strategy: str) -> int:
    history = bars[:index + 1]
    closes = [bar["close"] for bar in history]
    volumes = [bar["volume"] for bar in history]
    if len(closes) < 55:
        return 0
    price, sma20, sma50 = closes[-1], _sma(closes, 20), _sma(closes, 50)
    momentum = 1 if price > sma20 > sma50 else -1 if price < sma20 < sma50 else 0
    rsi = _rsi(closes)
    reversion = 1 if rsi < 32 and price < sma20 * .98 else -1 if rsi > 68 and price > sma20 * 1.02 else 0
    prior_high = max(bar["high"] for bar in history[-21:-1])
    prior_low = min(bar["low"] for bar in history[-21:-1])
    volume_ratio = volumes[-1] / max(1, _sma(volumes[:-1], 20))
    breakout = 1 if price > prior_high and volume_ratio > 1.15 else -1 if price < prior_low and volume_ratio > 1.15 else 0
    if strategy == "momentum":
        return momentum
    if strategy == "mean_reversion":
        return reversion
    if strategy == "breakout":
        return breakout
    score = momentum * 2 + reversion + breakout * 2
    return 1 if score >= 2 else -1 if score <= -2 else 0


def _segment(bars: List[Dict], strategy: str, capital: float, start_index: int,
             cost_bps: float = 12.0) -> Dict:
    equity, trades, daily_returns = capital, [], []
    peak, max_drawdown = capital, 0.0
    predictions, correct = 0, 0
    for index in range(max(55, start_index), len(bars) - 1):
        direction = _signal(bars, index, strategy)
        if direction == 0:
            daily_returns.append(0.0)
            continue
        entry, exit_price = bars[index + 1]["open"], bars[index + 1]["close"]
        gross_return = direction * (exit_price / entry - 1)
        net_return = gross_return - cost_bps / 10_000
        allocation = min(equity * .20, capital * .25)
        pnl = allocation * net_return
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        predictions += 1
        correct += int(gross_return > 0)
        daily_returns.append(pnl / max(1, equity - pnl))
        trades.append({"signal_date": bars[index]["date"], "trade_date": bars[index + 1]["date"],
                       "side": "LONG" if direction > 0 else "SHORT", "entry": round(entry, 2),
                       "exit": round(exit_price, 2), "pnl": round(pnl, 2),
                       "return_pct": round(net_return * 100, 2)})
    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] <= 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    volatility = pstdev(daily_returns) if len(daily_returns) > 1 else 0
    sharpe = mean(daily_returns) / volatility * math.sqrt(252) if volatility else 0
    return {"ending_capital": round(equity, 2), "net_pnl": round(equity - capital, 2),
            "return_pct": round((equity / capital - 1) * 100, 2), "trades": len(trades),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / max(1, len(trades)) * 100, 2),
            "directional_accuracy": round(correct / max(1, predictions) * 100, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
            "max_drawdown_pct": round(max_drawdown * 100, 2), "sharpe": round(sharpe, 2),
            "recent_trades": list(reversed(trades[-10:]))}


def run_backtest(symbol: str = "RELIANCE", strategy: str = "ensemble",
                 capital: float = 1_000_000, start: str = "", end: str = "") -> Dict:
    strategy = strategy if strategy in STRATEGIES else "ensemble"
    history = load_history(symbol)
    bars = history["candles"]
    if start:
        bars = [bar for bar in bars if bar["date"] >= start]
    if end:
        bars = [bar for bar in bars if bar["date"] <= end]
    if len(bars) < 80:
        raise ValueError("Choose a period with at least 80 daily candles")
    split = max(56, int(len(bars) * .70))
    in_sample = _segment(bars[:split], strategy, capital, 55)
    out_sample = _segment(bars, strategy, capital, split)
    benchmark = (bars[-1]["close"] / bars[split]["close"] - 1) * 100
    verdict = "promising" if out_sample["return_pct"] > 0 and out_sample["profit_factor"] and out_sample["profit_factor"] > 1.15 else "weak"
    return {"symbol": history["symbol"], "strategy": strategy, "source": history["source"],
            "data_mode": history["data_mode"], "period": {"start": bars[0]["date"], "end": bars[-1]["date"], "sessions": len(bars)},
            "methodology": {"train_test_split": "70 / 30 chronological", "execution": "signal at close, trade next open-to-close",
                            "costs_bps_per_trade": 12, "position_allocation_pct": 20, "look_ahead_protection": True},
            "in_sample": in_sample, "out_of_sample": out_sample,
            "benchmark_return_pct": round(benchmark, 2), "verdict": verdict,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "warning": "Historical performance is not a guarantee of future returns. This is paper-trading research, not investment advice."}
