"""Deterministic instrument routing for bearish ML signals."""
from dataclasses import dataclass
from datetime import date
from math import ceil
from typing import Dict


@dataclass
class RoutingDecision:
    allowed: bool
    order: Dict
    audit: Dict


def _is_ml_short(payload: Dict) -> bool:
    direction=str(payload.get("signal_direction") or payload.get("side") or "").upper()
    source=str(payload.get("signal_source") or payload.get("source") or "").lower()
    return direction in {"SHORT","BEARISH"} and (source in {"ml","model","ai_agent","shadow"} or bool(payload.get("model_version")))


def route_ml_signal(db,payload: Dict,order: Dict) -> RoutingDecision:
    if not _is_ml_short(payload):
        return RoutingDecision(True,order,{"required":False,"reason":"Not a bearish ML cash-equity signal"})
    original={"exchange":order["exchange"],"symbol":order["symbol"],"quantity":order["quantity"],"transaction_type":"SELL"}
    if order["exchange"] not in {"NSE","BSE"}:
        return RoutingDecision(True,order,{"required":False,"reason":"Signal already targets a non-cash instrument","original":original})
    preference=str(payload.get("bearish_route") or "BUY_PUT").upper()
    instrument_type="FUT" if preference=="SHORT_FUTURE" else "PE"
    try:
        if instrument_type=="PE":
            contract=db.execute("""SELECT symbol,exchange,instrument_type,expiry,strike,lot_size FROM instrument_master
                WHERE underlying_symbol=? AND exchange IN ('NFO','BFO') AND instrument_type='PE' AND is_active=TRUE AND expiry>=?
                ORDER BY expiry,ABS(strike-?) LIMIT 1""",(order["symbol"],date.today().isoformat(),order["limit_price"])).fetchone()
        else:
            contract=db.execute("""SELECT symbol,exchange,instrument_type,expiry,strike,lot_size FROM instrument_master
                WHERE underlying_symbol=? AND exchange IN ('NFO','BFO') AND instrument_type='FUT' AND is_active=TRUE AND expiry>=?
                ORDER BY expiry LIMIT 1""",(order["symbol"],date.today().isoformat())).fetchone()
    except Exception:
        contract=None
    if not contract:
        return RoutingDecision(False,order,{"required":True,"status":"REJECTED","reason":"No active derivatives contract for bearish ML signal","original":original})
    derivative_price=float(payload.get("derivative_limit_price") or 0)
    if derivative_price<=0:
        return RoutingDecision(False,order,{"required":True,"status":"REJECTED","reason":"Fresh derivative limit price is required; underlying price cannot price a derivative","original":original,"candidate":dict(contract)})
    lot=int(contract["lot_size"]); lots=max(1,ceil(int(order["quantity"])/lot))
    routed={**order,"symbol":contract["symbol"],"exchange":contract["exchange"],"transaction_type":"SELL" if contract["instrument_type"]=="FUT" else "BUY",
            "quantity":lots*lot,"limit_price":derivative_price,"product":"NRML","strategy":"ML bearish route: "+("Short Future" if contract["instrument_type"]=="FUT" else "Long Put")}
    audit={"required":True,"status":"ROUTED","reason":"Cash-equity overnight short prohibited by policy","original":original,
           "route":{"exchange":routed["exchange"],"symbol":routed["symbol"],"instrument_type":contract["instrument_type"],"expiry":contract["expiry"],
                    "strike":contract["strike"],"lot_size":lot,"lots":lots,"quantity":routed["quantity"],"transaction_type":routed["transaction_type"]}}
    return RoutingDecision(True,routed,audit)
