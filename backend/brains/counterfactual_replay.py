"""Automated Post-Market Counterfactual Replay Engine.

At 16:00 IST (post-market) or on-demand, this engine replays every rejected trade candidate
against actual forward 5-minute market bars to determine whether the rejection was:
- SAVED_LOSS: The trade would have hit Stop-Loss first (system protected capital, True Negative).
- MISSED_WIN: The trade would have reached Target first (gate was overly conservative, False Negative).
- NEUTRAL: Neither boundary reached, trade scratched or flat at EOD.

It compiles a comprehensive Gate Accuracy Score and Capital Preservation estimate.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger("nivesh.brains.counterfactual")
IST = ZoneInfo("Asia/Kolkata")

COUNTERFACTUAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS counterfactual_daily_audits (
    id BIGSERIAL PRIMARY KEY,
    audit_date DATE NOT NULL UNIQUE,
    total_rejected INTEGER NOT NULL DEFAULT 0,
    saved_losses INTEGER NOT NULL DEFAULT 0,
    missed_wins INTEGER NOT NULL DEFAULT 0,
    neutral_count INTEGER NOT NULL DEFAULT 0,
    gate_accuracy_pct NUMERIC(6, 2) NOT NULL DEFAULT 100.0,
    estimated_capital_saved NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
    gate_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_counterfactual_daily_audits_date 
    ON counterfactual_daily_audits(audit_date DESC);
"""


def ensure_counterfactual_schema(connection) -> None:
    """Ensure the daily audit aggregation table exists."""
    from backend.intelligence_memory import ensure_intelligence_schema
    ensure_intelligence_schema(connection)
    try:
        connection.execute(text(COUNTERFACTUAL_SCHEMA_SQL))
    except Exception as exc:
        logger.debug("Counterfactual schema initialization notice: %s", exc)


