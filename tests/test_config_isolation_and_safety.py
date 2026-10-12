"""Focused tests for configuration isolation, safety fuses, and fail-safe startup behavior.

Part of Phase 2A - Security Hardening & Test Baseline.
"""
import os
import unittest
from unittest.mock import patch

from backend import config as app_config
from backend.config import validate_runtime_security


class TestConfigIsolationAndSafety(unittest.TestCase):
    """Verifies that configuration defaults fail closed and protect execution fuses."""

    def test_live_trading_fuses_default_locked(self):
        """Verifies that live trading deployment fuses cannot default to enabled."""
        self.assertFalse(app_config.LIVE_TRADING_ENABLED, "LIVE_TRADING_ENABLED must be False by default")
        self.assertFalse(app_config.LIVE_ELIGIBLE, "LIVE_ELIGIBLE must be False by default")

    def test_production_runtime_security_fails_closed_on_missing_config(self):
        """Verifies validate_runtime_security() raises RuntimeError for insecure production configs."""
        # Test default/weak secret
        with patch.multiple(
            app_config,
            IS_PRODUCTION=True,
            JWT_SECRET="change-this-secret-before-deployment",
            MODEL_ARTIFACT_KEY="another-long-random-secret-key-32chars",
            BROKER_SESSION_KEY="a-third-long-random-secret-key-32chars",
            REDIS_URL="rediss://localhost:6379/0",
            COOKIE_SECURE=True,
            DEMO_MODE=False,
            SIGNUP_ENABLED=False,
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            ALLOWED_ORIGINS={"https://app.example.com"},
            ADMIN_EMAILS={"admin@example.com"},
            COGNITO_AUTH_REQUIRED=True,
            TRUSTED_ALB_ARN="arn:aws:elasticloadbalancing:ap-south-1:123:loadbalancer/app/test/1",
            COGNITO_LOGOUT_URL="https://auth.example.com/logout",
        ):
            with self.assertRaisesRegex(RuntimeError, "NIVESH_SECRET must be a unique random value"):
                validate_runtime_security()

    def test_production_runtime_security_fails_closed_on_insecure_redis(self):
        """Verifies validate_runtime_security() requires TLS rediss:// in production."""
        with patch.multiple(
            app_config,
            IS_PRODUCTION=True,
            JWT_SECRET="a-valid-long-random-secret-key-32chars",
            MODEL_ARTIFACT_KEY="another-long-random-secret-key-32chars",
            BROKER_SESSION_KEY="a-third-long-random-secret-key-32chars",
            REDIS_URL="redis://localhost:6379/0",  # non-TLS
            COOKIE_SECURE=True,
            DEMO_MODE=False,
            SIGNUP_ENABLED=False,
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            ALLOWED_ORIGINS={"https://app.example.com"},
            ADMIN_EMAILS={"admin@example.com"},
            COGNITO_AUTH_REQUIRED=True,
            TRUSTED_ALB_ARN="arn:aws:elasticloadbalancing:ap-south-1:123:loadbalancer/app/test/1",
            COGNITO_LOGOUT_URL="https://auth.example.com/logout",
        ):
            with self.assertRaisesRegex(RuntimeError, "TLS REDIS_URL is required"):
                validate_runtime_security()

    def test_production_runtime_security_fails_closed_on_insecure_cookie(self):
        """Verifies validate_runtime_security() requires COOKIE_SECURE=1 in production."""
        with patch.multiple(
            app_config,
            IS_PRODUCTION=True,
            JWT_SECRET="a-valid-long-random-secret-key-32chars",
            MODEL_ARTIFACT_KEY="another-long-random-secret-key-32chars",
            BROKER_SESSION_KEY="a-third-long-random-secret-key-32chars",
            REDIS_URL="rediss://localhost:6379/0",
            COOKIE_SECURE=False,  # Insecure
            DEMO_MODE=False,
            SIGNUP_ENABLED=False,
            DATABASE_URL="postgresql://user:pass@localhost:5432/db",
            ALLOWED_ORIGINS={"https://app.example.com"},
            ADMIN_EMAILS={"admin@example.com"},
            COGNITO_AUTH_REQUIRED=True,
            TRUSTED_ALB_ARN="arn:aws:elasticloadbalancing:ap-south-1:123:loadbalancer/app/test/1",
            COGNITO_LOGOUT_URL="https://auth.example.com/logout",
        ):
            with self.assertRaisesRegex(RuntimeError, "NIVESH_COOKIE_SECURE=1 is required"):
                validate_runtime_security()

    def test_production_runtime_security_fails_closed_on_non_postgres_database(self):
        """Verifies validate_runtime_security() rejects SQLite in production."""
        with patch.multiple(
            app_config,
            IS_PRODUCTION=True,
            JWT_SECRET="a-valid-long-random-secret-key-32chars",
            MODEL_ARTIFACT_KEY="another-long-random-secret-key-32chars",
            BROKER_SESSION_KEY="a-third-long-random-secret-key-32chars",
            REDIS_URL="rediss://localhost:6379/0",
            COOKIE_SECURE=True,
            DEMO_MODE=False,
            SIGNUP_ENABLED=False,
            DATABASE_URL="",  # Missing / non-postgres
            ALLOWED_ORIGINS={"https://app.example.com"},
            ADMIN_EMAILS={"admin@example.com"},
            COGNITO_AUTH_REQUIRED=True,
            TRUSTED_ALB_ARN="arn:aws:elasticloadbalancing:ap-south-1:123:loadbalancer/app/test/1",
            COGNITO_LOGOUT_URL="https://auth.example.com/logout",
        ):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL DATABASE_URL is required"):
                validate_runtime_security()

    def test_order_submission_hard_blocked_by_execution_fuses(self):
        """Verifies submit_intent unconditionally rejects orders when fuses are locked."""
        import sqlite3
        from backend.database import SCHEMA, create_user
        from backend.execution import create_intent, submit_intent

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        user_id = create_user(db, "Safety User", "safety@example.com", "strong-password-hash")

        # Create an approved order intent in DB
        db.execute(
            "INSERT INTO order_intents (user_id, client_order_id, symbol, exchange, transaction_type, "
            "quantity, order_type, limit_price, product, strategy, confidence, reasoning, status, risk_payload, created_at, updated_at) "
            "VALUES (?, 'TEST-ORDER-1', 'RELIANCE', 'NSE', 'BUY', 1, 'LIMIT', 2500.0, 'CNC', 'test', 0.85, 'Safety test', 'APPROVED', '{}', '2026-10-12', '2026-10-12')",
            (user_id,),
        )
        db.commit()
        intent_id = db.execute("SELECT id FROM order_intents WHERE client_order_id='TEST-ORDER-1'").fetchone()[0]

        # With LIVE_TRADING_ENABLED=0, submit_intent MUST raise ValueError
        with patch("backend.execution.LIVE_TRADING_ENABLED", False):
            with self.assertRaisesRegex(ValueError, "Live trading deployment flag is disabled"):
                submit_intent(db, user_id, intent_id)

        # With LIVE_TRADING_ENABLED=1 but LIVE_ELIGIBLE=False, submit_intent MUST raise ValueError
        with patch("backend.execution.LIVE_TRADING_ENABLED", True), patch("backend.execution.LIVE_ELIGIBLE", False):
            with self.assertRaisesRegex(ValueError, "Model promotion fuse LIVE_ELIGIBLE is false"):
                submit_intent(db, user_id, intent_id)

        db.close()


if __name__ == "__main__":
    unittest.main()
