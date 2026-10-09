"""Unit tests for Phase 2b: Profit-Harvest Layer, Reversal Analysis Engine, and A/B Replay."""

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text

from backend.brains.counterfactual_replay import run_profit_harvest_ab_replay
from backend.position_manager import PositionManager, replay_trade_walk_forward
from backend.profit_harvest import (
    calculate_harvest_stop,
    calculate_progress,
    compute_reversal_score,
)


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

    def test_multi_component_safety_requires_two_components_for_exit(self):
        """Require >= 2 independent active components to trigger EXIT action; otherwise downgrade to TIGHTEN."""
        # Single component scenario: mock score >= 70 but only 1 component active
        bars_single = [
            {"open": 100.0, "high": 105.0, "low": 99.5, "close": 104.5, "volume": 1000},
            {"open": 104.5, "high": 108.0, "low": 104.0, "close": 107.5, "volume": 1000},
            {"open": 107.5, "high": 108.0, "low": 98.0, "close": 98.5, "volume": 1000},
        ]
        # Only structure break active (score = 20), exit_threshold set artificially low to 20
        res = compute_reversal_score(bars_5m=bars_single, side="BUY", exit_threshold=20.0, tighten_threshold=15.0)
        self.assertEqual(res["active_count"], 1)
        # Because active_count is 1, it must be downgraded to TIGHTEN
        self.assertEqual(res["action"], "TIGHTEN")
        self.assertTrue(any("downgraded" in r for r in res["reasons"]))

        # Two components active: structure break + regime flip -> triggers EXIT
        res2 = compute_reversal_score(
            bars_5m=bars_single,
            side="BUY",
            index_regime_against=True,
            exit_threshold=25.0,
            tighten_threshold=15.0,
        )
        self.assertGreaterEqual(res2["active_count"], 2)
        self.assertEqual(res2["action"], "EXIT")

    def test_tightened_ema_atr_margin(self):
        """EMA rule requires close beyond EMA9 by at least 0.1 ATR to prevent tripping on small pullbacks."""
        # Flat series where ATR is ~1.0
        bars_flat = [
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000}
            for _ in range(12)
        ]
        # Last bar closes at 99.98 (only 0.02 below EMA9 100.0, well within 0.1 ATR = 0.20 margin)
        bars_flat[-1] = {"open": 100.0, "high": 100.5, "low": 99.5, "close": 99.98, "volume": 1000}
        res_small = compute_reversal_score(bars_5m=bars_flat, side="BUY")
        self.assertFalse(res_small["components"]["vwap_ema_cross"]["active"])

        # Last bar closes at 99.50 (> 0.1 ATR below EMA9 100.0)
        bars_flat[-1] = {"open": 100.0, "high": 100.2, "low": 99.0, "close": 99.50, "volume": 1000}
        res_large = compute_reversal_score(bars_5m=bars_flat, side="BUY")
        self.assertTrue(res_large["components"]["vwap_ema_cross"]["active"])

    def test_tightened_rsi_swing_divergence(self):
        """RSI rule requires swing pivot divergence across prior highs or overbought exhaustion rollover."""
        # Uptrend creating higher high while RSI is lower
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 105.5, 105.0, 106.2, 106.5]
        bars = [
            {"open": c - 0.5, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000}
            for c in closes
        ]
        res = compute_reversal_score(bars_5m=bars, side="BUY")
        # Check components dictionary is properly populated
        self.assertIn("rsi_divergence", res["components"])

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
        self.pm._harvest_cache = {}

        # Profit-harvest settings
        self.pm.harvest_trigger = 0.83
        self.pm.harvest_exit_threshold = 70.0
        self.pm.harvest_tighten_threshold = 40.0
        self.pm.harvest_lock_fraction = 0.65

    def tearDown(self):
        self.engine.dispose()

    @patch("backend.position_manager.record_shadow_exit")
    @patch("backend.position_manager.is_market_hours", return_value=True)
    def test_flag_off_exact_phase_1d_exit_equivalence(self, mock_mkt, mock_exit):
        """Move 50%-progress breakeven inside flag check. With flag OFF, exit logic must be byte-for-byte Phase 1d."""
        # Entry 100.0, SL 90.0 (1R=10 pts), Target 115.0 (1.5R=15 pts)
        # In Phase 1d, breakeven trigger is 1.10R (+11 pts -> 111.0).
        # At price 107.5 (+7.5 pts = 0.75R), target progress is 50%.
        # With flag OFF: SL must remain at initial SL 90.0 (NOT moved to breakeven).
        self.pm.profit_harvest_enabled = False

        pos = {
            "id": 101,
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

        self.pm.get_latest_price = MagicMock(return_value=(Decimal("107.5"), "zerodha_kite"))
        watermark = datetime.now(timezone.utc)
        eval_res = self.pm.evaluate_position(pos, watermark)

        self.assertIsNotNone(eval_res)
        self.assertIsNone(eval_res["exit_reason"])
        # Stop loss must NOT have been moved to breakeven! Must remain 90.0
        self.assertEqual(eval_res["stop_loss_price"], Decimal("90.0"))
        # Profit harvest layer must not have evaluated
        self.assertIsNone(eval_res.get("profit_harvest"))

        # NOW turn flag ON: at 50% target progress (107.5), stop moves to breakeven!
        self.pm.profit_harvest_enabled = True
        eval_res_on = self.pm.evaluate_position(pos, watermark)
        self.assertIsNotNone(eval_res_on)
        # Stop moved to breakeven (~100.0 + fee buffer)
        self.assertGreaterEqual(eval_res_on["stop_loss_price"], 100.0)

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
            "active_count": 2,
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

        self.pm.get_latest_price = MagicMock(return_value=(Decimal("112.5"), "zerodha_kite"))
        self.pm._get_recent_5m_bars = MagicMock(return_value=[
            {"open": 108.0, "high": 111.0, "low": 107.5, "close": 110.5, "volume": 1000},
            {"open": 110.5, "high": 113.0, "low": 110.0, "close": 112.0, "volume": 1000},
            {"open": 112.0, "high": 113.0, "low": 111.0, "close": 111.5, "volume": 1000},
        ])
        self.pm._get_recent_underlying_bars = MagicMock(return_value=[])

        with patch("backend.position_manager.compute_reversal_score", return_value={
            "reversal_score": 55.0,
            "action": "TIGHTEN",
            "active_count": 1,
            "components": {},
            "reasons": ["vwap cross", "rsi rollover"],
        }):
            watermark = datetime.now(timezone.utc)
            eval_res = self.pm.evaluate_position(pos, watermark)

            self.assertIsNotNone(eval_res)
            self.assertIsNone(eval_res["exit_reason"])
            # Tightened SL should be entry (100.0) + 0.65 * 12.5 = 108.125
            self.assertEqual(eval_res["stop_loss_price"], 108.125)


class TestProfitHarvestCounterfactualReplay(unittest.TestCase):
    """Test A/B counterfactual replay engine, seeded SQLite DBs, and bootstrap CI."""

    def test_ab_replay_database_harvest_helps_scenario(self):
        """Database replay where trade reaches 0.9 target then reverses: Arm B harvests, Arm A hits SL."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE instrument_master (
                    id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    instrument_type TEXT DEFAULT 'EQ',
                    tick_size NUMERIC DEFAULT 0.05
                )
            """))
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
                    realised_exit_price NUMERIC,
                    exit_reason TEXT,
                    net_pnl NUMERIC,
                    trade_mode TEXT DEFAULT 'INTRADAY',
                    estimated_fees NUMERIC DEFAULT 0,
                    improvement_note TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE live_market_bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER,
                    interval TEXT,
                    bar_time TIMESTAMP,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume NUMERIC
                )
            """))

            conn.execute(text("""
                INSERT INTO instrument_master (id, symbol) VALUES (1, 'RELIANCE');
            """))
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price,
                    stop_loss_price, take_profit_price, signal_at, exit_at,
                    realised_exit_price, exit_reason, net_pnl, estimated_fees
                ) VALUES (
                    1, 1, 'BUY', 10, 100.0, 90.0, 115.0,
                    '2026-07-15 09:30:00', '2026-07-15 10:05:00',
                    89.0, 'STOP_LOSS', -105.0, 5.0
                )
            """))

            # Seed bars where price hits 113.6 (0.90 of target 115.0), then exhibits 2-component reversal and crashes to 88.0
            bars = [
                ("2026-07-15 09:30:00", 100.0, 105.0, 100.0, 104.5, 1000),
                ("2026-07-15 09:35:00", 104.5, 110.0, 104.0, 109.5, 1000),
                ("2026-07-15 09:40:00", 109.5, 113.6, 109.0, 113.5, 1000),  # Progress = 0.906 >= 0.83
                # Rejection wick + structure break
                ("2026-07-15 09:45:00", 111.5, 114.0, 110.5, 111.0, 3500),
                ("2026-07-15 09:50:00", 111.0, 111.2, 107.5, 108.0, 3000),  # Reversal exit trigger
                ("2026-07-15 09:55:00", 108.0, 108.2, 98.0, 99.0, 1000),
                ("2026-07-15 10:00:00", 99.0, 99.5, 88.0, 89.0, 1000),     # Baseline hits SL
            ]
            for bt, o, h, l, c, v in bars:
                conn.execute(text("""
                    INSERT INTO live_market_bars (instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, volume)
                    VALUES (1, '5minute', :bt, :o, :h, :l, :c, :v)
                """), {"bt": bt, "o": o, "h": h, "l": l, "c": c, "v": v})

        res = run_profit_harvest_ab_replay(engine=engine, harvest_trigger=0.83, exit_threshold=60.0)
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["trades_reaching_harvest"], 1)
        self.assertEqual(res["trades_improved"], 1)
        self.assertGreater(res["delta_mean_r"], 0.25)
        eval_sample = res["sample_evaluations"][0]
        self.assertEqual(eval_sample["baseline_reason"], "BREAKEVEN_STOP")
        self.assertIn("PROFIT_HARVEST", eval_sample["harvest_reason"])

    def test_ab_replay_database_harvest_hurts_scenario(self):
        """Database replay where trade hits 0.84, shakes out Arm B via tightened stop, then continues to target for Arm A."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE instrument_master (
                    id INTEGER PRIMARY KEY,
                    symbol TEXT,
                    instrument_type TEXT DEFAULT 'EQ',
                    tick_size NUMERIC DEFAULT 0.05
                )
            """))
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
                    realised_exit_price NUMERIC,
                    exit_reason TEXT,
                    net_pnl NUMERIC,
                    trade_mode TEXT DEFAULT 'INTRADAY',
                    estimated_fees NUMERIC DEFAULT 0,
                    improvement_note TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE live_market_bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER,
                    interval TEXT,
                    bar_time TIMESTAMP,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume NUMERIC
                )
            """))

            conn.execute(text("""
                INSERT INTO instrument_master (id, symbol) VALUES (1, 'TCS');
            """))
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price,
                    stop_loss_price, take_profit_price, signal_at, exit_at,
                    realised_exit_price, exit_reason, net_pnl, estimated_fees
                ) VALUES (
                    1, 1, 'BUY', 10, 100.0, 90.0, 115.0,
                    '2026-07-15 09:30:00', '2026-07-15 10:05:00',
                    115.0, 'TAKE_PROFIT', 145.0, 5.0
                )
            """))

            # Seed bars where price reaches 112.6 (progress 0.84), tightens stop to 107.54, dips to 107.0 (shakes out Arm B), then hits TP 115.0 for Arm A
            bars = [
                ("2026-07-15 09:30:00", 100.0, 105.0, 100.0, 104.5, 1000),
                ("2026-07-15 09:35:00", 104.5, 110.0, 104.0, 109.5, 1000),
                ("2026-07-15 09:40:00", 109.5, 112.6, 109.0, 112.5, 1000),  # Progress 0.84, enters harvest
                ("2026-07-15 09:45:00", 111.5, 113.5, 111.0, 111.6, 3000),  # Rejection wick -> Action TIGHTEN
                ("2026-07-15 09:50:00", 111.0, 111.2, 107.0, 108.5, 1000),  # Shakes out Arm B at tightened stop
                ("2026-07-15 09:55:00", 108.5, 113.0, 108.0, 112.5, 1000),
                ("2026-07-15 10:00:00", 112.5, 115.5, 112.0, 115.2, 1000),  # Hits TP for Arm A
            ]
            for bt, o, h, l, c, v in bars:
                conn.execute(text("""
                    INSERT INTO live_market_bars (instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, volume)
                    VALUES (1, '5minute', :bt, :o, :h, :l, :c, :v)
                """), {"bt": bt, "o": o, "h": h, "l": l, "c": c, "v": v})

        res = run_profit_harvest_ab_replay(
            engine=engine,
            harvest_trigger=0.83,
            exit_threshold=80.0,
            tighten_threshold=20.0,
        )
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["trades_reaching_harvest"], 1)
        self.assertEqual(res["trades_hurt"], 1)
        self.assertLess(res["delta_mean_r"], 0.0)
        eval_sample = res["sample_evaluations"][0]
        self.assertEqual(eval_sample["baseline_reason"], "TAKE_PROFIT")
        self.assertIn("STOP", eval_sample["harvest_reason"])

    def test_ab_replay_queries_only_real_columns(self):
        """Replay DB query succeeds against real schema without asking for closed_at or theoretical_exit_price."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE instrument_master (id INTEGER PRIMARY KEY, symbol TEXT)"))
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
                    realised_exit_price NUMERIC,
                    exit_reason TEXT,
                    net_pnl NUMERIC,
                    trade_mode TEXT,
                    estimated_fees NUMERIC,
                    improvement_note TEXT
                )
            """))
        res = run_profit_harvest_ab_replay(engine=engine)
        self.assertEqual(res["total_trades"], 0)
        self.assertEqual(res["recommendation"], "INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()
