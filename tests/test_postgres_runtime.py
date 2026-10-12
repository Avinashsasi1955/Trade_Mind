import os
import unittest
from unittest.mock import MagicMock, patch

from scripts.manage_test_database import APPROVED_TEST_DB, validate_postgres_test_url


def _should_run_postgres_tests() -> bool:
    """Check opt-in flag. NEVER probes ambient DATABASE_URL or .env when RUN_POSTGRES != '1'."""
    if os.getenv("RUN_POSTGRES") != "1":
        return False
    # When RUN_POSTGRES is strictly "1", validate URL immediately so bad configuration fails closed
    validate_postgres_test_url(os.getenv("POSTGRES_TEST_DATABASE_URL", ""))
    return True


@unittest.skipUnless(
    _should_run_postgres_tests(),
    "PostgreSQL integration tests require explicit RUN_POSTGRES=1 and validated POSTGRES_TEST_DATABASE_URL",
)
class PostgresRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_url = validate_postgres_test_url(os.getenv("POSTGRES_TEST_DATABASE_URL", ""))
        os.environ["DATABASE_URL"] = test_url
        os.environ["NIVESH_ENV"] = "test"
        os.environ["NIVESH_LIVE_TRADING_ENABLED"] = "0"
        os.environ["LIVE_ELIGIBLE"] = "FALSE"

        import backend.config as cfg
        cfg.DATABASE_URL = test_url
        import backend.postgres_repositories as pr
        pr.DATABASE_URL = test_url
        import backend.history_store as hs
        hs.DATABASE_URL = test_url
        import backend.ml_pipeline as ml
        ml.DATABASE_URL = test_url
        import backend.postgres_compat as pc
        if pc._pool is not None:
            try:
                pc._pool.closeall()
            except Exception:
                pass
            pc._pool = None

        import psycopg2
        clean_dsn = test_url.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(clean_dsn, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), pg_is_in_recovery();")
                actual_db, in_recovery = cur.fetchone()
            if actual_db != APPROVED_TEST_DB:
                raise RuntimeError(
                    f"SAFETY GATE TRIGGERED: Connected database is '{actual_db}', expected '{APPROVED_TEST_DB}'. Aborting."
                )
            if in_recovery:
                raise RuntimeError("SAFETY GATE TRIGGERED: Connected database is in recovery (read replica). Aborting.")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        import backend.postgres_compat as pc
        if pc._pool is not None:
            try:
                pc._pool.closeall()
            except Exception:
                pass
            pc._pool = None
        os.environ["DATABASE_URL"] = ""
        import backend.config as cfg
        cfg.DATABASE_URL = ""
        import backend.postgres_repositories as pr
        pr.DATABASE_URL = ""
        import backend.history_store as hs
        hs.DATABASE_URL = ""
        import backend.ml_pipeline as ml
        ml.DATABASE_URL = ""


    def test_history_repository_routes_to_postgres_and_reads_bars(self):

        from backend.history_store import HistoryStore
        store = HistoryStore()
        # Ensure routing to PostgreSQL repository and prevent silent SQLite fallback
        self.assertEqual(type(store).__name__, "PostgresHistoryStore")
        self.assertIsNone(getattr(store, "path", "SENTINEL"))

        stats = store.stats()
        self.assertIsInstance(stats["bars"], int)
        self.assertGreaterEqual(stats["bars"], 0)
        self.assertEqual(stats["failures"], 0)

        # Retrieve fixture candles
        candles = store.candles("RELIANCE", "NSE", "day", 5)
        self.assertEqual(len(candles), 5)

        # Expected deterministic fixture candles in exact ascending order
        expected_candles = [
            ("2026-01-01", 2400.0, 2420.0, 2390.0, 2410.0, 100000),
            ("2026-01-02", 2410.0, 2430.0, 2400.0, 2420.0, 110000),
            ("2026-01-05", 2420.0, 2440.0, 2410.0, 2430.0, 120000),
            ("2026-01-06", 2430.0, 2450.0, 2420.0, 2440.0, 130000),
            ("2026-01-07", 2440.0, 2460.0, 2430.0, 2450.0, 140000),
        ]

        previous_ts = None
        for i, (date_prefix, exp_open, exp_high, exp_low, exp_close, exp_vol) in enumerate(expected_candles):
            c = candles[i]
            self.assertEqual(c["symbol"], "RELIANCE")
            self.assertEqual(c["exchange"], "NSE")
            self.assertEqual(c["interval"], "day")
            self.assertEqual(c["source"], "fixture")
            self.assertEqual(c["open"], exp_open)
            self.assertEqual(c["high"], exp_high)
            self.assertEqual(c["low"], exp_low)
            self.assertEqual(c["close"], exp_close)
            self.assertEqual(c["volume"], exp_vol)

            ts_str = str(c["timestamp"])
            self.assertTrue(ts_str.startswith(date_prefix), f"Expected date prefix {date_prefix}, got {ts_str}")
            if previous_ts is not None:
                self.assertGreater(c["timestamp"], previous_ts, "Candles must be strictly monotonically ascending")
            previous_ts = c["timestamp"]

    def test_research_repository_routes_to_postgres_and_reads_registry(self):
        from backend.ml_pipeline import ResearchStore, active_model
        store = ResearchStore()
        # Ensure routing to PostgreSQL repository and prevent silent SQLite fallback
        self.assertEqual(type(store).__name__, "PostgresResearchStore")
        self.assertTrue(getattr(store, "is_postgres", False))

        status = store.status()
        self.assertIsInstance(status["counts"]["feature_rows"], int)
        self.assertGreaterEqual(status["counts"]["feature_rows"], 0)

        model = active_model(store)
        self.assertIsNotNone(model)
        self.assertEqual(model["version"], "v3_test_01")
        self.assertEqual(model["model_name"], "shadow_ensemble")
        self.assertEqual(model["feature_set"], "daily_v2")
        self.assertEqual(model["algorithm"], "logistic_calibrated")
        self.assertEqual(model["status"], "active")

        # Verify model payload and live trading safety fuse
        payload = model["payload"]
        self.assertIsInstance(payload, dict)
        self.assertFalse(payload["live_eligible"], "Model in registry must have live_eligible=False")
        self.assertEqual(payload["features"], ["return_1d"])
        self.assertEqual(payload["weights"], [0.0])

    def test_release_preflight_is_audited_and_never_allows_orders(self):
        """Verify release preflight executes audit record and disallows live orders.

        CRITICAL AUDIT IMMUTABILITY SEMANTICS:
        backend/preflight.py commits an autonomous audit record into operational_preflight_runs on every execution.
        This test preserves production audit immutability and never deletes audit rows.
        Audit isolation is guaranteed strictly by running exclusively against the authorized disposable test database (nivesh_test),
        which is destroyed and recreated during administrative test teardown rather than mutated row-by-row.
        """
        from backend.preflight import run_preflight

        # Note: backend/preflight.py unconditionally contacts Redis via cache.ping()
        # regardless of require_integrations=False. Mock Redis to make dependencies
        # explicit and deterministic without relying on an ambient local Redis service.
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True

        test_redis_url = "redis://127.0.0.1:6379/15"
        with patch("redis.Redis.from_url", return_value=mock_redis) as mock_from_url:
            result = run_preflight(
                os.environ["DATABASE_URL"],
                test_redis_url,
                require_integrations=False,
            )
            mock_from_url.assert_called_once_with(test_redis_url, decode_responses=True, socket_timeout=3)
            mock_redis.ping.assert_called_once()

        self.assertFalse(result["orders_allowed"])
        self.assertTrue(result["checks"]["schema"]["passed"])
        self.assertTrue(result["checks"]["redis"]["passed"])

        # Also verify fail-closed behavior when Redis is unavailable: orders must STILL be disallowed
        mock_redis_down = MagicMock()
        mock_redis_down.ping.side_effect = ConnectionError("Redis unreachable")
        with patch("redis.Redis.from_url", return_value=mock_redis_down):
            down_result = run_preflight(
                os.environ["DATABASE_URL"],
                test_redis_url,
                require_integrations=False,
            )
        self.assertFalse(down_result["orders_allowed"])
        self.assertFalse(down_result["checks"]["redis"]["passed"])
        self.assertIn("redis", down_result["blockers"])
        self.assertEqual(down_result["status"], "BLOCKED")