def run_post_market_counterfactual_replay(
    engine,
    trade_date: Optional[date] = None,
    *,
    redis_client: Optional[Any] = None,
    assumed_risk_per_trade: float = 1500.0,
) -> Dict[str, Any]:
    """Replay rejected candidates for trade_date forward through actual 5m bars.
    
    Args:
        engine: SQLAlchemy database engine
        trade_date: The date to audit (defaults to today in IST)
        redis_client: Optional redis client for caching
        assumed_risk_per_trade: Capital saved per avoided stop loss (default ₹1,500)
    
    Returns:
        Dict containing audit metrics, gate accuracy %, breakdown by reason, and replay count.
    """
    if trade_date is None:
        trade_date = datetime.now(IST).date()

    with engine.begin() as conn:
        ensure_counterfactual_schema(conn)

        # 1. Sync any fresh candidate audits into counterfactual_candidate_log
        from backend.intelligence_memory import persist_counterfactual_candidates
        persist_counterfactual_candidates(conn, limit=2000)

        # 2. Fetch rejected candidates for the session
        candidates = conn.execute(text("""
            SELECT id, candidate_audit_id, observed_at, symbol, instrument_id,
                   signal, route, strategy, selector_stage, rejection_reason,
                   rr, quality_score, details, outcome_label
            FROM counterfactual_candidate_log
            WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date = :trade_date
              AND accepted = FALSE
            ORDER BY observed_at ASC
        """), {"trade_date": trade_date}).mappings().all()

        total_rejected = len(candidates)
        saved_losses = 0
        missed_wins = 0
        neutral_count = 0
        gate_breakdown: Dict[str, Dict[str, int]] = {}
        sample_replays: List[Dict[str, Any]] = []

        eod_time = datetime.combine(trade_date, time(15, 30, 0), tzinfo=IST)

        for cand in candidates:
            reason = str(cand["rejection_reason"] or "unspecified_gate").strip()
            if reason not in gate_breakdown:
                gate_breakdown[reason] = {"total": 0, "saved_losses": 0, "missed_wins": 0, "neutral": 0}
            gate_breakdown[reason]["total"] += 1

            details = cand["details"] or {}
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except Exception:
                    details = {}

            target_info = details.get("target") or {}
            entry_price = float(target_info.get("entry_price") or details.get("decision_price") or 0.0)
            signal_val = int(cand["signal"] or 0)
            direction = 1 if signal_val >= 0 else -1

            inst_id = cand.get("instrument_id")
            if not inst_id and cand.get("symbol"):
                inst_id = conn.execute(text("SELECT id FROM instrument_master WHERE symbol = :sym ORDER BY is_tradable DESC LIMIT 1"), {"sym": cand["symbol"]}).scalar_one_or_none()

            # Determine entry price fallback
            if entry_price <= 0.0 and inst_id:
                bar_row = conn.execute(text("""
                    SELECT close_price FROM live_market_bars
                    WHERE instrument_id = :inst_id AND interval = '5minute'
                      AND bar_time <= :observed_at
                    ORDER BY bar_time DESC LIMIT 1
                """), {"inst_id": inst_id, "observed_at": cand["observed_at"]}).scalar_one_or_none()
                if bar_row:
                    entry_price = float(bar_row)

            if entry_price <= 0.0:
                neutral_count += 1
                gate_breakdown[reason]["neutral"] += 1
                continue

            # Determine SL and TP levels
            stop_loss = float(target_info.get("stop_loss_price") or target_info.get("stop_loss") or 0.0)
            target_price = float(target_info.get("target_price") or target_info.get("take_profit") or 0.0)
            cand_rr = float(cand["rr"] or 2.0)

            if stop_loss <= 0.0:
                stop_loss = entry_price * (1.0 - (0.012 * direction))
            if target_price <= 0.0:
                target_price = entry_price * (1.0 + (0.012 * cand_rr * direction))

            # Fetch forward 5m bars
            forward_bars = []
            if inst_id:
                forward_bars = conn.execute(text("""
                    SELECT bar_time, open_price, high_price, low_price, close_price
                    FROM live_market_bars
                    WHERE instrument_id = :inst_id
                      AND interval = '5minute'
                      AND bar_time >= :observed_at
                      AND bar_time <= :eod_time
                    ORDER BY bar_time ASC
                    LIMIT 75
                """), {
                    "inst_id": inst_id,
                    "observed_at": cand["observed_at"],
                    "eod_time": eod_time,
                }).mappings().all()

            outcome = "NEUTRAL"
            exit_time = None
            mfe_pct = 0.0
            mae_pct = 0.0

            if forward_bars:
                for b in forward_bars:
                    high_p = float(b["high_price"])
                    low_p = float(b["low_price"])
                    close_p = float(b["close_price"])

                    # Track excursions
                    fav = ((high_p - entry_price) / entry_price * 100.0) if direction > 0 else ((entry_price - low_p) / entry_price * 100.0)
                    adv = ((entry_price - low_p) / entry_price * 100.0) if direction > 0 else ((high_p - entry_price) / entry_price * 100.0)
                    mfe_pct = max(mfe_pct, fav)
                    mae_pct = max(mae_pct, adv)

                    # Boundary checks
                    if direction > 0:  # LONG
                        if low_p <= stop_loss:
                            outcome = "SAVED_LOSS"
                            exit_time = b["bar_time"]
                            break
                        elif high_p >= target_price:
                            outcome = "MISSED_WIN"
                            exit_time = b["bar_time"]
                            break
                    else:  # SHORT
                        if high_p >= stop_loss:
                            outcome = "SAVED_LOSS"
                            exit_time = b["bar_time"]
                            break
                        elif low_p <= target_price:
                            outcome = "MISSED_WIN"
                            exit_time = b["bar_time"]
                            break

                # If bars finished without hitting boundaries, evaluate final close
                if outcome == "NEUTRAL" and forward_bars:
                    last_close = float(forward_bars[-1]["close_price"])
                    pnl_diff = (last_close - entry_price) * direction
                    if pnl_diff < -0.003 * entry_price:
                        outcome = "SAVED_LOSS"
                    elif pnl_diff > 0.006 * entry_price:
                        outcome = "MISSED_WIN"
                    else:
                        outcome = "NEUTRAL"

            # Tally counts
            if outcome == "SAVED_LOSS":
                saved_losses += 1
                gate_breakdown[reason]["saved_losses"] += 1
            elif outcome == "MISSED_WIN":
                missed_wins += 1
                gate_breakdown[reason]["missed_wins"] += 1
            else:
                neutral_count += 1
                gate_breakdown[reason]["neutral"] += 1

            # Update row in DB
            replay_meta = {
                "result": outcome,
                "entry_price": round(entry_price, 2),
                "stop_loss": round(stop_loss, 2),
                "target_price": round(target_price, 2),
                "mfe_pct": round(mfe_pct, 2),
                "mae_pct": round(mae_pct, 2),
                "exit_time": exit_time.isoformat() if exit_time else None,
            }
            details["counterfactual_replay"] = replay_meta

            conn.execute(text("""
                UPDATE counterfactual_candidate_log
                SET outcome_label = :outcome,
                    details = CAST(:details AS jsonb),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """), {
                "outcome": outcome,
                "details": json.dumps(details, default=str),
                "id": cand["id"],
            })

            if len(sample_replays) < 15:
                sample_replays.append({
                    "symbol": cand["symbol"],
                    "strategy": cand["strategy"] or "MOMENTUM",
                    "reason": reason,
                    "outcome": outcome,
                    "mfe_pct": round(mfe_pct, 2),
                    "mae_pct": round(mae_pct, 2),
                    "entry": round(entry_price, 2),
                })

        decisive = saved_losses + missed_wins
        gate_accuracy_pct = round((saved_losses / decisive * 100.0), 2) if decisive > 0 else 100.0
        capital_saved_est = round(saved_losses * assumed_risk_per_trade, 2)

        # Format gate accuracy per reason
        for r_k, r_v in gate_breakdown.items():
            r_dec = r_v["saved_losses"] + r_v["missed_wins"]
            r_v["accuracy_pct"] = round((r_v["saved_losses"] / r_dec * 100.0), 1) if r_dec > 0 else 100.0

        summary = {
            "audit_date": trade_date.isoformat(),
            "total_rejected": total_rejected,
            "saved_losses": saved_losses,
            "missed_wins": missed_wins,
            "neutral_count": neutral_count,
            "decisive_count": decisive,
            "gate_accuracy_pct": gate_accuracy_pct,
            "estimated_capital_saved_inr": capital_saved_est,
            "gate_breakdown": gate_breakdown,
            "sample_replays": sample_replays,
            "generated_at": datetime.now(IST).isoformat(),
        }

        # Persist aggregate daily audit
        conn.execute(text("""
            INSERT INTO counterfactual_daily_audits(
                audit_date, total_rejected, saved_losses, missed_wins,
                neutral_count, gate_accuracy_pct, estimated_capital_saved,
                gate_breakdown, details, updated_at
            ) VALUES (
                :audit_date, :total_rejected, :saved_losses, :missed_wins,
                :neutral_count, :gate_accuracy_pct, :estimated_capital_saved,
                CAST(:gate_breakdown AS jsonb), CAST(:details AS jsonb), CURRENT_TIMESTAMP
            )
            ON CONFLICT (audit_date) DO UPDATE SET
                total_rejected = EXCLUDED.total_rejected,
                saved_losses = EXCLUDED.saved_losses,
                missed_wins = EXCLUDED.missed_wins,
                neutral_count = EXCLUDED.neutral_count,
                gate_accuracy_pct = EXCLUDED.gate_accuracy_pct,
                estimated_capital_saved = EXCLUDED.estimated_capital_saved,
                gate_breakdown = EXCLUDED.gate_breakdown,
                details = EXCLUDED.details,
                updated_at = CURRENT_TIMESTAMP
        """), {
            "audit_date": trade_date,
            "total_rejected": total_rejected,
            "saved_losses": saved_losses,
            "missed_wins": missed_wins,
            "neutral_count": neutral_count,
            "gate_accuracy_pct": gate_accuracy_pct,
            "estimated_capital_saved": capital_saved_est,
            "gate_breakdown": json.dumps(gate_breakdown, default=str),
            "details": json.dumps({"samples": sample_replays}, default=str),
        })

        # Cache in Redis if available
        if redis_client:
            try:
                cache_key = f"nivesh:counterfactual:summary:{trade_date.isoformat()}"
                redis_client.setex(cache_key, timedelta(days=7), json.dumps(summary, default=str))
            except Exception as cache_err:
                logger.debug("Failed to cache counterfactual summary: %s", cache_err)

        logger.info(
            "Counterfactual replay complete for %s: %d rejected, %d saved losses (%.1f%% accuracy), ₹%.2f saved",
            trade_date, total_rejected, saved_losses, gate_accuracy_pct, capital_saved_est
        )
        return summary


