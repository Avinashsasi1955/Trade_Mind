import os
import unittest


def _postgres_is_reachable() -> bool:
    """Determine whether PostgreSQL integration tests should run.

    When RUN_POSTGRES=1 is explicitly set, always returns True so that
    connection failures (auth errors, invalid DSN, server offline) surface
    as real test errors rather than silent skips.

    When RUN_POSTGRES is not set, probes the connection and returns False
    for a clean skip when the database is unavailable.
    """
    if os.getenv("RUN_POSTGRES"):
        # Explicitly requested — let failures propagate as test errors.
        return True
    url = os.getenv("DATABASE_URL", "")
    if not url or not url.startswith("postgres"):
        return False
    try:
        import psycopg2
        clean_dsn = url.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(clean_dsn, connect_timeout=1)
        conn.close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_postgres_is_reachable(), "PostgreSQL integration environment is not reachable or not configured")
class PostgresRuntimeTests(unittest.TestCase):
    def test_history_repository_routes_to_postgres_and_reads_bars(self):
        from backend.history_store import HistoryStore
        store=HistoryStore(); stats=store.stats()
        self.assertEqual(type(store).__name__,"PostgresHistoryStore")
        self.assertGreater(stats["bars"],6_000_000)
        self.assertEqual(stats["failures"],0)
        self.assertGreaterEqual(len(store.candles("RELIANCE","NSE","day",5)),5)

    def test_research_repository_routes_to_postgres_and_reads_registry(self):
        from backend.ml_pipeline import ResearchStore, active_model
        store=ResearchStore(); status=store.status(); model=active_model(store)
        self.assertEqual(type(store).__name__,"PostgresResearchStore")
        self.assertGreater(status["counts"]["feature_rows"],3_000_000)
        self.assertIsNotNone(model)
        self.assertFalse(model["payload"]["live_eligible"])

    def test_release_preflight_is_audited_and_never_allows_orders(self):
        from backend.preflight import run_preflight
        result=run_preflight(os.environ["DATABASE_URL"],os.getenv("REDIS_URL","redis://127.0.0.1:6379/0"),require_integrations=False)
        self.assertFalse(result["orders_allowed"])
        self.assertTrue(result["checks"]["schema"]["passed"])
        self.assertTrue(result["checks"]["redis"]["passed"])
