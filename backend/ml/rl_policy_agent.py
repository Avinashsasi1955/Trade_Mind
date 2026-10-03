"""Autonomous 20-Validation RL Action Controller.

Phase C.4 Core Architecture:
Controls the operational transition from baseline observation to autonomous RL actions.

Lifecycle:
- Validations 1-19: COLD_START_BASELINE mode (Deterministic baseline only, zero RL overrides).
- Validations >= 20: RL_ACTIVE mode (Autonomous Contextual Bandit & Q-Adaptive Execution unlocked).

Features:
- Dynamic Strategy Selection: Bull Call/Put Spread vs Iron Condor vs Defensive Cash.
- Adaptive Confidence Hurdle: Delta tau in [-0.05, +0.05].
- Adaptive Position Sizing: Alpha size in [0.5, 1.2].
- Adaptive Take-Profit Multiplier: [1.2x, 2.5x].
- Dual-Tier Memo Integration: Trap cool-off gate & episodic priors.
- Immutable Steel Sandbox Invariants: ₹2,000 daily loss kill switch, 2 consecutive loss breaker.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from backend.intelligence_memory import get_episodic_prior, is_trap_cooloff_active
from backend.regime_router import (
    REGIME_RANGE,
    REGIME_TREND,
    REGIME_VOL_DEFENSE,
    get_completed_validation_count,
)

# Operational modes
MODE_COLD_START = "COLD_START_BASELINE"
MODE_RL_ACTIVE = "RL_ACTIVE"

ACTIVATION_VALIDATION_THRESHOLD = 20

# Hard invariant safety constraints
MAX_DAILY_LOSS_LIMIT = 2000.0
MAX_CONSECUTIVE_LOSS_LIMIT = 2


@dataclass
class RLActionDecision:
    mode: str
    checkpoint_count: int
    is_unlocked: bool
    recommended_strategy: str
    strategy_weights: Dict[str, float]
    base_hurdle: float
    hurdle_delta: float
    effective_hurdle: float
    size_multiplier: float
    take_profit_multiplier: float
    trailing_giveback_pct: float
    is_allowed: bool
    rejection_reason: str
    safety_status: str
    episodic_prior: Dict[str, Any]
    active_trap: Optional[Dict[str, Any]]
    state_vector: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RLPolicyAgent:
    """Reinforcement Learning Policy Controller with Steel Sandbox Safety Fuses."""

    def __init__(self, model_version: str = "direction-v3.0"):
        self.model_version = model_version
        # Contextual Bandit feature weights (learned / adaptive parameters)
        self.weights = {
            "bias": 0.0,
            "regime_trend": 0.02,
            "regime_range": -0.01,
            "regime_vol_defense": 0.04,
            "poc_distance": 0.015,
            "asi_alignment": -0.02,
            "iv_rank": 0.03,
            "consec_losses": 0.04,
            "size_trend": 0.15,
            "size_range": 0.0,
            "size_loss_penalty": -0.30,
        }

    def ensure_rl_schema(self, connection) -> None:
        dialect = getattr(connection.dialect, "name", "sqlite")
        if dialect == "postgresql":
            sql = """
            CREATE TABLE IF NOT EXISTS rl_policy_weights (
                id BIGSERIAL PRIMARY KEY,
                model_version TEXT NOT NULL UNIQUE,
                weights_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                total_updates INT NOT NULL DEFAULT 0,
                cumulative_reward NUMERIC(14, 4) NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        else:
            sql = """
            CREATE TABLE IF NOT EXISTS rl_policy_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_version TEXT NOT NULL UNIQUE,
                weights_json TEXT NOT NULL DEFAULT '{}',
                total_updates INTEGER NOT NULL DEFAULT 0,
                cumulative_reward REAL NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        for stmt in sql.strip().split(";"):
            if stmt.strip():
                connection.execute(text(stmt.strip()))

    def get_operational_mode(self, connection) -> Tuple[str, int]:
        """Returns current operational mode and completed validation count."""
        count = get_completed_validation_count(connection)
        if count >= ACTIVATION_VALIDATION_THRESHOLD:
            return MODE_RL_ACTIVE, count
        return MODE_COLD_START, count

    def evaluate_action(
        self,
        connection,
        symbol: str,
        regime: str,
        features: Optional[Dict[str, Any]] = None,
        portfolio_state: Optional[Dict[str, Any]] = None,
        candidate_direction: int = 1,
    ) -> RLActionDecision:
        """Evaluates and produces an autonomous RL action decision bounded by safety invariants."""
        self.ensure_rl_schema(connection)
        mode, count = self.get_operational_mode(connection)
        is_unlocked = (mode == MODE_RL_ACTIVE)

        feats = features or {}
        port = portfolio_state or {}

        # 1. State vector extraction
        day_pnl = float(port.get("day_pnl", 0.0))
        consec_losses = int(port.get("consecutive_losses", 0))
        net_delta = float(port.get("net_delta", 0.0))
        net_theta = float(port.get("net_theta", 0.0))
        iv_rank = float(feats.get("iv_rank", 0.35))
        poc_dist_bps = float(feats.get("poc_dist_bps", 0.0))
        asi_dir = float(feats.get("asi_dir", 0.0))

        state_vector = {
            "symbol": symbol.upper(),
            "regime": regime,
            "day_pnl": day_pnl,
            "consec_losses": consec_losses,
            "net_delta": net_delta,
            "net_theta": net_theta,
            "iv_rank": iv_rank,
            "poc_dist_bps": poc_dist_bps,
            "asi_dir": asi_dir,
            "candidate_direction": candidate_direction,
        }

        # 2. Hard Invariant Steel Sandbox Fuse Check
        safety_status = "NORMAL"
        is_allowed = True
        rejection_reason = ""

        if day_pnl <= -MAX_DAILY_LOSS_LIMIT:
            safety_status = "DAILY_LOSS_LIMIT_REACHED"
            is_allowed = False
            rejection_reason = f"Capital preservation lock: Daily loss ₹{abs(day_pnl):.2f} reached ₹{MAX_DAILY_LOSS_LIMIT} ceiling."

        if consec_losses >= MAX_CONSECUTIVE_LOSS_LIMIT:
            safety_status = "CONSECUTIVE_LOSS_CIRCUIT_BREAKER"
            is_allowed = False
            rejection_reason = f"Circuit breaker active: {consec_losses} consecutive losses triggered 30m cooling period."

        # 3. Tier 1 Session Trap Working Memory Check
        side_tag = "BULL_TRAP" if candidate_direction > 0 else "BEAR_TRAP"
        trap_active, active_trap = is_trap_cooloff_active(connection, symbol, side_tag)
        if trap_active and active_trap:
            is_allowed = False
            rejection_reason = f"Tier-1 Trap Memory active: {active_trap.get('trap_type')} sweep cool-off until {active_trap.get('cooloff_until')}."

        # 4. Tier 2 Episodic Cross-Session Prior
        episodic_prior = get_episodic_prior(connection, symbol, regime)
        prior_factor = float(episodic_prior.get("prior_factor", 1.0))

        # 5. Baseline deterministic parameters
        base_hurdle = 0.65
        hurdle_delta = 0.0
        size_multiplier = 1.0
        tp_mult = 1.5
        trailing_giveback = 0.35

        strategy_weights = {
            "BULL_CALL_SPREAD": 0.25,
            "BEAR_PUT_SPREAD": 0.25,
            "IRON_CONDOR": 0.25,
            "NO_TRADE_DEFENSE": 0.25,
        }

        if not is_unlocked:
            # Baseline deterministic mode (Validations 1-19)
            if regime == REGIME_TREND:
                rec_strategy = "BULL_CALL_SPREAD" if candidate_direction > 0 else "BEAR_PUT_SPREAD"
                strategy_weights = {"BULL_CALL_SPREAD": 0.70, "BEAR_PUT_SPREAD": 0.20, "IRON_CONDOR": 0.05, "NO_TRADE_DEFENSE": 0.05}
                size_multiplier = 1.0
                tp_mult = 1.8
                trailing_giveback = 0.35
            elif regime == REGIME_RANGE:
                rec_strategy = "IRON_CONDOR"
                strategy_weights = {"BULL_CALL_SPREAD": 0.10, "BEAR_PUT_SPREAD": 0.10, "IRON_CONDOR": 0.75, "NO_TRADE_DEFENSE": 0.05}
                size_multiplier = 1.0
                tp_mult = 1.3
                trailing_giveback = 0.25
            else:
                rec_strategy = "NO_TRADE_DEFENSE"
                strategy_weights = {"BULL_CALL_SPREAD": 0.05, "BEAR_PUT_SPREAD": 0.05, "IRON_CONDOR": 0.10, "NO_TRADE_DEFENSE": 0.80}
                size_multiplier = 0.5
                tp_mult = 1.2
                trailing_giveback = 0.20

            effective_hurdle = base_hurdle

        else:
            # RL Active Autonomous Mode (Validations >= 20)
            # A. Adaptive Confidence Hurdle: Delta tau in [-0.05, +0.05]
            raw_delta = (
                self.weights.get("bias", 0.0)
                + (self.weights["regime_trend"] if regime == REGIME_TREND else 0.0)
                + (self.weights["regime_range"] if regime == REGIME_RANGE else 0.0)
                + (self.weights["regime_vol_defense"] if regime == REGIME_VOL_DEFENSE else 0.0)
                + (consec_losses * self.weights["consec_losses"])
                + (iv_rank * self.weights["iv_rank"])
            )
            # Apply episodic prior adjustment
            if prior_factor < 1.0:
                raw_delta += 0.02  # tighten hurdle if historical win rate is poor
            elif prior_factor > 1.0:
                raw_delta -= 0.02  # loosen hurdle if historical win rate has strong edge

            hurdle_delta = max(-0.05, min(0.05, round(raw_delta, 4)))
            effective_hurdle = round(base_hurdle + hurdle_delta, 4)

            # B. Adaptive Position Sizing: Alpha size in [0.5, 1.2]
            raw_size = 1.0
            if regime == REGIME_TREND:
                raw_size += self.weights["size_trend"]
            elif regime == REGIME_RANGE:
                raw_size += self.weights["size_range"]
            elif regime == REGIME_VOL_DEFENSE:
                raw_size = 0.5

            if consec_losses > 0:
                raw_size += consec_losses * self.weights["size_loss_penalty"]

            # Incorporate prior factor into sizing
            raw_size *= prior_factor
            size_multiplier = max(0.5, min(1.2, round(raw_size, 2)))

            # C. Dynamic Strategy Reinforcement & Take Profit Optimization
            if regime == REGIME_TREND:
                rec_strategy = "BULL_CALL_SPREAD" if candidate_direction > 0 else "BEAR_PUT_SPREAD"
                strategy_weights = {
                    "BULL_CALL_SPREAD": 0.80 if candidate_direction > 0 else 0.10,
                    "BEAR_PUT_SPREAD": 0.80 if candidate_direction < 0 else 0.10,
                    "IRON_CONDOR": 0.05,
                    "NO_TRADE_DEFENSE": 0.05,
                }
                tp_mult = 2.2  # Let trend winners run
                trailing_giveback = 0.30
            elif regime == REGIME_RANGE:
                rec_strategy = "IRON_CONDOR"
                strategy_weights = {
                    "BULL_CALL_SPREAD": 0.08,
                    "BEAR_PUT_SPREAD": 0.08,
                    "IRON_CONDOR": 0.80,
                    "NO_TRADE_DEFENSE": 0.04,
                }
                tp_mult = 1.35  # Tight range harvesting
                trailing_giveback = 0.22
            else:
                rec_strategy = "NO_TRADE_DEFENSE"
                strategy_weights = {
                    "BULL_CALL_SPREAD": 0.05,
                    "BEAR_PUT_SPREAD": 0.05,
                    "IRON_CONDOR": 0.10,
                    "NO_TRADE_DEFENSE": 0.80,
                }
                size_multiplier = 0.5
                tp_mult = 1.2
                trailing_giveback = 0.20

        # Enforce zero sizing if safety rule vetoes
        if not is_allowed:
            size_multiplier = 0.0
            rec_strategy = "NO_TRADE_DEFENSE"

        return RLActionDecision(
            mode=mode,
            checkpoint_count=count,
            is_unlocked=is_unlocked,
            recommended_strategy=rec_strategy,
            strategy_weights=strategy_weights,
            base_hurdle=base_hurdle,
            hurdle_delta=hurdle_delta,
            effective_hurdle=effective_hurdle,
            size_multiplier=size_multiplier,
            take_profit_multiplier=tp_mult,
            trailing_giveback_pct=trailing_giveback,
            is_allowed=is_allowed,
            rejection_reason=rejection_reason,
            safety_status=safety_status,
            episodic_prior=episodic_prior,
            active_trap=active_trap,
            state_vector=state_vector,
        )

    def record_feedback_reward(
        self,
        connection,
        reward_value: float,
        state_features: Dict[str, Any],
        action_name: str,
    ) -> Dict[str, Any]:
        """Update contextual policy parameters based on observed empirical trade reward."""
        self.ensure_rl_schema(connection)
        dialect = getattr(connection.dialect, "name", "sqlite")

        # Bound update step to prevent catastrophic policy drift (KL divergence barrier)
        lr = 0.01
        clipped_reward = max(-2.0, min(2.0, reward_value))

        if state_features.get("regime") == REGIME_TREND and action_name in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"):
            self.weights["regime_trend"] += lr * clipped_reward
        elif state_features.get("regime") == REGIME_RANGE and action_name == "IRON_CONDOR":
            self.weights["regime_range"] += lr * clipped_reward

        # Clamp weights
        for k in self.weights:
            self.weights[k] = round(max(-0.5, min(0.5, self.weights[k])), 4)

        meta_val = json.dumps(self.weights)
        if dialect == "postgresql":
            connection.execute(text("""
                INSERT INTO rl_policy_weights (model_version, weights_json, total_updates, cumulative_reward, updated_at)
                VALUES (:mver, CAST(:weights AS jsonb), 1, :rew, CURRENT_TIMESTAMP)
                ON CONFLICT (model_version) DO UPDATE SET
                    weights_json = CAST(:weights AS jsonb),
                    total_updates = rl_policy_weights.total_updates + 1,
                    cumulative_reward = rl_policy_weights.cumulative_reward + EXCLUDED.cumulative_reward,
                    updated_at = CURRENT_TIMESTAMP
            """), {"mver": self.model_version, "weights": meta_val, "rew": clipped_reward})
        else:
            row = connection.execute(text(
                "SELECT id, total_updates, cumulative_reward FROM rl_policy_weights WHERE model_version = :mver"
            ), {"mver": self.model_version}).mappings().first()
            if row:
                connection.execute(text("""
                    UPDATE rl_policy_weights
                    SET weights_json = :weights, total_updates = :updates,
                        cumulative_reward = :cum_rew, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """), {
                    "weights": meta_val,
                    "updates": row["total_updates"] + 1,
                    "cum_rew": float(row["cumulative_reward"]) + clipped_reward,
                    "id": row["id"],
                })
            else:
                connection.execute(text("""
                    INSERT INTO rl_policy_weights (model_version, weights_json, total_updates, cumulative_reward, updated_at)
                    VALUES (:mver, :weights, 1, :rew, CURRENT_TIMESTAMP)
                """), {"mver": self.model_version, "weights": meta_val, "rew": clipped_reward})

        return {
            "status": "UPDATED",
            "model_version": self.model_version,
            "clipped_reward": clipped_reward,
            "weights": self.weights,
        }