def get_latest_counterfactual_summary(engine, trade_date: Optional[date] = None, *, redis_client: Optional[Any] = None) -> Dict[str, Any]:
    """Retrieve the cached or persisted counterfactual summary for a session."""
    if trade_date is None:
        trade_date = datetime.now(IST).date()

    if redis_client:
        try:
            cached = redis_client.get(f"nivesh:counterfactual:summary:{trade_date.isoformat()}")
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    with engine.begin() as conn:
        ensure_counterfactual_schema(conn)
        row = conn.execute(text("""
            SELECT audit_date, total_rejected, saved_losses, missed_wins,
                   neutral_count, gate_accuracy_pct, estimated_capital_saved,
                   gate_breakdown, details, updated_at
            FROM counterfactual_daily_audits
            WHERE audit_date = :trade_date
            ORDER BY audit_date DESC LIMIT 1
        """), {"trade_date": trade_date}).mappings().one_or_none()

        if row:
            return {
                "audit_date": row["audit_date"].isoformat(),
                "total_rejected": row["total_rejected"],
                "saved_losses": row["saved_losses"],
                "missed_wins": row["missed_wins"],
                "neutral_count": row["neutral_count"],
                "gate_accuracy_pct": float(row["gate_accuracy_pct"]),
                "estimated_capital_saved_inr": float(row["estimated_capital_saved"]),
                "gate_breakdown": row["gate_breakdown"] or {},
                "details": row["details"] or {},
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }

    # Return empty default if not yet computed
    return {
        "audit_date": trade_date.isoformat(),
        "total_rejected": 0,
        "saved_losses": 0,
        "missed_wins": 0,
        "neutral_count": 0,
        "gate_accuracy_pct": 100.0,
        "estimated_capital_saved_inr": 0.0,
        "gate_breakdown": {},
        "details": {},
        "status": "pending_post_market_eval",
    }


