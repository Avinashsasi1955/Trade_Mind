"""Pytest configuration and test environment isolation.

Ensures that unit and system test runs are isolated from local deployment
configuration (.env) by default. Prevents accidental connection attempts
to external services (PostgreSQL, Redis) during test runs.

CRITICAL NETWORK AND SECURITY LIMITATIONS:
- Localhost checks (host in {'localhost', '127.0.0.1'}, port=5432) verify only that
  the client connects to a local socket endpoint.
- THEY CANNOT RULE OUT SSH LOCAL PORT FORWARDING (e.g. `ssh -L 5432:remote-server:5432`)
  or other local port proxies. If an SSH tunnel is active on port 5432, connections will
  be transparently forwarded to the remote server.
- Operators MUST ensure no SSH tunnels or port forwarding proxies are active on port 5432.
- Target database must be strictly 'nivesh_test'. Any staging, production, or maintenance
  database target is strictly rejected.
"""
import os
import pytest

from scripts.manage_test_database import validate_postgres_test_url


def pytest_configure(config):
    """Configure isolated environment variables before test collection and execution."""
    # Ensure test environment is explicitly isolated
    os.environ["NIVESH_ENV"] = "test"
    os.environ["NIVESH_LIVE_TRADING_ENABLED"] = "0"
    os.environ["LIVE_ELIGIBLE"] = "FALSE"

    # Integration testing requires RUN_POSTGRES to equal exactly "1".
    # Any other value (or absence) enforces strict isolation from local external services.
    if os.getenv("RUN_POSTGRES") != "1":
        # Set to empty string so that tests use clean, in-memory SQLite fixtures
        os.environ["DATABASE_URL"] = ""
        os.environ["REDIS_URL"] = ""
        os.environ.pop("POSTGRES_TEST_DATABASE_URL", None)
    else:
        # Explicit integration run: require POSTGRES_TEST_DATABASE_URL and validate strictly
        validate_postgres_test_url(os.getenv("POSTGRES_TEST_DATABASE_URL", ""))
        # Maintain DATABASE_URL="" globally so that SQLite unit tests remain isolated;
        # PostgresRuntimeTests activates DATABASE_URL within its scoped setUpClass/tearDownClass lifecycle.
        os.environ["DATABASE_URL"] = ""
        os.environ["REDIS_URL"] = ""

