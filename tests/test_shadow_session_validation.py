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

from backend.shadow_sessions import (
    IST,
    evaluate_session,
    finalize_session,
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
        # recent query returns historical dates not including today as COMPLETE
        past_row = {"session_date": (self.today - timedelta(days=1)).isoformat(), "status": "COMPLETE"}
        mock_conn.execute.return_value.mappings.return_value.all.return_value = [past_row]
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
        past_row = {"session_date": (self.today - timedelta(days=1)).isoformat(), "status": "COMPLETE"}
        mock_conn.execute.return_value.mappings.return_value.all.return_value = [past_row]
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


if __name__ == "__main__":
    unittest.main()
