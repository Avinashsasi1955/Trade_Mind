from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass
class Signal:
    symbol: str
    name: str
    price: float
    change_pct: float
    action: str
    confidence: float
    strategy: str
    reasoning: str
    stop_loss: float
    target: float


class TradingAgent:
    """Explainable multi-strategy ensemble for simulation—not a profit guarantee."""

    def _momentum(self, stock: Dict) -> float:
        return max(-1.0, min(1.0, stock["change_pct"] / 3.5))

    def _volume_breakout(self, stock: Dict) -> float:
        volume = (stock["volume_ratio"] - 1) / 1.5
        breakout = stock["breakout_pct"] / 2.5
        return max(-1.0, min(1.0, volume * 0.45 + breakout * 0.55))

    def _rsi_quality(self, stock: Dict) -> float:
        rsi = stock["rsi"]
        if 52 <= rsi <= 68:
            return 0.75
        if rsi > 76:
            return -0.5
        if rsi < 34:
            return 0.35
        return 0.15

    def _vwap(self, stock: Dict) -> float:
        return max(-1.0, min(1.0, stock["vwap_pct"] / 1.8))

    def _select_strategy(self, stock: Dict, ensemble: float, position: Optional[Dict]) -> Dict:
        """Select a playbook from regime + exposure; never from a user strategy preference."""
        if position:
            if stock["change_pct"] <= -1.5 or stock["volatility"] >= 1.9:
                return {"name": "Protective Collar", "action": "HOLD", "why": "existing long exposure needs downside protection"}
            if stock["rsi"] >= 70 and stock["change_pct"] > 0:
                return {"name": "Covered Call", "action": "HOLD", "why": "existing shares are extended and may support capped-upside income"}
            return {"name": "Momentum Hold", "action": "HOLD", "why": "the existing position remains aligned with trend"}
        if stock["breakout_pct"] >= .8 and stock["volume_ratio"] >= 1.45:
            return {"name": "Breakout Trading", "action": "BUY", "why": "price expansion is confirmed by relative volume"}
        if stock["change_pct"] >= 1.8 and stock["vwap_pct"] > .4:
            return {"name": "Intraday Momentum", "action": "BUY", "why": "trend, VWAP and price impulse agree"}
        if stock["rsi"] <= 35 and stock["vwap_pct"] < -.8:
            return {"name": "Mean Reversion", "action": "BUY", "why": "an oversold displacement may revert toward VWAP"}
        if abs(stock["breakout_pct"]) < .35 and stock["volatility"] < 1.45:
            return {"name": "Volatility Squeeze", "action": "HOLD", "why": "compression needs a confirmed directional break"}
        if ensemble < -.25:
            return {"name": "Long Put / Avoid Long", "action": "HOLD", "why": "bearish structure rejects a new cash-equity long"}
        return {"name": "VWAP Pullback", "action": "BUY", "why": "constructive trend is closest to an executable pullback setup"}

    def analyse(self, stocks: List[Dict], risk_profile: str, positions: Optional[Dict[str, Dict]] = None) -> List[Signal]:
        risk_penalty = {"conservative": 8, "balanced": 3, "aggressive": 0}.get(risk_profile, 3)
        positions = positions or {}
        signals = []
        for stock in stocks:
            components = {
                "Momentum": self._momentum(stock),
                "Breakout": self._volume_breakout(stock),
                "RSI confirmation": self._rsi_quality(stock),
                "VWAP structure": self._vwap(stock),
            }
            ensemble = components["Momentum"] * .30 + components["Breakout"] * .34 + components["RSI confirmation"] * .18 + components["VWAP structure"] * .18
            confidence = max(35, min(96, round(58 + ensemble * 39 - risk_penalty, 1)))
            decision = self._select_strategy(stock, ensemble, positions.get(stock["symbol"]))
            action = decision["action"] if confidence >= 72 or decision["action"] == "HOLD" else "HOLD"
            strongest = sorted(components, key=components.get, reverse=True)[:2]
            stop_pct = {"conservative": .018, "balanced": .025, "aggressive": .035}.get(risk_profile, .025)
            target_pct = stop_pct * 2.1
            reasoning = (
                f"The algorithm selected {decision['name']} because {decision['why']}. "
                f"{strongest[0]} and {strongest[1]} provide the strongest confirmation. "
                f"Price is {stock['change_pct']:+.2f}% today on {stock['volume_ratio']:.1f}× relative volume; "
                f"RSI is {stock['rsi']:.0f} and price is {stock['vwap_pct']:+.2f}% from VWAP. "
                f"Risk is capped with a ₹{stock['price'] * (1-stop_pct):,.2f} stop and a 2.1:1 planned reward-to-risk ratio."
            )
            signals.append(Signal(stock["symbol"], stock["name"], stock["price"], stock["change_pct"], action, confidence, decision["name"], reasoning, round(stock["price"] * (1-stop_pct), 2), round(stock["price"] * (1+target_pct), 2)))
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    def serialise(self, signals: List[Signal]) -> List[Dict]:
        return [asdict(signal) for signal in signals]
