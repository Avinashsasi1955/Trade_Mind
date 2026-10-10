"""Unit tests for Phase C.3 Dual-Tier Intelligence Memo Package."""

import unittest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine

from backend.intelligence_memory import (
    ensure_intelligence_schema,
    record_session_trap,
    is_trap_cooloff_active,
    get_active_session_traps,
    resolve_session_trap,
    record_episodic_observation,
    get_episodic_prior,
    get_top_episodic_patterns,
    publish_brain_event,
)


class TestIntelligenceMemory(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.conn = self.engine.connect()
        ensure_intelligence_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.engine.dispose()

    def test_schema_and_brain_event(self):
        event = publish_brain_event(
            self.conn,
            event_type="TEST_EVENT",
            source="test_runner",
            payload={"msg": "hello"},
            symbol="NIFTY",
            severity="INFO"
        )
        self.assertIn("id", event)
        self.assertIsNotNone(event["occurred_at"])

    def test_tier1_session_trap_recording_and_cooloff(self):
        # 1. Initially no trap cool-off
        active, trap = is_trap_cooloff_active(self.conn, "NIFTY", "BULL_TRAP")
        self.assertFalse(active)
        self.assertIsNone(trap)

        # 2. Record a trap (Wilder ASI sweep at 24850)
        now_dt = datetime(2026, 6, 1, 9, 30, 0, tzinfo=timezone.utc)
        record = record_session_trap(
            self.conn,
            symbol="NIFTY",
            trap_type="WILDER_ASI_SWEEP",
            side="BULL_TRAP",
            price=24850.0,
            cooloff_minutes=30,
            notes="Swept 24850 liquidity with sharp negative delta rejection",
            detected_at=now_dt
        )
        self.assertEqual(record["status"], "RECORDED")
        self.assertEqual(record["symbol"], "NIFTY")

        # 3. Cool-off must now be active for BULL_TRAP
        active, trap_data = is_trap_cooloff_active(self.conn, "NIFTY", "BULL_TRAP", at_time=now_dt + timedelta(minutes=5))
        self.assertTrue(active)
        self.assertIsNotNone(trap_data)
        self.assertEqual(trap_data["symbol"], "NIFTY")
        self.assertEqual(trap_data["trap_type"], "WILDER_ASI_SWEEP")

        # 4. Cool-off must NOT be active for BEAR_TRAP or other symbol
        active_bear, _ = is_trap_cooloff_active(self.conn, "NIFTY", "BEAR_TRAP", at_time=now_dt + timedelta(minutes=5))
        self.assertFalse(active_bear)
        active_bnf, _ = is_trap_cooloff_active(self.conn, "BANKNIFTY", "BULL_TRAP", at_time=now_dt + timedelta(minutes=5))
        self.assertFalse(active_bnf)

        # 5. After 31 minutes, cool-off expires
        active_later, _ = is_trap_cooloff_active(self.conn, "NIFTY", "BULL_TRAP", at_time=now_dt + timedelta(minutes=31))
        self.assertFalse(active_later)

        # 6. Listing active traps
        active_list = get_active_session_traps(self.conn, symbol="NIFTY")
        self.assertEqual(len(active_list), 1)

        # 7. Resolving trap
        resolved = resolve_session_trap(self.conn, trap_data["id"])
        self.assertTrue(resolved)
        active_after_resolve, _ = is_trap_cooloff_active(self.conn, "NIFTY", "BULL_TRAP", at_time=now_dt + timedelta(minutes=5))
        self.assertFalse(active_after_resolve)

    def test_tier2_episodic_cross_session_memory(self):
        # 1. Baseline unobserved prior
        prior = get_episodic_prior(self.conn, "BANKNIFTY", "RANGE_MEAN_REVERSION")
        self.assertEqual(prior["total_observations"], 0)
        self.assertEqual(prior["prior_factor"], 1.0)
        self.assertEqual(prior["prior_label"], "NEUTRAL_UNOBSERVED")

        # 2. Add 5 successful observations of Iron Condors in range regime
        for _ in range(5):
            record_episodic_observation(
                self.conn,
                symbol="BANKNIFTY",
                regime="RANGE_MEAN_REVERSION",
                pattern_name="IRON_CONDOR_POC_BOUNCE",
                outcome_pnl=1450.0,
                was_success=True,
                metadata={"vix": 13.8}
            )

        prior_after = get_episodic_prior(self.conn, "BANKNIFTY", "RANGE_MEAN_REVERSION")
        self.assertEqual(prior_after["total_observations"], 5)
        self.assertEqual(prior_after["win_rate"], 1.0)
        self.assertEqual(prior_after["prior_factor"], 1.15)
        self.assertEqual(prior_after["prior_label"], "HIGH_CONVICTION_EDGE")

        # 3. Add 8 failing observations to test drag penalty (5 wins / 13 total = 38.5% <= 40%)
        for _ in range(8):
            record_episodic_observation(
                self.conn,
                symbol="BANKNIFTY",
                regime="RANGE_MEAN_REVERSION",
                pattern_name="FAILED_BREAKOUT_FADE",
                outcome_pnl=-1800.0,
                was_success=False,
            )

        prior_penalized = get_episodic_prior(self.conn, "BANKNIFTY", "RANGE_MEAN_REVERSION")
        self.assertEqual(prior_penalized["total_observations"], 13)
        self.assertLess(prior_penalized["win_rate"], 0.40)
        self.assertEqual(prior_penalized["prior_factor"], 0.85)
        self.assertEqual(prior_penalized["prior_label"], "HISTORICAL_DRAG_PENALTY")

        # 4. Top patterns list
        top_patterns = get_top_episodic_patterns(self.conn, "BANKNIFTY")
        self.assertEqual(len(top_patterns), 2)


if __name__ == "__main__":
    unittest.main()
