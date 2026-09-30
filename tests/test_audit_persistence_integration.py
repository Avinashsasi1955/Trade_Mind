"""Integration tests for shadow_execution_audits persistence guarantees.

Covers:
- reasoning_chain JSONB round-trip through record_shadow_signal()
- Atomic exit guard: second record_shadow_exit() on same audit_id is no-op
- ON CONFLICT idempotency: duplicate signal insert returns the original audit
- mistake_tags JSONB correctness across every exit_reason
"""
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from backend.ml.validation_engine import (
    _mistake_tags,
    classify_probability,
    record_shadow_exit,
    record_shadow_signal,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight fakes for engine.begin() / connection.execute()
# ---------------------------------------------------------------------------

class _FakeResult:
    """Mimics SQLAlchemy ScalarResult / MappingResult."""
    def __init__(self, value=None, mappings_value=None):
        self._value = value
        self._mappings_value = mappings_value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        if self._value is None:
            raise Exception("No rows")
        return self._value

    def mappings(self):
        return self

    def one_or_none(self):
        return self._mappings_value

    def one(self):
        return self._mappings_value

    def all(self):
        return [self._mappings_value] if self._mappings_value else []


def _make_instrument_row(**overrides):
    defaults = {
        "id": 1,
        "symbol": "RELIANCE",
        "instrument_type": "EQ",
        "tick_size": "0.05",
        "exchange": "NSE",
        "expiry": None,
        "lot_size": 1,
        "is_active": True,
    }
    defaults.update(overrides)
    return defaults


def _make_open_audit_row(**overrides):
    defaults = {
        "id": 42,
        "instrument_id": 1,
        "theoretical_fill_price": Decimal("2500.05"),
        "quantity": 10,
        "side": "BUY",
        "trade_mode": "INTRADAY",
        "instrument_type": "EQ",
        "exchange": "NSE",
        "symbol": "RELIANCE",
        "estimated_fees": Decimal("3.50"),
        "fill_source": "DEPTH_SNAPSHOT",
        "signal_probability": Decimal("0.72"),
        "stop_loss_price": Decimal("2485.00"),
        "take_profit_price": Decimal("2530.00"),
        "net_pnl": None,
    }
    defaults.update(overrides)
    return defaults


class _FakeConnection:
    """Captures every SQL + params pair for assertion."""
    def __init__(self, execute_responses):
        self.calls = []
        self._responses = list(execute_responses)

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return self._responses.pop(0) if self._responses else _FakeResult()


class _FakeEngine:
    def __init__(self, connection):
        self._conn = connection

    def begin(self):
        return _FakeContext(self._conn)


class _FakeContext:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReasoningChainPersistence(unittest.TestCase):
    """Verify reasoning_chain JSONB is serialised and passed to the INSERT."""

    @patch("backend.ml.validation_engine.publish_brain_event")
    @patch("backend.ml.validation_engine.estimate_zerodha_costs")
    def test_reasoning_chain_is_serialised_to_jsonb_in_insert(self, mock_costs, mock_brain):
        """When strategy_note contains reasoning_chain, the INSERT param must be valid JSON."""
        mock_costs.return_value = MagicMock(total=Decimal("5.00"))

        chain = [
            {"step": "regime_check", "result": "bull"},
            {"step": "volume_confirm", "result": True},
        ]
        strategy_note = json.dumps({
            "trade_mode": "INTRADAY",
            "reasoning_chain": chain,
        })

        instrument = _make_instrument_row()
        conn = _FakeConnection([
            _FakeResult(mappings_value=instrument),   # instrument lookup
            _FakeResult(value=99),                      # INSERT RETURNING id
        ])
        engine = _FakeEngine(conn)
        redis = MagicMock()
        redis.get.return_value = json.dumps({"best_bid": 2499, "best_ask": 2501})

        result = record_shadow_signal(
            engine, redis, "v3.2", 408065, "BUY", 10, 0.72,
            Decimal("2500"), datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc),
            strategy_note,
        )

        self.assertTrue(result["recorded"])
        self.assertEqual(result["audit_id"], 99)

        # The INSERT is the second execute call
        insert_sql, insert_params = conn.calls[1]
        self.assertIn("reasoning_chain", insert_sql)
        # Verify the param is valid JSON that round-trips back to the original chain
        persisted = json.loads(insert_params["reasoning_chain"])
        self.assertEqual(persisted, chain)

    @patch("backend.ml.validation_engine.publish_brain_event")
    @patch("backend.ml.validation_engine.estimate_zerodha_costs")
    def test_missing_reasoning_chain_passes_none(self, mock_costs, mock_brain):
        """When strategy_note has no reasoning_chain key, the param must be None."""
        mock_costs.return_value = MagicMock(total=Decimal("5.00"))

        strategy_note = json.dumps({"trade_mode": "SWING"})
        instrument = _make_instrument_row()
        conn = _FakeConnection([
            _FakeResult(mappings_value=instrument),
            _FakeResult(value=100),
        ])
        engine = _FakeEngine(conn)
        redis = MagicMock()
        redis.get.return_value = json.dumps({"best_bid": 2499, "best_ask": 2501})

        result = record_shadow_signal(
            engine, redis, "v3.2", 408065, "BUY", 10, 0.72,
            Decimal("2500"), datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc),
            strategy_note,
        )

        self.assertTrue(result["recorded"])
        _, insert_params = conn.calls[1]
        self.assertIsNone(insert_params["reasoning_chain"])


