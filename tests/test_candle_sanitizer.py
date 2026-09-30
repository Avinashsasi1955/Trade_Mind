"""Unit tests for TickSanitizer and BarInvariantValidator."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from backend.candle_sanitizer import BarInvariantValidator, TickSanitizer


class TestCandleSanitizer(unittest.TestCase):
    def setUp(self):
        self.sanitizer = TickSanitizer(equity_jump_threshold=0.03, option_jump_threshold=0.20, max_stale_seconds=3.0)
        self.now = datetime.now(timezone.utc)

    def test_non_positive_price(self):
        valid, reason = self.sanitizer.validate_tick(1, Decimal("0"), self.now, 100)
        self.assertFalse(valid)
        self.assertEqual(reason, "NON_POSITIVE_PRICE")

        valid, reason = self.sanitizer.validate_tick(1, Decimal("-10"), self.now, 100)
        self.assertFalse(valid)

    def test_freak_tick_rejection(self):
        # Baseline tick: 100.0
        valid, _ = self.sanitizer.validate_tick(1, Decimal("100.0"), self.now, 100)
        self.assertTrue(valid)

        # Freak spike: 108.0 (+8% > 3% threshold)
        t1 = self.now + timedelta(seconds=1)
        valid, reason = self.sanitizer.validate_tick(1, Decimal("108.0"), t1, 120)
        self.assertFalse(valid)
        self.assertTrue("FREAK_TICK_SPIKE" in str(reason))

        # Normal small move: 100.50
        t2 = self.now + timedelta(seconds=2)
        valid, _ = self.sanitizer.validate_tick(1, Decimal("100.50"), t2, 130)
        self.assertTrue(valid)

    def test_stale_timestamp_rejection(self):
        # Initial tick
        self.sanitizer.validate_tick(2, Decimal("500.0"), self.now, 1000)

        # Stale tick older than 3 seconds
        stale_time = self.now - timedelta(seconds=5)
        valid, reason = self.sanitizer.validate_tick(2, Decimal("501.0"), stale_time, 1050)
        self.assertFalse(valid)
        self.assertTrue("STALE_OUT_OF_ORDER" in str(reason))

    def test_bar_invariant_sanitization(self):
        # Corrupted raw bar where high < max(open, close)
        corrupted_bar = {
            "instrument_id": 10,
            "interval": "1minute",
            "bar_time": self.now,
            "open": Decimal("100.0"),
            "close": Decimal("105.0"),
            "high": Decimal("102.0"),  # Invalid: less than close
            "low": Decimal("98.0"),
            "first_volume": 100,
            "last_volume": 250,
            "first_oi": 500,
            "last_oi": 600,
        }

        sanitized = BarInvariantValidator.sanitize_bar(corrupted_bar)
        self.assertGreaterEqual(sanitized["high"], Decimal("105.0"))
        self.assertLessEqual(sanitized["low"], Decimal("100.0"))
        self.assertEqual(sanitized["volume"], 150)
        self.assertEqual(sanitized["oi"], 600)
        self.assertEqual(sanitized["oi_change"], 100)


if __name__ == "__main__":
    unittest.main()
