"""Unit tests for Phase 2: Profit-Harvest Layer and Reversal Analysis Engine."""

import unittest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, text

from backend.profit_harvest import (
    calculate_progress,
    calculate_harvest_stop,
    compute_reversal_score,
)
from backend.position_manager import PositionManager
from backend.brains.counterfactual_replay import run_profit_harvest_ab_replay


class TestProfitHarvestMathAndLogic(unittest.TestCase):
    """Test progress calculation, monotonic stop tightening, and reversal components."""

    def test_calculate_progress_buy_and_sell(self):
        # Entry 100, Stop 90, Target 115 (1.5R move = 15 pts)
        entry = Decimal("100.0")
        target_buy = Decimal("115.0")

        # At 112.5 (1.25R out of 1.5R), progress is 12.5 / 15.0 = 0.8333...
        prog_buy = calculate_progress("BUY", entry, Decimal("112.5"), target_buy)
        self.assertAlmostEqual(prog_buy, 0.83333, places=4)

        # At 107.5 (0.75R out of 1.5R, 50% target progress)
        prog_50 = calculate_progress("BUY", entry, Decimal("107.5"), target_buy)
        self.assertAlmostEqual(prog_50, 0.50, places=4)

        # Sell side: Entry 100, Target 85 (15 pts down)
        target_sell = Decimal("85.0")
        prog_sell = calculate_progress("SELL", entry, Decimal("87.5"), target_sell)
        self.assertAlmostEqual(prog_sell, 0.83333, places=4)

    def test_calculate_harvest_stop_strictly_monotonic(self):
        entry = Decimal("100.0")
        price_buy = Decimal("112.5")  # +12.5 profit
        # 65% lock of 12.5 profit = +8.125 -> stop = 108.125
        current_sl = Decimal("105.0")
        new_sl = calculate_harvest_stop("BUY", entry, price_buy, current_sl, lock_fraction=0.65)
        self.assertEqual(new_sl, Decimal("108.1250"))

        # Monotonicity test: if existing SL was already higher (e.g. 110.0), it MUST NOT loosen
        higher_sl = Decimal("110.0")
        monotonic_sl = calculate_harvest_stop("BUY", entry, price_buy, higher_sl, lock_fraction=0.65)
        self.assertEqual(monotonic_sl, Decimal("110.0"))

        # SELL side: Entry 100, Price 87.5 (12.5 profit down)
        price_sell = Decimal("87.5")
        # 65% lock of 12.5 = -8.125 -> stop = 91.875
        current_sell_sl = Decimal("95.0")
        new_sell_sl = calculate_harvest_stop("SELL", entry, price_sell, current_sell_sl, lock_fraction=0.65)
        self.assertEqual(new_sell_sl, Decimal("91.8750"))

        # SELL Monotonicity: if existing stop was tighter (e.g. 90.0), it MUST NOT loosen up
        tighter_sell_sl = Decimal("90.0")
        monotonic_sell_sl = calculate_harvest_stop("SELL", entry, price_sell, tighter_sell_sl, lock_fraction=0.65)
        self.assertEqual(monotonic_sell_sl, Decimal("90.0"))

    def test_reversal_score_rejection_wick(self):
        # Candle with huge upper wick (> 2x body) and high volume
        bars_5m = [
            {"open": 100.0, "high": 105.0, "low": 99.5, "close": 104.5, "volume": 1000},
            {"open": 104.5, "high": 108.0, "low": 104.0, "close": 107.5, "volume": 1000},
            # Rejection bar: open 107.5, high 112.5, close 108.0 (body = 0.5, upper wick = 4.5 > 2*body), vol = 2500
            {"open": 107.5, "high": 112.5, "low": 107.0, "close": 108.0, "volume": 2500},
        ]
        res = compute_reversal_score(bars_5m=bars_5m, side="BUY")
        self.assertTrue(res["components"]["rejection_wick"]["active"])
        self.assertGreaterEqual(res["components"]["rejection_wick"]["score"], 20.0)

    def test_reversal_score_vwap_and_structure_exit(self):
        # Consecutive breakdown bars crossing VWAP with breakdown below prior swing low
        bars_5m = [
            {"open": 110.0, "high": 112.0, "low": 109.5, "close": 111.5, "volume": 1000},
            {"open": 111.5, "high": 113.0, "low": 111.0, "close": 112.5, "volume": 1000},
            {"open": 112.5, "high": 112.5, "low": 108.0, "close": 108.5, "volume": 3000},  # Sharp breakdown
        ]
        # VWAP at 111.0, latest close at 108.5 (below VWAP & prior low 109.5)
        res = compute_reversal_score(
            bars_5m=bars_5m,
            side="BUY",
            vwap=111.0,
            index_regime_against=True,
            exit_threshold=70.0,
            tighten_threshold=40.0,
        )
        self.assertTrue(res["components"]["structure_break"]["active"])
        self.assertTrue(res["components"]["vwap_ema_cross"]["active"])
        self.assertTrue(res["components"]["regime_flip"]["active"])
        self.assertGreaterEqual(res["reversal_score"], 40.0)

    def test_reversal_score_options_iv_decay(self):
        bars_5m_call = [
            {"open": 49.0, "high": 50.5, "low": 48.5, "close": 50.0, "volume": 400},
            {"open": 50.0, "high": 52.0, "low": 49.0, "close": 51.0, "volume": 500},
            {"open": 51.0, "high": 51.5, "low": 47.0, "close": 48.0, "volume": 800},  # Premium dropped -5.8%
        ]
        underlying_bars = [
            {"open": 24500.0, "high": 24520.0, "low": 24490.0, "close": 24510.0, "volume": 5000},
            {"open": 24510.0, "high": 24530.0, "low": 24505.0, "close": 24515.0, "volume": 5000},  # Underlying flat/up
        ]
        res = compute_reversal_score(
            bars_5m=bars_5m_call,
            side="BUY",
            is_option=True,
            underlying_bars=underlying_bars,
        )
        self.assertTrue(res["components"]["option_iv_decay"]["active"])
        self.assertEqual(res["components"]["option_iv_decay"]["score"], 15.0)


