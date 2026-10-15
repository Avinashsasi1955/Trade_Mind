"""Tests for Shadow Day validation, lifecycle, and 90-session counting (Phase 2).

Validates the Shadow Day fix in backend/shadow_sessions.py:
1. Pre-market close finalization restrictions and force override.
2. Recognition of completed sessions and incrementing effective_completed toward 90.
3. Fail-closed behavior for incomplete, stale, or invalid sessions.
4. Auto-finalization and recovery of eligible post-market sessions.
5. Mid-market status preservation (STARTED) avoiding premature rejection.
6. 4 closed take-profit trades evaluation against documented validation rules.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backend.shadow_sessions import (
    IST,
    count_promotion_eligible_sessions,
    evaluate_session,
    finalize_session,
    is_promotion_eligible_session,
    materialize_session,
    open_session,
    status,
)


class TestShadowSessionValidation(unittest.TestCase):
    def setUp(self):
        self.mock_engine = MagicMock()
        self.today = datetime.now(IST).date()

    def test_finalize_session_before_market_close_requires_force(self):
        """Verify that finalize_session cannot be called before 15:35 IST without force=True."""
        # Simulate 14:00 IST (during active market hours)
        active_market_time = datetime(self.today.year, self.today.month, self.today.day, 14, 0, 0, tzinfo=IST)
        with patch("backend.shadow_sessions.datetime") as mock_dt:
            mock_dt.now.return_value = active_market_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            with self.assertRaisesRegex(ValueError, "Cannot finalize live session before market close"):
                finalize_session(self.mock_engine, self.today, force=False)

    def test_finalize_session_post_market_or_with_force_succeeds(self):
        """Verify finalize_session succeeds post-market (15:40 IST) or when force=True."""
        post_market_time = datetime(self.today.year, self.today.month, self.today.day, 16, 0, 0, tzinfo=IST)
        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": True,
            "rejection_reasons": [],
            "warnings": [],
            "metrics": {
                "one_minute_bars": 375,
                "five_minute_bars": 75,
                "instruments_seen": 60,
                "predictions": 120,
                "critical_errors": 0,
            },
            "paper_pnl": {
                "realised_pnl": 500.0,
                "unrealised_pnl": 0.0,
                "net_marked_pnl": 500.0,
                "closed_trades": 4,
                "open_trades": 0,
                "rejected_trades": 0,
                "win_rate_pct": 100.0,
                "profit_factor": 999.0,
            },
        }

        mock_conn = MagicMock()
        valid_row = {
            "session_date": self.today.isoformat(),
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {"repair_ratio_1m": 0.0, "repair_ratio_5m": 0.0},
        }
        mock_conn.execute.return_value.mappings.return_value.all.return_value = [valid_row]
        self.mock_engine.begin.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.datetime") as mock_dt, \
             patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval):
            mock_dt.now.return_value = post_market_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            res = finalize_session(self.mock_engine, self.today, force=False)
            self.assertEqual(res["status"], "COMPLETE")
            self.assertEqual(res["completed_sessions"], 1)
            mock_conn.execute.assert_called()

    def test_status_recognizes_eligible_today_and_counts_toward_90(self):
        """Verify status() counts an eligible day in effective_completed toward the 90 target."""
        post_market_time = datetime(self.today.year, self.today.month, self.today.day, 16, 0, 0, tzinfo=IST)

        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": True,
            "rejection_reasons": [],
            "warnings": [],
            "metrics": {"one_minute_bars": 375, "five_minute_bars": 75, "predictions": 50},
            "paper_pnl": {
                "closed_trades": 4,
                "winning_trades": 4,
                "realised_pnl": 1200.0,
                "net_marked_pnl": 1200.0,
            },
        }

        # Mock database queries: 10 historical complete sessions
        mock_conn = MagicMock()
        # counts query returns 10 COMPLETE sessions
        mock_conn.execute.return_value.fetchall.return_value = [("COMPLETE", 10)]
        past_rows = [
            {"session_date": (self.today - timedelta(days=i)).isoformat(), "status": "COMPLETE", "rejection_reasons": [], "metrics": {"repair_ratio_1m": 0.0, "repair_ratio_5m": 0.0}}
            for i in range(1, 11)
        ]
        mock_conn.execute.return_value.mappings.return_value.all.return_value = past_rows
        # t_row for today returns STARTED
        mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = {"status": "STARTED"}
        self.mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.datetime") as mock_dt, \
             patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval), \
             patch("backend.shadow_sessions.finalize_session") as mock_finalize, \
             patch("backend.shadow_sessions._persist_daily_log"):
            mock_dt.now.return_value = post_market_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            stat = status(self.mock_engine)

            # Auto-finalization triggered
            mock_finalize.assert_called_with(self.mock_engine, self.today)
            # Effective completed sessions increments from 10 to 11
            self.assertEqual(stat["completed_sessions"], 10)
            self.assertEqual(stat["effective_completed_sessions"], 11)
            self.assertEqual(stat["target"], 90)
            self.assertEqual(stat["remaining_sessions"], 79)
            self.assertTrue(stat["today"]["eligible"])

    def test_status_does_not_falsely_count_ineligible_session(self):
        """Verify status() fails closed and does not increment effective_completed when session has rejection reasons."""
        post_market_time = datetime(self.today.year, self.today.month, self.today.day, 16, 0, 0, tzinfo=IST)

        # Ineligible evaluation (e.g. insufficient live-feed bucket coverage)
        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": False,
            "rejection_reasons": ["insufficient one-minute bucket coverage"],
            "warnings": [],
            "metrics": {"one_minute_bars": 120, "five_minute_bars": 24, "predictions": 5},
            "paper_pnl": {"closed_trades": 0, "open_trades": 0, "net_marked_pnl": 0.0},
        }

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("COMPLETE", 10)]
        past_rows = [
            {"session_date": (self.today - timedelta(days=i)).isoformat(), "status": "COMPLETE", "rejection_reasons": [], "metrics": {"repair_ratio_1m": 0.0, "repair_ratio_5m": 0.0}}
            for i in range(1, 11)
        ]
        mock_conn.execute.return_value.mappings.return_value.all.return_value = past_rows
        mock_conn.execute.return_value.mappings.return_value.one_or_none.return_value = {"status": "REJECTED"}
        self.mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.datetime") as mock_dt, \
             patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval), \
             patch("backend.shadow_sessions.finalize_session") as mock_finalize, \
             patch("backend.shadow_sessions._persist_daily_log"):
            mock_dt.now.return_value = post_market_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            stat = status(self.mock_engine)

            # Did not finalize to complete
            mock_finalize.assert_not_called()
            # Effective completed sessions does NOT increment
            self.assertEqual(stat["completed_sessions"], 10)
            self.assertEqual(stat["effective_completed_sessions"], 10)
            self.assertFalse(stat["today"]["eligible"])

    def test_status_during_market_hours_preserves_started_state(self):
        """Verify that during market hours, today's status is preserved as STARTED and not marked REJECTED."""
        mid_market_time = datetime(self.today.year, self.today.month, self.today.day, 12, 30, 0, tzinfo=IST)

        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": False,  # Coverage naturally incomplete mid-day
            "rejection_reasons": ["insufficient one-minute bucket coverage"],
            "warnings": [],
            "metrics": {"one_minute_bars": 180, "five_minute_bars": 36, "predictions": 25},
            "paper_pnl": {
                "closed_trades": 2,
                "realised_pnl": 450.0,
                "net_marked_pnl": 450.0,
            },
        }

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("COMPLETE", 10)]
        # Suppose a previously aborted run wrote REJECTED into forward_shadow_sessions
        today_row = {"session_date": self.today.isoformat(), "status": "REJECTED"}
        mock_conn.execute.return_value.mappings.return_value.all.return_value = [today_row]
        self.mock_engine.connect.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.datetime") as mock_dt, \
             patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval), \
             patch("backend.shadow_sessions._persist_daily_log"):
            mock_dt.now.return_value = mid_market_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            stat = status(self.mock_engine)

            # In recent sessions, today's status is enriched to STARTED during market hours
            recent_today = next(r for r in stat["recent"] if str(r["session_date"]) == self.today.isoformat())
            self.assertEqual(recent_today["status"], "STARTED")
            self.assertEqual(recent_today["paper_closed_trades"], 2)
            self.assertEqual(recent_today["paper_realised_pnl"], 450.0)

    def test_materialize_session_rejects_complete_override_when_validation_fails(self):
        """Verify status_override='COMPLETE' cannot bypass failed eligibility or clear rejection reasons."""
        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": False,
            "rejection_reasons": ["insufficient one-minute bucket coverage (repair ratio too high)"],
            "warnings": [],
            "metrics": {
                "one_minute_bars": 375,
                "five_minute_bars": 75,
                "repaired_one_minute_bars": 300,
                "repaired_five_minute_bars": 60,
                "repair_ratio_1m": 0.80,
                "instruments_seen": 50,
                "predictions": 100,
                "critical_errors": 0,
            },
            "paper_pnl": {
                "realised_pnl": 0.0,
                "unrealised_pnl": 0.0,
                "net_marked_pnl": 0.0,
                "closed_trades": 0,
                "open_trades": 0,
                "rejected_trades": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
            },
        }

        mock_conn = MagicMock()
        mock_conn.execute.return_value.mappings.return_value.all.return_value = []
        self.mock_engine.begin.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval), \
             patch("backend.shadow_sessions._persist_daily_log"):
            res = materialize_session(self.mock_engine, self.today, status_override="COMPLETE")

            # CRITICAL: Status must be REJECTED, not COMPLETE
            self.assertEqual(res["status"], "REJECTED")
            # Rejection reasons must NOT be cleared
            self.assertIn("insufficient one-minute bucket coverage (repair ratio too high)", res["rejection_reasons"])
            # SQL must insert status='REJECTED'
            insert_call_args = mock_conn.execute.call_args_list[0]
            params = insert_call_args[0][1]
            self.assertEqual(params["status"], "REJECTED")
            self.assertIn("insufficient one-minute bucket coverage (repair ratio too high)", params["reasons"])

    def test_excessive_rest_repair_cannot_become_eligible_through_override(self):
        """Verify that a session with excessive REST-repaired data cannot become eligible or count toward promotion."""
        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": False,
            "rejection_reasons": [
                "insufficient one-minute bucket coverage (repair ratio too high)",
                "insufficient five-minute bucket coverage (repair ratio too high)",
            ],
            "warnings": ["REST backfill repaired missing candles (repair ratio 1m: 85.0%, 5m: 85.0%)"],
            "metrics": {
                "one_minute_bars": 375,
                "five_minute_bars": 75,
                "repaired_one_minute_bars": 320,
                "repaired_five_minute_bars": 64,
                "repair_ratio_1m": 0.85,
                "repair_ratio_5m": 0.85,
                "max_repair_ratio": 0.10,
                "instruments_seen": 60,
                "predictions": 150,
                "critical_errors": 0,
            },
            "paper_pnl": {
                "realised_pnl": 100.0,
                "unrealised_pnl": 0.0,
                "net_marked_pnl": 100.0,
                "closed_trades": 1,
                "open_trades": 0,
                "rejected_trades": 0,
                "win_rate_pct": 100.0,
                "profit_factor": 1.0,
            },
        }

        mock_conn = MagicMock()
        mock_conn.execute.return_value.mappings.return_value.all.return_value = []
        self.mock_engine.begin.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval), \
             patch("backend.shadow_sessions._persist_daily_log"):
            res = materialize_session(self.mock_engine, self.today, status_override="COMPLETE")
            self.assertEqual(res["status"], "REJECTED")
            self.assertEqual(len(res["rejection_reasons"]), 2)
            # Warning recorded that override was rejected
            self.assertTrue(any("Administrative override 'COMPLETE' rejected" in w for w in res["warnings"]))

    def test_materialize_session_succeeds_when_eligible(self):
        """Verify materialize_session marks COMPLETE when session genuinely meets all validation criteria."""
        mock_eval = {
            "session_date": self.today.isoformat(),
            "eligible": True,
            "rejection_reasons": [],
            "warnings": [],
            "metrics": {
                "one_minute_bars": 375,
                "five_minute_bars": 75,
                "repaired_one_minute_bars": 0,
                "repaired_five_minute_bars": 0,
                "instruments_seen": 60,
                "predictions": 120,
                "critical_errors": 0,
            },
            "paper_pnl": {
                "realised_pnl": 500.0,
                "unrealised_pnl": 0.0,
                "net_marked_pnl": 500.0,
                "closed_trades": 4,
                "open_trades": 0,
                "rejected_trades": 0,
                "win_rate_pct": 100.0,
                "profit_factor": 999.0,
            },
        }

        mock_conn = MagicMock()
        valid_row = {
            "session_date": self.today.isoformat(),
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {"repair_ratio_1m": 0.0, "repair_ratio_5m": 0.0},
        }
        mock_conn.execute.return_value.mappings.return_value.all.return_value = [valid_row] * 11
        self.mock_engine.begin.return_value.__enter__.return_value = mock_conn

        with patch("backend.shadow_sessions.evaluate_session", return_value=mock_eval), \
             patch("backend.shadow_sessions._persist_daily_log"):
            res = materialize_session(self.mock_engine, self.today, status_override="COMPLETE")
            self.assertEqual(res["status"], "COMPLETE")
            self.assertEqual(res["rejection_reasons"], [])

    def test_promotion_counting_valid_completed_session_counts(self):
        """Regression test 1: A valid completed session satisfies criteria and counts toward promotion."""
        valid_row = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "repair_ratio_1m": 0.02,
                "repair_ratio_5m": 0.01,
                "repaired_one_minute_bars": 10,
                "one_minute_bars": 365,
            },
        }
        self.assertTrue(is_promotion_eligible_session(valid_row))

    def test_promotion_counting_excessive_rest_repair_does_not_count(self):
        """Regression test 2: A completed session with excessive REST repair (e.g. 80.3%) does not count."""
        oct15_row = {
            "session_date": "2026-10-15",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "repair_ratio_1m": 0.8027,
                "repair_ratio_5m": 0.8027,
                "repaired_one_minute_bars": 162605,
                "one_minute_bars": 40000,
            },
        }
        self.assertFalse(is_promotion_eligible_session(oct15_row))

    def test_promotion_counting_rejected_session_does_not_count_even_with_override(self):
        """Regression test 3: A rejected session with failed validation or override does not count."""
        rejected_with_override = {
            "session_date": "2026-10-12",
            "status": "REJECTED",
            "live_eligible": False,
            "rejection_reasons": ["insufficient one-minute bucket coverage"],
            "metrics": {"admin_status_override": "COMPLETE"},
        }
        self.assertFalse(is_promotion_eligible_session(rejected_with_override))

        # Even if status was forcefully set to COMPLETE while rejection_reasons existed
        corrupt_override = {
            "session_date": "2026-10-12",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": ["insufficient one-minute bucket coverage"],
            "metrics": {"admin_status_override": "COMPLETE"},
        }
        self.assertFalse(is_promotion_eligible_session(corrupt_override))

    def test_historical_invalid_complete_record_cannot_inflate_promotion_count(self):
        """Regression test 4: A historical invalid COMPLETE record (e.g. Oct 15 with 80.3% repair) cannot inflate promotion count."""
        from sqlalchemy import create_engine
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE forward_shadow_sessions (
                    session_date DATE PRIMARY KEY,
                    status VARCHAR,
                    live_eligible BOOLEAN,
                    rejection_reasons TEXT,
                    metrics TEXT,
                    one_minute_bars INT,
                    five_minute_bars INT
                )
            """))
            # Insert 1 genuine valid session
            conn.execute(text("""
                INSERT INTO forward_shadow_sessions VALUES
                ('2026-10-14', 'COMPLETE', 0, '[]', '{"repair_ratio_1m": 0.01, "repair_ratio_5m": 0.01}', 375, 75)
            """))
            # Insert historical October 15, 2026 session with status='COMPLETE' but 80.3% repair
            conn.execute(text("""
                INSERT INTO forward_shadow_sessions VALUES
                ('2026-10-15', 'COMPLETE', 0, '[]', '{"repair_ratio_1m": 0.8027, "repair_ratio_5m": 0.8027}', 375, 75)
            """))
            # Insert 1 rejected session
            conn.execute(text("""
                INSERT INTO forward_shadow_sessions VALUES
                ('2026-10-13', 'REJECTED', 0, '["insufficient live instrument coverage"]', '{}', 100, 20)
            """))

        # Raw query for status='COMPLETE' yields 2 rows, but promotion evidence count must strictly be 1
        count = count_promotion_eligible_sessions(engine)
        self.assertEqual(count, 1)

    def test_promotion_calculation_does_not_depend_circularly_on_live_eligible(self):
        """Regression test 5: Promotion counting does not circularly require live_eligible=TRUE to count valid evidence."""
        # forward_shadow_sessions table has CHECK(NOT live_eligible), so all valid sessions have live_eligible=FALSE
        session = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {"repair_ratio_1m": 0.05, "repair_ratio_5m": 0.05},
        }
        self.assertTrue(is_promotion_eligible_session(session))

        from sqlalchemy import create_engine
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE forward_shadow_sessions (
                    session_date DATE PRIMARY KEY,
                    status VARCHAR,
                    live_eligible BOOLEAN,
                    rejection_reasons TEXT,
                    metrics TEXT,
                    one_minute_bars INT,
                    five_minute_bars INT
                )
            """))
            # 5 valid sessions, all with live_eligible=FALSE
            for i in range(1, 6):
                conn.execute(text(f"""
                    INSERT INTO forward_shadow_sessions VALUES
                    ('2026-10-0{i}', 'COMPLETE', 0, '[]', '{{"repair_ratio_1m": 0.0, "repair_ratio_5m": 0.0}}', 375, 75)
                """))
        # Must count all 5 without deadlock on live_eligible
        self.assertEqual(count_promotion_eligible_sessions(engine), 5)

    def test_is_promotion_eligible_fails_closed_on_missing_or_empty_metrics(self):
        """Regression test 6: Missing, null, or empty metrics fail closed and cannot count as promotion evidence."""
        # None metrics
        session_null_metrics = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": None,
        }
        self.assertFalse(is_promotion_eligible_session(session_null_metrics))

        # Empty dict metrics
        session_empty_metrics = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {},
        }
        self.assertFalse(is_promotion_eligible_session(session_empty_metrics))

        # Malformed JSON string metrics
        session_corrupt_json = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": "{corrupted_json",
        }
        self.assertFalse(is_promotion_eligible_session(session_corrupt_json))

    def test_is_promotion_eligible_fails_closed_on_missing_repair_evidence(self):
        """Regression test 7: Legacy records lacking repair ratios and bucket evidence are rejected."""
        # Has other metrics like predictions and trades, but no repair evidence
        session_no_repair = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "predictions": 120,
                "instruments_seen": 60,
                "critical_errors": 0,
            },
        }
        self.assertFalse(is_promotion_eligible_session(session_no_repair))

    def test_is_promotion_eligible_fails_closed_on_malformed_or_negative_repair_counts(self):
        """Regression test 8: Negative counts, impossible bucket counts, or malformed values fail closed."""
        # Negative repair ratio
        self.assertFalse(is_promotion_eligible_session({
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {"repair_ratio_1m": -0.05, "repair_ratio_5m": 0.05},
        }))

        # Repair ratio > 1.0
        self.assertFalse(is_promotion_eligible_session({
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {"repair_ratio_1m": 1.25, "repair_ratio_5m": 0.05},
        }))

        # Negative total buckets
        self.assertFalse(is_promotion_eligible_session({
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "all_one_minute_buckets": -375,
                "one_minute_buckets": 10,
                "all_five_minute_buckets": 75,
                "five_minute_buckets": 75,
            },
        }))

        # Negative live buckets
        self.assertFalse(is_promotion_eligible_session({
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "all_one_minute_buckets": 375,
                "one_minute_buckets": -10,
                "all_five_minute_buckets": 75,
                "five_minute_buckets": 75,
            },
        }))

        # Impossible bucket count (live_buckets > all_buckets)
        self.assertFalse(is_promotion_eligible_session({
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "all_one_minute_buckets": 350,
                "one_minute_buckets": 380,
                "all_five_minute_buckets": 75,
                "five_minute_buckets": 75,
            },
        }))

        # Non-numeric string ratio
        self.assertFalse(is_promotion_eligible_session({
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {"repair_ratio_1m": "corrupt_data", "repair_ratio_5m": 0.02},
        }))

    def test_database_row_retrieval_failure_fails_closed_without_scalar_fallback(self):
        """Regression test 9: Database failure in row retrieval raises or fails closed and never falls back to scalar_one."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("Database read failure")
        with self.assertRaises(RuntimeError):
            count_promotion_eligible_sessions(mock_conn)

        # Ensure that even if scalar_one was configured on a result, failure to read rows raises and never returns scalar_one
        mock_conn2 = MagicMock()
        mock_result = MagicMock()
        mock_result.mappings.side_effect = RuntimeError("Mappings extraction failure")
        mock_result.scalar_one.return_value = 100  # Raw count that must NEVER be returned
        mock_conn2.execute.return_value = mock_result
        with self.assertRaises(RuntimeError):
            count_promotion_eligible_sessions(mock_conn2)

    def test_repair_ratio_uses_exact_evaluate_session_denominator(self):
        """Regression test 10: Fallback ratio accurately uses all_buckets as denominator matching evaluate_session."""
        # 1. Valid session using bucket fallback: (375 - 360) / 375 = 15 / 375 = 0.04 (4% <= 10%)
        # and 5m: (75 - 72) / 75 = 3 / 75 = 0.04 (4% <= 10%)
        valid_buckets = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "all_one_minute_buckets": 375,
                "one_minute_buckets": 360,
                "all_five_minute_buckets": 75,
                "five_minute_buckets": 72,
            },
        }
        self.assertTrue(is_promotion_eligible_session(valid_buckets))

        # 2. Denominator is 0: fails closed without division error
        zero_denominator = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "all_one_minute_buckets": 0,
                "one_minute_buckets": 0,
                "all_five_minute_buckets": 0,
                "five_minute_buckets": 0,
            },
        }
        self.assertFalse(is_promotion_eligible_session(zero_denominator))

        # 3. Excessive repair using correct denominator: (375 - 300) / 375 = 75 / 375 = 20% > 10%
        excessive_repair_buckets = {
            "session_date": "2026-10-14",
            "status": "COMPLETE",
            "live_eligible": False,
            "rejection_reasons": [],
            "metrics": {
                "all_one_minute_buckets": 375,
                "one_minute_buckets": 300,
                "all_five_minute_buckets": 75,
                "five_minute_buckets": 75,
            },
        }
        self.assertFalse(is_promotion_eligible_session(excessive_repair_buckets))


if __name__ == "__main__":
    unittest.main()
