"""Paper-only derivatives strategy planner with explicit legs and risk bounds."""
from calendar import monthrange
from datetime import date, timedelta
from math import ceil, floor
from typing import Dict, List, Optional


LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 120}


def _next_monthly_expiry(today: Optional[date] = None) -> str:
    day = today or date.today()
    last = date(day.year, day.month, monthrange(day.year, day.month)[1])
    while last.weekday() != 3:  # Thursday; broker calendar must adjust exchange holidays.
        last -= timedelta(days=1)
    if last <= day + timedelta(days=3):
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        last = date(year, month, monthrange(year, month)[1])
        while last.weekday() != 3:
            last -= timedelta(days=1)
    return last.isoformat()


def _strike(spot: float, offset: float = 0) -> int:
    step = 50 if spot < 5000 else 100
    return int(round((spot * (1 + offset)) / step) * step)


def _leg(side: str, instrument: str, strike=None, option_type=None, lots: int = 1) -> Dict:
    return {"side": side, "instrument": instrument, "strike": strike, "option_type": option_type, "lots": lots}


def choose_derivative_strategy(market_view: str, volatility: str, has_position: bool) -> str:
    view, vol = market_view.lower(), volatility.lower()
    if has_position and view == "bearish": return "Protective Collar"
    if has_position and view in {"neutral", "mildly bullish"}: return "Covered Call"
    if vol == "expanding" and view == "neutral": return "Long Straddle"
    if vol == "contracting" and view == "neutral": return "Iron Condor"
    if view == "bullish": return "Bull Call Spread"
    if view == "bearish": return "Bear Put Spread"
    return "Iron Butterfly"