class TestPositionManagerProfitHarvestIntegration(unittest.TestCase):
    """Test PositionManager handling of harvest triggers, flags, and exits."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE shadow_execution_audits (
                    id INTEGER PRIMARY KEY,
                    instrument_id INTEGER,
                    side TEXT,
                    quantity INTEGER,
                    theoretical_fill_price NUMERIC,
                    stop_loss_price NUMERIC,
                    take_profit_price NUMERIC,
                    signal_at TIMESTAMP,
                    exit_at TIMESTAMP,
                    exit_price NUMERIC,
                    realised_exit_price NUMERIC,
                    exit_reason TEXT,
                    net_pnl NUMERIC,
                    mistake_tags TEXT,
                    audit_status TEXT DEFAULT 'RECONCILED',
                    trade_mode TEXT DEFAULT 'INTRADAY',
                    holding_days INTEGER DEFAULT 0,
                    max_holding_days INTEGER DEFAULT 15,
                    estimated_fees NUMERIC DEFAULT 0,
                    improvement_note TEXT,
                    reasoning_chain TEXT
                );
            """))

        self.pm = PositionManager.__new__(PositionManager)
        self.pm.engine = self.engine
        self.pm.redis = None
        self.pm.breakeven_trigger_r = Decimal("1.10")
        self.pm.profit_lock_trigger_r = Decimal("1.60")
        self.pm.profit_lock_guaranteed_r = Decimal("1.00")
        self.pm.trailing_trigger_r = Decimal("1.50")
        self.pm.trailing_giveback_r = Decimal("0.40")
        self.pm.adverse_cut_window_seconds = 90.0
        self.pm.adverse_cut_threshold_r = Decimal("0.40")
        self.pm.stagnation_tighten_seconds = 300.0
        self.pm.stagnation_tighten_r = Decimal("0.35")
        self.pm.stagnation_scratch_seconds = 900.0
        self.pm.stagnation_min_expansion_r = Decimal("0.25")
        self.pm.force_flat_time = "15:15"
        self.pm.price_max_age_seconds = 180
        self.pm.price_max_age_swing_seconds = 1800
        self.pm.stale_data_exit_minutes = 30
        self.pm.stale_open_grace_seconds = 120
        self.pm.stale_positions_tracker = {}
        self.pm.get_exit_bars = MagicMock(return_value=[])

        # Profit-harvest settings
        self.pm.harvest_trigger = 0.83
        self.pm.harvest_exit_threshold = 70.0
        self.pm.harvest_tighten_threshold = 40.0
        self.pm.harvest_lock_fraction = 0.65

    def tearDown(self):
        self.engine.dispose()

    @patch("backend.position_manager.record_shadow_exit")
    @patch("backend.position_manager.is_market_hours", return_value=True)
    def test_profit_harvest_flag_off_does_not_force_exit(self, mock_mkt, mock_exit):
        """When NIVESH_PROFIT_HARVEST_ENABLED=0 (default), score >= 70 records info but does NOT exit."""
        self.pm.profit_harvest_enabled = False

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (id, instrument_id, side, theoretical_fill_price, stop_loss_price, take_profit_price, net_pnl)
                VALUES (101, 999, 'BUY', 100.0, 90.0, 115.0, NULL)
            """))

        pos = {
            "id": 101,
            "instrument_id": 999,
            "symbol": "TCS",
            "side": "BUY",
            "theoretical_fill_price": Decimal("100.0"),
            "stop_loss_price": Decimal("90.0"),    # 1R = 10 pts
            "take_profit_price": Decimal("115.0"),  # Target = 15 pts (1.5R)
            "quantity": 10,
            "signal_at": datetime.now(timezone.utc),
            "trade_mode": "INTRADAY",
            "improvement_note": "{}",
            "exchange": "NSE",
        }

        # Latest price at 112.5 (progress = 0.8333 >= 0.83)
        self.pm.get_latest_price = MagicMock(return_value=(Decimal("112.5"), "zerodha_kite"))
        self.pm._get_recent_5m_bars = MagicMock(return_value=[
            {"open": 110.0, "high": 113.0, "low": 108.0, "close": 108.5, "volume": 3000},
            {"open": 108.5, "high": 109.0, "low": 107.0, "close": 107.5, "volume": 3000},
            {"open": 107.5, "high": 108.0, "low": 106.0, "close": 106.5, "volume": 3000},
        ])
        self.pm._get_recent_underlying_bars = MagicMock(return_value=[])

        watermark = datetime.now(timezone.utc)
        eval_res = self.pm.evaluate_position(pos, watermark)

        self.assertIsNotNone(eval_res)
        self.assertIsNone(eval_res["exit_reason"])
        self.assertIn("profit_harvest", eval_res)
        self.assertIsNotNone(eval_res["profit_harvest"])
        self.assertFalse(eval_res["profit_harvest"]["enabled"])

    @patch("backend.position_manager.record_shadow_exit")
    @patch("backend.position_manager.is_market_hours", return_value=True)
    def test_profit_harvest_flag_on_triggers_reversal_exit(self, mock_mkt, mock_exit):
        """When NIVESH_PROFIT_HARVEST_ENABLED=1, reversal score >= 70 triggers PROFIT_HARVEST_REVERSAL_EXIT."""
        self.pm.profit_harvest_enabled = True

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (id, instrument_id, side, theoretical_fill_price, stop_loss_price, take_profit_price, net_pnl)
                VALUES (102, 999, 'BUY', 100.0, 90.0, 115.0, NULL)
            """))

        pos = {
            "id": 102,
            "instrument_id": 999,
            "symbol": "TCS",
            "side": "BUY",
            "theoretical_fill_price": Decimal("100.0"),
            "stop_loss_price": Decimal("90.0"),
            "take_profit_price": Decimal("115.0"),
            "quantity": 10,
            "signal_at": datetime.now(timezone.utc),
            "trade_mode": "INTRADAY",
            "improvement_note": "{}",
            "exchange": "NSE",
        }

        self.pm.get_latest_price = MagicMock(return_value=(Decimal("112.5"), "zerodha_kite"))
        self.pm._get_recent_5m_bars = MagicMock(return_value=[
            {"open": 108.0, "high": 111.0, "low": 107.5, "close": 110.5, "volume": 1000},
            {"open": 110.5, "high": 113.0, "low": 110.0, "close": 112.0, "volume": 1000},
            {"open": 112.0, "high": 115.0, "low": 107.0, "close": 107.5, "volume": 5000},
        ])
        self.pm._get_recent_underlying_bars = MagicMock(return_value=[])

        with patch("backend.position_manager.compute_reversal_score", return_value={
            "reversal_score": 85.0,
            "action": "EXIT",
            "components": {},
            "reasons": ["massive rejection wick", "structure breakdown"],
        }):
            watermark = datetime.now(timezone.utc)
            eval_res = self.pm.evaluate_position(pos, watermark)

            self.assertIsNotNone(eval_res)
            self.assertEqual(eval_res["exit_reason"], "PROFIT_HARVEST_REVERSAL_EXIT")
            self.assertEqual(eval_res["exit_price"], Decimal("112.5"))

    @patch("backend.position_manager.record_shadow_exit")
    @patch("backend.position_manager.is_market_hours", return_value=True)
    def test_profit_harvest_flag_on_tightens_stop_loss(self, mock_mkt, mock_exit):
        """When NIVESH_PROFIT_HARVEST_ENABLED=1 and action == TIGHTEN, stop is tightened to lock 65% profit."""
        self.pm.profit_harvest_enabled = True

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (id, instrument_id, side, theoretical_fill_price, stop_loss_price, take_profit_price, net_pnl)
                VALUES (103, 999, 'BUY', 100.0, 90.0, 115.0, NULL)
            """))

        pos = {
            "id": 103,
            "instrument_id": 999,
            "symbol": "TCS",
            "side": "BUY",
            "theoretical_fill_price": Decimal("100.0"),
            "stop_loss_price": Decimal("90.0"),
            "take_profit_price": Decimal("115.0"),
            "quantity": 10,
            "signal_at": datetime.now(timezone.utc),
            "trade_mode": "INTRADAY",
            "improvement_note": "{}",
            "exchange": "NSE",
        }

        # Price reaches 112.5 (+12.5 open profit)
        self.pm.get_latest_price = MagicMock(return_value=(Decimal("112.5"), "zerodha_kite"))
        self.pm._get_recent_5m_bars = MagicMock(return_value=[
            {"open": 108.0, "high": 111.0, "low": 107.5, "close": 110.5, "volume": 1000},
            {"open": 110.5, "high": 113.0, "low": 110.0, "close": 112.0, "volume": 1000},
            {"open": 112.0, "high": 113.0, "low": 111.0, "close": 111.5, "volume": 1000},
        ])
        self.pm._get_recent_underlying_bars = MagicMock(return_value=[])

        # Action is TIGHTEN (score = 55.0)
        with patch("backend.position_manager.compute_reversal_score", return_value={
            "reversal_score": 55.0,
            "action": "TIGHTEN",
            "components": {},
            "reasons": ["vwap cross", "rsi rollover"],
        }):
            watermark = datetime.now(timezone.utc)
            eval_res = self.pm.evaluate_position(pos, watermark)

            self.assertIsNotNone(eval_res)
            self.assertIsNone(eval_res["exit_reason"])  # Position still open
            # Tightened SL should be entry (100.0) + 0.65 * 12.5 = 108.125
            self.assertEqual(eval_res["stop_loss_price"], 108.125)


class TestProfitHarvestCounterfactualReplay(unittest.TestCase):
    """Test A/B counterfactual replay engine and bootstrap confidence interval."""

    def test_counterfactual_replay_bootstrap_and_recommendation(self):
        sample_trades = [
            {
                "symbol": "RELIANCE",
                "side": "BUY",
                "entry": 2500.0,
                "initial_sl": 2480.0,     # 1R = 20 pts
                "target_price": 2530.0,   # 1.5R = 30 pts (progress 1.25R = +25 pts -> 2525.0)
                "max_favorable_price": 2526.0,  # progress = 26/30 = 0.867 >= 0.83
                "reversal_score": 80.0,         # Exits near 2525
                "reversal_price": 2524.0,
                "exit_price": 2485.0,           # Baseline roundtripped back to near SL (+0.25R)
            },
            {
                "symbol": "INFY",
                "side": "BUY",
                "entry": 1500.0,
                "initial_sl": 1485.0,     # 1R = 15 pts
                "target_price": 1522.5,   # 1.5R = 22.5 pts
                "max_favorable_price": 1520.0,  # progress = 20/22.5 = 0.888 >= 0.83
                "reversal_score": 50.0,         # Tightens stop to lock 65% profit: 1500 + 0.65*20 = 1513.0
                "exit_price": 1502.0,           # Baseline fell back to near entry
            },
            {
                "symbol": "HDFCBANK",
                "side": "BUY",
                "entry": 1600.0,
                "initial_sl": 1580.0,
                "target_price": 1630.0,
                "max_favorable_price": 1610.0,  # progress = 10/30 = 0.33 < 0.83 (did not trigger)
                "reversal_score": 20.0,
                "exit_price": 1605.0,
            },
        ] * 10  # 30 trades

        res = run_profit_harvest_ab_replay(
            trades=sample_trades,
            harvest_trigger=0.83,
            exit_threshold=70.0,
            tighten_threshold=40.0,
            lock_fraction=0.65,
            n_bootstraps=500,
        )

        self.assertEqual(res["total_trades"], 30)
        self.assertEqual(res["trades_reaching_harvest"], 20)
        self.assertGreater(res["trades_improved"], 0)
        self.assertGreater(res["delta_mean_r"], 0.0)
        ci_lower, ci_upper = res["ci_95"]
        self.assertLessEqual(ci_lower, ci_upper)
        self.assertIn(res["recommendation"], ("ENABLE", "NEUTRAL"))


if __name__ == "__main__":
    unittest.main()
