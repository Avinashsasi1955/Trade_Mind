"""Senior market regime and missed-opportunity intelligence.

Paper-only evidence layer.  It challenges the Senior selector by recording the
best visible setups, rejected/missed opportunities, and later counterfactual
outcomes.  It never places orders and never overrides risk gates.
"""
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS senior_market_opportunities(
    id BIGSERIAL PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    session_block TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT,
    instrument_type TEXT,
    opportunity_side TEXT NOT NULL,
    option_route TEXT,
    regime TEXT NOT NULL,
    strategy TEXT NOT NULL,
    confidence NUMERIC(8,2) NOT NULL,
    quality_score NUMERIC(8,2) NOT NULL DEFAULT 0,
    risk_reward NUMERIC(8,3) NOT NULL DEFAULT 0,
    ml_probability NUMERIC(10,6) NOT NULL DEFAULT 0.5,
    senior_decision TEXT NOT NULL,
    rejection_reason TEXT,
    actual_trade_audit_id BIGINT,
    counterfactual_5m NUMERIC(14,4),
    counterfactual_15m NUMERIC(14,4),
    counterfactual_30m NUMERIC(14,4),
    outcome_label TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_senior_market_opportunities_observed
    ON senior_market_opportunities(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_senior_market_opportunities_symbol
    ON senior_market_opportunities(symbol, observed_at DESC);
"""


def ensure_schema(connection) -> None:
    connection.execute(text(SCHEMA_SQL))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _session_block(watermark: datetime) -> str:
    ist = watermark.astimezone(ZoneInfo("Asia/Kolkata")).time()
    if ist < datetime.strptime("09:45", "%H:%M").time():
        return "opening"
    if ist < datetime.strptime("11:30", "%H:%M").time():
        return "morning"
    if ist < datetime.strptime("13:30", "%H:%M").time():
        return "midday"
    if ist < datetime.strptime("15:00", "%H:%M").time():
        return "afternoon"
    return "closing"


def _ema(values: List[float], span: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def _regime_and_score(bars: List[Dict]) -> Dict:
    if len(bars) < 6:
        return {"regime": "insufficient", "signal": 0, "confidence": 0, "reason": "less than 6 bars"}
    closes = [_safe_float(bar.get("close")) for bar in bars]
    highs = [_safe_float(bar.get("high")) for bar in bars]
    lows = [_safe_float(bar.get("low")) for bar in bars]
    volumes = [max(0, int(bar.get("volume") or 0)) for bar in bars]
    last = closes[-1]
    base = closes[max(0, len(closes) - 7)]
    move = (last / base - 1) if base else 0
    ema5 = _ema(closes, 5)[-1]
    ema13 = _ema(closes, 13 if len(closes) >= 13 else max(6, len(closes) // 2))[-1]
    atr_window = min(10, len(bars))
    atr = sum(max(0.01, h - l) for h, l in zip(highs[-atr_window:], lows[-atr_window:])) / max(1, atr_window)
    prior_high = max(highs[-min(12, len(highs)):-1])
    prior_low = min(lows[-min(12, len(lows)):-1])
    avg_volume = sum(volumes[-min(12, len(volumes)):-1]) / max(1, len(volumes[-min(12, len(volumes)):-1]))
    volume_ratio = volumes[-1] / avg_volume if avg_volume else 1.0
    trend_up = last >= ema5 >= ema13
    trend_down = last <= ema5 <= ema13
    breakout_up = last > prior_high
    breakout_down = last < prior_low
    signal = 1 if trend_up and (breakout_up or move > 0.001) else -1 if trend_down and (breakout_down or move < -0.001) else 0
    if signal > 0:
        regime = "trend_up_breakout" if breakout_up else "trend_up_pullback"
    elif signal < 0:
        regime = "trend_down_breakdown" if breakout_down else "trend_down_pullback"
    else:
        regime = "range_or_chop"
    confidence = 35
    confidence += 22 if abs(move) >= 0.0015 else 10 if abs(move) >= 0.0008 else 0
    confidence += 18 if breakout_up or breakout_down else 8 if signal else 0
    confidence += 14 if volume_ratio >= 1.1 else 7 if volume_ratio >= 0.8 else 0
    confidence += 12 if (trend_up and signal > 0) or (trend_down and signal < 0) else 0
    rr = max(0.8, min(3.0, (atr * 1.35) / max(0.01, atr * 0.72)))
    return {
        "regime": regime,
        "signal": signal,
        "confidence": round(min(100, confidence), 2),
        "risk_reward": round(rr, 2),
        "volume_ratio": round(volume_ratio, 3),
        "recent_move_pct": round(move * 100, 4),
        "last": last,
        "atr": round(atr, 4),
        "reason": f"{regime}; move {move*100:.2f}%; volume {volume_ratio:.2f}x",
    }


def _candidate_row(item: Dict, accepted_ids: set) -> Dict:
    bars = item.get("intraday") or []
    scan = _regime_and_score(bars)
    chart = item.get("chart_gate") or {}
    policy = item.get("policy_candidate")
    probability = _safe_float(item.get("probability"), 0.5)
    structure = chart.get("structure") or {}
    signal = int(
        chart.get("signal")
        or structure.get("direction")
        or item.get("paper_probe_signal")
        or item.get("signal")
        or scan.get("signal")
        or 0
    )
    side = "CALL" if signal > 0 else "PUT" if signal < 0 else "WAIT"
    strategy = chart.get("strategy") or ("SENIOR_VISIBLE_CALL" if signal > 0 else "SENIOR_VISIBLE_PUT" if signal < 0 else "NO_TRADE")
    accepted = item.get("instrument_id") in accepted_ids and bool(chart.get("accepted")) and strategy != "NO_TRADE"
    rejection = None
    if not accepted:
        if not chart.get("accepted"):
            rejection = chart.get("reason") or "chart_gate_rejected"
        elif policy and not getattr(policy, "accepted", False) and not item.get("senior_opportunity"):
            rejection = "model_policy_rejected"
        else:
            rejection = "senior_selector_or_risk_gate_rejected"
    quality = _safe_float((item.get("_entry_quality") or {}).get("score"))
    if not quality:
        quality = min(100.0, max(0.0, _safe_float((chart.get("structure") or {}).get("confidence")) * 0.55 + scan["confidence"] * 0.45))
    score = min(100.0, max(0.0, scan["confidence"] + min(12, abs(probability - 0.5) * 100) + min(10, _safe_float(chart.get("rr")) * 2)))
    return {
        "symbol": item.get("symbol"),
        "exchange": item.get("exchange"),
        "instrument_type": item.get("instrument_type"),
        "opportunity_side": side,
        "option_route": f"{chart.get('side','')} {chart.get('option_type','')}".strip() or None,
        "regime": scan["regime"],
        "strategy": strategy,
        "confidence": round(score, 2),
        "quality_score": quality,
        "risk_reward": _safe_float(chart.get("rr"), scan.get("risk_reward", 0)),
        "ml_probability": probability,
        "senior_decision": "TRADED" if accepted else "MISSED_OR_REJECTED",
        "rejection_reason": rejection,
        "details": {"chart_gate": chart, "scan": scan,
                    "instrument_local_direction": item.get("_instrument_local_direction") or chart.get("local_direction") or {},
                    "session": item.get("_session_case") or {}, "policy_signal": int(item.get("signal") or 0),
                    "derivative_ticket": item.get("derivative_ticket")},
    }


def record_opportunity_scan(connection, watermark: datetime, candidates: Iterable[Dict],
                            selected: Iterable[Dict], limit: int = 5) -> Dict:
    ensure_schema(connection)
    selected_ids = {item.get("instrument_id") for item in selected}
    rows = [_candidate_row(item, selected_ids) for item in candidates if item.get("symbol")]
    rows = [row for row in rows if row["opportunity_side"] in {"CALL", "PUT"}]
    rows.sort(key=lambda row: (row["senior_decision"] == "TRADED", row["confidence"], row["risk_reward"]), reverse=True)
    deduped = {}
    for row in rows:
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("exchange") or "").upper(),
            str(row.get("instrument_type") or "").upper(),
            str(row.get("opportunity_side") or "").upper(),
            str(row.get("strategy") or "").upper(),
        )
        current = deduped.get(key)
        if current is None or (row["senior_decision"] == "TRADED", row["confidence"], row["risk_reward"]) > (
            current["senior_decision"] == "TRADED", current["confidence"], current["risk_reward"]
        ):
            deduped[key] = row
    rows = list(deduped.values())
    rows.sort(key=lambda row: (row["senior_decision"] == "TRADED", row["confidence"], row["risk_reward"]), reverse=True)
    rows = rows[:max(1, limit)]
    session = _session_block(watermark)
    for row in rows:
        connection.execute(text("""
            INSERT INTO senior_market_opportunities(
                observed_at,session_block,symbol,exchange,instrument_type,opportunity_side,option_route,
                regime,strategy,confidence,quality_score,risk_reward,ml_probability,senior_decision,
                rejection_reason,details
            ) VALUES (
                :observed_at,:session_block,:symbol,:exchange,:instrument_type,:opportunity_side,:option_route,
                :regime,:strategy,:confidence,:quality_score,:risk_reward,:ml_probability,:senior_decision,
                :rejection_reason,CAST(:details AS jsonb)
            )
        """), {**row, "observed_at": watermark, "session_block": session, "details": json.dumps(row["details"], default=str)})
    return {"status": "success", "recorded": len(rows), "top": rows, "session_block": session}


def update_counterfactuals(connection, watermark: Optional[datetime] = None) -> Dict:
    ensure_schema(connection)
    watermark = watermark or datetime.now(timezone.utc)
    rows = connection.execute(text("""
        SELECT id,observed_at,symbol,opportunity_side,details
        FROM senior_market_opportunities
        WHERE observed_at <= :watermark - INTERVAL '5 minutes'
          AND (counterfactual_5m IS NULL OR counterfactual_15m IS NULL OR counterfactual_30m IS NULL)
        ORDER BY observed_at DESC
        LIMIT 200
    """), {"watermark": watermark}).mappings().all()
    updated = 0
    for row in rows:
        payload = row.get("details") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        entry = _safe_float((payload.get("scan") or {}).get("last"))
        if not entry:
            continue
        values = {}
        for minutes in (5, 15, 30):
            price = connection.execute(text("""
                SELECT b.close_price
                FROM live_market_bars b
                JOIN instrument_master i ON i.id=b.instrument_id
                WHERE i.symbol=:symbol AND b.interval='5minute'
                  AND b.bar_time>=:target_time
                ORDER BY b.bar_time ASC
                LIMIT 1
            """), {"symbol": row["symbol"], "target_time": row["observed_at"] + timedelta(minutes=minutes)}).scalar_one_or_none()
            if price is not None:
                direction = 1 if row["opportunity_side"] == "CALL" else -1
                values[f"counterfactual_{minutes}m"] = round((float(price) - entry) * direction, 4)
        if not values:
            continue
        best = max(values.values())
        worst = min(values.values())
        label = "would_have_worked" if best > abs(worst) and best > 0 else "would_have_failed" if worst < 0 else "unclear"
        connection.execute(text("""
            UPDATE senior_market_opportunities
            SET counterfactual_5m=COALESCE(:counterfactual_5m,counterfactual_5m),
                counterfactual_15m=COALESCE(:counterfactual_15m,counterfactual_15m),
                counterfactual_30m=COALESCE(:counterfactual_30m,counterfactual_30m),
                outcome_label=:outcome_label
            WHERE id=:id
        """), {"id": row["id"], "outcome_label": label,
               "counterfactual_5m": values.get("counterfactual_5m"),
               "counterfactual_15m": values.get("counterfactual_15m"),
               "counterfactual_30m": values.get("counterfactual_30m")})
        updated += 1
    return {"status": "success", "updated": updated}
