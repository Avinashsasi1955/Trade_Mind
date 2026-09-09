"""Deterministic quality scoring for shadow-paper trades.

The score is intentionally explainable and paper-only.  It does not predict
profit; it grades whether the trade had enough evidence, risk control and exit
discipline to be useful for model improvement.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Dict, Iterable, Mapping


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except Exception:
        return default


def _note(value) -> Dict:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {"raw": str(value)}
    except Exception:
        return {"raw": str(value)}


def _rr(note: Dict) -> float:
    if note.get("rr") is not None:
        return _num(note.get("rr"))
    text = " ".join(str(v) for v in note.values())
    found = re.search(r"R:R\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    return _num(found.group(1)) if found else 0.0


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_trade(row: Mapping) -> Dict:
    entry = _num(row.get("entry_price") or row.get("theoretical_fill_price") or row.get("decision_price"))
    latest = _num(row.get("latest_price") or row.get("realised_exit_price") or entry)
    qty = max(0, int(_num(row.get("quantity"))))
    side = str(row.get("side") or "BUY").upper()
    pnl = _num(row.get("marked_pnl") if row.get("marked_pnl") is not None else row.get("net_pnl"))
    fees = _num(row.get("estimated_fees"))
    sl = _num(row.get("stop_loss_price"))
    tp = _num(row.get("take_profit_price"))
    probability = _num(row.get("signal_probability"), 0.5)
    exit_reason = str(row.get("exit_reason") or ("OPEN" if row.get("is_open") else "")).upper()
    fill_source = str(row.get("fill_source") or "")
    note = _note(row.get("improvement_note"))
    raw_note = " ".join(str(v) for v in note.values()).lower()
    mistake_tags = set(str(item) for item in (_note(row.get("mistake_tags")).get("tags", []) if isinstance(_note(row.get("mistake_tags")), dict) else []))
    if not mistake_tags and row.get("mistake_tags"):
        try:
            parsed_tags = json.loads(str(row.get("mistake_tags")))
            mistake_tags = set(str(item) for item in parsed_tags if isinstance(parsed_tags, list))
        except Exception:
            mistake_tags = {str(row.get("mistake_tags"))}
    rr = _rr(note)
    notional = max(1.0, entry * max(1, qty))

    edge = abs(probability - 0.5) * 2
    entry_quality = _clip(45 + edge * 45)
    if "fresh live feed" in raw_note or "live candles" in raw_note:
        entry_quality += 8
    if "fallback" in raw_note or fill_source != "DEPTH_SNAPSHOT":
        entry_quality -= 8
    if "stale_signal_loss" in mistake_tags:
        entry_quality -= 12
    entry_quality = _clip(entry_quality)

    trend_alignment = 55
    if any(key in raw_note for key in ("breakout", "breakdown", "pullback", "momentum")):
        trend_alignment += 25
    if "range" in raw_note:
        trend_alignment += 10
    if "not confirmed" in raw_note or "no_trade" in raw_note:
        trend_alignment -= 35
    trend_alignment = _clip(trend_alignment)

    volume_confirmation = 60
    if "volume" in raw_note and "passed" in raw_note:
        volume_confirmation += 25
    if "zero live range" in raw_note or "zero intraday range" in raw_note:
        volume_confirmation -= 45
    volume_confirmation = _clip(volume_confirmation)

    rr_quality = _clip(35 + min(rr, 3.0) / 3.0 * 65) if rr else 45

    sl_quality = 35
    if entry > 0 and sl > 0 and tp > 0:
        if side == "BUY":
            risk = max(0.0, entry - sl)
            reward = max(0.0, tp - entry)
        else:
            risk = max(0.0, sl - entry)
            reward = max(0.0, entry - tp)
        risk_pct = risk / entry if entry else 0
        actual_rr = reward / risk if risk else 0
        sl_quality = 55
        if 0.001 <= risk_pct <= 0.20:
            sl_quality += 20
        if actual_rr >= 1.25:
            sl_quality += 20
        if risk_pct > 0.30:
            sl_quality -= 25
    sl_quality = _clip(sl_quality)

    if exit_reason in {"PROFIT_CAPTURE", "TAKE_PROFIT", "TRAILING_STOP"}:
        exit_quality = 92
    elif exit_reason == "BREAKEVEN_STOP":
        exit_quality = 78
    elif exit_reason == "MANUAL_EXIT":
        exit_quality = 70 if pnl >= 0 else 45
    elif exit_reason == "TIME_EXIT":
        exit_quality = 65 if pnl >= 0 else 42
    elif exit_reason == "STOP_LOSS":
        exit_quality = 38 if pnl < 0 else 55
        if "stale_signal_loss" in mistake_tags:
            exit_quality -= 12
    elif exit_reason == "OPEN":
        exit_quality = 65 if pnl >= 0 else 50
    else:
        exit_quality = 55
    exit_quality = _clip(exit_quality)

    cost_drag = fees / max(1.0, abs(pnl) + fees)
    cost_score = _clip(100 - cost_drag * 85)

    final = (
        entry_quality * 0.18
        + trend_alignment * 0.16
        + volume_confirmation * 0.10
        + rr_quality * 0.16
        + sl_quality * 0.16
        + exit_quality * 0.16
        + cost_score * 0.08
    )
    label = "A" if final >= 82 else "B" if final >= 68 else "C" if final >= 52 else "D"
    parts = {
        "entry_quality": round(entry_quality, 1),
        "trend_alignment": round(trend_alignment, 1),
        "volume_confirmation": round(volume_confirmation, 1),
        "risk_reward_quality": round(rr_quality, 1),
        "sl_distance_quality": round(sl_quality, 1),
        "exit_quality": round(exit_quality, 1),
        "cost_drag_score": round(cost_score, 1),
    }
    weakest = min(parts.items(), key=lambda item: item[1])
    strongest = max(parts.items(), key=lambda item: item[1])
    return {
        "score": round(final, 1),
        "grade": label,
        "components": parts,
        "weakest": weakest[0],
        "strongest": strongest[0],
        "summary": f"Grade {label}: strongest {strongest[0].replace('_',' ')}, weakest {weakest[0].replace('_',' ')}.",
    }


def quality_feedback(scores: Iterable[Mapping]) -> Dict:
    items=[dict(item) for item in scores if item]
    if not items:
        return {
            "sample_size": 0,
            "average_score": 0,
            "weakest_components": {},
            "recommendations": ["No scored paper trades yet; start with live-feed paper trades before tuning the model."],
        }
    weakest=Counter(str(item.get("weakest") or "unknown") for item in items)
    component_totals={}
    component_counts={}
    for item in items:
        for name,value in dict(item.get("components") or {}).items():
            component_totals[name]=component_totals.get(name,0.0)+_num(value)
            component_counts[name]=component_counts.get(name,0)+1
    component_avgs={name:round(total/max(1,component_counts[name]),1) for name,total in component_totals.items()}
    ordered=weakest.most_common(3)
    recommendation_map={
        "entry_quality": "Tighten entry selection: require stronger model edge, fresher bars, and fewer fallback fills.",
        "trend_alignment": "Improve chart confirmation: prefer trades aligned with breakout, pullback, momentum, or clear range logic.",
        "volume_confirmation": "Avoid thin moves: require stronger volume or liquidity confirmation before entry.",
        "risk_reward_quality": "Reject setups with weak reward-to-risk; raise the minimum R:R before paper entry.",
        "sl_distance_quality": "Tune stop placement: stops should be close enough to control loss but not inside normal candle noise.",
        "exit_quality": "Improve exits: lock profit faster, reduce late time exits, and review stop-loss hits after good unrealised profit.",
        "cost_drag_score": "Reduce cost drag: avoid tiny expected-profit trades where fees and slippage dominate.",
    }
    return {
        "sample_size": len(items),
        "average_score": round(sum(_num(item.get("score")) for item in items)/len(items),2),
        "weakest_components": dict(ordered),
        "component_averages": component_avgs,
        "recommendations": [recommendation_map.get(name,f"Review {name.replace('_',' ')}.") for name,_ in ordered],
    }
