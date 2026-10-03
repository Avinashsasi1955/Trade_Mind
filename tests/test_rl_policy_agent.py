"""Unit tests for Phase C.4: 20-Validation RL Action Controller."""

import unittest
from datetime import datetime, timezone
from sqlalchemy import create_engine

from backend.intelligence_memory import ensure_intelligence_schema, record_session_trap, record_episodic_observation
from backend.ml.rl_policy_agent import (
    RLPolicyAgent,
    MODE_COLD_START,
    MODE_RL_ACTIVE,
    MAX_DAILY_LOSS_LIMIT,
    MAX_CONSECUTIVE_LOSS_LIMIT,
)
from backend.regime_router import (
    ensure_checkpoint_schema,
    record_validation_checkpoint,
    REGIME_TREND,
    REGIME_RANGE,
    REGIME_VOL_DEFENSE,
)


class TestRLPolicyAgent(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.conn = self.engine.connect()
        ensure_checkpoint_schema(self.conn)
        ensure_intelligence_schema(self.conn)
        self.agent = RLPolicyAgent()

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_cold_start_mode_below_20_validations(self):
        # Seed 15 checkpoints
        for i in range(1, 16):
            record_validation_checkpoint(
                self.conn,
                checkpoint_number=i,
                checkpoint_type="WALK_FORWARD_FOLD",
                model_version="direction-v3.0",
                net_pnl=1200.0,
            )

        mode, count = self.agent.get_operational_mode(self.conn)
        self.assertEqual(mode, MODE_COLD_START)
        self.assertEqual(count, 15)

        # Baseline evaluation
        decision = self.agent.evaluate_action(
            self.conn,
            symbol="NIFTY",
            regime=REGIME_TREND,
            candidate_direction=1
        )
        self.assertEqual(decision.mode, MODE_COLD_START)
        self.assertFalse(decision.is_unlocked)
        self.assertEqual(decision.recommended_strategy, "BULL_CALL_SPREAD")
        self.assertEqual(decision.hurdle_delta, 0.0)
        self.assertEqual(decision.effective_hurdle, 0.65)
        self.assertEqual(decision.size_multiplier, 1.0)

    def test_rl_active_mode_at_or_above_20_validations(self):
        # Seed 21 checkpoints (matching our current live system count)
        for i in range(1, 22):
            record_validation_checkpoint(
                self.conn,
                checkpoint_number=i,
                checkpoint_type="LIVE_SHADOW_DISCIPLINED_PRESERVATION" if i == 21 else "HISTORICAL_SHADOW_REPLAY",
                model_version="direction-v3.0",
                net_pnl=1000.0 * i,
            )

        mode, count = self.agent.get_operational_mode(self.conn)
        self.assertEqual(mode, MODE_RL_ACTIVE)
        self.assertEqual(count, 21)

        # RL Active evaluation
        decision = self.agent.evaluate_action(
            self.conn,
            symbol="NIFTY",
            regime=REGIME_TREND,
            candidate_direction=1,
            features={"iv_rank": 0.25, "poc_dist_bps": 15.0}
        )
        self.assertEqual(decision.mode, MODE_RL_ACTIVE)
        self.assertTrue(decision.is_unlocked)
        self.assertEqual(decision.recommended_strategy, "BULL_CALL_SPREAD")
        # In RL active mode, hurdle is dynamically adapted
        self.assertNotEqual(decision.hurdle_delta, 0.0)
        self.assertGreaterEqual(decision.size_multiplier, 1.0)
        self.assertGreaterEqual(decision.take_profit_multiplier, 2.0)

    def test_steel_sandbox_daily_loss_invariant(self):
        # Seed 21 checkpoints to ensure RL is active
        for i in range(1, 22):
            record_validation_checkpoint(self.conn, checkpoint_number=i, checkpoint_type="FOLD", model_version="v3.0")

        # Simulate breach of ₹2,000 daily loss
        decision = self.agent.evaluate_action(
            self.conn,
            symbol="NIFTY",
            regime=REGIME_TREND,
            portfolio_state={"day_pnl": -2050.0, "consecutive_losses": 1}
        )
        self.assertFalse(decision.is_allowed)
        self.assertEqual(decision.safety_status, "DAILY_LOSS_LIMIT_REACHED")
        self.assertEqual(decision.size_multiplier, 0.0)
        self.assertEqual(decision.recommended_strategy, "NO_TRADE_DEFENSE")
        self.assertIn("Daily loss", decision.rejection_reason)

    def test_steel_sandbox_consecutive_losses_circuit_breaker(self):
        for i in range(1, 22):
            record_validation_checkpoint(self.conn, checkpoint_number=i, checkpoint_type="FOLD", model_version="v3.0")

        # Simulate 2 consecutive losses
        decision = self.agent.evaluate_action(
            self.conn,
            symbol="BANKNIFTY",
            regime=REGIME_RANGE,
            portfolio_state={"day_pnl": -800.0, "consecutive_losses": 2}
        )
        self.assertFalse(decision.is_allowed)
        self.assertEqual(decision.safety_status, "CONSECUTIVE_LOSS_CIRCUIT_BREAKER")
        self.assertEqual(decision.size_multiplier, 0.0)
        self.assertEqual(decision.recommended_strategy, "NO_TRADE_DEFENSE")
        self.assertIn("consecutive losses", decision.rejection_reason)

    def test_tier1_trap_working_memory_gate(self):
        for i in range(1, 22):
            record_validation_checkpoint(self.conn, checkpoint_number=i, checkpoint_type="FOLD", model_version="v3.0")

        # Record an active bull trap on NIFTY
        record_session_trap(
            self.conn,
            symbol="NIFTY",
            trap_type="WILDER_ASI_SWEEP",
            side="BULL_TRAP",
            price=24800.0,
            cooloff_minutes=30,
        )

        # Attempt to take a bullish trade on NIFTY
        decision = self.agent.evaluate_action(
            self.conn,
            symbol="NIFTY",
            regime=REGIME_TREND,
            candidate_direction=1,  # Bullish
        )
        self.assertFalse(decision.is_allowed)
        self.assertIn("Tier-1 Trap Memory", decision.rejection_reason)
        self.assertIsNotNone(decision.active_trap)

    def test_tier2_episodic_prior_modulation(self):
        for i in range(1, 22):
            record_validation_checkpoint(self.conn, checkpoint_number=i, checkpoint_type="FOLD", model_version="v3.0")

        # Record 5 high win-rate episodic observations for BANKNIFTY in RANGE
        for _ in range(5):
            record_episodic_observation(
                self.conn,
                symbol="BANKNIFTY",
                regime=REGIME_RANGE,
                pattern_name="IRON_CONDOR_POC",
                outcome_pnl=1200.0,
                was_success=True,
            )

        decision = self.agent.evaluate_action(
            self.conn,
            symbol="BANKNIFTY",
            regime=REGIME_RANGE,
            candidate_direction=0,
        )
        self.assertTrue(decision.is_allowed)
        self.assertEqual(decision.recommended_strategy, "IRON_CONDOR")
        self.assertEqual(decision.episodic_prior["prior_label"], "HIGH_CONVICTION_EDGE")
        self.assertGreaterEqual(decision.size_multiplier, 1.1)

    def test_record_feedback_reward_updates_weights(self):
        res = self.agent.record_feedback_reward(
            self.conn,
            reward_value=1.5,
            state_features={"regime": REGIME_TREND},
            action_name="BULL_CALL_SPREAD"
        )
        self.assertEqual(res["status"], "UPDATED")
        self.assertGreater(self.agent.weights["regime_trend"], 0.02)


if __name__ == "__main__":
    unittest.main()
