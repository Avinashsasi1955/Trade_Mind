"""Tests for chart provenance and data_mode classification.

Ensures synthetic 1-second proxy bars (upstox_rest_1s_proxy) are never
misrepresented as genuine provider live microstructure, and verifies truthful
source labeling across all chart timeframes and source combinations.
"""

from __future__ import annotations

import unittest
from backend.charting import determine_chart_provenance


class TestChartProvenance(unittest.TestCase):
    def test_pure_live_1second_microstructure(self):
        """Genuine provider live 1s ticks must be labeled provider_live_1second_microstructure."""
        candles = [
            {"time": "2026-10-15T10:00:00+05:30", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 50, "source": "upstox_v3"},
            {"time": "2026-10-15T10:00:01+05:30", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 80, "source": "upstox_v3"},
        ]
        res = determine_chart_provenance(candles, timeframe="1s", used_partial=False)
        self.assertEqual(res["data_mode"], "provider_live_1second_microstructure")
        self.assertTrue(res["source_summary"]["has_live_stream"])
        self.assertFalse(res["source_summary"]["has_synthetic_proxy"])
        self.assertEqual(res["source_summary"]["primary_source_type"], "live_stream")

    def test_synthetic_1second_proxy_labeling(self):
        """Synthetic proxy bars must NEVER be labeled provider_live_1second_microstructure."""
        candles = [
            {"time": "2026-10-15T10:00:00+05:30", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 50, "source": "upstox_rest_1s_proxy"},
            {"time": "2026-10-15T10:00:01+05:30", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 80, "source": "upstox_rest_1s_proxy"},
        ]
        res = determine_chart_provenance(candles, timeframe="1s", used_partial=False)
        self.assertEqual(res["data_mode"], "synthetic_1second_rest_proxy")
        self.assertFalse(res["source_summary"]["has_live_stream"])
        self.assertTrue(res["source_summary"]["has_synthetic_proxy"])
        self.assertEqual(res["source_summary"]["primary_source_type"], "synthetic_proxy")
        # Critical assertion: must NOT claim to be live microstructure
        self.assertNotEqual(res["data_mode"], "provider_live_1second_microstructure")

    def test_mixed_live_and_proxy_1second(self):
        """A mixture of live stream and synthetic proxy bars must be labeled mixed_live_and_proxy_1second."""
        candles = [
            {"time": "2026-10-15T10:00:00+05:30", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 50, "source": "upstox_rest_1s_proxy"},
            {"time": "2026-10-15T10:00:01+05:30", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 80, "source": "upstox_v3"},
        ]
        res = determine_chart_provenance(candles, timeframe="1s", used_partial=False)
        self.assertEqual(res["data_mode"], "mixed_live_and_proxy_1second")
        self.assertTrue(res["source_summary"]["has_live_stream"])
        self.assertTrue(res["source_summary"]["has_synthetic_proxy"])

    def test_rest_historical_1minute_bars(self):
        """REST-repaired 1m candles must be labeled provider_rest_completed_bars."""
        candles = [
            {"time": "2026-10-15T10:00:00+05:30", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 500, "source": "upstox_rest_intraday"},
            {"time": "2026-10-15T10:01:00+05:30", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 800, "source": "upstox_rest_intraday"},
        ]
        res = determine_chart_provenance(candles, timeframe="1m", used_partial=False)
        self.assertEqual(res["data_mode"], "provider_rest_completed_bars")
        self.assertTrue(res["source_summary"]["has_rest_repair"])
        self.assertFalse(res["source_summary"]["has_live_stream"])
        self.assertEqual(res["source_summary"]["primary_source_type"], "rest_repair")

    def test_live_forming_candle_precedence(self):
        """When used_partial is True, data_mode must reflect forming candle regardless of underlying sources."""
        candles = [
            {"time": "2026-10-15T10:00:00+05:30", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 500, "source": "upstox_v3"},
            {"time": "2026-10-15T10:01:00+05:30", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 800, "source": "live_forming"},
        ]
        res = determine_chart_provenance(candles, timeframe="1m", used_partial=True)
        self.assertEqual(res["data_mode"], "provider_live_forming_candle")
        self.assertTrue(res["source_summary"]["has_live_stream"])


if __name__ == "__main__":
    unittest.main()
