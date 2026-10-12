import os
import unittest
from datetime import date
from decimal import Decimal
import json
from sqlalchemy import create_engine, text

from backend.shadow_sessions import get_code_version, _persist_daily_log


class TestShadowSessionCodeVersion(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_get_code_version_docker_git_commit_env(self):
        """Verify Docker environments pass GIT_COMMIT environment variable."""
        os.environ["GIT_COMMIT"] = "abc123dockerbuildhash"
        ver = get_code_version()
        self.assertEqual(ver, "abc123dockerbuildhash")

    def test_get_code_version_docker_code_version_env(self):
        """Verify fallback environment variables."""
        os.environ.pop("GIT_COMMIT", None)
        os.environ["CODE_VERSION"] = "release-v3.18-commit"
        ver = get_code_version()
        self.assertEqual(ver, "release-v3.18-commit")

    def test_get_code_version_git_fallback(self):
        """Verify git checkout fallback returns a non-empty string or 'unknown'."""
        for k in ("GIT_COMMIT", "NIVESH_CODE_VERSION", "CODE_VERSION", "GIT_COMMIT_HASH", "GIT_SHA"):
            os.environ.pop(k, None)
        ver = get_code_version()
        self.assertIsInstance(ver, str)
        self.assertTrue(len(ver) > 0)

    def test_persist_daily_log_with_code_version_column(self):
        """Test writing daily log into in-memory SQLite database containing code_version column."""
        os.environ["GIT_COMMIT"] = "feedface998877"
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE shadow_session_daily_log (
                    session_date DATE PRIMARY KEY,
                    status TEXT,
                    valid BOOLEAN,
                    completed_sessions INTEGER,
                    effective_completed_sessions INTEGER,
                    live_one_minute_buckets INTEGER,
                    live_five_minute_buckets INTEGER,
                    live_instruments_seen INTEGER,
                    repaired_one_minute_bars INTEGER,
                    repaired_five_minute_bars INTEGER,
                    predictions INTEGER,
                    trades_taken INTEGER,
                    closed_trades INTEGER,
                    win_rate_pct NUMERIC,
                    net_pnl NUMERIC,
                    rejection_reasons TEXT,
                    warnings TEXT,
                    mistakes TEXT,
                    metrics TEXT,
                    code_version TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))

        today = {
            "session_date": date(2026, 10, 10),
            "status": "COMPLETED",
            "eligible": True,
            "metrics": {"instruments_seen": 50},
            "paper_pnl": {"closed_trades": 10, "win_rate_pct": 60.0, "net_marked_pnl": 1500.0},
        }

        _persist_daily_log(engine, today, completed=15, effective_completed=15)

        with engine.connect() as conn:
            row = conn.execute(text("SELECT session_date, code_version, metrics FROM shadow_session_daily_log")).mappings().one()
            self.assertEqual(str(row["session_date"]), "2026-10-10")
            self.assertEqual(row["code_version"], "feedface998877")
            metrics_dict = json.loads(row["metrics"])
            self.assertEqual(metrics_dict.get("code_version"), "feedface998877")

    def test_persist_daily_log_without_code_version_column_graceful(self):
        """Test backwards compatibility with legacy schema lacking code_version column."""
        os.environ["GIT_COMMIT"] = "legacytest123"
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE shadow_session_daily_log (
                    session_date DATE PRIMARY KEY,
                    status TEXT,
                    valid BOOLEAN,
                    completed_sessions INTEGER,
                    effective_completed_sessions INTEGER,
                    live_one_minute_buckets INTEGER,
                    live_five_minute_buckets INTEGER,
                    live_instruments_seen INTEGER,
                    repaired_one_minute_bars INTEGER,
                    repaired_five_minute_bars INTEGER,
                    predictions INTEGER,
                    trades_taken INTEGER,
                    closed_trades INTEGER,
                    win_rate_pct NUMERIC,
                    net_pnl NUMERIC,
                    rejection_reasons TEXT,
                    warnings TEXT,
                    mistakes TEXT,
                    metrics TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))

        today = {
            "session_date": date(2026, 10, 10),
            "status": "COMPLETED",
            "eligible": True,
            "metrics": {},
            "paper_pnl": {"closed_trades": 5},
        }

        # Should not raise even though code_version column does not exist
        _persist_daily_log(engine, today, completed=5, effective_completed=5)

        with engine.connect() as conn:
            row = conn.execute(text("SELECT metrics FROM shadow_session_daily_log")).mappings().one()
            metrics_dict = json.loads(row["metrics"])
            self.assertEqual(metrics_dict.get("code_version"), "legacytest123")


if __name__ == "__main__":
    unittest.main()
