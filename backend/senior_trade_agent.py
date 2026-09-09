"""Senior paper-trade agent.

This module is deliberately deterministic and paper-only.  It gives each
shadow trade an institutional-style review that can be displayed beside
Positions and Trade History without enabling live broker execution.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Dict, Mapping


def _decimal(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(default)


def _note(value) -> Dict:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed=json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {"raw": str(value)}
    except Exception:
        return {"raw": str(value)}


def _option_route_direction(option_type: str, option_side: str) -> int:
    option_type=str(option_type or "").upper()
    option_side=str(option_side or "").upper()
    if option_type=="CE" and option_side=="BUY":
        return 1
    if option_type=="PE" and option_side=="SELL":
        return 1
    if option_type=="PE" and option_side=="BUY":
        return -1
    if option_type=="CE" and option_side=="SELL":
        return -1
    return 0


def _geometry_ok(side: str, entry: Decimal, stop_loss: Decimal, take_profit: Decimal) -> bool:
    side=str(side or "").upper()
    if entry<=0 or stop_loss<=0 or take_profit<=0:
        return False
    if side=="BUY":
        return stop_loss < entry < take_profit
    if side=="SELL":
        return take_profit < entry < stop_loss
    return False


def review_shadow_trade(row: Mapping) -> Dict:
    """Return a compact senior-agent review for one shadow-paper trade."""
    note=_note(row.get("improvement_note"))
    strategy=str(note.get("strategy") or "").upper()
    route=str(note.get("route") or "").upper()
    decision_report=dict(note.get("senior_decision_report") or {})
    sentiment=dict(note.get("sentiment_gate") or decision_report.get("sentiment") or {})
    option_grade=dict(note.get("option_grade") or decision_report.get("option_grade") or {})
    session_case=dict(note.get("session_case") or {})
    bias=str(session_case.get("bias") or "none").lower()
    side=str(row.get("side") or "").upper()
    instrument_type=str(row.get("instrument_type") or "").upper()
    entry=_decimal(row.get("entry_price") or row.get("theoretical_fill_price") or row.get("decision_price"))
    latest=_decimal(row.get("latest_price") or row.get("realised_exit_price") or entry)
    stop_loss=_decimal(row.get("stop_loss_price"))
    take_profit=_decimal(row.get("take_profit_price"))
    pnl=_decimal(row.get("marked_pnl") if row.get("marked_pnl") is not None else row.get("net_pnl"))
    fees=_decimal(row.get("estimated_fees"))
    quality=dict(row.get("quality") or {})
    quality_score=_decimal(quality.get("score"))
    is_open=bool(row.get("is_open"))
    reasons=[]
    action="APPROVE"
    severity="good"

    if decision_report:
        report_action=str(decision_report.get("action") or "").replace("_"," ")
        if report_action:
            reasons.append(report_action)
        for blocker in decision_report.get("blockers") or []:
            reasons.append(str(blocker))
        if option_grade.get("grade")=="C":
            action="BLOCK" if is_open else "LEARN_MISTAKE"
            severity="bad"
            reasons.append("option grade C is analysis-only")
        elif option_grade.get("grade") in {"A","B"}:
            reasons.append(f"option grade {option_grade.get('grade')} passed")
        if sentiment.get("alignment")=="support":
            reasons.append(f"sentiment supports trade ({sentiment.get('label','neutral')}, {sentiment.get('articles',0)} articles)")
        elif sentiment.get("alignment")=="conflict":
            if action=="APPROVE":
                action="WATCH"
            severity="warn" if severity!="bad" else severity
            reasons.append(f"sentiment conflicts with trade ({sentiment.get('label','neutral')}, {sentiment.get('articles',0)} articles)")
        elif sentiment.get("available") is False:
            reasons.append("sentiment unavailable/neutral fallback")

    geometry_ok=_geometry_ok(side,entry,stop_loss,take_profit)
    if not geometry_ok:
        action="BLOCK" if is_open else "INVALID_REVIEW"
        severity="bad"
        reasons.append("SL/TP geometry is not aligned with trade side")

    option_type = instrument_type if instrument_type in {"CE","PE"} else str(note.get("fallback_from_option") or "").upper()
    option_side = side
    if "CE BUY" in route:
        option_type, option_side = "CE", "BUY"
    elif "PE BUY" in route:
        option_type, option_side = "PE", "BUY"
    elif "CE SELL" in route:
        option_type, option_side = "CE", "SELL"
    elif "PE SELL" in route:
        option_type, option_side = "PE", "SELL"
    elif "CALL_BUY" in strategy:
        option_type, option_side = "CE", "BUY"
    elif "PUT_BUY" in strategy:
        option_type, option_side = "PE", "BUY"
    elif "CALL_SELL" in strategy:
        option_type, option_side = "CE", "SELL"
    elif "PUT_SELL" in strategy:
        option_type, option_side = "PE", "SELL"

    strategy_direction=_option_route_direction(option_type,option_side)
    execution_direction=1 if side=="BUY" else -1 if side=="SELL" else 0
    if instrument_type in {"EQ","FUT"} and strategy_direction and execution_direction and strategy_direction!=execution_direction:
        action="BLOCK" if is_open else "INVALID_REVIEW"
        severity="bad"
        reasons.append("strategy direction conflicts with actual execution side")

    if bias=="call" and strategy_direction<0:
        action="BLOCK" if is_open else "INVALID_REVIEW"
        severity="bad"
        reasons.append("bearish route conflicts with bullish session bias")
    if bias=="put" and strategy_direction>0:
        action="BLOCK" if is_open else "INVALID_REVIEW"
        severity="bad"
        reasons.append("bullish route conflicts with bearish session bias")

    notional=max(Decimal("1"),entry*max(Decimal("1"),_decimal(row.get("quantity"),"1")))
    pnl_pct=pnl/notional*Decimal("100")
    risk_distance=abs(entry-stop_loss)
    reward_distance=abs(take_profit-entry)
    rr=reward_distance/risk_distance if risk_distance>0 else Decimal("0")
    if rr and rr<Decimal("1.25"):
        severity="warn" if severity!="bad" else severity
        if action=="APPROVE":
            action="WATCH"
        reasons.append(f"reward/risk is weak at {rr:.2f}")

    if is_open:
        if pnl < -(fees + Decimal("25")):
            action="EXIT_NOW" if severity!="bad" else action
            severity="bad"
            reasons.append("open trade is losing beyond fees plus buffer")
        elif pnl > fees + Decimal("75"):
            action="PROTECT_PROFIT" if action=="APPROVE" else action
            severity="good" if severity!="bad" else severity
            reasons.append("move SL into net-profit protection if latest bar confirms")
        elif abs(pnl_pct) < Decimal("0.05"):
            if action=="APPROVE":
                action="WATCH"
            reasons.append("trade is flat; wait for confirmation or time exit")
    else:
        if pnl>0:
            action="LEARN_WINNER"
            severity="good"
            reasons.append("closed trade was profitable; mine similar conditions")
        elif pnl<0:
            action="LEARN_MISTAKE"
            severity="bad"
            reasons.append("closed trade lost money; avoid similar setup until reviewed")

    if quality_score and quality_score<Decimal("52"):
        severity="bad"
        if action=="APPROVE":
            action="WATCH"
        reasons.append(f"quality grade is weak ({quality_score})")
    elif quality_score and quality_score<Decimal("68"):
        if action=="APPROVE":
            action="WATCH"
        reasons.append(f"quality is only moderate ({quality_score})")

    if not reasons:
        reasons.append("strategy route, risk geometry and current state are aligned")

    return {
        "persona":"Senior Trade Agent",
        "experience_label":"20-year-style rulebook",
        "action":action,
        "severity":severity,
        "decision_report":decision_report,
        "sentiment":sentiment,
        "option_grade":option_grade,
        "strategy_direction":"bullish" if strategy_direction>0 else "bearish" if strategy_direction<0 else "neutral",
        "execution_direction":"bullish" if execution_direction>0 else "bearish" if execution_direction<0 else "neutral",
        "geometry_ok":geometry_ok,
        "rr":float(round(rr,4)) if rr else 0,
        "reasons":reasons[:4],
        "orders_allowed":False,
    }
