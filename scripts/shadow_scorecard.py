#!/usr/bin/env python3
"""Shadow-Trading Scorecard Engine (Phase 6a)
=============================================================================
Reads closed shadow trades from `shadow_execution_audits` and candidate vetoes
from `trade_candidate_audits` / counterfactual replay tables to generate an
auditable, read-only performance scorecard.

SAFETY CONSTRAINTS:
- Strictly READ-ONLY. Does not modify any orders or trading state.
- No live-order bridge. Does not touch execution paths.
- Standalone execution: works on PostgreSQL or SQLite fixtures.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:
    import numpy as np
except ImportError:
    np = None

import random
import statistics
from sqlalchemy import create_engine, inspect, text


def _safe_mean(vals: Sequence[float]) -> float:
    if not vals:
        return 0.0
    return float(sum(vals) / len(vals))


def _safe_percentile(vals: Sequence[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    return float(s[f] * (c - k) + s[c] * (k - f))


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_scorecard")
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ShadowTrade:
    id: int
    symbol: str
    side: str
    trade_mode: str
    signal_at: datetime
    exit_at: Optional[datetime]
    quantity: int
    decision_price: float
    theoretical_fill_price: float
    realised_exit_price: Optional[float]
    stop_loss_price: Optional[float]
    take_profit_price: Optional[float]
    estimated_fees: float
    net_pnl: Optional[float]
    exit_reason: Optional[str]
    one_tick_penalty: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    holding_days: int = 0
    net_r: float = 0.0
    gross_r: float = 0.0
    risk_rupees: float = 0.0
    hold_time_minutes: float = 0.0
    trade_date: str = ""


@dataclass
class ScorecardMetrics:
    trade_count: int = 0
    sessions_covered: int = 0
    win_rate_pct: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    payoff_ratio: float = 0.0
    profit_factor: float = 0.0
    expectancy_net_r: float = 0.0
    expectancy_ci_95: Tuple[float, float] = (0.0, 0.0)
    max_drawdown_r: float = 0.0
    longest_losing_streak: int = 0
    avg_hold_time_minutes: float = 0.0
    insufficient_data: bool = False
    insufficient_reason: str = ""


def _parse_datetime(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    if isinstance(val, str):
        try:
            val_clean = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(val_clean)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                return datetime.strptime(val[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                return None
    return None


def calculate_trade_r(
    side: str,
    entry_price: float,
    exit_price: Optional[float],
    stop_loss_price: Optional[float],
    quantity: int,
    net_pnl: Optional[float],
    estimated_fees: float = 0.0,
) -> Tuple[float, float, float]:
    """Calculate (net_r, gross_r, risk_rupees)."""
    if entry_price <= 0 or quantity <= 0:
        return 0.0, 0.0, 0.0

    side_norm = side.upper()
    if stop_loss_price is not None and stop_loss_price > 0:
        if side_norm == "BUY":
            r_points = entry_price - stop_loss_price
        else:
            r_points = stop_loss_price - entry_price
    else:
        r_points = entry_price * 0.01  # 1% fallback

    if r_points <= 0.0001:
        r_points = max(0.05, entry_price * 0.005)

    risk_rupees = quantity * r_points

    if exit_price is not None:
        if side_norm == "BUY":
            gross_pnl = quantity * (exit_price - entry_price)
        else:
            gross_pnl = quantity * (entry_price - exit_price)
        gross_r = gross_pnl / risk_rupees if risk_rupees > 0 else 0.0
    else:
        gross_pnl = 0.0
        gross_r = 0.0

    if net_pnl is not None:
        net_r = net_pnl / risk_rupees if risk_rupees > 0 else 0.0
    else:
        net_pnl_calc = gross_pnl - estimated_fees
        net_r = net_pnl_calc / risk_rupees if risk_rupees > 0 else 0.0

    return round(float(net_r), 4), round(float(gross_r), 4), round(float(risk_rupees), 2)


def compute_metrics(
    trades: Sequence[ShadowTrade],
    n_bootstraps: int = 1000,
    seed: int = 42,
    min_data_threshold: int = 5,
) -> ScorecardMetrics:
    """Compute complete statistical performance metrics with 95% day-block bootstrap interval."""
    n = len(trades)
    if n < min_data_threshold:
        return ScorecardMetrics(
            trade_count=n,
            insufficient_data=True,
            insufficient_reason=f"INSUFFICIENT DATA (n={n})",
        )

    # Distinct sessions
    sessions = {t.trade_date for t in trades if t.trade_date}
    sessions_count = len(sessions) if sessions else 1

    net_rs = [t.net_r for t in trades]
    wins = [r for r in net_rs if r > 0.0]
    losses = [r for r in net_rs if r < 0.0]

    win_rate = (len(wins) / n * 100.0) if n > 0 else 0.0
    avg_win_r = _safe_mean(wins) if wins else 0.0
    avg_loss_r = _safe_mean(losses) if losses else 0.0

    payoff_ratio = (avg_win_r / abs(avg_loss_r)) if abs(avg_loss_r) > 1e-9 else (999.0 if avg_win_r > 0 else 0.0)

    # Profit factor: sum(wins) / sum(|losses|) in R
    sum_wins = sum(wins)
    sum_losses = sum(abs(r) for r in losses)
    if sum_losses > 1e-9:
        profit_factor = sum_wins / sum_losses
    else:
        profit_factor = 999.0 if sum_wins > 0 else 0.0

    expectancy = _safe_mean(net_rs)

    # Block bootstrap resampled by trading day across all symbols
    trades_by_day = defaultdict(list)
    for t in trades:
        trades_by_day[t.trade_date].append(t.net_r)

    unique_days = sorted(list(trades_by_day.keys()))
    if len(unique_days) >= 2:
        boot_means = []
        if np is not None:
            rng = np.random.default_rng(seed)
            for _ in range(n_bootstraps):
                sampled_days = rng.choice(unique_days, size=len(unique_days), replace=True)
                sampled_rs = []
                for d in sampled_days:
                    sampled_rs.extend(trades_by_day[d])
                if sampled_rs:
                    boot_means.append(float(np.mean(sampled_rs)))
            if boot_means:
                ci_lower = float(np.percentile(boot_means, 2.5))
                ci_upper = float(np.percentile(boot_means, 97.5))
            else:
                ci_lower, ci_upper = expectancy, expectancy
        else:
            rng = random.Random(seed)
            n_days = len(unique_days)
            for _ in range(n_bootstraps):
                sampled_days = [rng.choice(unique_days) for _ in range(n_days)]
                sampled_rs = []
                for d in sampled_days:
                    sampled_rs.extend(trades_by_day[d])
                if sampled_rs:
                    boot_means.append(_safe_mean(sampled_rs))
            if boot_means:
                ci_lower = _safe_percentile(boot_means, 2.5)
                ci_upper = _safe_percentile(boot_means, 97.5)
            else:
                ci_lower, ci_upper = expectancy, expectancy
    else:
        ci_lower, ci_upper = expectancy, expectancy

    # Max Drawdown in R (chronological)
    sorted_trades = sorted(trades, key=lambda t: t.exit_at or t.signal_at)
    cum_r = 0.0
    peak_r = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        cum_r += t.net_r
        if cum_r > peak_r:
            peak_r = cum_r
        dd = peak_r - cum_r
        if dd > max_dd:
            max_dd = dd

    # Longest losing streak
    longest_losing = 0
    curr_losing = 0
    for t in sorted_trades:
        if t.net_r <= 0.0:
            curr_losing += 1
            if curr_losing > longest_losing:
                longest_losing = curr_losing
        else:
            curr_losing = 0

    # Average hold time
    hold_times = [t.hold_time_minutes for t in trades if t.hold_time_minutes > 0]
    avg_hold = _safe_mean(hold_times) if hold_times else 0.0

    return ScorecardMetrics(
        trade_count=n,
        sessions_covered=sessions_count,
        win_rate_pct=round(win_rate, 2),
        avg_win_r=round(avg_win_r, 4),
        avg_loss_r=round(avg_loss_r, 4),
        payoff_ratio=round(payoff_ratio, 2),
        profit_factor=round(profit_factor, 2),
        expectancy_net_r=round(expectancy, 4),
        expectancy_ci_95=(round(ci_lower, 4), round(ci_upper, 4)),
        max_drawdown_r=round(max_dd, 4),
        longest_losing_streak=longest_losing,
        avg_hold_time_minutes=round(avg_hold, 1),
        insufficient_data=False,
    )


def compute_exit_attribution(trades: Sequence[ShadowTrade]) -> Dict[str, Dict[str, Any]]:
    """Compute trade count, total net R, and average net R by exit reason."""
    if not trades:
        return {}

    by_reason = defaultdict(list)
    for t in trades:
        reason = (t.exit_reason or "UNKNOWN").upper().strip()
        by_reason[reason].append(t)

    result = {}
    for reason, group in sorted(by_reason.items(), key=lambda x: len(x[1]), reverse=True):
        count = len(group)
        tot_r = sum(t.net_r for t in group)
        mean_r = tot_r / count if count > 0 else 0.0
        tot_pnl = sum(t.net_pnl or 0.0 for t in group)
        result[reason] = {
            "trade_count": count,
            "total_net_r": round(tot_r, 4),
            "mean_net_r": round(mean_r, 4),
            "total_net_pnl": round(tot_pnl, 2),
            "share_pct": round(count / len(trades) * 100.0, 1),
        }
    return result


def compute_cost_realism(trades: Sequence[ShadowTrade]) -> Dict[str, Any]:
    """Compute assumed versus realised slippage per trade where fills exist.
    
    Both assumed and realised slippage use identical units:
    - Points per share (pts/share)
    - Normalized R per trade
    Realised slippage is measured directly as actual fill versus decision price.
    """
    fills = [t for t in trades if t.theoretical_fill_price > 0 and t.decision_price > 0]
    n_fills = len(fills)
    if n_fills == 0:
        return {
            "fills_analyzed": 0,
            "status": "INSUFFICIENT DATA (n=0)",
        }

    assumed_slips_pts = []
    realised_slips_pts = []
    assumed_slips_r = []
    realised_slips_r = []

    for t in fills:
        qty = t.quantity if t.quantity > 0 else 1
        # Assumed slippage in points per share: one_tick_penalty or distance from decision to fill
        if t.one_tick_penalty is not None and t.one_tick_penalty > 0:
            assumed_pts = float(t.one_tick_penalty)
        else:
            assumed_pts = abs(float(t.theoretical_fill_price - t.decision_price))
        assumed_slips_pts.append(assumed_pts)
        if t.risk_rupees > 0:
            assumed_slips_r.append((assumed_pts * qty) / t.risk_rupees)

        # Realised slippage measured directly as actual fill versus decision price
        realised_pts = abs(float(t.theoretical_fill_price - t.decision_price))
        realised_slips_pts.append(realised_pts)
        if t.risk_rupees > 0:
            realised_slips_r.append((realised_pts * qty) / t.risk_rupees)

    avg_assumed_pts = _safe_mean(assumed_slips_pts) if assumed_slips_pts else 0.0
    avg_realised_pts = _safe_mean(realised_slips_pts) if realised_slips_pts else 0.0
    slippage_drag_pts = avg_realised_pts - avg_assumed_pts

    avg_assumed_r = _safe_mean(assumed_slips_r) if assumed_slips_r else 0.0
    avg_realised_r = _safe_mean(realised_slips_r) if realised_slips_r else 0.0
    slippage_drag_r = avg_realised_r - avg_assumed_r

    return {
        "fills_analyzed": n_fills,
        "avg_assumed_slippage_points": round(avg_assumed_pts, 4),
        "avg_realised_slippage_points": round(avg_realised_pts, 4),
        "slippage_drag_points": round(slippage_drag_pts, 4),
        "avg_assumed_slippage_r": round(avg_assumed_r, 4),
        "avg_realised_slippage_r": round(avg_realised_r, 4),
        "slippage_drag_r": round(slippage_drag_r, 4),
        "status": "MEASURED",
    }


def compute_layer_attribution(
    engine,
    since_date: date,
    until_date: Optional[date] = None,
    mode_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze vetoes by architecture layer (Chart Gate, Brains, LLM) and counterfactual replay fidelity."""
    if isinstance(until_date, str) and until_date.lower() in ("intraday", "swing"):
        mode_filter = until_date
        until_date = None

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if "trade_candidate_audits" not in existing_tables:
        return {
            "status": "NOT MEASURABLE",
            "reason": "Table 'trade_candidate_audits' does not exist in database",
            "layers": {},
        }

    dialect = getattr(getattr(engine, "dialect", None), "name", "")
    date_filter = "observed_at >= :since" if dialect == "sqlite" else "(observed_at AT TIME ZONE 'Asia/Kolkata')::date >= :since"
    params: Dict[str, Any] = {"since": since_date.isoformat() if dialect == "sqlite" else since_date}
    if until_date:
        date_filter += " AND " + ("observed_at <= :until" if dialect == "sqlite" else "(observed_at AT TIME ZONE 'Asia/Kolkata')::date <= :until")
        params["until"] = until_date.isoformat() if dialect == "sqlite" else until_date

    # Query candidate rejections
    mode_sql = ""
    if mode_filter:
        mode_sql = "AND UPPER(trade_mode) = :mode"
        params["mode"] = mode_filter.upper()

    try:
        with engine.connect() as conn:
            query = text(f"""
                SELECT id, selector_stage, rejection_reason, accepted,
                       trade_mode, observed_at, details
                FROM trade_candidate_audits
                WHERE {date_filter} {mode_sql}
            """)
            rows = conn.execute(query, params).mappings().all()
    except Exception as exc:
        return {
            "status": "NOT MEASURABLE",
            "reason": f"Failed querying candidate audits: {exc}",
            "layers": {},
        }

    total_candidates = len(rows)
    if total_candidates == 0:
        return {
            "status": "INSUFFICIENT DATA (n=0)",
            "reason": "No candidate audits recorded since requested date",
            "layers": {},
        }

    # Fidelity check verification from counterfactual replay
    # Phase 2d / 3b fidelity check requires >= 80% fidelity on modelled exits
    fidelity_passed = False
    fidelity_reason = "No counterfactual replay audits found"
    fidelity_pct = 0.0

    if "counterfactual_daily_audits" in existing_tables:
        try:
            with engine.connect() as conn:
                cf_d_filt = "audit_date >= :since"
                cf_params = {"since": since_date.isoformat() if dialect == "sqlite" else since_date}
                if until_date:
                    cf_d_filt += " AND audit_date <= :until"
                    cf_params["until"] = until_date.isoformat() if dialect == "sqlite" else until_date
                cf_query = text(f"""
                    SELECT AVG(gate_accuracy_pct) as avg_acc, COUNT(*) as c
                    FROM counterfactual_daily_audits
                    WHERE {cf_d_filt}
                """)
                cf_row = conn.execute(cf_query, cf_params).mappings().one_or_none()
                if cf_row and cf_row["c"] and cf_row["c"] > 0:
                    fidelity_pct = float(cf_row["avg_acc"] or 0.0)
                    if fidelity_pct >= 80.0:
                        fidelity_passed = True
                    else:
                        fidelity_reason = f"Replay fidelity {fidelity_pct:.1f}% below 80.0% required gate"
        except Exception:
            pass

    # Check counterfactual candidate outcomes
    cf_outcomes = {}
    if "counterfactual_candidate_log" in existing_tables:
        try:
            with engine.connect() as conn:
                log_query = text(f"""
                    SELECT candidate_audit_id, outcome_label
                    FROM counterfactual_candidate_log
                    WHERE outcome_label IS NOT NULL
                """)
                for r in conn.execute(log_query).mappings().all():
                    cf_outcomes[r["candidate_audit_id"]] = (r["outcome_label"] or "").upper()
        except Exception:
            pass

    # Classify layers: Chart Gate, Brains, LLM
    layer_stats = {
        "Chart Gate": {"vetoed": 0, "would_have_won": 0, "would_have_lost": 0, "neutral": 0},
        "Brains": {"vetoed": 0, "would_have_won": 0, "would_have_lost": 0, "neutral": 0},
        "LLM": {"vetoed": 0, "would_have_won": 0, "would_have_lost": 0, "neutral": 0},
        "Other / Execution": {"vetoed": 0, "would_have_won": 0, "would_have_lost": 0, "neutral": 0},
    }

    for r in rows:
        if bool(r["accepted"]):
            continue

        stage = (r.get("selector_stage") or "").lower()
        reason = (r.get("rejection_reason") or "").lower()

        if any(k in stage or k in reason for k in ["llm", "claude", "gpt", "deep_thinker_llm"]):
            layer = "LLM"
        elif any(k in stage or k in reason for k in ["chart", "swing_risk", "stop_cap", "adx", "pattern", "candle"]):
            layer = "Chart Gate"
        elif any(k in stage or k in reason for k in ["brain", "risk", "signal", "sentinel", "regime", "volatility"]):
            layer = "Brains"
        else:
            layer = "Other / Execution"

        layer_stats[layer]["vetoed"] += 1

        cand_id = r.get("id")
        outcome = cf_outcomes.get(cand_id, "")
        if "WIN" in outcome:
            layer_stats[layer]["would_have_won"] += 1
        elif "LOSS" in outcome or "SAVED" in outcome:
            layer_stats[layer]["would_have_lost"] += 1
        else:
            layer_stats[layer]["neutral"] += 1

    # Format result per instructions
    res_layers = {}
    for layer_name, stats in layer_stats.items():
        vetoed = stats["vetoed"]
        if vetoed == 0:
            continue

        if not fidelity_passed:
            res_layers[layer_name] = {
                "vetoed_count": vetoed,
                "counterfactual_result": "NOT MEASURABLE",
                "reason": fidelity_reason,
            }
        else:
            res_layers[layer_name] = {
                "vetoed_count": vetoed,
                "counterfactual_result": "MEASURED",
                "fidelity_pct": round(fidelity_pct, 2),
                "would_have_won": stats["would_have_won"],
                "would_have_lost": stats["would_have_lost"],
                "neutral": stats["neutral"],
            }

    return {
        "status": "PASS" if fidelity_passed else "NOT MEASURABLE",
        "fidelity_passed": fidelity_passed,
        "fidelity_reason": fidelity_reason if not fidelity_passed else "Replay fidelity check passed",
        "fidelity_pct": round(fidelity_pct, 2),
        "layers": res_layers,
    }


