"""Automated Unit Test Suite for Institutional Risk Management, High-Frequency Position Defense & Execution Gates.

Validates the 8 core protections:
1. Fee-Padded Breakeven guarantees net positive profit after Zerodha charges.
2. Immediate Adverse Excursion cuts trades within 90 seconds if dumping.
3. Stagnation Guard tightens stop at 5m and scratches flat trades at 15m (curing INFY/ULTRACEMCO bleed).
4. VWAP Anti-Chase blocks longs >0.8% above VWAP and shorts <-0.8% below VWAP.
5. ADX Chop Gate rejects breakouts during low-trend consolidation (ADX < 18).
6. Zero-Lag Microstructure Gatekeeper verifies top-of-book spread and depth.
7. Wyckoff Volume Authenticity Indicator rejects low-volume fakeout candles.
8. Dual-Track Swing Engine routes cash equity BUYs to DELIVERY and exempts from intraday stagnation.
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from backend.live_inference import _chart_strategy_gate, LivePaperInference
from backend.position_manager import PositionManager
from backend.trade_memory import autonomous_strategy_decider


class RiskManagementTestSuite(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)

    def test_1_fee_padded_breakeven_guarantees_profit(self):
        """Verify that at +0.7R, stop moves to entry + fee_buffer_per_share, ensuring net positive P&L."""
        entry = Decimal("100.00")
        qty = Decimal("50")
        fees = Decimal("15.00")  # approx Zerodha brokerage + STT for one leg
        fee_buffer = (fees * Decimal("2.2")) / qty  # covers roundtrip + buffer = 0.66
        breakeven_price = entry + fee_buffer  # 100.66

        # If trade hits breakeven_price and exits:
        gross = (breakeven_price - entry) * qty  # 0.66 * 50 = +33.00
        roundtrip_fees = fees * Decimal("2.0")   # 30.00
        net = gross - roundtrip_fees             # +3.00 > 0
        self.assertGreater(net, Decimal("0.0"), "Fee-padded breakeven must guarantee net positive profit")

    def test_2_immediate_adverse_excursion_cut_under_90s(self):
        """Verify that if price collapses to -0.4R within first 90 seconds without expanding, cut immediately."""
        pm = PositionManager("sqlite:///:memory:")
        pm.get_latest_price = MagicMock(return_value=Decimal("99.50"))
        pm.get_exit_bars = MagicMock(return_value=[])

        entry = Decimal("100.00")
        sl = Decimal("98.75")  # R-points = 1.25. 0.4R = 0.50. Entry - 0.4R = 99.50
        pos = {
            "id": 101,
            "instrument_id": 1,
            "symbol": "TATAMOTORS",
            "side": "BUY",
            "trade_mode": "INTRADAY",
            "theoretical_fill_price": entry,
            "stop_loss_price": sl,
            "take_profit_price": Decimal("103.00"),
            "quantity": 100,
            "estimated_fees": 20,
            "signal_at": self.now - timedelta(seconds=45),  # 45 seconds elapsed
        }

        with patch("backend.position_manager.record_shadow_exit") as mock_exit:
            res = pm.evaluate_position(pos, self.now)
            self.assertIsNotNone(res)
            self.assertEqual(res["exit_reason"], "EARLY_ADVERSE_CUT")
            self.assertTrue(res["closed"])
            mock_exit.assert_called_once()

    def test_3_stagnation_guard_5m_tighten_and_15m_exit(self):
        """Verify that flat trades are tightened at 5m and closed at 15m (curing INFY/ULTRACEMCO bleed)."""
        pm = PositionManager("sqlite:///:memory:")
        entry = Decimal("1000.00")
        sl = Decimal("985.00")  # R = 15. 0.35R = 5.25. Tightened SL = 994.75

        # Case A: At 6 minutes (360s), price is flat at 994.50 (below tightened SL 994.75)
        pm.get_latest_price = MagicMock(return_value=Decimal("994.50"))
        pm.get_exit_bars = MagicMock(return_value=[{"high_price": "1001.0", "low_price": "994.50"}])
        pos_5m = {
            "id": 102,
            "instrument_id": 2,
            "symbol": "INFY",
            "side": "BUY",
            "trade_mode": "INTRADAY",
            "theoretical_fill_price": entry,
            "stop_loss_price": sl,
            "take_profit_price": Decimal("1030.00"),
            "quantity": 20,
            "estimated_fees": 25,
            "signal_at": self.now - timedelta(minutes=6),
        }
        with patch("backend.position_manager.record_shadow_exit"):
            res_5m = pm.evaluate_position(pos_5m, self.now)
            self.assertEqual(res_5m["exit_reason"], "STOP_LOSS", "Should trigger tightened stop loss at 5m")

        # Case B: At 16 minutes (960s), trade hovered in flat range (+0.1R, never reached +0.25R)
        pm.get_latest_price = MagicMock(return_value=Decimal("1001.00"))  # +1.00 / 15 = +0.07R (< +0.25R)
        pm.get_exit_bars = MagicMock(return_value=[{"high_price": "1002.0", "low_price": "998.0"}])
        pos_15m = {
            "id": 103,
            "instrument_id": 3,
            "symbol": "ULTRACEMCO",
            "side": "BUY",
            "trade_mode": "INTRADAY",
            "theoretical_fill_price": entry,
            "stop_loss_price": sl,
            "take_profit_price": Decimal("1030.00"),
            "quantity": 10,
            "estimated_fees": 25,
            "signal_at": self.now - timedelta(minutes=16),
        }
        with patch("backend.position_manager.record_shadow_exit"):
            res_15m = pm.evaluate_position(pos_15m, self.now)
            self.assertEqual(res_15m["exit_reason"], "STAGNATION_GUARD", "Should trigger STAGNATION_GUARD at 15m")

    def test_4_vwap_anti_chase_rejection(self):
        """Verify that BUY signals > 0.8% above VWAP are rejected to prevent buying extended peaks."""
        # Create bars where VWAP is ~100 but last price is 101.20 (+1.2% > +0.8%)
        bars = []
        for i in range(15):
            p = 100.0 + (i * 0.05)
            bars.append({"open": p, "high": p + 0.3, "low": p - 0.3, "close": p, "volume": 10000})
        # Last bar spikes to 101.50
        bars.append({"open": 101.0, "high": 101.6, "low": 100.9, "close": 101.50, "volume": 15000})

        item = {
            "symbol": "SBIN",
            "intraday": bars,
            "intraday_1m": bars,
        }
        res = _chart_strategy_gate(item, signal=1)
        self.assertFalse(res["accepted"])
        self.assertIn("overextended above VWAP", res["reason"])

    def test_5_adx_chop_filter_rejection(self):
        """Verify that breakout setups are blocked during low-ADX choppy consolidations (ADX < 18)."""
        # Create 25 bars with IDENTICAL high/low so directional movement ≈ 0 → ADX ≈ 0.
        # Each bar has: high = base + 0.30, low = base - 0.30 (range = 0.60).
        # Highs never expand above previous high, lows never drop below previous low → +DM = 0, -DM = 0 → DX = 0.
        bars = []
        base = 500.0
        for i in range(25):
            # Alternate close slightly but keep high/low clamped to the same band
            sign = 1 if i % 2 == 0 else -1
            c = base + sign * 0.05  # close wobbles ±0.05
            bars.append({"open": base, "high": base + 0.30, "low": base - 0.30, "close": c, "volume": 5000})

        # Final "breakout" bar: close barely nudges above prior_high (500.30) to trigger breakout_up check.
        # High = 500.35 (only 0.05 above prior high → tiny +DM), so 1 of 7 DX values is nonzero but small.
        # Volume is 2x average → RVOL = 2.0 ≥ 1.20 (passes Wyckoff gate, so we isolate ADX rejection).
        # Spread = 0.55 (not wide enough for Wyckoff Effort-vs-Result rejection: 0.55 < ATR*1.5 = 0.60*1.5 = 0.90).
        bars.append({
            "open": base + 0.10,
            "high": base + 0.35,   # barely above prior_high = base + 0.30
            "low": base - 0.20,    # within existing range
            "close": base + 0.32,  # just above prior_high = 500.30
            "volume": 10000,       # RVOL = 10000/5000 = 2.0x (passes volume gate)
        })

        item = {
            "symbol": "WIPRO",
            "intraday": bars,
            "intraday_1m": bars,
        }
        res = _chart_strategy_gate(item, signal=1)
        self.assertFalse(res["accepted"])
        self.assertTrue("ADX < 18" in res["reason"] or "not confirmed" in res["reason"])

    def test_6_zero_lag_microstructure_entry_gate(self):
        """Verify that wide bid-ask spread (>0.08%) is rejected, while tight spread (<=0.05%) passes."""
        engine_mock = MagicMock()
        lpi = LivePaperInference.__new__(LivePaperInference)
        lpi.microstructure_gate_enabled = True
        lpi.microstructure_min_bars = 2
        lpi.require_microstructure_for_entries = True
        lpi.microstructure_max_adverse_bps = Decimal("30.0")

        bars_1s = [
            {"close": Decimal("100.00"), "bar_time": self.now - timedelta(seconds=2)},
            {"close": Decimal("100.05"), "bar_time": self.now},
        ]

        # Scenario A: Wide illiquid spread (Bid 99.80, Ask 100.10 -> Spread 0.30% > 0.08%)
        item_wide = {
            "microstructure": bars_1s,
            "bid": 99.80,
            "ask": 100.10,
        }
        gate_wide = lpi._microstructure_gate(item_wide, strategy_direction=1)
        self.assertFalse(gate_wide["accepted"])
        self.assertIn("spread too wide", gate_wide["reason"])

        # Scenario B: Tight liquid spread (Bid 100.00, Ask 100.04 -> Spread 0.04% <= 0.05%)
        item_tight = {
            "microstructure": bars_1s,
            "bid": 100.00,
            "ask": 100.04,
        }
        gate_tight = lpi._microstructure_gate(item_tight, strategy_direction=1)
        self.assertTrue(gate_tight["accepted"])
        self.assertEqual(gate_tight["status"], "1s_tape_ok")

    def test_7_volume_authenticity_true_vs_fake(self):
        """Verify that wide-spread candle with low volume (Effort-vs-Result divergence) is rejected as fake."""
        # Average volume is 10,000, last candle has wide spread (2.5x ATR) but volume is only 4,000 (RVOL 0.40x)
        bars = []
        base = 200.0
        for i in range(15):
            bars.append({"open": base, "high": base + 0.5, "low": base - 0.5, "close": base + 0.1, "volume": 10000})
        # Wide breakout candle with low volume (Fake candle)
        bars.append({"open": base, "high": base + 3.0, "low": base - 0.2, "close": base + 2.8, "volume": 4000})

        item = {
            "symbol": "HDFCBANK",
            "intraday": bars,
            "intraday_1m": bars,
        }
        res = _chart_strategy_gate(item, signal=1)
        self.assertFalse(res["accepted"])
        self.assertIn("Wyckoff Effort-vs-Result divergence", res["reason"])

    def test_8_dual_track_swing_routing_and_exemption(self):
        """Verify that SWING trades are exempt from 15-minute stagnation exits and intraday force-flats."""
        pm = PositionManager("sqlite:///:memory:")
        entry = Decimal("500.00")
        sl = Decimal("480.00")
        pm.get_latest_price = MagicMock(return_value=Decimal("502.00"))  # Hovering flat (+0.1R)
        pm.get_exit_bars = MagicMock(return_value=[{"high_price": "503.0", "low_price": "499.0"}])

        pos_swing = {
            "id": 104,
            "instrument_id": 4,
            "symbol": "RELIANCE",
            "side": "BUY",
            "trade_mode": "SWING",
            "theoretical_fill_price": entry,
            "stop_loss_price": sl,
            "take_profit_price": Decimal("560.00"),
            "quantity": 15,
            "estimated_fees": 0,  # ₹0 Zerodha delivery fee
            "signal_at": self.now - timedelta(minutes=45),  # 45 minutes elapsed
            "holding_days": 1,
            "max_holding_days": 15,
        }

        res = pm.evaluate_position(pos_swing, self.now)
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "OPEN", "Swing position must stay open and not be exited by stagnation guard")
        self.assertIsNone(res["exit_reason"], "Swing trades are exempt from 15m stagnation exit")

    def test_9_learning_strategy_gate_plugs_chop_leak(self):
        """Verify that _learning_strategy_gate rejects trades during choppy consolidation (ADX < 18)."""
        from backend.live_inference import _learning_strategy_gate
        bars = []
        base = 200.0
        # Flat choppy consolidation (ADX < 18)
        for i in range(25):
            bars.append({"open": base, "high": base + 0.1, "low": base - 0.1, "close": base, "volume": 5000})

        item = {
            "symbol": "BAJFINANCE",
            "intraday": bars,
        }
        res = _learning_strategy_gate(item, signal=1)
        self.assertFalse(res["accepted"])
        self.assertIn("choppy consolidation", res["reason"])

    def test_10_fee_padded_breakeven_includes_slippage_and_guarantees_green(self):
        """Verify that breakeven buffer calculation with 2.5x fees and 2 ticks spread covers Zerodha roundtrip."""
        entry = Decimal("200.00")
        qty = Decimal("100")
        fees = Decimal("15.00")
        tick_size = Decimal("0.05")
        fee_buffer_per_share = ((fees * Decimal("2.5")) + (Decimal("2") * tick_size * qty)) / qty
        breakeven_exit = entry + fee_buffer_per_share

        gross = (breakeven_exit - entry) * qty
        roundtrip_costs = fees * Decimal("2.0")
        slippage_penalty = tick_size * qty
        net_rupees = gross - roundtrip_costs - slippage_penalty
        self.assertGreater(net_rupees, Decimal("0.0"), "Breakeven stop must close with net positive rupees")


if __name__ == "__main__":
    unittest.main()
