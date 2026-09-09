"""Deterministic pre-trade controls. Models cannot bypass this module."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .database import now_iso
from .market import price_map
from .security_master import resolve_security
from .config import LIVE_TRADING_ENABLED


@dataclass
class RiskDecision:
    allowed: bool
    checks: List[Dict]
    order_value: float
    gross_exposure_after: float
    reason: str


def _check(name: str, passed: bool, detail: str) -> Dict:
    return {"name":name,"passed":bool(passed),"detail":detail}


class RiskEngine:
    def evaluate(self, db, user_id: int, order: Dict, quote_timestamp: Optional[str] = None) -> RiskDecision:
        policy=db.execute("SELECT * FROM risk_policies WHERE user_id=?",(user_id,)).fetchone()
        settings=db.execute("SELECT * FROM settings WHERE user_id=?",(user_id,)).fetchone()
        portfolio=db.execute("SELECT * FROM portfolios WHERE user_id=?",(user_id,)).fetchone()
        switch=db.execute("SELECT * FROM kill_switches WHERE user_id=?",(user_id,)).fetchone()
        if not all((policy,settings,portfolio,switch)): raise ValueError("Risk configuration is incomplete")
        symbol=order["symbol"].upper(); side=order["transaction_type"].upper(); quantity=int(order["quantity"]); price=float(order.get("limit_price") or order.get("price") or 0)
        order_value=max(0,quantity*price); prices=price_map()
        holdings=db.execute("SELECT * FROM holdings WHERE user_id=?",(user_id,)).fetchall()
        gross=sum(x["quantity"]*prices.get(x["symbol"],x["average_price"]) for x in holdings)
        gross_after=gross+order_value if side=="BUY" else max(0,gross-order_value)
        holding=next((x for x in holdings if x["symbol"]==symbol),None)
        security=resolve_security(symbol) if order.get("exchange","").upper() in {"NSE","BSE"} else {"symbol":symbol}
        today=datetime.now(timezone.utc).date().isoformat()
        orders_today=db.execute("SELECT COUNT(*) count FROM order_intents WHERE user_id=? AND created_at>=?",(user_id,today)).fetchone()["count"]
        realised_today=db.execute("SELECT COALESCE(SUM(realised_pnl),0) pnl FROM trades WHERE user_id=? AND created_at>=?",(user_id,today)).fetchone()["pnl"]
        quote_age=0.0
        if quote_timestamp:
            try: quote_age=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(quote_timestamp.replace("Z","+00:00"))).total_seconds())
            except ValueError: quote_age=999999
        max_position_value=portfolio["starting_capital"]*settings["max_position_pct"]/100
        loss_limit=portfolio["starting_capital"]*settings["max_daily_loss_pct"]/100
        if realised_today<=-loss_limit and not switch["active"]:
            db.execute("UPDATE kill_switches SET active=1,reason='Automatic daily realised-loss limit',activated_at=?,updated_at=? WHERE user_id=?",(now_iso(),now_iso(),user_id)); db.commit(); switch=db.execute("SELECT * FROM kill_switches WHERE user_id=?",(user_id,)).fetchone()
        is_derivative=order.get("exchange","").upper() in {"NFO","BFO","MCX","CDS"}
        checks=[
            _check("kill_switch",not switch["active"],switch["reason"] if switch["active"] else "Inactive"),
            _check("instrument_eligibility",bool(security),"Active security-master record required"),
            _check("valid_quantity",quantity>0,f"Quantity {quantity}"),
            _check("limit_price",price>0 and order.get("order_type","LIMIT") in {"LIMIT","SL"},"Only price-protected LIMIT/SL orders are enabled"),
            _check("confidence",float(order.get("confidence",0))>=policy["min_confidence"],f"Minimum {policy['min_confidence']}%"),
            _check("order_value",order_value<=policy["max_order_value"],f"₹{order_value:,.2f} / ₹{policy['max_order_value']:,.2f}"),
            _check("position_size",side=="SELL" or order_value<=max_position_value,f"Maximum ₹{max_position_value:,.2f}"),
            _check("gross_exposure",gross_after<=portfolio["starting_capital"]*policy["max_gross_exposure_pct"]/100,f"After ₹{gross_after:,.2f}"),
            _check("order_frequency",orders_today<policy["max_orders_per_day"],f"{orders_today}/{policy['max_orders_per_day']} today"),
            _check("open_positions",bool(holding) or side=="SELL" or len(holdings)<policy["max_open_positions"],f"{len(holdings)}/{policy['max_open_positions']} open"),
            _check("loss_guard",realised_today>-loss_limit,f"Realised today ₹{realised_today:,.2f}; floor −₹{loss_limit:,.2f}"),
            _check("quote_freshness",not quote_timestamp or quote_age<=policy["quote_max_age_seconds"],f"Quote age {quote_age:.1f}s"),
            _check("live_quote_required",not LIVE_TRADING_ENABLED or bool(quote_timestamp),"Timestamped quote required in live mode"),
            _check("derivative_permission",not is_derivative or bool(policy["allow_derivatives"]),"Derivative permission required"),
            _check("cash",side=="SELL" or portfolio["cash"]>=order_value,f"Cash ₹{portfolio['cash']:,.2f}"),
            _check("sell_inventory",side=="BUY" or is_derivative or bool(holding and holding["quantity"]>=quantity),"Cash-equity naked shorting disabled; derivative shorts require an eligible contract"),
        ]
        allowed=all(x["passed"] for x in checks); failures=[x["name"] for x in checks if not x["passed"]]
        return RiskDecision(allowed,checks,round(order_value,2),round(gross_after,2),"All controls passed" if allowed else "Blocked by: "+", ".join(failures))


def policy(db,user_id:int)->Dict:
    row=db.execute("SELECT * FROM risk_policies WHERE user_id=?",(user_id,)).fetchone(); return dict(row)


def switch_status(db,user_id:int)->Dict:
    row=db.execute("SELECT * FROM kill_switches WHERE user_id=?",(user_id,)).fetchone(); return dict(row)


def set_kill_switch(db,user_id:int,active:bool,reason:str="")->Dict:
    stamp=now_iso(); db.execute("UPDATE kill_switches SET active=?,reason=?,activated_at=?,updated_at=? WHERE user_id=?",(int(active),reason[:200] if active else "",stamp if active else None,stamp,user_id))
    if active:
        db.execute("UPDATE order_intents SET status='CANCELLED_BY_KILL_SWITCH',updated_at=? WHERE user_id=? AND status IN ('CREATED','APPROVAL_PENDING','APPROVED')",(stamp,user_id))
    db.commit(); return switch_status(db,user_id)