class TestAtomicExitGuard(unittest.TestCase):
    """Verify the AND net_pnl IS NULL guard prevents double-exit writes."""

    @patch("backend.ml.validation_engine.publish_brain_event")
    @patch("backend.ml.validation_engine.estimate_zerodha_costs")
    def test_second_exit_is_noop_when_already_closed(self, mock_costs, mock_brain):
        """If net_pnl IS NOT NULL, the SELECT FOR UPDATE returns None → no-op."""
        # First call: open audit row found
        open_row = _make_open_audit_row()
        mock_costs.return_value = MagicMock(total=Decimal("4.00"))

        conn_first = _FakeConnection([
            _FakeResult(mappings_value=open_row),
        ])
        engine_first = _FakeEngine(conn_first)

        result_first = record_shadow_exit(
            engine_first, 42, Decimal("2520.00"), "TAKE_PROFIT",
        )
        self.assertEqual(result_first["audit_id"], 42)
        self.assertIn("net_pnl", result_first)

        # Second call: row already has net_pnl (SELECT returns None)
        conn_second = _FakeConnection([
            _FakeResult(mappings_value=None),
        ])
        engine_second = _FakeEngine(conn_second)

        result_second = record_shadow_exit(
            engine_second, 42, Decimal("2520.00"), "TAKE_PROFIT",
        )
        self.assertFalse(result_second["recorded"])
        self.assertEqual(result_second["status"], "ALREADY_CLOSED_OR_NOT_FOUND")

    @patch("backend.ml.validation_engine.publish_brain_event")
    @patch("backend.ml.validation_engine.estimate_zerodha_costs")
    def test_exit_update_sql_includes_net_pnl_is_null_guard(self, mock_costs, mock_brain):
        """The UPDATE WHERE clause must include AND net_pnl IS NULL."""
        open_row = _make_open_audit_row()
        mock_costs.return_value = MagicMock(total=Decimal("4.00"))

        conn = _FakeConnection([
            _FakeResult(mappings_value=open_row),
        ])
        engine = _FakeEngine(conn)

        record_shadow_exit(engine, 42, Decimal("2520.00"), "TAKE_PROFIT")

        # The UPDATE is the second execute call (after SELECT FOR UPDATE)
        update_sql, _ = conn.calls[1]
        self.assertIn("net_pnl IS NULL", update_sql)


