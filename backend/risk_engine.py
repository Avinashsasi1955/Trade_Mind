import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .database import now_iso
from .market import is_synthetic_market_data_allowed, price_map
from .security_master import resolve_security
from .config import DATABASE_URL, LIVE_TRADING_ENABLED
from .market_data_feed import validate_market_data_for_entry, validate_quote_timestamp


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
        order_value=max(0,quantity*price)

        policy_dict = dict(policy) if hasattr(policy, "keys") else (policy if isinstance(policy, dict) else {})
        max_age = float(policy_dict.get("quote_max_age_seconds", 30.0))
        allow_synth = is_synthetic_market_data_allowed()

        # Resolve prices from live market view when DATABASE_URL is configured
        db_url = os.getenv("DATABASE_URL") or DATABASE_URL
        prices = {}
        if db_url:
            try:
                import sys
                mod = sys.modules.get(__name__)
                if mod and hasattr(mod, "live_price_map"):
                    prices = mod.live_price_map(db_url) or {}
                else:
                    from .live_market_view import price_map as live_price_map
                    prices = live_price_map(db_url) or {}
            except Exception:
                prices = {}
        if not prices and allow_synth:
            prices = price_map()

        if not prices:
            return RiskDecision(
                allowed=False,
                checks=[_check("market_data_reliability", False, "MARKET_DATA_STORAGE_UNAVAILABLE: No reliable prices available")],
                order_value=order_value,
                gross_exposure_after=order_value,
                reason="MARKET_DATA_STORAGE_UNAVAILABLE",
            )

        # Confirm the risk engine has a valid, fresh price for the exact order symbol
        is_derivative=order.get("exchange","").upper() in {"NFO","BFO","MCX","CDS"}

        # Ensure quote_symbol (if specified) matches the exact order instrument
        quote_symbol = order.get("quote_symbol")
        if quote_symbol:
            qs = str(quote_symbol).upper().strip()
            expected_symbols = {symbol}
            if is_derivative and (order.get("underlying") or order.get("original_symbol")):
                expected_symbols.add(str(order.get("underlying") or order.get("original_symbol")).upper().strip())
            if qs not in expected_symbols:
                return RiskDecision(
                    allowed=False,
                    checks=[_check("market_data_reliability", False, f"QUOTE_SYMBOL_MISMATCH: Quote symbol {qs} does not match order symbol {symbol}")],
                    order_value=order_value,
                    gross_exposure_after=order_value,
                    reason=f"QUOTE_SYMBOL_MISMATCH: {qs} != {symbol}",
                )

        ref_price = prices.get(symbol)
        if ref_price is None and is_derivative:
            ref_price = prices.get(order.get("underlying") or order.get("original_symbol"))
        if ref_price is None:
            return RiskDecision(
                allowed=False,
                checks=[_check("market_data_reliability", False, f"SYMBOL_PRICE_UNAVAILABLE: No reliable market price for symbol {symbol}")],
                order_value=order_value,
                gross_exposure_after=order_value,
                reason=f"SYMBOL_PRICE_UNAVAILABLE: {symbol}",
            )
        if isinstance(ref_price, dict):
            rf_val = ref_price.get("price")
            rf_src = ref_price.get("source") or ref_price.get("quote_source")
            rf_ts = ref_price.get("observed_at") or ref_price.get("source_timestamp") or ref_price.get("bar_time")
            is_valid_ref, ref_err = validate_market_data_for_entry(
                price=rf_val,
                observed_at=rf_ts,
                source=rf_src,
                max_age_seconds=max_age,
                allow_synthetic=allow_synth,
            )
            if not is_valid_ref:
                return RiskDecision(
                    allowed=False,
                    checks=[_check("market_data_reliability", False, f"SYMBOL_PRICE_UNRELIABLE: {symbol} - {ref_err}")],
                    order_value=order_value,
                    gross_exposure_after=order_value,
                    reason=f"SYMBOL_PRICE_UNRELIABLE: {symbol} ({ref_err})",
                )
        else:
            try:
                rf_val = float(ref_price)
                if rf_val <= 0 or math.isnan(rf_val) or math.isinf(rf_val):
                    raise ValueError()
            except (ValueError, TypeError):
                return RiskDecision(
                    allowed=False,
                    checks=[_check("market_data_reliability", False, f"SYMBOL_PRICE_INVALID: Invalid price {ref_price} for symbol {symbol}")],
                    order_value=order_value,
                    gross_exposure_after=order_value,
                    reason=f"SYMBOL_PRICE_INVALID: {symbol}",
                )

        holdings=db.execute("SELECT * FROM holdings WHERE user_id=?",(user_id,)).fetchall()
        gross=sum(x["quantity"]*(prices[x["symbol"]]["price"] if isinstance(prices.get(x["symbol"]), dict) else prices.get(x["symbol"],x["average_price"])) for x in holdings)
        gross_after=gross+order_value if side=="BUY" else max(0,gross-order_value)
        holding=next((x for x in holdings if x["symbol"]==symbol),None)
        security=resolve_security(symbol) if order.get("exchange","").upper() in {"NSE","BSE"} else {"symbol":symbol}
        today=datetime.now(timezone.utc).date().isoformat()
        orders_today=db.execute("SELECT COUNT(*) count FROM order_intents WHERE user_id=? AND created_at>=?",(user_id,today)).fetchone()["count"]
        realised_today=db.execute("SELECT COALESCE(SUM(realised_pnl),0) pnl FROM trades WHERE user_id=? AND created_at>=?",(user_id,today)).fetchone()["pnl"]
        max_position_value=portfolio["starting_capital"]*settings["max_position_pct"]/100
        loss_limit=portfolio["starting_capital"]*settings["max_daily_loss_pct"]/100
        if realised_today<=-loss_limit and not switch["active"]:
            db.execute("UPDATE kill_switches SET active=1,reason='Automatic daily realised-loss limit',activated_at=?,updated_at=? WHERE user_id=?",(now_iso(),now_iso(),user_id)); db.commit(); switch=db.execute("SELECT * FROM kill_switches WHERE user_id=?",(user_id,)).fetchone()

        effective_timestamp = quote_timestamp or order.get("quote_timestamp") or order.get("timestamp")
        quote_src = order.get("quote_source") or order.get("source")
        if not quote_src and allow_synth:
            quote_src = "SYNTHETIC_FIXTURE"

        is_valid_ts, quote_age, ts_err = validate_quote_timestamp(
            observed_at=effective_timestamp,
            max_age_seconds=max_age,
        )
        is_valid_quote, quote_reason = validate_market_data_for_entry(
            price=price,
            observed_at=effective_timestamp,
            source=quote_src,
            max_age_seconds=max_age,
            allow_synthetic=allow_synth,
        )

        is_fresh_quote = is_valid_ts
        quote_freshness_detail = f"Quote age {quote_age:.1f}s" if is_fresh_quote else (ts_err or "Timestamp missing or quote stale")
        live_quote_detail = "Timestamped quote verified" if is_valid_ts else (ts_err or "Timestamped quote required for entry authorization")

        checks=[
            _check("kill_switch",not switch["active"],switch["reason"] if switch["active"] else "Inactive"),
            _check("instrument_eligibility",bool(security),"Active security-master record required"),
            _check("valid_quantity",quantity>0,f"Quantity {quantity}"),
            _check("limit_price",price>0 and order.get("order_type","LIMIT") in {"LIMIT","SL"},"Only price-protected LIMIT/SL orders are enabled"),
            _check("market_data_reliability",is_valid_quote,quote_reason or "Verified"),
            _check("confidence",float(order.get("confidence",0))>=policy["min_confidence"],f"Minimum {policy['min_confidence']}%"),
            _check("order_value",order_value<=policy["max_order_value"],f"₹{order_value:,.2f} / ₹{policy['max_order_value']:,.2f}"),
            _check("position_size",side=="SELL" or order_value<=max_position_value,f"Maximum ₹{max_position_value:,.2f}"),
            _check("gross_exposure",gross_after<=portfolio["starting_capital"]*policy["max_gross_exposure_pct"]/100,f"After ₹{gross_after:,.2f}"),
            _check("order_frequency",orders_today<policy["max_orders_per_day"],f"{orders_today}/{policy['max_orders_per_day']} today"),
            _check("open_positions",bool(holding) or side=="SELL" or len(holdings)<policy["max_open_positions"],f"{len(holdings)}/{policy['max_open_positions']} open"),
            _check("loss_guard",realised_today>-loss_limit,f"Realised today ₹{realised_today:,.2f}; floor −₹{loss_limit:,.2f}"),
            _check("quote_freshness",is_fresh_quote,quote_freshness_detail),
            _check("live_quote_required",is_valid_ts,live_quote_detail),
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
