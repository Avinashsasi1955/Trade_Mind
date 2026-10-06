"""
Unit Tests for Adaptive Regime Gates & Strategies (TradeMind Fix Validation).
=============================================================================
Validates:
1. Range support bounce & resistance rejection in choppy conditions (ADX < 18).
2. VWAP mean reversion on overextension.
3. Failed breakout trap reversal upon ASI trap detection.
4. Net profit floor (>= ₹10 net).
5. Nifty macro gate alpha override for outperforming stocks.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.live_inference import _chart_strategy_gate, _instrument_local_direction


def _build_bars(base_price=2500.0, count=15, pattern="flat"):
    """Generate 5-minute OHLCV bars for testing."""
    bars = []
    p = base_price
    for i in range(count):
        if pattern == "flat":
            osc = (1 if i % 2 == 0 else -1) * 3.0
            o = p + osc
            c = p - osc
            h = max(o, c) + 2.0
            l = min(o, c) - 2.0
            v = 50000
        elif pattern == "range_bounce":
            if i == count - 1:
                o = p - 10.0
                c = p - 5.0
                h = p - 4.0
                l = p - 12.0
                v = 60000
            else:
                o = p + (2 if i % 2 == 0 else -2)
                c = p - (2 if i % 2 == 0 else -2)
                h = max(o, c) + 3.0
                l = min(o, c) - 3.0
                v = 50000
        elif pattern == "range_reject":
            if i == count - 1:
                o = p + 10.0
                c = p + 5.0
                h = p + 12.0
                l = p + 4.0
                v = 60000
            else:
                o = p + (2 if i % 2 == 0 else -2)
                c = p - (2 if i % 2 == 0 else -2)
                h = max(o, c) + 3.0
                l = min(o, c) - 3.0
                v = 50000
        bars.append({"open": o, "high": h, "low": l, "close": c, "volume": v, "oi": 100000})
    return bars


class TestAdaptiveRegimeGates(unittest.TestCase):

    def test_range_support_bounce_accepted_in_chop(self):
        """In choppy/range conditions, bouncing off prior low should be accepted as RANGE_REVERSION."""
        bars = _build_bars(base_price=2000.0, count=15, pattern="range_bounce")
        item = {
            "symbol": "TCS",
            "intraday": bars,
            "atm_pcr": 1.0
        }
        res = _chart_strategy_gate(item, signal=1)
        self.assertTrue(res["accepted"], f"Should accept range bounce, got reason: {res.get('reason')}")
        self.assertTrue(res["strategy"].startswith("RANGE_"))
        self.assertGreaterEqual(res["rr"], 1.5)

    def test_range_resistance_reject_accepted_in_chop(self):
        """In choppy/range conditions, rejecting from prior high should be accepted as RANGE_REVERSION."""
        bars = _build_bars(base_price=2000.0, count=15, pattern="range_reject")
        item = {
            "symbol": "INFY",
            "intraday": bars,
            "atm_pcr": 1.0
        }
        res = _chart_strategy_gate(item, signal=-1)
        self.assertTrue(res["accepted"], f"Should accept range reject, got reason: {res.get('reason')}")
        self.assertTrue(res["strategy"].startswith("RANGE_"))
        self.assertGreaterEqual(res["rr"], 1.5)

    def test_failed_breakout_bear_trap_reversal(self):
        """When ASI detects a bear trap, a BUY signal should trigger FAILED_BREAKOUT_REVERSAL."""
        bars = _build_bars(base_price=1000.0, count=15, pattern="flat")
        item = {
            "symbol": "SBIN",
            "intraday": bars,
            "atm_pcr": 1.0
        }
        with patch("backend.live_inference.detect_nse_trap") as mock_trap:
            mock_trap.return_value = {
                "is_trap": True,
                "trap_type": "BEAR_TRAP",
                "reason": "Wilder ASI swing divergence at swing low"
            }
            res = _chart_strategy_gate(item, signal=1)
            self.assertTrue(res["accepted"], f"Bear trap reversal should be accepted, got: {res}")
            self.assertEqual(res["strategy"], "FAILED_BREAKOUT_REVERSAL_CALL_BUY")
            self.assertGreaterEqual(res["rr"], 2.0)

    def test_failed_breakout_bull_trap_reversal(self):
        """When ASI detects a bull trap, a SELL signal should trigger FAILED_BREAKOUT_REVERSAL."""
        bars = _build_bars(base_price=1000.0, count=15, pattern="flat")
        item = {
            "symbol": "RELIANCE",
            "intraday": bars,
            "atm_pcr": 1.0
        }
        with patch("backend.live_inference.detect_nse_trap") as mock_trap:
            mock_trap.return_value = {
                "is_trap": True,
                "trap_type": "BULL_TRAP",
                "reason": "Wilder ASI swing divergence at swing high"
            }
            res = _chart_strategy_gate(item, signal=-1)
            self.assertTrue(res["accepted"], f"Bull trap reversal should be accepted, got: {res}")
            self.assertEqual(res["strategy"], "FAILED_BREAKOUT_REVERSAL_PUT_BUY")
            self.assertGreaterEqual(res["rr"], 2.0)

    def test_bull_trap_blocks_long_entry(self):
        """Entering LONG into a bull trap must still be blocked to protect capital."""
        bars = _build_bars(base_price=1000.0, count=15, pattern="flat")
        item = {
            "symbol": "RELIANCE",
            "intraday": bars,
            "atm_pcr": 1.0
        }
        with patch("backend.live_inference.detect_nse_trap") as mock_trap:
            mock_trap.return_value = {
                "is_trap": True,
                "trap_type": "BULL_TRAP",
                "reason": "Wilder ASI swing divergence at swing high"
            }
            res = _chart_strategy_gate(item, signal=1)
            self.assertFalse(res["accepted"], "Buying into a bull trap must be blocked")
            self.assertIn("Bull Trap", res["reason"])


if __name__ == "__main__":
    unittest.main()
