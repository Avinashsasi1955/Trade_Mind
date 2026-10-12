"""Pytest configuration and test environment isolation.

Ensures that unit and system test runs are isolated from local deployment
configuration (.env) by default. Prevents accidental connection attempts
to external services (PostgreSQL, Redis) during test runs.
"""
import os
import pytest


def pytest_configure(config):
    """Configure isolated environment variables before test collection and execution."""
    # Ensure test environment is explicitly isolated
    os.environ["NIVESH_ENV"] = "test"
    os.environ["NIVESH_LIVE_TRADING_ENABLED"] = "0"
    os.environ["LIVE_ELIGIBLE"] = "FALSE"

    # If the tester did not explicitly request live integration testing via RUN_POSTGRES,
    # isolate DATABASE_URL and REDIS_URL to avoid connection failures to offline services.
    if not os.getenv("RUN_POSTGRES"):
        # Set to empty string so that tests use clean, in-memory SQLite fixtures
        os.environ["DATABASE_URL"] = ""
        os.environ["REDIS_URL"] = ""
