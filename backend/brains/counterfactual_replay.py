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