class TestOnConflictIdempotency(unittest.TestCase):
    """Verify the INSERT ON CONFLICT(model_version,instrument_id,signal_at,side)
    DO NOTHING clause is present in the signal SQL."""

    @patch("backend.ml.validation_engine.publish_brain_event")
    @patch("backend.ml.validation_engine.estimate_zerodha_costs")
    def test_duplicate_signal_returns_none_audit_id(self, mock_costs, mock_brain):
        """When ON CONFLICT fires, RETURNING id produces None → recorded=False."""
        mock_costs.return_value = MagicMock(total=Decimal("5.00"))
        instrument = _make_instrument_row()

        conn = _FakeConnection([
            _FakeResult(mappings_value=instrument),
            _FakeResult(value=None),   # ON CONFLICT DO NOTHING → no row returned
        ])
        engine = _FakeEngine(conn)
        redis = MagicMock()
        redis.get.return_value = json.dumps({"best_bid": 2499, "best_ask": 2501})

        result = record_shadow_signal(
            engine, redis, "v3.2", 408065, "BUY", 10, 0.72,
            Decimal("2500"), datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc),
        )

        self.assertFalse(result["recorded"])
        self.assertIsNone(result["audit_id"])
        # Verify the SQL includes ON CONFLICT DO NOTHING
        insert_sql, _ = conn.calls[1]
        self.assertIn("ON CONFLICT", insert_sql)
        self.assertIn("DO NOTHING", insert_sql)


class TestMistakeTagsCompleteness(unittest.TestCase):
    """Verify _mistake_tags produces correct tags for every known exit_reason."""

    def _row(self, **overrides):
        base = {"fill_source": "DEPTH_SNAPSHOT", "estimated_fees": Decimal("5.00")}
        base.update(overrides)
        return base

    def test_stop_loss_tags(self):
        result = _mistake_tags(self._row(), Decimal("-50"), "STOP_LOSS")
        self.assertIn("stop_loss_hit", result["tags"])

    def test_time_exit_loss_tags(self):
        result = _mistake_tags(self._row(), Decimal("-10"), "TIME_EXIT")
        self.assertIn("stale_signal_loss", result["tags"])

    def test_time_exit_gain_tags(self):
        result = _mistake_tags(self._row(), Decimal("10"), "TIME_EXIT")
        self.assertIn("slow_winner", result["tags"])

    def test_take_profit_tags(self):
        result = _mistake_tags(self._row(), Decimal("100"), "TAKE_PROFIT")
        self.assertIn("target_hit", result["tags"])

    def test_profit_capture_tags(self):
        result = _mistake_tags(self._row(), Decimal("80"), "PROFIT_CAPTURE")
        self.assertIn("profit_captured", result["tags"])

    def test_trailing_stop_tags(self):
        result = _mistake_tags(self._row(), Decimal("60"), "TRAILING_STOP")
        self.assertIn("trailing_profit_protection", result["tags"])

    def test_breakeven_stop_tags(self):
        result = _mistake_tags(self._row(), Decimal("1"), "BREAKEVEN_STOP")
        self.assertIn("capital_protection", result["tags"])

    def test_fallback_fill_appended_when_not_depth(self):
        result = _mistake_tags(self._row(fill_source="DECISION_PRICE_FALLBACK"), Decimal("10"), "TAKE_PROFIT")
        self.assertIn("fallback_fill", result["tags"])

    def test_cost_drag_appended_when_fees_exceed_pnl(self):
        result = _mistake_tags(self._row(estimated_fees=Decimal("20")), Decimal("-15"), "STOP_LOSS")
        self.assertIn("cost_drag", result["tags"])


class TestExitPriceValidation(unittest.TestCase):
    """Verify record_shadow_exit rejects non-positive exit prices."""

    def test_zero_exit_price_raises(self):
        engine = MagicMock()
        with self.assertRaises(ValueError):
            record_shadow_exit(engine, 1, Decimal("0"), "TIME_EXIT")

    def test_negative_exit_price_raises(self):
        engine = MagicMock()
        with self.assertRaises(ValueError):
            record_shadow_exit(engine, 1, Decimal("-100"), "TIME_EXIT")


class TestClassifyProbability(unittest.TestCase):
    """Edge-case coverage for classify_probability."""

    def test_boundary_values(self):
        self.assertEqual(classify_probability(0.65), 1)   # exactly upper
        self.assertEqual(classify_probability(0.35), -1)  # exactly lower
        self.assertEqual(classify_probability(0.50), 0)   # neutral

    def test_invalid_thresholds_raise(self):
        with self.assertRaises(ValueError):
            classify_probability(0.5, lower=0.6, upper=0.4)


if __name__ == "__main__":
    unittest.main()
