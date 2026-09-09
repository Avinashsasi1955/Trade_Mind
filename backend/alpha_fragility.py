"""Alpha fragility diagnostics for live-shadow trading.

This module implements the "reject fragile alpha before the market exposes it"
idea as a read-only robustness layer.  It intentionally does not place trades
or bypass the Senior/risk gates.  It scores today's shadow trades, candidate
rejections, and Senior missed-opportunity memory against simple adversarial
stress cases so Operations/Copilot can explain when an apparent edge is weak.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List

from sqlalchemy import create_engine, text


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _jsonable(row: Any) -> Dict:
    data = dict(row or {})
    for key, value in list(data.items()):
        if isinstance(value, Decimal):
            data[key] = float(value)
        elif hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


def _reason_map(rows: Iterable[Any]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for row in rows or []:
        data = dict(row)
        reason = str(data.get("reason") or "unknown")
        result[reason] = _int(data.get("count"))
    return result


@dataclass
class FragilityPenalty:
    reason: str
    points: float
    detail: str


def alpha_fragility_status(database_url: str | None, *, limit: int = 80) -> Dict:
    """Return today's alpha robustness/fragility snapshot.

    The score is deliberately conservative.  A low score means the evidence is
    fragile or incomplete; it is *not* a prediction that the next trade will
    lose.  A high score means the day/setup has survived more of the project's
    stress lenses, but still remains paper-only evidence.
    """

    if not database_url:
        return {
            "status": "unavailable",
            "reason": "DATABASE_URL missing",
            "mode": "READ_ONLY_ALPHA_GUARD",
            "orders_allowed": False,
        }

    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            trade = connection.execute(text("""
                SELECT COUNT(*) total_trades,
                       COUNT(*) FILTER(WHERE audit_status='RECONCILED' AND net_pnl IS NULL) open_trades,
                       COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
                       COUNT(*) FILTER(WHERE net_pnl > 0) winning_trades,
                       COUNT(*) FILTER(WHERE net_pnl < 0) losing_trades,
                       COUNT(*) FILTER(WHERE exit_reason='STOP_LOSS') stop_loss_hits,
                       COUNT(*) FILTER(WHERE exit_reason ILIKE '%PROFIT%' OR exit_reason ILIKE '%TARGET%') profit_exits,
                       COUNT(*) FILTER(WHERE COALESCE(exit_reason,'') ILIKE '%TIME%') time_exits,
                       COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) realised_pnl,
                       COALESCE(AVG(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) avg_closed_pnl,
                       COALESCE(MIN(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) worst_trade_pnl,
                       COALESCE(MAX(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) best_trade_pnl,
                       COALESCE(AVG(signal_probability) FILTER(WHERE signal_probability IS NOT NULL),0) avg_probability
                FROM shadow_execution_audits
                WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
            """)).mappings().one()

            side_rows = connection.execute(text("""
                SELECT a.side, i.instrument_type, COUNT(*) trades,
                       COALESCE(SUM(a.net_pnl) FILTER(WHERE a.net_pnl IS NOT NULL),0) pnl
                FROM shadow_execution_audits a
                JOIN instrument_master i ON i.id=a.instrument_id
                WHERE (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                GROUP BY a.side, i.instrument_type
                ORDER BY trades DESC
            """)).mappings().all()

            candidate = connection.execute(text("""
                SELECT COUNT(*) evaluated,
                       COUNT(*) FILTER(WHERE accepted) accepted,
                       COUNT(*) FILTER(WHERE NOT accepted) rejected,
                       COALESCE(AVG(quality_score) FILTER(WHERE quality_score IS NOT NULL),0) avg_quality,
                       COALESCE(AVG(rr) FILTER(WHERE rr IS NOT NULL),0) avg_rr,
                       COALESCE(AVG(expected_net_edge_bps) FILTER(WHERE expected_net_edge_bps IS NOT NULL),0) avg_edge_bps
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
            """)).mappings().one()

            candidate_reason_rows = connection.execute(text("""
                SELECT COALESCE(rejection_reason,'accepted') reason, COUNT(*) count
                FROM trade_candidate_audits
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                  AND NOT accepted
                GROUP BY COALESCE(rejection_reason,'accepted')
                ORDER BY count DESC
                LIMIT 8
            """)).mappings().all()

            senior = connection.execute(text("""
                SELECT COUNT(*) scanned,
                       COUNT(*) FILTER(WHERE senior_decision='TRADED') traded,
                       COUNT(*) FILTER(WHERE senior_decision<>'TRADED') missed,
                       COUNT(*) FILTER(WHERE outcome_label='would_have_worked') missed_worked,
                       COUNT(*) FILTER(WHERE opportunity_side='CALL') call_setups,
                       COUNT(*) FILTER(WHERE opportunity_side='PUT') put_setups,
                       COALESCE(AVG(confidence),0) avg_confidence,
                       COALESCE(AVG(risk_reward),0) avg_rr
                FROM senior_market_opportunities
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
            """)).mappings().one()

            strategy_rows = connection.execute(text("""
                SELECT COALESCE(strategy,'unknown') strategy,
                       COALESCE(session_block,'unknown') session_block,
                       COALESCE(regime,'unknown') regime,
                       COUNT(*) scanned,
                       COUNT(*) FILTER(WHERE senior_decision='TRADED') traded,
                       COUNT(*) FILTER(WHERE outcome_label='would_have_worked') missed_worked,
                       COALESCE(AVG(confidence),0) avg_confidence,
                       COALESCE(AVG(risk_reward),0) avg_rr
                FROM senior_market_opportunities
                WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                GROUP BY COALESCE(strategy,'unknown'), COALESCE(session_block,'unknown'), COALESCE(regime,'unknown')
                ORDER BY missed_worked DESC, scanned DESC, avg_confidence DESC
                LIMIT :limit
            """), {"limit": limit}).mappings().all()

            mistake_rows = connection.execute(text("""
                SELECT tag reason, COUNT(*) count
                FROM (
                    SELECT UNNEST(ARRAY[
                        CASE WHEN COALESCE(strategy_note,'') ILIKE '%stale_signal_loss%' THEN 'stale_signal_loss' END,
                        CASE WHEN COALESCE(strategy_note,'') ILIKE '%fallback_fill%' THEN 'fallback_fill' END,
                        CASE WHEN COALESCE(strategy_note,'') ILIKE '%cost_drag%' THEN 'cost_drag' END,
                        CASE WHEN exit_reason='STOP_LOSS' THEN 'stop_loss_hit' END,
                        CASE WHEN COALESCE(exit_reason,'') ILIKE '%TIME%' THEN 'late_time_exit' END
                    ]) AS tag
                    FROM shadow_execution_audits
                    WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                ) tags
                WHERE tag IS NOT NULL
                GROUP BY tag
                ORDER BY count DESC
                LIMIT 8
            """)).mappings().all()

            recent_trades = connection.execute(text("""
                SELECT a.signal_at, i.symbol, i.instrument_type, a.side, a.signal_probability,
                       a.decision_price, a.theoretical_fill_price, a.net_pnl, a.exit_reason,
                       a.strategy_note
                FROM shadow_execution_audits a
                JOIN instrument_master i ON i.id=a.instrument_id
                WHERE (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                ORDER BY a.signal_at DESC, a.id DESC
                LIMIT 12
            """)).mappings().all()
    except Exception as exc:
        return {
            "status": "error",
            "reason": str(exc),
            "mode": "READ_ONLY_ALPHA_GUARD",
            "orders_allowed": False,
        }
    finally:
        engine.dispose()

    total_trades = _int(trade["total_trades"])
    closed_trades = _int(trade["closed_trades"])
    losing_trades = _int(trade["losing_trades"])
    stop_loss_hits = _int(trade["stop_loss_hits"])
    realised_pnl = _float(trade["realised_pnl"])
    worst_trade = _float(trade["worst_trade_pnl"])
    best_trade = _float(trade["best_trade_pnl"])
    evaluated = _int(candidate["evaluated"])
    accepted = _int(candidate["accepted"])
    rejected = _int(candidate["rejected"])
    missed_worked = _int(senior["missed_worked"])
    scanned = _int(senior["scanned"])
    traded_visible = _int(senior["traded"])

    penalties: List[FragilityPenalty] = []

    if closed_trades < 20:
        penalties.append(FragilityPenalty(
            "low_live_sample",
            18 if closed_trades == 0 else max(4, 18 - closed_trades * 0.6),
            f"Only {closed_trades} closed paper trades today; fragile until more full-session evidence exists.",
        ))
    if realised_pnl < 0:
        penalties.append(FragilityPenalty(
            "negative_realised_pnl",
            min(22, abs(realised_pnl) / 75),
            f"Today's realised paper P&L is {realised_pnl:.2f}.",
        ))
    if closed_trades and losing_trades / closed_trades > 0.45:
        penalties.append(FragilityPenalty(
            "loss_rate_high",
            min(16, (losing_trades / closed_trades - 0.45) * 40),
            f"{losing_trades}/{closed_trades} closed trades are losses.",
        ))
    if closed_trades and stop_loss_hits / closed_trades > 0.25:
        penalties.append(FragilityPenalty(
            "stop_loss_noise",
            min(18, (stop_loss_hits / closed_trades - 0.25) * 48),
            f"{stop_loss_hits} stop-loss exits; check whether stops are inside normal noise.",
        ))
    if evaluated and accepted / evaluated < 0.03 and traded_visible == 0:
        penalties.append(FragilityPenalty(
            "over_strict_selector",
            10,
            f"{accepted}/{evaluated} candidates accepted; no trades means the day teaches less.",
        ))
    if scanned and missed_worked / scanned > 0.12:
        penalties.append(FragilityPenalty(
            "missed_opportunity_alpha",
            min(18, (missed_worked / scanned) * 45),
            f"{missed_worked}/{scanned} Senior visible setups later worked counterfactually.",
        ))
    if rejected and evaluated and rejected / evaluated > 0.90 and missed_worked:
        penalties.append(FragilityPenalty(
            "policy_rejected_working_setups",
            8,
            "Most candidates were rejected while missed-opportunity tracker found working setups.",
        ))

    side_counts: Dict[str, int] = {}
    instrument_counts: Dict[str, int] = {}
    for row in side_rows:
        side_counts[str(row["side"] or "unknown")] = side_counts.get(str(row["side"] or "unknown"), 0) + _int(row["trades"])
        inst = str(row["instrument_type"] or "unknown")
        instrument_counts[inst] = instrument_counts.get(inst, 0) + _int(row["trades"])
    if total_trades:
        dominant = max(side_counts.values() or [0])
        if dominant / total_trades > 0.80 and len(side_counts) <= 1:
            penalties.append(FragilityPenalty(
                "one_direction_bias",
                8,
                f"Trades are concentrated in one direction: {side_counts}.",
            ))
        if instrument_counts.get("CE", 0) + instrument_counts.get("PE", 0) == 0 and scanned:
            penalties.append(FragilityPenalty(
                "option_route_not_exercised",
                4,
                "Options opportunities exist, but actual ledger has no CE/PE evidence today.",
            ))

    mistake_map = _reason_map(mistake_rows)
    if mistake_map.get("stale_signal_loss", 0):
        penalties.append(FragilityPenalty(
            "stale_signal_loss",
            min(14, mistake_map["stale_signal_loss"] * 2.5),
            "Stale signal losses indicate entries/exits are too late for the current regime.",
        ))
    if mistake_map.get("cost_drag", 0):
        penalties.append(FragilityPenalty(
            "cost_drag",
            min(10, mistake_map["cost_drag"] * 2),
            "Cost drag is visible; tiny edge trades are fragile after fees/slippage.",
        ))

    robust_score = _clamp(100 - sum(p.points for p in penalties))
    fragility_score = round(100 - robust_score, 1)
    robust_score = round(robust_score, 1)
    worst_case_pnl = round(realised_pnl + worst_trade - abs(worst_trade) * 0.35 - max(25.0, total_trades * 3.5), 2)
    win_rate = round((_int(trade["winning_trades"]) / closed_trades * 100), 1) if closed_trades else 0.0

    if robust_score >= 75 and closed_trades >= 20 and realised_pnl >= 0:
        status = "robust_candidate"
        robust = True
    elif robust_score < 55 or realised_pnl < 0:
        status = "fragile"
        robust = False
    else:
        status = "needs_more_evidence"
        robust = False

    stress_tests = [
        {
            "name": "late_entry_1_candle",
            "result": "fail" if mistake_map.get("stale_signal_loss", 0) or worst_case_pnl < 0 else "watch",
            "impact": "Reject/exit faster when signal is stale before fill.",
        },
        {
            "name": "spread_widening",
            "result": "fail" if mistake_map.get("cost_drag", 0) or _float(candidate["avg_edge_bps"]) < 25 else "pass",
            "impact": "Avoid tiny expected-profit trades where fees/slippage dominate.",
        },
        {
            "name": "stop_loss_slip",
            "result": "fail" if stop_loss_hits > max(1, closed_trades * 0.25) else "pass",
            "impact": "Widen/avoid stops only if R:R remains above threshold.",
        },
        {
            "name": "regime_mismatch",
            "result": "fail" if missed_worked and traded_visible == 0 else "watch",
            "impact": "Compare session/regime setups against rejected candidate rules.",
        },
        {
            "name": "direction_crowding",
            "result": "fail" if total_trades and max(side_counts.values() or [0]) / max(1, total_trades) > 0.80 and len(side_counts) <= 1 else "pass",
            "impact": "Recalculate CALL/PUT or BUY/SELL per symbol, not once for the market.",
        },
    ]

    recommendations = [
        "Use this layer as a rejection/size-reduction warning, not as a profit guarantee.",
        "Require robust_score ≥ 75 before using a day as strong model-learning evidence.",
        "Study missed_worked setups before relaxing Senior/chart gates.",
        "Keep 1s data as entry/exit confirmation until true continuous 1s storage is proven.",
    ]
    if any(p.reason == "stale_signal_loss" for p in penalties):
        recommendations.insert(1, "Tighten stale-signal rejection and profit-lock exits before increasing trade count.")
    if any(p.reason == "over_strict_selector" for p in penalties):
        recommendations.insert(1, "No-trade days still need counterfactual review; selector may be too strict for that session regime.")
    if any(p.reason == "one_direction_bias" for p in penalties):
        recommendations.insert(1, "Verify each stock route independently so PUT/SELL setups can reach the ledger when valid.")

    return {
        "status": status,
        "mode": "READ_ONLY_ALPHA_GUARD",
        "robust": robust,
        "robust_score": robust_score,
        "fragility_score": fragility_score,
        "failure_reasons": [
            {"reason": p.reason, "points": round(p.points, 1), "detail": p.detail}
            for p in sorted(penalties, key=lambda item: item.points, reverse=True)
        ],
        "worst_case_pnl": worst_case_pnl,
        "stress_tests": stress_tests,
        "trade_evidence": {
            "total_trades": total_trades,
            "open_trades": _int(trade["open_trades"]),
            "closed_trades": closed_trades,
            "win_rate_pct": win_rate,
            "realised_pnl": round(realised_pnl, 2),
            "avg_closed_pnl": round(_float(trade["avg_closed_pnl"]), 2),
            "best_trade_pnl": round(best_trade, 2),
            "worst_trade_pnl": round(worst_trade, 2),
            "stop_loss_hits": stop_loss_hits,
            "profit_exits": _int(trade["profit_exits"]),
            "time_exits": _int(trade["time_exits"]),
            "avg_probability": round(_float(trade["avg_probability"]) * 100, 1),
            "side_distribution": side_counts,
            "instrument_distribution": instrument_counts,
            "mistake_tags": mistake_map,
        },
        "candidate_evidence": {
            "evaluated": evaluated,
            "accepted": accepted,
            "rejected": rejected,
            "accept_rate_pct": round(accepted / evaluated * 100, 2) if evaluated else 0.0,
            "avg_quality": round(_float(candidate["avg_quality"]), 1),
            "avg_rr": round(_float(candidate["avg_rr"]), 2),
            "avg_edge_bps": round(_float(candidate["avg_edge_bps"]), 1),
            "rejection_reasons": _reason_map(candidate_reason_rows),
        },
        "senior_opportunity_evidence": {
            "scanned": scanned,
            "traded": traded_visible,
            "missed": _int(senior["missed"]),
            "missed_worked": missed_worked,
            "call_setups": _int(senior["call_setups"]),
            "put_setups": _int(senior["put_setups"]),
            "avg_confidence": round(_float(senior["avg_confidence"]), 1),
            "avg_rr": round(_float(senior["avg_rr"]), 2),
            "regime_strategy_memory": [_jsonable(row) for row in strategy_rows[:12]],
        },
        "recent_trades": [_jsonable(row) for row in recent_trades],
        "recommendations": recommendations[:8],
        "explainability": "Minimax-style guard: accept evidence only if it survives late entry, spread, stop-slip, regime mismatch and direction-crowding stress.",
        "paper_only": True,
        "orders_allowed": False,
    }
