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


from backend.position_manager import (
    ADVERSE_CUT_WINDOW_SECONDS,
    ADVERSE_CUT_THRESHOLD_R,
    STAGNATION_SCRATCH_SECONDS,
    STAGNATION_MIN_EXPANSION_R,
    STAGNATION_TIGHTEN_SECONDS,
    STAGNATION_TIGHTEN_R,
    FORCE_FLAT_TIME,
)


def resample_1m_to_5m(bars_1m: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resample 1-minute bars into 5-minute OHLCV candles, excluding partial edge buckets."""
    if not bars_1m:
        return []

    from collections import defaultdict
    buckets: Dict[datetime, List[Dict[str, Any]]] = defaultdict(list)

    for b in bars_1m:
        bt = b.get("bar_time")
        if isinstance(bt, str):
            try:
                dt = datetime.fromisoformat(bt)
            except Exception:
                continue
        elif isinstance(bt, datetime):
            dt = bt
        else:
            continue

        bucket_minute = (dt.minute // 5) * 5
        bucket_dt = dt.replace(minute=bucket_minute, second=0, microsecond=0)
        buckets[bucket_dt].append(b)

    sorted_keys = sorted(buckets.keys())
    resampled_5m = []

    for i, k in enumerate(sorted_keys):
        group = buckets[k]
        # Exclude partial edge buckets (less than 5 1-minute bars at start or end)
        if len(group) < 5 and (i == 0 or i == len(sorted_keys) - 1):
            continue

        o = float(group[0].get("open_price") if group[0].get("open_price") is not None else group[0].get("open", 0.0))
        h = max(float(x.get("high_price") if x.get("high_price") is not None else x.get("high", 0.0)) for x in group)
        l = min(float(x.get("low_price") if x.get("low_price") is not None else x.get("low", 0.0)) for x in group)
        c = float(group[-1].get("close_price") if group[-1].get("close_price") is not None else group[-1].get("close", 0.0))
        v = sum(float(x.get("volume") or 0.0) for x in group)

        resampled_5m.append({
            "bar_time": k.isoformat() if isinstance(group[0].get("bar_time"), str) else k,
            "open_price": o,
            "high_price": h,
            "low_price": l,
            "close_price": c,
            "volume": v,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "interval": "5minute",
            "source": "upstox_v3",
        })

    return resampled_5m


def validate_bar_continuity(
    bars: List[Dict[str, Any]],
    signal_at: Any = None,
    exit_at: Any = None,
) -> tuple[bool, Optional[str]]:
    """Validate that bars cover signal to exit with no gaps over 10 minutes.

    Returns (is_valid, exclusion_reason).
    """
    if not bars:
        return False, "NO_BARS"

    def _to_dt(val: Any) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val)
            except Exception:
                return None
        return None

    first_bt = _to_dt(bars[0].get("bar_time"))
    last_bt = _to_dt(bars[-1].get("bar_time"))
    sig_dt = _to_dt(signal_at)
    ex_dt = _to_dt(exit_at)

    # Check gap between signal_at and first bar
    if sig_dt and first_bt:
        s_comp = sig_dt.replace(tzinfo=None) if sig_dt.tzinfo else sig_dt
        f_comp = first_bt.replace(tzinfo=None) if first_bt.tzinfo else first_bt
        if f_comp > s_comp and (f_comp - s_comp) > timedelta(minutes=10):
            return False, f"INITIAL_BAR_GAP_OVER_10M ({round((f_comp - s_comp).total_seconds() / 60, 1)}m)"

    # Check gap between last bar and exit_at
    if ex_dt and last_bt:
        e_comp = ex_dt.replace(tzinfo=None) if ex_dt.tzinfo else ex_dt
        l_comp = last_bt.replace(tzinfo=None) if last_bt.tzinfo else last_bt
        if e_comp > l_comp and (e_comp - l_comp) > timedelta(minutes=10):
            return False, f"FINAL_BAR_GAP_OVER_10M ({round((e_comp - l_comp).total_seconds() / 60, 1)}m)"

    # Check consecutive bars gap
    for i in range(len(bars) - 1):
        t1 = _to_dt(bars[i].get("bar_time"))
        t2 = _to_dt(bars[i + 1].get("bar_time"))
        if t1 and t2:
            t1_comp = t1.replace(tzinfo=None) if t1.tzinfo else t1
            t2_comp = t2.replace(tzinfo=None) if t2.tzinfo else t2
            gap = t2_comp - t1_comp
            if t1_comp.date() == t2_comp.date():
                if gap > timedelta(minutes=10):
                    return False, f"IN_SESSION_GAP_OVER_10M ({round(gap.total_seconds() / 60, 1)}m)"
            else:
                if t1_comp.time() < time(15, 20) or t2_comp.time() > time(9, 25):
                    return False, f"INTER_SESSION_GAP_OVER_10M ({round(gap.total_seconds() / 60, 1)}m)"

    return True, None


def _block_bootstrap_by_day(
    records: List[Dict[str, Any]],
    n_bootstraps: int = 1000,
    random_seed: int = 42,
) -> tuple[float, float]:
    """Resample trades by trading day (block bootstrap) to account for cross-sectional clustering."""
    if not records:
        return (0.0, 0.0)
    import random
    from collections import defaultdict
    day_groups: Dict[date, List[float]] = defaultdict(list)
    for r in records:
        day_groups[r["trade_date"]].append(r["delta_r"])
    days = list(day_groups.keys())
    if not days:
        return (0.0, 0.0)
    if len(days) == 1:
        vals = day_groups[days[0]]
        m_val = sum(vals) / len(vals)
        return (round(m_val, 4), round(m_val, 4))

    rng = random.Random(random_seed)
    means: List[float] = []
    n_days = len(days)
    for _ in range(n_bootstraps):
        sampled_days = [days[rng.randint(0, n_days - 1)] for _ in range(n_days)]
        sampled_deltas = [d for d_day in sampled_days for d in day_groups[d_day]]
        if sampled_deltas:
            means.append(sum(sampled_deltas) / len(sampled_deltas))

    if not means:
        return (0.0, 0.0)
    means.sort()
    ci_lower = means[int(0.025 * len(means))]
    ci_upper = means[int(0.975 * len(means))]
    return (round(ci_lower, 4), round(ci_upper, 4))


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

    Phase 2d Specifications:
    1. Walk exits on 1-minute upstox_v3 bars (or 1-second where present).
    2. Reversal score evaluated ONLY from CLOSED 5-minute bars built from 1m bars, excluding partial edge buckets.
    3. Non-circular fidelity check: When walk reaches no exit and falls through to recorded exit, returns
       exit_reason = 'FELL_THROUGH_TO_RECORDED' and counts as NOT matched. Reports fell_through_count.
       Computes fidelity on all trades, and separately on modelled subset.
    4. Out-of-sample Gate: Requires >= 100 harvest-triggered trades in the TEST half. Computes test CI on triggered
       trades only, resampled by trading day (block bootstrap). ENABLE only if test CI excludes zero.
    """
    from backend.position_manager import replay_trade_walk_forward

    trade_samples: List[Dict[str, Any]] = []

    if trades is not None:
        trade_samples = list(trades)
    elif engine is not None:
        try:
            with engine.connect() as conn:
                params: Dict[str, Any] = {}
                date_filter = ""
                dialect = getattr(getattr(engine, "dialect", None), "name", "")
                if start_date:
                    if dialect == "sqlite":
                        date_filter += " AND date(a.signal_at) >= date(:start_d)"
                    else:
                        date_filter += " AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date >= :start_d"
                    params["start_d"] = start_date.isoformat() if dialect == "sqlite" else start_date
                if end_date:
                    if dialect == "sqlite":
                        date_filter += " AND date(a.signal_at) <= date(:end_d)"
                    else:
                        date_filter += " AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date <= :end_d"
                    params["end_d"] = end_date.isoformat() if dialect == "sqlite" else end_date

                query = text(f"""
                    SELECT a.id, a.instrument_id, a.side, a.signal_at, a.exit_at,
                           a.stop_loss_price, a.take_profit_price,
                           a.theoretical_fill_price, a.realised_exit_price,
                           a.exit_reason, a.improvement_note,
                           COALESCE(a.trade_mode, 'INTRADAY') AS trade_mode,
                           a.quantity, a.estimated_fees, i.symbol
                    FROM shadow_execution_audits a
                    JOIN instrument_master i ON i.id = a.instrument_id
                    WHERE a.net_pnl IS NOT NULL
                      AND a.theoretical_fill_price IS NOT NULL
                      AND a.realised_exit_price IS NOT NULL
                      {date_filter}
                    ORDER BY a.signal_at ASC
                """)
                rows = conn.execute(query, params).mappings().all()

                has_source_col = True
                try:
                    conn.execute(text("SELECT source FROM live_market_bars LIMIT 1"))
                except Exception:
                    has_source_col = False

                source_filter = "AND source = 'upstox_v3'" if has_source_col else ""

                for r in rows:
                    note = {}
                    if r.get("improvement_note"):
                        try:
                            note = json.loads(r["improvement_note"]) if isinstance(r["improvement_note"], str) else r["improvement_note"]
                        except Exception:
                            note = {}

                    trade_bars: List[Dict[str, Any]] = []
                    if r.get("instrument_id") and r.get("signal_at") and r.get("exit_at"):
                        # Walk exits on 1-minute upstox_v3 bars
                        b_1m_rows = conn.execute(
                            text(f"""
                                SELECT bar_time, open_price, high_price, low_price, close_price, volume, interval
                                FROM live_market_bars
                                WHERE instrument_id = :inst_id
                                  AND interval IN ('1minute', '1m')
                                  {source_filter}
                                  AND bar_time >= :sig_at AND bar_time <= :ex_at
                                ORDER BY bar_time ASC
                            """),
                            {"inst_id": r["instrument_id"], "sig_at": r["signal_at"], "ex_at": r["exit_at"]}
                        ).mappings().all()

                        if b_1m_rows:
                            trade_bars = [dict(b) for b in b_1m_rows]
                        else:
                            # Fallback to 5m if 1m not available
                            b_5m_rows = conn.execute(
                                text(f"""
                                    SELECT bar_time, open_price, high_price, low_price, close_price, volume, interval
                                    FROM live_market_bars
                                    WHERE instrument_id = :inst_id
                                      AND interval IN ('5minute', '5m')
                                      {source_filter}
                                      AND bar_time >= :sig_at AND bar_time <= :ex_at
                                    ORDER BY bar_time ASC
                                """),
                                {"inst_id": r["instrument_id"], "sig_at": r["signal_at"], "ex_at": r["exit_at"]}
                            ).mappings().all()
                            if b_5m_rows:
                                trade_bars = [dict(b) for b in b_5m_rows]

                    trade_samples.append({
                        "id": r["id"],
                        "symbol": r["symbol"],
                        "side": r["side"],
                        "entry": float(r["theoretical_fill_price"]),
                        "theoretical_fill_price": float(r["theoretical_fill_price"]),
                        "initial_sl": float(r["stop_loss_price"] or 0.0),
                        "stop_loss_price": float(r["stop_loss_price"] or 0.0),
                        "target_price": float(r["take_profit_price"] or 0.0),
                        "take_profit_price": float(r["take_profit_price"] or 0.0),
                        "exit_price": float(r["realised_exit_price"]),
                        "realised_exit_price": float(r["realised_exit_price"]),
                        "exit_reason": r.get("exit_reason"),
                        "instrument_id": r.get("instrument_id"),
                        "signal_at": r.get("signal_at"),
                        "exit_at": r.get("exit_at"),
                        "trade_mode": r.get("trade_mode"),
                        "quantity": int(r.get("quantity") or 1),
                        "estimated_fees": float(r.get("estimated_fees") or 0.0),
                        "note": note,
                        "bars": trade_bars,
                    })
        except Exception as err:
            logger.warning("Failed to fetch historical shadow trades for A/B replay: %s", err)

    # Coverage tracking
    trades_considered = len(trade_samples)
    trades_excluded = 0
    exclusion_reasons: Dict[str, int] = {}
    valid_trades: List[Dict[str, Any]] = []

    def _has_1m_resolution(bars_list: List[Dict[str, Any]]) -> bool:
        if not bars_list:
            return False
        if any(b.get("interval") in ("1minute", "1m") for b in bars_list[:5]):
            return True
        if len(bars_list) >= 2 and (bars_list[0].get("bar_time") or bars_list[0].get("timestamp")) and (bars_list[1].get("bar_time") or bars_list[1].get("timestamp")):
            try:
                bt0 = bars_list[0].get("bar_time") or bars_list[0].get("timestamp")
                bt1 = bars_list[1].get("bar_time") or bars_list[1].get("timestamp")
                t0 = datetime.fromisoformat(bt0) if isinstance(bt0, str) else bt0
                t1 = datetime.fromisoformat(bt1) if isinstance(bt1, str) else bt1
                if 0 < abs((t1 - t0).total_seconds()) <= 60:
                    return True
            except Exception:
                pass
        return False

    # Pick one resolution per run: 1-minute where present, else 5-minute
    has_1m_in_run = False
    for t in trade_samples:
        r_bars = t.get("bars") or []
        if _has_1m_resolution(r_bars):
            has_1m_in_run = True
            break
        bars_1m_check = [b for b in r_bars if b.get("interval") in ("1minute", "1m")]
        if bars_1m_check:
            has_1m_in_run = True
            break

    run_resolution = "1m" if has_1m_in_run else "5m"
    dropped_resolution_count = 0

    for t in trade_samples:
        raw_bars = t.get("bars") or []
        if not raw_bars:
            trades_excluded += 1
            exclusion_reasons["NO_BARS"] = exclusion_reasons.get("NO_BARS", 0) + 1
            continue

        has_source = any("source" in b for b in raw_bars)
        if has_source:
            candidate_bars = [b for b in raw_bars if b.get("source") in ("upstox_v3", None)]
        else:
            candidate_bars = raw_bars

        # Filter strictly to the chosen run resolution
        bars_1m = [b for b in candidate_bars if b.get("interval") in ("1minute", "1m")]
        bars_5m = [b for b in candidate_bars if b.get("interval") in ("5minute", "5m")]

        if run_resolution == "1m":
            if bars_1m:
                chosen_bars = bars_1m
            elif _has_1m_resolution(candidate_bars):
                chosen_bars = candidate_bars
            else:
                trades_excluded += 1
                dropped_resolution_count += 1
                exclusion_reasons["DROPPED_NOT_1M_RESOLUTION"] = exclusion_reasons.get("DROPPED_NOT_1M_RESOLUTION", 0) + 1
                continue
        else:
            if bars_5m:
                chosen_bars = bars_5m
            elif not _has_1m_resolution(candidate_bars):
                chosen_bars = candidate_bars
            else:
                trades_excluded += 1
                dropped_resolution_count += 1
                exclusion_reasons["DROPPED_NOT_5M_RESOLUTION"] = exclusion_reasons.get("DROPPED_NOT_5M_RESOLUTION", 0) + 1
                continue

        # Validate continuity: coverage with no gaps > 10m
        is_valid, exc_reason = validate_bar_continuity(
            chosen_bars,
            signal_at=t.get("signal_at"),
            exit_at=t.get("exit_at"),
        )
        if not is_valid:
            trades_excluded += 1
            reason_key = exc_reason or "GAP_EXCEEDS_10M"
            exclusion_reasons[reason_key] = exclusion_reasons.get(reason_key, 0) + 1
            continue

        t_copy = dict(t)
        t_copy["bars"] = chosen_bars
        valid_trades.append(t_copy)

    if not valid_trades:
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
            "coverage": {
                "total_trades_considered": trades_considered,
                "trades_used": 0,
                "trades_excluded": trades_excluded,
                "exclusion_reasons": exclusion_reasons,
            },
            "fidelity": {
                "trades_compared": 0,
                "fell_through_count": 0,
                "within_01r_count": 0,
                "fidelity_all_pct": 0.0,
                "fidelity_modelled_pct": 0.0,
                "fidelity_pct": 0.0,
                "reason_match_pct": 0.0,
                "unmodelled_trades_count": 0,
                "modelled_exits": [
                    "STOP_LOSS", "GAP_DOWN_STOP", "GAP_UP_STOP", "BREAKEVEN_STOP",
                    "TRAILING_STOP", "TAKE_PROFIT", "FORCE_FLAT", "EARLY_ADVERSE_CUT", "STAGNATION_GUARD"
                ],
                "unmodelled_exits": [
                    "STALE_DATA_EXIT", "MANUAL_CLOSE", "RMS_AUTO_SQUAREOFF"
                ],
            },
            "split": {
                "train": {"trades_count": 0, "triggered_count": 0, "delta_mean_r": 0.0, "ci_95": (0.0, 0.0)},
                "test": {"trades_count": 0, "triggered_count": 0, "delta_mean_r": 0.0, "ci_95": (0.0, 0.0)},
            },
            "by_mode": {
                "intraday": {"total_trades": 0, "trades_reaching_harvest": 0, "baseline_mean_r": 0.0, "harvest_mean_r": 0.0, "delta_mean_r": 0.0, "ci_95": (0.0, 0.0)},
                "swing": {"total_trades": 0, "trades_reaching_harvest": 0, "baseline_mean_r": 0.0, "harvest_mean_r": 0.0, "delta_mean_r": 0.0, "ci_95": (0.0, 0.0)},
            },
            "sample_evaluations": [],
        }

    MODELLED_CORE_EXITS = {
        "STOP_LOSS", "GAP_DOWN_STOP", "GAP_UP_STOP", "BREAKEVEN_STOP",
        "TRAILING_STOP", "TAKE_PROFIT", "FORCE_FLAT", "EARLY_ADVERSE_CUT", "STAGNATION_GUARD"
    }
    UNMODELLED_EXITS = {"STALE_DATA_EXIT", "MANUAL_CLOSE", "RMS_AUTO_SQUAREOFF"}

    baseline_rs: List[float] = []
    harvest_rs: List[float] = []
    deltas: List[float] = []
    harvest_triggered_count = 0
    improved_count = 0
    hurt_count = 0
    unchanged_count = 0
    fell_through_count = 0
    eval_records: List[Dict[str, Any]] = []

    for t in valid_trades:
        entry = float(t.get("theoretical_fill_price") or t.get("entry") or 0.0)
        sl = float(t.get("stop_loss_price") or t.get("initial_sl") or 0.0)
        side = str(t.get("side") or "BUY").upper()
        target = float(t.get("take_profit_price") or t.get("target_price") or 0.0)
        rec_exit = float(t.get("realised_exit_price") or t.get("exit_price") or entry)
        rec_reason = str(t.get("exit_reason") or "")

        r_points = abs(entry - sl)
        if r_points <= 1e-4:
            r_points = max(1.0, entry * 0.01)

        if target <= 0.0:
            target = entry + (1.5 * r_points) if side == "BUY" else entry - (1.5 * r_points)

        target_dist = abs(target - entry)
        bars = t["bars"]

        # Compute max favorable price directly from valid bars
        if side == "BUY":
            max_favorable = max(float(b.get("high_price") if b.get("high_price") is not None else b.get("high", entry)) for b in bars)
            fav_dist = max_favorable - entry
        else:
            max_favorable = min(float(b.get("low_price") if b.get("low_price") is not None else b.get("low", entry)) for b in bars)
            fav_dist = entry - max_favorable

        max_prog = (fav_dist / target_dist) if target_dist > 0 else 0.0
        is_triggered = (max_prog >= harvest_trigger)
        if is_triggered:
            harvest_triggered_count += 1

        # Walk bars forward
        res_a = replay_trade_walk_forward(
            trade=t,
            bars=bars,
            harvest_enabled=False,
        )
        res_b = replay_trade_walk_forward(
            trade=t,
            bars=bars,
            harvest_enabled=True,
            harvest_trigger=harvest_trigger,
            exit_threshold=exit_threshold,
            tighten_threshold=tighten_threshold,
            lock_fraction=lock_fraction,
        )

        # Non-circular fidelity check
        fell_through = (res_a["exit_reason"] == "FELL_THROUGH_TO_RECORDED")
        if fell_through:
            fell_through_count += 1
            diff_r_match = False
            reason_match = False
            diff_r = 999.0
        else:
            diff_r = abs(res_a["exit_price"] - rec_exit) / r_points
            diff_r_match = (diff_r <= 0.10)
            reason_match = (res_a["exit_reason"] == rec_reason)

        baseline_r = res_a["realized_r"]
        harvest_r = res_b["realized_r"]
        delta_r = round(harvest_r - baseline_r, 4)

        baseline_rs.append(baseline_r)
        harvest_rs.append(harvest_r)
        deltas.append(delta_r)

        if delta_r > 0.005:
            improved_count += 1
        elif delta_r < -0.005:
            hurt_count += 1
        else:
            unchanged_count += 1

        # Extract trade date for chronological split
        sig_val = t.get("signal_at")
        t_date = date.min
        if isinstance(sig_val, datetime):
            t_date = sig_val.astimezone(IST).date() if sig_val.tzinfo else sig_val.date()
        elif isinstance(sig_val, str):
            try:
                dt_p = datetime.fromisoformat(sig_val)
                t_date = dt_p.astimezone(IST).date() if dt_p.tzinfo else dt_p.date()
            except Exception:
                pass

        sub_minute_exits = {"EARLY_ADVERSE_CUT", "STAGNATION_GUARD"}
        is_sub_minute_unmodelled = (run_resolution != "1m" and rec_reason in sub_minute_exits)
        is_unmodelled_trade = (rec_reason in UNMODELLED_EXITS) or is_sub_minute_unmodelled
        is_core_trade = (rec_reason in MODELLED_CORE_EXITS) and not is_sub_minute_unmodelled

        eval_records.append({
            "id": t.get("id"),
            "symbol": t.get("symbol"),
            "side": side,
            "entry": round(entry, 2),
            "baseline_exit": round(res_a["exit_price"], 2),
            "harvest_exit": round(res_b["exit_price"], 2),
            "realised_exit": round(rec_exit, 2),
            "baseline_reason": res_a["exit_reason"],
            "harvest_reason": res_b["exit_reason"],
            "recorded_reason": rec_reason,
            "baseline_r": baseline_r,
            "harvest_r": harvest_r,
            "delta_r": delta_r,
            "progress": round(max_prog, 3),
            "max_favorable": round(max_favorable, 2),
            "diff_r_fidelity": round(diff_r, 4),
            "diff_r_match": diff_r_match,
            "reason_match": reason_match,
            "fell_through": fell_through,
            "is_core": is_core_trade,
            "is_unmodelled": is_unmodelled_trade,
            "is_triggered": is_triggered,
            "trade_mode": str(t.get("trade_mode") or "INTRADAY").upper(),
            "trade_date": t_date,
        })

    N = len(valid_trades)
    within_01r_count = sum(1 for r in eval_records if r["diff_r_match"])
    fidelity_all_pct = (within_01r_count / N * 100.0) if N > 0 else 0.0
    reason_match_pct = (sum(1 for r in eval_records if r["reason_match"]) / N * 100.0) if N > 0 else 0.0

    core_trades = [r for r in eval_records if r["is_core"]]
    core_matched = sum(1 for r in core_trades if r["diff_r_match"])
    fidelity_core_pct = (core_matched / len(core_trades) * 100.0) if core_trades else 0.0

    unmodelled_count = sum(1 for r in eval_records if r["is_unmodelled"])

    # Chronological date train/test split (50/50)
    unique_dates = sorted(list({rec["trade_date"] for rec in eval_records}))
    if len(unique_dates) >= 2:
        mid_date_idx = len(unique_dates) // 2
        train_date_set = set(unique_dates[:mid_date_idx])
        train_records = [rec for rec in eval_records if rec["trade_date"] in train_date_set]
        test_records = [rec for rec in eval_records if rec["trade_date"] not in train_date_set]
    else:
        mid_idx = len(eval_records) // 2
        train_records = eval_records[:mid_idx]
        test_records = eval_records[mid_idx:]

    test_triggered = [r for r in test_records if r["is_triggered"]]
    train_triggered = [r for r in train_records if r["is_triggered"]]
    test_triggered_count = len(test_triggered)
    train_triggered_count = len(train_triggered)

    # Compute test CI on triggered trades only, block bootstrapped by trading day
    test_ci_95 = _block_bootstrap_by_day(test_triggered, n_bootstraps, random_seed)
    train_ci_95 = _block_bootstrap_by_day(train_triggered, n_bootstraps, random_seed)
    overall_ci = _block_bootstrap_by_day([r for r in eval_records if r["is_triggered"]], n_bootstraps, random_seed)

    base_mean_r = sum(baseline_rs) / N
    harv_mean_r = sum(harvest_rs) / N
    delta_mean_r = sum(deltas) / N
    base_win_rate = (sum(1 for r in baseline_rs if r > 0.0) / N * 100.0) if N > 0 else 0.0
    harv_win_rate = (sum(1 for r in harvest_rs if r > 0.0) / N * 100.0) if N > 0 else 0.0

    train_deltas = [r["delta_r"] for r in train_triggered]
    test_deltas = [r["delta_r"] for r in test_triggered]
    train_delta_mean_r = (sum(train_deltas) / len(train_deltas)) if train_deltas else 0.0
    test_delta_mean_r = (sum(test_deltas) / len(test_deltas)) if test_deltas else 0.0

    # Per-trade-mode breakdown
    mode_breakdown: Dict[str, Any] = {}
    for mode_key in ("INTRADAY", "SWING"):
        m_recs = [r for r in eval_records if r["trade_mode"] == mode_key]
        m_trig = [r for r in m_recs if r["is_triggered"]]
        m_deltas = [r["delta_r"] for r in m_trig]
        m_base = [r["baseline_r"] for r in m_recs]
        m_harv = [r["harvest_r"] for r in m_recs]
        m_ci = _block_bootstrap_by_day(m_trig, n_bootstraps, random_seed) if m_trig else (0.0, 0.0)
        mode_breakdown[mode_key.lower()] = {
            "total_trades": len(m_recs),
            "trades_reaching_harvest": len(m_trig),
            "baseline_mean_r": round(sum(m_base) / len(m_base), 4) if m_base else 0.0,
            "harvest_mean_r": round(sum(m_harv) / len(m_harv), 4) if m_harv else 0.0,
            "delta_mean_r": round(sum(m_deltas) / len(m_deltas), 4) if m_deltas else 0.0,
            "ci_95": (round(m_ci[0], 4), round(m_ci[1], 4)),
        }

    # Recommendation determination (Phase 2d / 3b):
    # 1. Fidelity gate: >= 80% on both overall and modelled subset, and at least 1 modelled exit trade
    # 2. Triggered gate: >= 100 triggered trades in the TEST half
    # 3. Test CI & Train Delta: BOTH test CI lower bound > 0.0 AND train-half mean delta > 0.0 for ENABLE
    if len(core_trades) == 0 or fidelity_all_pct < 80.0 or fidelity_core_pct < 80.0:
        recommendation = "REPLAY_NOT_FAITHFUL"
    elif test_triggered_count < 100:
        recommendation = "INSUFFICIENT_TRIGGERED_TRADES"
    elif test_ci_95[0] > 0.0 and train_delta_mean_r > 0.0:
        recommendation = "ENABLE"
    elif test_ci_95[1] < 0.0:
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
        "ci_95": (round(overall_ci[0], 4), round(overall_ci[1], 4)),
        "baseline_win_rate": round(base_win_rate, 2),
        "harvest_win_rate": round(harv_win_rate, 2),
        "recommendation": recommendation,
        "coverage": {
            "total_trades_considered": trades_considered,
            "trades_used": N,
            "trades_excluded": trades_excluded,
            "exclusion_reasons": exclusion_reasons,
            "run_resolution": run_resolution,
            "dropped_resolution_count": dropped_resolution_count,
        },
        "fidelity": {
            "trades_compared": N,
            "fell_through_count": fell_through_count,
            "within_01r_count": within_01r_count,
            "fidelity_all_pct": round(fidelity_all_pct, 2),
            "fidelity_modelled_pct": round(fidelity_core_pct, 2),
            "modelled_trades_compared": len(core_trades),
            "fidelity_pct": round(fidelity_all_pct, 2),
            "reason_match_pct": round(reason_match_pct, 2),
            "unmodelled_trades_count": unmodelled_count,
            "modelled_exits": list(MODELLED_CORE_EXITS),
            "unmodelled_exits": list(UNMODELLED_EXITS),
        },
        "split": {
            "train": {
                "trades_count": len(train_records),
                "triggered_count": train_triggered_count,
                "delta_mean_r": round(train_delta_mean_r, 4),
                "ci_95": (round(train_ci_95[0], 4), round(train_ci_95[1], 4)),
            },
            "test": {
                "trades_count": len(test_records),
                "triggered_count": test_triggered_count,
                "delta_mean_r": round(test_delta_mean_r, 4),
                "ci_95": (round(test_ci_95[0], 4), round(test_ci_95[1], 4)),
            },
        },
        "by_mode": mode_breakdown,
        "sample_evaluations": eval_records[:20],
    }

