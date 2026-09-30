"""Unit tests for Welles Wilder's ASI and NSE Trap Detection."""
from datetime import datetime, timedelta, timezone
import unittest

from backend.asi_indicator import calculate_asi, detect_nse_trap


class TestASIIndicator(unittest.TestCase):
    def test_asi_series_generation(self):
        candles = []
        base_time = datetime(2026, 9, 25, 9, 15, tzinfo=timezone.utc)
        for i in range(20):
            candles.append({
                "time": (base_time + timedelta(minutes=i * 5)).isoformat(),
                "open": 100.0 + i * 0.5,
                "high": 101.0 + i * 0.5,
                "low": 99.5 + i * 0.5,
                "close": 100.8 + i * 0.5,
                "volume": 5000,
            })

        asi = calculate_asi(candles)
        self.assertEqual(len(asi), len(candles))
        self.assertEqual(asi[0]["asi"], 0.0)
        # In a steady uptrend, accumulated ASI should be positive
        self.assertGreater(asi[-1]["asi"], 0.0)

    def test_bull_trap_detection(self):
        # Construct scenario: price reaches a high of 110, consolidates, then spikes to 111.5 on candle wick,
        # but ASI fails to reach the previous swing ASI (Bearish Divergence / Bull Trap).
        candles = []
        base_time = datetime(2026, 9, 25, 9, 15, tzinfo=timezone.utc)
        # 10 bars rallying to 110
        for i in range(10):
            candles.append({
                "time": (base_time + timedelta(minutes=i * 5)).isoformat(),
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.5 + i,
                "close": 100.5 + i,
                "volume": 5000,
            })
        # 5 bars pulling back to 107
        for i in range(5):
            candles.append({
                "time": (base_time + timedelta(minutes=(10 + i) * 5)).isoformat(),
                "open": 109.0 - i * 0.4,
                "high": 109.5 - i * 0.4,
                "low": 107.0 - i * 0.4,
                "close": 107.5 - i * 0.4,
                "volume": 3000,
            })
        # Final bar: Fake wick spikes to 112, but closes weak at 107.5 (classic liquidity sweep trap)
        candles.append({
            "time": (base_time + timedelta(minutes=15 * 5)).isoformat(),
            "open": 107.5,
            "high": 112.0,  # Sweeps prior high of 110.0
            "low": 107.0,
            "close": 107.5,  # Weak close
            "volume": 1000,
        })

        asi = calculate_asi(candles)
        trap = detect_nse_trap(candles, asi, lookback=12)

        self.assertTrue(trap["is_trap"])
        self.assertEqual(trap["trap_type"], "BULL_TRAP")
        self.assertEqual(trap["action"], "VETO_CALL_BUY")


if __name__ == "__main__":
    unittest.main()