def check_data_integrity(
    engine,
    since_date: date,
    open_trades: Sequence[ShadowTrade],
    until_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Check for open positions, missing bars, and session anomalies."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    open_pos_summary = []
    for t in open_trades:
        open_pos_summary.append({
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "trade_mode": t.trade_mode,
            "signal_at": t.signal_at.isoformat() if t.signal_at else "",
            "decision_price": t.decision_price,
            "stop_loss_price": t.stop_loss_price,
        })

    anomalous_sessions = []
    if "shadow_session_daily_log" in existing_tables:
        try:
            with engine.connect() as conn:
                dialect = getattr(getattr(engine, "dialect", None), "name", "")
                d_filt = "session_date >= :since"
                d_params: Dict[str, Any] = {"since": since_date.isoformat() if dialect == "sqlite" else since_date}
                if until_date:
                    d_filt += " AND session_date <= :until"
                    d_params["until"] = until_date.isoformat() if dialect == "sqlite" else until_date
                q = text(f"""
                    SELECT session_date, status, valid, warnings, mistakes
                    FROM shadow_session_daily_log
                    WHERE {d_filt} AND (valid = FALSE OR status != 'COMPLETED')
                    ORDER BY session_date DESC
                """)
                rows = conn.execute(q, d_params).mappings().all()
                for r in rows:
                    anomalous_sessions.append({
                        "session_date": str(r["session_date"]),
                        "status": r["status"],
                        "valid": r["valid"],
                        "warnings": r["warnings"],
                    })
        except Exception:
            pass

    return {
        "open_positions_count": len(open_trades),
        "open_positions": open_pos_summary,
        "anomalous_sessions_count": len(anomalous_sessions),
        "anomalous_sessions": anomalous_sessions,
    }


def evaluate_promotion_gates(
    metrics: ScorecardMetrics,
    max_dd_limit: float = 5.0,
) -> Dict[str, Any]:
    """Evaluate promotion criteria (pass/fail, no auto-action) and report exact deficits."""
    gates = {}
    all_passed = True

    # Gate 1: Trades >= 300
    trades_req = 300
    t_pass = metrics.trade_count >= trades_req
    t_delta = metrics.trade_count - trades_req
    gates["min_trades"] = {
        "description": "At least 300 trades",
        "required": trades_req,
        "actual": metrics.trade_count,
        "passed": t_pass,
        "margin": t_delta,
        "message": f"{metrics.trade_count} / {trades_req} ({'PASS' if t_pass else f'short by {abs(t_delta)} trades'})",
    }
    if not t_pass:
        all_passed = False

    # Gate 2: Sessions >= 20
    sess_req = 20
    s_pass = metrics.sessions_covered >= sess_req
    s_delta = metrics.sessions_covered - sess_req
    gates["min_sessions"] = {
        "description": "At least 20 sessions",
        "required": sess_req,
        "actual": metrics.sessions_covered,
        "passed": s_pass,
        "margin": s_delta,
        "message": f"{metrics.sessions_covered} / {sess_req} ({'PASS' if s_pass else f'short by {abs(s_delta)} sessions'})",
    }
    if not s_pass:
        all_passed = False

    # Gate 3: Profit factor >= 1.3
    pf_req = 1.3
    pf_pass = metrics.profit_factor >= pf_req
    pf_delta = round(metrics.profit_factor - pf_req, 2)
    gates["profit_factor"] = {
        "description": "Profit Factor >= 1.30",
        "required": pf_req,
        "actual": metrics.profit_factor,
        "passed": pf_pass,
        "margin": pf_delta,
        "message": f"{metrics.profit_factor:.2f} >= {pf_req:.2f} ({'PASS' if pf_pass else f'short by {abs(pf_delta):.2f}'})",
    }
    if not pf_pass:
        all_passed = False

    # Gate 4: Payoff >= 1.2
    po_req = 1.2
    po_pass = metrics.payoff_ratio >= po_req
    po_delta = round(metrics.payoff_ratio - po_req, 2)
    gates["payoff_ratio"] = {
        "description": "Payoff Ratio >= 1.20",
        "required": po_req,
        "actual": metrics.payoff_ratio,
        "passed": po_pass,
        "margin": po_delta,
        "message": f"{metrics.payoff_ratio:.2f} >= {po_req:.2f} ({'PASS' if po_pass else f'short by {abs(po_delta):.2f}'})",
    }
    if not po_pass:
        all_passed = False

    # Gate 5: Expectancy 95% CI Lower Bound > 0.0
    if metrics.insufficient_data or metrics.trade_count == 0:
        ci_pass = False
        gates["expectancy_ci_positive"] = {
            "description": "Expectancy 95% CI Lower Bound > 0.00R",
            "required": 0.0001,
            "actual": "NO DATA",
            "passed": False,
            "margin": None,
            "message": "NO DATA",
        }
    else:
        ci_lower = metrics.expectancy_ci_95[0]
        ci_pass = ci_lower > 0.0
        ci_delta = round(ci_lower - 0.0, 4)
        gates["expectancy_ci_positive"] = {
            "description": "Expectancy 95% CI Lower Bound > 0.00R",
            "required": 0.0001,
            "actual": ci_lower,
            "passed": ci_pass,
            "margin": ci_delta,
            "message": f"{ci_lower:+.4f}R > 0.00R ({'PASS' if ci_pass else f'lower bound is {ci_lower:+.4f}R'})",
        }
    if not ci_pass:
        all_passed = False

    # Gate 6: Max Drawdown in R <= max_dd_limit
    dd_pass = metrics.max_drawdown_r <= max_dd_limit
    dd_delta = round(max_dd_limit - metrics.max_drawdown_r, 4)
    gates["max_drawdown"] = {
        "description": f"Max Drawdown <= {max_dd_limit:.2f}R",
        "required": max_dd_limit,
        "actual": metrics.max_drawdown_r,
        "passed": dd_pass,
        "margin": dd_delta,
        "message": f"{metrics.max_drawdown_r:.2f}R <= {max_dd_limit:.2f}R ({'PASS' if dd_pass else f'exceeded by {abs(dd_delta):.2f}R'})",
    }
    if not dd_pass:
        all_passed = False

    return {
        "all_gates_passed": all_passed,
        "overall_status": "ELIGIBLE FOR PROMOTION" if all_passed else "NOT ELIGIBLE",
        "gates": gates,
    }


def load_shadow_trades_from_db(
    engine,
    since_date: date,
    until_date: Optional[date] = None,
    mode_filter: Optional[str] = None,
) -> Tuple[List[ShadowTrade], List[ShadowTrade]]:
    """Fetch closed trades and open trades from shadow_execution_audits."""
    if isinstance(until_date, str) and until_date.lower() in ("intraday", "swing"):
        mode_filter = until_date
        until_date = None

    inspector = inspect(engine)
    if "shadow_execution_audits" not in set(inspector.get_table_names()):
        return [], []

    dialect = getattr(getattr(engine, "dialect", None), "name", "")
    date_filter = "a.signal_at >= :since" if dialect == "sqlite" else "(a.signal_at AT TIME ZONE 'Asia/Kolkata')::date >= :since"
    params: Dict[str, Any] = {"since": since_date.isoformat() if dialect == "sqlite" else since_date}
    if until_date:
        date_filter += " AND " + ("a.signal_at <= :until" if dialect == "sqlite" else "(a.signal_at AT TIME ZONE 'Asia/Kolkata')::date <= :until")
        params["until"] = until_date.isoformat() if dialect == "sqlite" else until_date

    mode_sql = ""
    if mode_filter:
        mode_sql = "AND UPPER(COALESCE(a.trade_mode, 'INTRADAY')) = :mode"
        params["mode"] = mode_filter.upper()

    closed_trades: List[ShadowTrade] = []
    open_trades: List[ShadowTrade] = []

    has_inst = "instrument_master" in set(inspector.get_table_names())
    sym_join = "LEFT JOIN instrument_master i ON i.id = a.instrument_id" if has_inst else ""
    sym_col = "COALESCE(i.symbol, 'UNKNOWN')" if has_inst else "'UNKNOWN'"

    query = text(f"""
        SELECT a.id, a.side, COALESCE(a.trade_mode, 'INTRADAY') as trade_mode,
               a.signal_at, a.exit_at, a.quantity, a.decision_price,
               COALESCE(a.theoretical_fill_price, a.decision_price) as theoretical_fill_price,
               a.realised_exit_price, a.stop_loss_price, a.take_profit_price,
               COALESCE(a.estimated_fees, 0) as estimated_fees, a.net_pnl, a.exit_reason,
               a.one_tick_penalty, a.best_bid, a.best_ask, COALESCE(a.holding_days, 0) as holding_days,
               {sym_col} as symbol
        FROM shadow_execution_audits a
        {sym_join}
        WHERE {date_filter} {mode_sql}
        ORDER BY a.signal_at ASC
    """)

    with engine.connect() as conn:
        rows = conn.execute(query, params).mappings().all()

    for r in rows:
        sig_dt = _parse_datetime(r["signal_at"])
        exit_dt = _parse_datetime(r["exit_at"])
        if sig_dt is None:
            continue

        trade_dt_str = sig_dt.astimezone(IST).strftime("%Y-%m-%d") if sig_dt else ""

        entry_p = float(r["theoretical_fill_price"] or r["decision_price"] or 0.0)
        exit_p = float(r["realised_exit_price"]) if r["realised_exit_price"] is not None else None
        sl_p = float(r["stop_loss_price"]) if r["stop_loss_price"] is not None else None
        tp_p = float(r["take_profit_price"]) if r["take_profit_price"] is not None else None
        qty = int(r["quantity"] or 1)
        fees = float(r["estimated_fees"] or 0.0)
        net_pnl = float(r["net_pnl"]) if r["net_pnl"] is not None else None

        hold_mins = 0.0
        if sig_dt and exit_dt:
            hold_mins = max(0.0, (exit_dt - sig_dt).total_seconds() / 60.0)

        net_r, gross_r, risk_rs = calculate_trade_r(
            side=str(r["side"] or "BUY"),
            entry_price=entry_p,
            exit_price=exit_p,
            stop_loss_price=sl_p,
            quantity=qty,
            net_pnl=net_pnl,
            estimated_fees=fees,
        )

        trade = ShadowTrade(
            id=int(r["id"]),
            symbol=str(r["symbol"]),
            side=str(r["side"] or "BUY").upper(),
            trade_mode=str(r["trade_mode"] or "INTRADAY").upper(),
            signal_at=sig_dt,
            exit_at=exit_dt,
            quantity=qty,
            decision_price=float(r["decision_price"] or entry_p),
            theoretical_fill_price=entry_p,
            realised_exit_price=exit_p,
            stop_loss_price=sl_p,
            take_profit_price=tp_p,
            estimated_fees=fees,
            net_pnl=net_pnl,
            exit_reason=str(r["exit_reason"]) if r["exit_reason"] else None,
            one_tick_penalty=float(r["one_tick_penalty"]) if r["one_tick_penalty"] is not None else None,
            best_bid=float(r["best_bid"]) if r["best_bid"] is not None else None,
            best_ask=float(r["best_ask"]) if r["best_ask"] is not None else None,
            holding_days=int(r["holding_days"] or 0),
            net_r=net_r,
            gross_r=gross_r,
            risk_rupees=risk_rs,
            hold_time_minutes=hold_mins,
            trade_date=trade_dt_str,
        )

        if exit_p is not None and net_pnl is not None:
            closed_trades.append(trade)
        else:
            open_trades.append(trade)

    return closed_trades, open_trades


def generate_full_scorecard(
    engine,
    since_date: date,
    until_date: Optional[date] = None,
    mode_filter: Optional[str] = None,
    max_dd_limit: float = 5.0,
) -> Dict[str, Any]:
    """Generate comprehensive shadow-trading scorecard report."""
    if isinstance(until_date, str) and until_date.lower() in ("intraday", "swing"):
        mode_filter = until_date
        until_date = None

    closed_trades, open_trades = load_shadow_trades_from_db(engine, since_date, until_date, mode_filter)

    # Segregate trades by mode & direction
    intraday_trades = [t for t in closed_trades if t.trade_mode == "INTRADAY"]
    swing_trades = [t for t in closed_trades if t.trade_mode == "SWING"]

    categories = {
        "overall": closed_trades,
        "overall_long": [t for t in closed_trades if t.side == "BUY"],
        "overall_short": [t for t in closed_trades if t.side == "SELL"],
        "intraday_all": intraday_trades,
        "intraday_long": [t for t in intraday_trades if t.side == "BUY"],
        "intraday_short": [t for t in intraday_trades if t.side == "SELL"],
        "swing_all": swing_trades,
        "swing_long": [t for t in swing_trades if t.side == "BUY"],
        "swing_short": [t for t in swing_trades if t.side == "SELL"],
    }

    metrics_by_category = {}
    for cat_name, t_list in categories.items():
        metrics_by_category[cat_name] = compute_metrics(t_list)

    # Attribution sections
    exit_attribution = compute_exit_attribution(closed_trades)
    cost_realism = compute_cost_realism(closed_trades)
    layer_attribution = compute_layer_attribution(engine, since_date, until_date, mode_filter)
    data_integrity = check_data_integrity(engine, since_date, open_trades, until_date)

    # Promotion Gates
    overall_metrics = metrics_by_category["overall"]
    promotion_gates = evaluate_promotion_gates(overall_metrics, max_dd_limit=max_dd_limit)

    db_url_str = engine.url.render_as_string(hide_password=True) if hasattr(engine, "url") else str(engine)

    return {
        "generated_at": datetime.now(IST).isoformat(),
        "database_source": db_url_str,
        "since_date": since_date.isoformat(),
        "until_date": until_date.isoformat() if until_date else None,
        "mode_filter": mode_filter.upper() if mode_filter else "ALL",
        "total_closed_trades": len(closed_trades),
        "total_open_trades": len(open_trades),
        "metrics": {k: asdict(v) for k, v in metrics_by_category.items()},
        "exit_attribution": exit_attribution,
        "cost_realism": cost_realism,
        "layer_attribution": layer_attribution,
        "data_integrity": data_integrity,
        "promotion_gates": promotion_gates,
    }


def format_plain_text_report(report: Dict[str, Any]) -> str:
    """Format structured report dict into human-readable, auditable text."""
    lines = []
    lines.append("=" * 78)
    lines.append("             SHADOW-TRADING STATISTICAL SCORECARD (PHASE 6A)")
    lines.append("=" * 78)
    lines.append(f"  Database     : {report.get('database_source', 'UNKNOWN')}")
    lines.append(f"  Generated At : {report['generated_at']}")
    lines.append(f"  Since Date   : {report['since_date']}")
    if report.get("until_date"):
        lines.append(f"  Until Date   : {report['until_date']}")
    lines.append(f"  Mode Filter  : {report['mode_filter']}")
    lines.append(f"  Closed Trades: {report['total_closed_trades']}")
    lines.append(f"  Open Trades  : {report['total_open_trades']}")
    lines.append("=" * 78)

    # Section 1: Performance Summary Table
    lines.append("\n[1] PERFORMANCE SUMMARY PER MODE & DIRECTION")
    lines.append("-" * 78)
    header = f"{'Segment':<16} | {'Trades':>6} | {'Win %':>6} | {'Payoff':>6} | {'PF':>5} | {'Expectancy (95% CI)':^28}"
    lines.append(header)
    lines.append("-" * 78)

    order = [
        ("Overall", "overall"),
        ("  Long", "overall_long"),
        ("  Short", "overall_short"),
        ("Intraday", "intraday_all"),
        ("  Long", "intraday_long"),
        ("  Short", "intraday_short"),
        ("Swing", "swing_all"),
        ("  Long", "swing_long"),
        ("  Short", "swing_short"),
    ]

    for label, key in order:
        m = report["metrics"].get(key, {})
        if m.get("insufficient_data"):
            lines.append(f"{label:<16} | {m.get('trade_count', 0):>6} | {m.get('insufficient_reason', 'INSUFFICIENT DATA'):^48}")
        else:
            exp_str = f"{m['expectancy_net_r']:+.2f}R [{m['expectancy_ci_95'][0]:+.2f}, {m['expectancy_ci_95'][1]:+.2f}]"
            lines.append(
                f"{label:<16} | {m['trade_count']:>6} | {m['win_rate_pct']:>5.1f}% | "
                f"{m['payoff_ratio']:>6.2f} | {m['profit_factor']:>5.2f} | {exp_str:^28}"
            )
    lines.append("-" * 78)

    # Detailed statistics for Overall
    om = report["metrics"].get("overall", {})
    if not om.get("insufficient_data"):
        lines.append(f"  * Sessions Covered      : {om['sessions_covered']}")
        lines.append(f"  * Average Win / Loss    : {om['avg_win_r']:+.2f}R / {om['avg_loss_r']:+.2f}R")
        lines.append(f"  * Max Drawdown in R     : {om['max_drawdown_r']:.2f}R")
        lines.append(f"  * Longest Losing Streak : {om['longest_losing_streak']} trades")
        lines.append(f"  * Average Hold Time     : {om['avg_hold_time_minutes']:.1f} minutes")

    # Section 2: Exit Attribution
    lines.append("\n[2] EXIT ATTRIBUTION (Closed Trades)")
    lines.append("-" * 78)
    exit_attr = report.get("exit_attribution", {})
    if not exit_attr:
        lines.append("  INSUFFICIENT DATA (n=0)")
    else:
        lines.append(f"{'Exit Reason':<28} | {'Trades':>6} | {'Share %':>7} | {'Total Net R':>12} | {'Mean Net R':>10}")
        lines.append("-" * 78)
        for reason, d in exit_attr.items():
            lines.append(
                f"{reason:<28} | {d['trade_count']:>6} | {d['share_pct']:>6.1f}% | "
                f"{d['total_net_r']:>+11.2f}R | {d['mean_net_r']:>+9.2f}R"
            )

    # Section 3: Layer Attribution & Replay Fidelity
    lines.append("\n[3] LAYER ATTRIBUTION (Candidate Vetoes & Counterfactual)")
    lines.append("-" * 78)
    layer_attr = report.get("layer_attribution", {})
    l_status = layer_attr.get("status", "UNKNOWN")
    lines.append(f"  Fidelity Gate Status: {l_status} ({layer_attr.get('fidelity_reason', '')})")
    layers = layer_attr.get("layers", {})
    if not layers:
        lines.append("  INSUFFICIENT DATA (n=0)")
    else:
        lines.append(f"{'Architecture Layer':<20} | {'Vetoed':>8} | {'Counterfactual Replay Finding':<44}")
        lines.append("-" * 78)
        for layer_name, ld in layers.items():
            c_res = ld.get("counterfactual_result")
            if c_res == "NOT MEASURABLE":
                res_str = f"NOT MEASURABLE ({ld.get('reason', 'fidelity check failed')})"
            else:
                res_str = f"Won: {ld.get('would_have_won', 0)} | Lost: {ld.get('would_have_lost', 0)} | Neutral: {ld.get('neutral', 0)}"
            lines.append(f"{layer_name:<20} | {ld.get('vetoed_count', 0):>8} | {res_str:<44}")

    # Section 4: Cost Realism
    lines.append("\n[4] COST REALISM & SLIPPAGE AUDIT")
    lines.append("-" * 78)
    cr = report.get("cost_realism", {})
    if cr.get("status") == "INSUFFICIENT DATA (n=0)":
        lines.append("  INSUFFICIENT DATA (n=0 fills analyzed)")
    else:
        lines.append(f"  Fills Analyzed       : {cr.get('fills_analyzed', 0)}")
        lines.append(f"  Assumed Slippage     : {cr.get('avg_assumed_slippage_points', 0.0):.4f} pts/share ({cr.get('avg_assumed_slippage_r', 0.0):+.4f}R)")
        lines.append(f"  Realised Slippage    : {cr.get('avg_realised_slippage_points', 0.0):.4f} pts/share ({cr.get('avg_realised_slippage_r', 0.0):+.4f}R) [fill vs decision]")
        drag_pts = cr.get('slippage_drag_points', 0.0)
        drag_r = cr.get('slippage_drag_r', 0.0)
        lines.append(f"  Net Slippage Drag    : {drag_pts:+.4f} pts/share ({drag_r:+.4f}R)")

    # Section 5: Data Integrity
    lines.append("\n[5] DATA INTEGRITY AUDIT")
    lines.append("-" * 78)
    di = report.get("data_integrity", {})
    lines.append(f"  Open Positions at Report Time : {di.get('open_positions_count', 0)}")
    if di.get("open_positions"):
        for p in di["open_positions"][:5]:
            lines.append(f"    - #{p['id']} {p['symbol']} ({p['side']}) Signal: {p['signal_at']} Entry: {p['decision_price']}")
        if len(di["open_positions"]) > 5:
            lines.append(f"    ... and {len(di['open_positions']) - 5} more open positions")

    lines.append(f"  Anomalous Sessions / Missing Bars: {di.get('anomalous_sessions_count', 0)}")
    if di.get("anomalous_sessions"):
        for s in di["anomalous_sessions"]:
            lines.append(f"    - Date: {s['session_date']} Status: {s['status']} Warnings: {s['warnings']}")

    # Section 6: Promotion Gates (Pass / Fail)
    lines.append("\n[6] PROMOTION GATES (Read-Only Evaluation)")
    lines.append("-" * 78)
    pg = report.get("promotion_gates", {})
    lines.append(f"  VERDICT: {pg.get('overall_status', 'UNKNOWN')}\n")
    gates = pg.get("gates", {})
    for g_id, g in gates.items():
        status_tag = "[PASS]" if g.get("passed") else "[FAIL]"
        lines.append(f"  {status_tag:<7} {g.get('description', g_id):<42} : {g.get('message', '')}")
    lines.append("=" * 78)

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Shadow-trading statistical scorecard report generator.")
    parser.add_argument("--since", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="End date (YYYY-MM-DD, optional)")
    parser.add_argument("--mode", choices=["intraday", "swing"], default=None, help="Filter trade mode (intraday or swing)")
    parser.add_argument("--json", dest="json_out", default=None, help="Path to write structured JSON report")
    parser.add_argument("--db", dest="db_url", default=None, help="Optional DATABASE_URL override")
    parser.add_argument("--max-dd-limit", type=float, default=5.0, help="Configurable maximum drawdown limit in R (default: 5.0)")

    args = parser.parse_args()

    try:
        since_date = date.fromisoformat(args.since)
    except ValueError:
        logger.error(f"Invalid date format for --since: {args.since}. Expected YYYY-MM-DD.")
        sys.exit(1)

    until_date = None
    if args.until:
        try:
            until_date = date.fromisoformat(args.until)
        except ValueError:
            logger.error(f"Invalid date format for --until: {args.until}. Expected YYYY-MM-DD.")
            sys.exit(1)

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        # Check standard default candidate URLs
        candidates = [
            "postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging",
            "postgresql://avinash@localhost:5432/nivesh_v3_staging",
            "postgresql://localhost:5432/nivesh_v3_staging",
        ]
        try:
            from backend.config import DATABASE_URL
            if DATABASE_URL:
                candidates.insert(0, DATABASE_URL)
        except Exception:
            pass

        # Find first connectable URL
        for cand in candidates:
            cand_norm = cand.replace("postgresql://", "postgresql+psycopg2://", 1) if cand.startswith("postgresql://") else cand
            try:
                test_eng = create_engine(cand_norm, pool_pre_ping=True)
                with test_eng.connect():
                    db_url = cand_norm
                    break
            except Exception:
                continue

        if not db_url:
            db_url = "postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging"

    if db_url and db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    logger.info(f"Connecting to database: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    engine = create_engine(db_url, pool_pre_ping=True)

    report = generate_full_scorecard(
        engine=engine,
        since_date=since_date,
        until_date=until_date,
        mode_filter=args.mode,
        max_dd_limit=args.max_dd_limit,
    )

    text_output = format_plain_text_report(report)
    print(text_output)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Report written to JSON: {args.json_out}")


if __name__ == "__main__":
    main()