def run_profit_harvest_ab_replay(
    engine: Optional[Any] = None,
    trades: Optional[List[Dict[str, Any]]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    harvest_trigger: float = 0.83,
    exit_threshold: float = 70.0,
    tighten_threshold: float = 40.0,
    lock_fraction: float = 0.65,
    n_bootstraps: int = 1000,
    random_seed: int = 42,
) -> Dict[str, Any]:
    """A/B Counterfactual Replay comparing Baseline execution (Arm A) vs Profit-Harvest (Arm B).

    For each trade:
    - Arm A (Baseline): Realized R under standard exit rules (SL, TP, trailing, EOD).
    - Arm B (Harvest): Counterfactual execution with Profit-Harvest enabled:
        - When favorable progress >= harvest_trigger (default 0.83, i.e. 1.25R of 1.5R):
            - If reversal_score >= exit_threshold (70.0): market exit at reversal candle.
            - If reversal_score >= tighten_threshold (40.0): tighten stop to lock lock_fraction (0.65) of open profit.
            - Else: continue baseline trailing.

    Computes delta R (Arm B - Arm A) and 95% bootstrap confidence interval.
    """
    import random
    from backend.profit_harvest import (
        calculate_progress,
        calculate_harvest_stop,
        compute_reversal_score,
    )

    trade_samples: List[Dict[str, Any]] = []

    if trades is not None:
        trade_samples = list(trades)
    elif engine is not None:
        try:
            with engine.connect() as conn:
                params: Dict[str, Any] = {}
                date_filter = ""
                if start_date:
                    date_filter += " AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date >= :start_d"
                    params["start_d"] = start_date
                if end_date:
                    date_filter += " AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date <= :end_d"
                    params["end_d"] = end_date

                query = text(f"""
                    SELECT a.id, a.instrument_id, a.side, a.signal_at, a.closed_at,
                           a.stop_loss_price, a.take_profit_price,
                           a.theoretical_fill_price, a.theoretical_exit_price,
                           a.exit_reason, a.improvement_note, i.symbol
                    FROM shadow_execution_audits a
                    JOIN instrument_master i ON i.id = a.instrument_id
                    WHERE a.net_pnl IS NOT NULL
                      AND a.theoretical_fill_price IS NOT NULL
                      AND a.theoretical_exit_price IS NOT NULL
                      {date_filter}
                    ORDER BY a.signal_at ASC
                """)
                rows = conn.execute(query, params).mappings().all()
                for r in rows:
                    note = {}
                    if r.get("improvement_note"):
                        try:
                            note = json.loads(r["improvement_note"]) if isinstance(r["improvement_note"], str) else r["improvement_note"]
                        except Exception:
                            note = {}
                    trade_samples.append({
                        "id": r["id"],
                        "symbol": r["symbol"],
                        "side": r["side"],
                        "entry": float(r["theoretical_fill_price"]),
                        "initial_sl": float(r["stop_loss_price"] or 0.0),
                        "target_price": float(r["take_profit_price"] or 0.0),
                        "exit_price": float(r["theoretical_exit_price"]),
                        "exit_reason": r.get("exit_reason"),
                        "instrument_id": r.get("instrument_id"),
                        "signal_at": r.get("signal_at"),
                        "closed_at": r.get("closed_at"),
                        "note": note,
                    })
        except Exception as err:
            logger.warning("Failed to fetch historical shadow trades for A/B replay: %s", err)

    if not trade_samples:
        return {
            "total_trades": 0,
            "trades_reaching_harvest": 0,
            "trades_improved": 0,
            "trades_hurt": 0,
            "trades_unchanged": 0,
            "baseline_mean_r": 0.0,
            "harvest_mean_r": 0.0,
            "delta_mean_r": 0.0,
            "ci_95": (0.0, 0.0),
            "baseline_win_rate": 0.0,
            "harvest_win_rate": 0.0,
            "recommendation": "INSUFFICIENT_DATA",
            "trades": [],
        }

    baseline_rs: List[float] = []
    harvest_rs: List[float] = []
    deltas: List[float] = []
    harvest_triggered_count = 0
    improved_count = 0
    hurt_count = 0
    unchanged_count = 0
    eval_records: List[Dict[str, Any]] = []

    for t in trade_samples:
        entry = float(t.get("entry") or 0.0)
        sl = float(t.get("initial_sl") or 0.0)
        side = str(t.get("side") or "BUY").upper()
        baseline_exit = float(t.get("exit_price") or entry)

        r_points = abs(entry - sl)
        if r_points <= 1e-4:
            # Fallback 1% risk if missing stop
            r_points = max(1.0, entry * 0.01)

        target = float(t.get("target_price") or 0.0)
        if target <= 0.0:
            target = entry + (1.5 * r_points) if side == "BUY" else entry - (1.5 * r_points)

        target_dist = abs(target - entry)
        if target_dist <= 1e-4:
            continue

        baseline_r = (baseline_exit - entry) / r_points if side == "BUY" else (entry - baseline_exit) / r_points

        # Determine favorable move achieved
        max_favorable = float(t.get("max_favorable_price") or (
            baseline_exit if (baseline_exit > entry if side == "BUY" else baseline_exit < entry) else entry
        ))
        favorable_dist = (max_favorable - entry) if side == "BUY" else (entry - max_favorable)
        max_progress = favorable_dist / target_dist if target_dist > 0 else 0.0

        # Evaluate Harvest arm
        harvest_r = baseline_r
        harvest_action = "NONE"
        harvest_exit_price = baseline_exit

        bars = t.get("bars")
        rev_score = float(t.get("reversal_score") or 0.0)

        if max_progress >= harvest_trigger:
            harvest_triggered_count += 1

            # If explicit bars provided, compute reversal score
            if bars and len(bars) >= 3 and rev_score == 0.0:
                rev_res = compute_reversal_score(
                    bars_5m=bars,
                    side=side,
                    exit_threshold=exit_threshold,
                    tighten_threshold=tighten_threshold,
                )
                rev_score = rev_res["reversal_score"]

            if rev_score >= exit_threshold:
                harvest_action = "EXIT"
                harvest_exit_price = float(t.get("reversal_price") or max_favorable)
                harvest_r = (harvest_exit_price - entry) / r_points if side == "BUY" else (entry - harvest_exit_price) / r_points
            elif rev_score >= tighten_threshold:
                harvest_action = "TIGHTEN"
                tightened_sl = entry + (lock_fraction * (max_favorable - entry)) if side == "BUY" else entry - (lock_fraction * (entry - max_favorable))
                # Check if baseline pulled back below tightened stop
                pulled_back = baseline_exit < tightened_sl if side == "BUY" else baseline_exit > tightened_sl
                if pulled_back:
                    harvest_exit_price = tightened_sl
                    harvest_r = (tightened_sl - entry) / r_points if side == "BUY" else (entry - tightened_sl) / r_points
                else:
                    harvest_r = baseline_r
            else:
                harvest_action = "HOLD"
                harvest_r = baseline_r

        delta_r = harvest_r - baseline_r
        baseline_rs.append(baseline_r)
        harvest_rs.append(harvest_r)
        deltas.append(delta_r)

        if delta_r > 0.005:
            improved_count += 1
        elif delta_r < -0.005:
            hurt_count += 1
        else:
            unchanged_count += 1

        eval_records.append({
            "symbol": t.get("symbol"),
            "side": side,
            "entry": round(entry, 2),
            "baseline_exit": round(baseline_exit, 2),
            "harvest_exit": round(harvest_exit_price, 2),
            "baseline_r": round(baseline_r, 3),
            "harvest_r": round(harvest_r, 3),
            "delta_r": round(delta_r, 3),
            "progress": round(max_progress, 3),
            "reversal_score": round(rev_score, 1),
            "harvest_action": harvest_action,
        })

    N = len(deltas)
    rng = random.Random(random_seed)
    bootstrap_means: List[float] = []
    for _ in range(n_bootstraps):
        sample = [deltas[rng.randint(0, N - 1)] for _ in range(N)]
        bootstrap_means.append(sum(sample) / N)
    bootstrap_means.sort()

    ci_lower = bootstrap_means[int(0.025 * len(bootstrap_means))]
    ci_upper = bootstrap_means[int(0.975 * len(bootstrap_means))]

    base_mean_r = sum(baseline_rs) / N
    harv_mean_r = sum(harvest_rs) / N
    delta_mean_r = sum(deltas) / N

    base_wins = sum(1 for r in baseline_rs if r > 0.0)
    harv_wins = sum(1 for r in harvest_rs if r > 0.0)
    base_win_rate = (base_wins / N * 100.0) if N > 0 else 0.0
    harv_win_rate = (harv_wins / N * 100.0) if N > 0 else 0.0

    if ci_lower > 0.0:
        recommendation = "ENABLE"
    elif ci_upper < 0.0:
        recommendation = "KEEP_OFF"
    else:
        recommendation = "NEUTRAL"

    return {
        "total_trades": N,
        "trades_reaching_harvest": harvest_triggered_count,
        "trades_improved": improved_count,
        "trades_hurt": hurt_count,
        "trades_unchanged": unchanged_count,
        "baseline_mean_r": round(base_mean_r, 4),
        "harvest_mean_r": round(harv_mean_r, 4),
        "delta_mean_r": round(delta_mean_r, 4),
        "ci_95": (round(ci_lower, 4), round(ci_upper, 4)),
        "baseline_win_rate": round(base_win_rate, 2),
        "harvest_win_rate": round(harv_win_rate, 2),
        "recommendation": recommendation,
        "sample_evaluations": eval_records[:20],
    }

