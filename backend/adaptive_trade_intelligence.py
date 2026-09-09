"""Adaptive paper-trade intelligence for trap avoidance and reward learning.

This module is intentionally deterministic and paper-only.  It does not train
itself into live execution; it turns closed shadow trades into auditable reward
points and uses those points to reject repeated bad setups during later paper
sessions.
"""
import json
import math
from datetime import datetime
from decimal import Decimal
from typing import Dict, Mapping, Optional

from sqlalchemy import text


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS adaptive_trade_rewards(
    id BIGSERIAL PRIMARY KEY,
    audit_id BIGINT NOT NULL UNIQUE REFERENCES shadow_execution_audits(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    underlying_symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    regime TEXT NOT NULL,
    hour_bucket SMALLINT NOT NULL,
    quality_score NUMERIC(8,2) NOT NULL,
    reward_points NUMERIC(12,4) NOT NULL,
    net_pnl NUMERIC(24,10) NOT NULL,
    exit_reason TEXT,
    mistake_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    signature_hash TEXT NOT NULL,
    learned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_signature ON adaptive_trade_rewards(signature_hash, learned_at DESC);
CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_underlying ON adaptive_trade_rewards(underlying_symbol, instrument_type, side, learned_at DESC);
CREATE INDEX IF NOT EXISTS idx_adaptive_rewards_strategy ON adaptive_trade_rewards(strategy, regime, learned_at DESC);
CREATE TABLE IF NOT EXISTS adaptive_trade_guardrails(
    trade_date DATE PRIMARY KEY,
    reward_points NUMERIC(14,4) NOT NULL DEFAULT 0,
    trades_scored INTEGER NOT NULL DEFAULT 0,
    hard_kill_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    kill_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def ensure_adaptive_schema(connection) -> None:
    connection.execute(text(SCHEMA_SQL))


def _safe_json(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _strategy_from_note(note, row=None) -> str:
    payload = _safe_json(note, {})
    if isinstance(payload, dict):
        value = payload.get("strategy") or payload.get("risk_manager", {}).get("strategy")
        if value and str(value).upper() not in ("UNKNOWN_STRATEGY", "UNKNOWN", "NO_TRADE"):
            return str(value).upper()[:80]
    side = str((row or {}).get("side") or "BUY").upper()
    itype = str((row or {}).get("instrument_type") or "EQ").upper()
    if itype == "CE":
        return "MOMENTUM_CALL_BUY" if side == "BUY" else "TREND_PULLBACK_CALL_SELL"
    elif itype == "PE":
        return "BREAKDOWN_PUT_BUY" if side == "BUY" else "TREND_PULLBACK_PUT_SELL"
    return "BREAKOUT_CALL_BUY" if side == "BUY" else "BREAKDOWN_PUT_BUY"


def _regime_from_note(note) -> str:
    payload = _safe_json(note, {})
    if isinstance(payload, dict):
        policy = payload.get("policy_candidate") or payload.get("market_quality") or {}
        if isinstance(policy, dict) and policy.get("regime"):
            return str(policy["regime"]).lower()[:40]
    return "intraday_shadow"


def signature(row: Mapping, strategy: str, regime: str) -> str:
    hour = int(row["signal_at"].astimezone().hour if hasattr(row["signal_at"], "astimezone") else datetime.fromisoformat(str(row["signal_at"])).hour)
    parts = [
        str(row.get("underlying_symbol") or row.get("symbol") or "").upper().replace(" ", ""),
        str(row.get("instrument_type") or "").upper(),
        str(row.get("side") or "").upper(),
        strategy.upper(),
        regime.lower(),
        str(hour),
    ]
    import hashlib
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def reward_points(net_pnl: Decimal, quality_score: float, exit_reason: str, mistake_tags) -> Decimal:
    """Bounded RL-style reward: profit helps, repeated mistakes hurt harder."""
    pnl = Decimal(net_pnl or 0)
    scaled = Decimal(str(math.tanh(float(pnl) / 500.0))) * Decimal("10")
    quality = (Decimal(str(quality_score)) - Decimal("60")) / Decimal("10")
    penalty = Decimal("0")
    tags = set(str(item) for item in (mistake_tags or []))
    if exit_reason == "STOP_LOSS":
        penalty += Decimal("4")
    if "cost_drag" in tags:
        penalty += Decimal("2")
    if "fallback_fill" in tags:
        penalty += Decimal("2")
    if "stop_loss_hit" in tags:
        penalty += Decimal("2")
    if "stale_signal_loss" in tags:
        penalty += Decimal("4")
    if "slow_winner" in tags:
        penalty += Decimal("1")
    return (scaled + quality - penalty).quantize(Decimal("0.0001"))


def sync_adaptive_rewards(engine, score_trade_fn, limit: int = 1000) -> Dict:
    """Export newly closed paper trades into reward memory."""
    inserted = 0
    with engine.begin() as connection:
        ensure_adaptive_schema(connection)
        rows = connection.execute(text("""
            SELECT a.*, i.exchange, i.symbol, i.instrument_type,
                   COALESCE(i.underlying_symbol,i.symbol) underlying_symbol
            FROM shadow_execution_audits a
            JOIN instrument_master i ON i.id=a.instrument_id
            LEFT JOIN adaptive_trade_rewards r ON r.audit_id=a.id
            WHERE a.net_pnl IS NOT NULL AND r.audit_id IS NULL
            ORDER BY a.exit_at DESC NULLS LAST, a.id DESC
            LIMIT :limit
        """), {"limit": limit}).mappings().all()
        for row in rows:
            strategy = _strategy_from_note(row.get("improvement_note"), row)
            regime = _regime_from_note(row.get("improvement_note"))
            quality = score_trade_fn({
                **dict(row),
                "entry_price": row.get("theoretical_fill_price") or row.get("decision_price"),
                "latest_price": row.get("realised_exit_price") or row.get("theoretical_fill_price") or row.get("decision_price"),
                "is_open": False,
            })
            tags = _safe_json(row.get("mistake_tags"), [])
            points = reward_points(Decimal(row["net_pnl"] or 0), float(quality.get("score") or 0), str(row.get("exit_reason") or ""), tags)
            sig = signature(row, strategy, regime)
            connection.execute(text("""
                INSERT INTO adaptive_trade_rewards(audit_id,model_version,exchange,symbol,underlying_symbol,
                    instrument_type,side,strategy,regime,hour_bucket,quality_score,reward_points,net_pnl,
                    exit_reason,mistake_tags,signature_hash)
                VALUES(:audit_id,:model_version,:exchange,:symbol,:underlying,:type,:side,:strategy,:regime,
                    :hour,:quality,:reward,:net,:exit_reason,CAST(:tags AS jsonb),:signature)
                ON CONFLICT(audit_id) DO NOTHING
            """), {
                "audit_id": row["id"], "model_version": row["model_version"], "exchange": row["exchange"],
                "symbol": row["symbol"], "underlying": row["underlying_symbol"], "type": row["instrument_type"],
                "side": row["side"], "strategy": strategy, "regime": regime,
                "hour": row["signal_at"].astimezone().hour if hasattr(row["signal_at"], "astimezone") else 0,
                "quality": Decimal(str(quality.get("score") or 0)), "reward": points,
                "net": row["net_pnl"], "exit_reason": row.get("exit_reason"), "tags": json.dumps(tags),
                "signature": sig,
            })
            inserted += 1
        connection.execute(text("""
            INSERT INTO adaptive_trade_guardrails(trade_date,reward_points,trades_scored,updated_at)
            SELECT (learned_at AT TIME ZONE 'Asia/Kolkata')::date, COALESCE(SUM(reward_points),0),
                   COUNT(*), CURRENT_TIMESTAMP
            FROM adaptive_trade_rewards
            WHERE (learned_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
            GROUP BY (learned_at AT TIME ZONE 'Asia/Kolkata')::date
            ON CONFLICT(trade_date) DO UPDATE SET reward_points=EXCLUDED.reward_points,
                trades_scored=EXCLUDED.trades_scored, updated_at=CURRENT_TIMESTAMP
        """))
    return {"status": "success", "inserted": inserted, "orders_allowed": False}


def trap_assessment(engine, item: Mapping, target: Mapping, quality: Mapping,
                    min_samples: int = 3, max_loss_rate: Decimal = Decimal("0.62"),
                    min_avg_reward: Decimal = Decimal("-1.0")) -> Dict:
    """Return whether the candidate resembles repeatedly losing setups."""
    chart = item.get("chart_gate") or {}
    strategy = str(chart.get("strategy") or "UNKNOWN_STRATEGY").upper()[:80]
    regime = str(getattr(item.get("policy_candidate"), "regime", None) or "intraday_shadow")
    pseudo = {
        "signal_at": item["session"]["timestamp"],
        "underlying_symbol": item.get("symbol"),
        "symbol": item.get("symbol"),
        "instrument_type": target.get("kind"),
        "side": target.get("side"),
    }
    sig = signature(pseudo, strategy, regime)
    underlying = str(item.get("symbol") or "").upper().replace(" ", "")
    with engine.begin() as connection:
        ensure_adaptive_schema(connection)
        exact = connection.execute(text("""
            SELECT COUNT(*) samples,
                   COALESCE(AVG(reward_points),0) avg_reward,
                   COALESCE(AVG(CASE WHEN net_pnl<0 THEN 1.0 ELSE 0.0 END),0) loss_rate,
                   COALESCE(AVG(CASE WHEN exit_reason='STOP_LOSS' THEN 1.0 ELSE 0.0 END),0) stop_rate
            FROM adaptive_trade_rewards
            WHERE signature_hash=:signature
        """), {"signature": sig}).mappings().one()
        family = connection.execute(text("""
            SELECT COUNT(*) samples,
                   COALESCE(AVG(reward_points),0) avg_reward,
                   COALESCE(AVG(CASE WHEN net_pnl<0 THEN 1.0 ELSE 0.0 END),0) loss_rate,
                   COALESCE(AVG(CASE WHEN exit_reason='STOP_LOSS' THEN 1.0 ELSE 0.0 END),0) stop_rate
            FROM adaptive_trade_rewards
            WHERE underlying_symbol=:underlying
              AND instrument_type=:type
              AND side=:side
              AND strategy=:strategy
        """), {"underlying": underlying, "type": target.get("kind"), "side": target.get("side"), "strategy": strategy}).mappings().one()
    source = exact if int(exact["samples"] or 0) >= min_samples else family
    samples = int(source["samples"] or 0)
    avg_reward = Decimal(str(source["avg_reward"] or 0))
    loss_rate = Decimal(str(source["loss_rate"] or 0))
    stop_rate = Decimal(str(source["stop_rate"] or 0))
    risk_penalty = float(max(Decimal("0"), -avg_reward) * Decimal("3") + loss_rate * Decimal("12") + stop_rate * Decimal("8"))
    if samples >= min_samples and (loss_rate >= max_loss_rate or avg_reward <= min_avg_reward):
        return {"accepted": False, "reason": f"adaptive trap memory rejected: {samples} similar trades, loss_rate {loss_rate:.2%}, avg_reward {avg_reward}",
                "samples": samples, "avg_reward": str(avg_reward), "loss_rate": str(round(loss_rate, 4)),
                "stop_rate": str(round(stop_rate, 4)), "risk_penalty": round(risk_penalty, 4)}
    bonus = max(-8.0, min(8.0, float(avg_reward))) if samples else 0.0
    return {"accepted": True, "samples": samples, "avg_reward": str(avg_reward),
            "loss_rate": str(round(loss_rate, 4)), "stop_rate": str(round(stop_rate, 4)),
            "risk_penalty": round(risk_penalty, 4), "reward_bonus": round(bonus, 4)}