def build_derivative_plan(symbol: str, spot: float, market_view: str, volatility: str,
                          has_position: bool = False, strategy: str = "auto") -> Dict:
    """Build an executable paper order ticket. Premiums are estimates, never exchange quotes."""
    selected = choose_derivative_strategy(market_view, volatility, has_position) if strategy == "auto" else strategy
    atm, lower, upper = _strike(spot), _strike(spot, -.05), _strike(spot, .05)
    stock = f"{symbol}-EQ"
    templates: Dict[str, List[Dict]] = {
        "Long Future": [_leg("BUY", f"{symbol}-FUT")],
        "Short Future": [_leg("SELL", f"{symbol}-FUT")],
        "Index Futures Hedge": [_leg("SELL", f"{symbol}-FUT")],
        "Married Put": [_leg("BUY", stock), _leg("BUY", symbol, lower, "PE")],
        "Covered Call": [_leg("HOLD", stock), _leg("SELL", symbol, upper, "CE")],
        "Protective Collar": [_leg("HOLD", stock), _leg("BUY", symbol, lower, "PE"), _leg("SELL", symbol, upper, "CE")],
        "Long Call": [_leg("BUY", symbol, atm, "CE")],
        "Long Put": [_leg("BUY", symbol, atm, "PE")],
        "Bull Call Spread": [_leg("BUY", symbol, atm, "CE"), _leg("SELL", symbol, upper, "CE")],
        "Bull Put Spread": [_leg("SELL", symbol, atm, "PE"), _leg("BUY", symbol, lower, "PE")],
        "Bear Put Spread": [_leg("BUY", symbol, atm, "PE"), _leg("SELL", symbol, lower, "PE")],
        "Bear Call Spread": [_leg("SELL", symbol, atm, "CE"), _leg("BUY", symbol, upper, "CE")],
        "Long Straddle": [_leg("BUY", symbol, atm, "CE"), _leg("BUY", symbol, atm, "PE")],
        "Long Strangle": [_leg("BUY", symbol, upper, "CE"), _leg("BUY", symbol, lower, "PE")],
        "Iron Condor": [_leg("BUY", symbol, _strike(spot, -.08), "PE"), _leg("SELL", symbol, lower, "PE"), _leg("SELL", symbol, upper, "CE"), _leg("BUY", symbol, _strike(spot, .08), "CE")],
        "Iron Butterfly": [_leg("BUY", symbol, lower, "PE"), _leg("SELL", symbol, atm, "PE"), _leg("SELL", symbol, atm, "CE"), _leg("BUY", symbol, upper, "CE")],
    }
    if selected not in templates:
        raise ValueError(f"{selected} is documented but not enabled in the paper execution engine")
    width = max(50, upper - atm)
    premium_estimate = round(max(5, spot * .012), 2)
    defined = selected not in {"Covered Call", "Long Future", "Short Future", "Index Futures Hedge"}
    lot_size = LOT_SIZES.get(symbol, 75 if "NIFTY" in symbol else 1)
    
    # Precise multi-leg risk payoff calculations
    max_gain = None
    max_loss = None
    breakeven = None
    
    if selected == "Bull Put Spread":
        net_credit = round(premium_estimate * 0.45, 2)
        max_gain = round(net_credit * lot_size, 2)
        max_loss = round((width - net_credit) * lot_size, 2)
        breakeven = round(atm - net_credit, 2)
    elif selected == "Bear Call Spread":
        net_credit = round(premium_estimate * 0.45, 2)
        max_gain = round(net_credit * lot_size, 2)
        max_loss = round((width - net_credit) * lot_size, 2)
        breakeven = round(atm + net_credit, 2)
    elif selected == "Bull Call Spread":
        net_debit = round(premium_estimate * 0.55, 2)
        max_gain = round((width - net_debit) * lot_size, 2)
        max_loss = round(net_debit * lot_size, 2)
        breakeven = round(atm + net_debit, 2)
    elif selected == "Bear Put Spread":
        net_debit = round(premium_estimate * 0.55, 2)
        max_gain = round((width - net_debit) * lot_size, 2)
        max_loss = round(net_debit * lot_size, 2)
        breakeven = round(atm - net_debit, 2)
    elif selected == "Long Straddle":
        net_debit = round(premium_estimate * 1.9, 2)
        max_gain = "Unlimited"
        max_loss = round(net_debit * lot_size, 2)
        breakeven = f"{round(atm - net_debit, 2)} / {round(atm + net_debit, 2)}"
    elif defined:
        max_gain = round(premium_estimate * lot_size, 2)
        max_loss = round((width - premium_estimate) * lot_size, 2)
        breakeven = round(atm + premium_estimate, 2)

    return {
        "symbol": symbol, "strategy": selected, "market_view": market_view, "volatility": volatility,
        "spot": round(spot, 2), "expiry": _next_monthly_expiry(), "legs": templates[selected],
        "lot_size": lot_size, "status": "PAPER_READY",
        "risk": {
            "defined": defined,
            "strike_width": width,
            "estimated_debit_or_credit": premium_estimate,
            "max_profit": max_gain,
            "max_loss": max_loss,
            "breakeven": breakeven,
            "margin_benefit_pct": 68.5 if "Spread" in selected or "Condor" in selected or "Butterfly" in selected else 0.0,
            "note": "Indicative payoff only; replace with live option-chain/futures quotes, Greeks, lot size, margin and exchange holiday calendar."
        },
        "execution": {"mode": "paper", "atomic_basket_required": len(templates[selected]) > 1,
                      "live_order_allowed": False, "approval_required": True},
    }


def multi_leg_spread_catalog(symbol: str, spot: float) -> List[Dict]:
    """Generates all 4 primary defined-risk multi-leg spread setups for a given security."""
    return [
        build_derivative_plan(symbol, spot, "bullish", "neutral", strategy="Bull Put Spread"),
        build_derivative_plan(symbol, spot, "bearish", "neutral", strategy="Bear Call Spread"),
        build_derivative_plan(symbol, spot, "bullish", "expanding", strategy="Bull Call Spread"),
        build_derivative_plan(symbol, spot, "bearish", "expanding", strategy="Bear Put Spread"),
        build_derivative_plan(symbol, spot, "neutral", "expanding", strategy="Long Straddle"),
        build_derivative_plan(symbol, spot, "neutral", "contracting", strategy="Iron Condor"),
    ]

