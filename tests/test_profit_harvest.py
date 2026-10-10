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
            "signal_at": datetime(2026, 6, 1, 4, 30, 0, tzinfo=timezone.utc),
            "trade_mode": "INTRADAY",
            "improvement_note": "{}",
            "exchange": "NSE",
        }

        self.pm.get_latest_price = MagicMock(return_value=(Decimal("107.5"), "zerodha_kite"))
        watermark = datetime(2026, 6, 1, 4, 45, 0, tzinfo=timezone.utc)
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
            "signal_at": datetime(2026, 6, 1, 4, 30, 0, tzinfo=timezone.utc),
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
            watermark = datetime(2026, 6, 1, 4, 45, 0, tzinfo=timezone.utc)
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

    def test_overlapping_1m_and_5m_rows_not_double_counted(self):
        """Single timeframe: When both 1m and 5m bars exist for upstox_v3, only 5m bars are queried."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE instrument_master (id INTEGER PRIMARY KEY, symbol TEXT, tick_size NUMERIC DEFAULT 0.05)"))
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
                    source TEXT,
                    bar_time TIMESTAMP,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume NUMERIC
                )
            """))
            conn.execute(text("INSERT INTO instrument_master (id, symbol) VALUES (1, 'INFY')"))
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price,
                    stop_loss_price, take_profit_price, signal_at, exit_at,
                    realised_exit_price, exit_reason, net_pnl, estimated_fees
                ) VALUES (
                    1, 1, 'BUY', 10, 100.0, 90.0, 115.0,
                    '2026-07-15 09:30:00', '2026-07-15 09:40:00',
                    109.5, 'TAKE_PROFIT', 90.0, 5.0
                )
            """))
            # Insert 5m bars
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, source, bar_time, open_price, high_price, low_price, close_price, volume)
                VALUES (1, '5minute', 'upstox_v3', '2026-07-15 09:30:00', 100.0, 105.0, 99.5, 104.0, 1000),
                       (1, '5minute', 'upstox_v3', '2026-07-15 09:35:00', 104.0, 110.0, 103.5, 109.5, 1200)
            """))
            # Insert overlapping 1m bars for the same time window
            for m in range(30, 40):
                conn.execute(text(f"""
                    INSERT INTO live_market_bars (instrument_id, interval, source, bar_time, open_price, high_price, low_price, close_price, volume)
                    VALUES (1, '1minute', 'upstox_v3', '2026-07-15 09:{m:02d}:00', 100.0, 105.0, 99.5, 104.0, 200)
                """))

        res = run_profit_harvest_ab_replay(engine=engine)
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["coverage"]["trades_used"], 1)
        self.assertEqual(res["coverage"]["trades_excluded"], 0)

    def test_missing_bars_and_gapped_trades_excluded(self):
        """Missing-bars and gapped trades are excluded and tracked in coverage without synthetic fallback."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE instrument_master (id INTEGER PRIMARY KEY, symbol TEXT, tick_size NUMERIC DEFAULT 0.05)"))
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
                    source TEXT,
                    bar_time TIMESTAMP,
                    open_price NUMERIC,
                    high_price NUMERIC,
                    low_price NUMERIC,
                    close_price NUMERIC,
                    volume NUMERIC
                )
            """))
            conn.execute(text("INSERT INTO instrument_master (id, symbol) VALUES (1, 'NIFTY_CE'), (2, 'BANKNIFTY_PE'), (3, 'TCS')"))

            # Trade 1: Valid continuous bars
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price,
                    stop_loss_price, take_profit_price, signal_at, exit_at,
                    realised_exit_price, exit_reason, net_pnl, estimated_fees
                ) VALUES (
                    1, 1, 'BUY', 10, 100.0, 90.0, 115.0,
                    '2026-07-15 09:30:00', '2026-07-15 09:40:00',
                    108.0, 'TAKE_PROFIT', 75.0, 5.0
                )
            """))
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, source, bar_time, open_price, high_price, low_price, close_price, volume)
                VALUES (1, '5minute', 'upstox_v3', '2026-07-15 09:30:00', 100.0, 105.0, 99.0, 104.0, 1000),
                       (1, '5minute', 'upstox_v3', '2026-07-15 09:35:00', 104.0, 109.0, 103.0, 108.0, 1000)
            """))

            # Trade 2: No bars at all (common for options)
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price,
                    stop_loss_price, take_profit_price, signal_at, exit_at,
                    realised_exit_price, exit_reason, net_pnl, estimated_fees
                ) VALUES (
                    2, 2, 'BUY', 10, 50.0, 40.0, 70.0,
                    '2026-07-15 09:30:00', '2026-07-15 09:45:00',
                    55.0, 'TAKE_PROFIT', 45.0, 5.0
                )
            """))

            # Trade 3: Gap > 10m between bars (25 minute gap)
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price,
                    stop_loss_price, take_profit_price, signal_at, exit_at,
                    realised_exit_price, exit_reason, net_pnl, estimated_fees
                ) VALUES (
                    3, 3, 'BUY', 10, 100.0, 90.0, 115.0,
                    '2026-07-15 09:30:00', '2026-07-15 10:10:00',
                    108.0, 'TAKE_PROFIT', 75.0, 5.0
                )
            """))
            conn.execute(text("""
                INSERT INTO live_market_bars (instrument_id, interval, source, bar_time, open_price, high_price, low_price, close_price, volume)
                VALUES (3, '5minute', 'upstox_v3', '2026-07-15 09:30:00', 100.0, 105.0, 99.0, 104.0, 1000),
                       (3, '5minute', 'upstox_v3', '2026-07-15 09:55:00', 104.0, 109.0, 103.0, 108.0, 1000)
            """))

        res = run_profit_harvest_ab_replay(engine=engine)
        self.assertEqual(res["coverage"]["total_trades_considered"], 3)
        self.assertEqual(res["coverage"]["trades_used"], 1)
        self.assertEqual(res["coverage"]["trades_excluded"], 2)
        self.assertIn("NO_BARS", res["coverage"]["exclusion_reasons"])
        self.assertTrue(any("GAP" in k for k in res["coverage"]["exclusion_reasons"]))

    def test_in_bar_ordering_stop_and_target_preserved(self):
        """In-bar ordering: Prior SL checked before reversal/close; TP checked before close."""
        # Case A: Bar low penetrates prior stop (90.0), close ends at 102.0. Must exit at stop (90.0), not 102.0!
        trade_sl = {
            "entry": 100.0,
            "stop_loss_price": 90.0,
            "take_profit_price": 115.0,
            "side": "BUY",
            "quantity": 1,
            "estimated_fees": 0,
            "trade_mode": "INTRADAY",
        }
        bars_sl = [
            {"bar_time": "2026-07-15 09:30:00", "open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0, "volume": 1000},
            {"bar_time": "2026-07-15 09:35:00", "open": 98.0, "high": 104.0, "low": 88.0, "close": 102.0, "volume": 2000},
        ]
        res_a = replay_trade_walk_forward(trade_sl, bars_sl, harvest_enabled=False)
        self.assertEqual(res_a["exit_price"], 90.0)
        self.assertEqual(res_a["exit_reason"], "STOP_LOSS")

        # Case B: Bar high reaches target (115.0), close ends at 111.0. Must exit at TP (115.0), not 111.0!
        trade_tp = {
            "entry": 100.0,
            "stop_loss_price": 90.0,
            "take_profit_price": 115.0,
            "side": "BUY",
            "quantity": 1,
            "estimated_fees": 0,
            "trade_mode": "INTRADAY",
        }
        bars_tp = [
            {"bar_time": "2026-07-15 09:30:00", "open": 100.0, "high": 108.0, "low": 99.0, "close": 107.0, "volume": 1000},
            {"bar_time": "2026-07-15 09:35:00", "open": 107.0, "high": 116.0, "low": 106.0, "close": 111.0, "volume": 2000},
        ]
        res_b = replay_trade_walk_forward(trade_tp, bars_tp, harvest_enabled=False)
        self.assertEqual(res_b["exit_price"], 115.0)
        self.assertEqual(res_b["exit_reason"], "TAKE_PROFIT")

    def test_fidelity_gate_fails_when_arm_a_diverges(self):
        """Fidelity check: If Arm A deviates > 0.1R from recorded exit, fidelity < 80% and recommendation is REPLAY_NOT_FAITHFUL."""
        trade = {
            "id": 1,
            "symbol": "SBIN",
            "side": "BUY",
            "entry": 100.0,
            "initial_sl": 90.0,
            "target_price": 115.0,
            "realised_exit_price": 99.0,
            "exit_reason": "MANUAL_CLOSE",
            "trade_mode": "INTRADAY",
            "bars": [
                {"bar_time": "2026-07-15 09:30:00", "open": 100.0, "high": 102.0, "low": 98.0, "close": 99.0, "volume": 1000},
                {"bar_time": "2026-07-15 09:35:00", "open": 99.0, "high": 100.0, "low": 89.0, "close": 91.0, "volume": 1000},
            ]
        }
        res = run_profit_harvest_ab_replay(trades=[trade])
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["fidelity"]["fidelity_pct"], 0.0)
        self.assertEqual(res["recommendation"], "REPLAY_NOT_FAITHFUL")

    def test_minimum_sample_and_train_test_split_gate(self):
        """Require >= 100 harvest-triggered trades, and chronological date train/test split."""
        trades = []
        for i in range(10):
            day = 10 + i
            trades.append({
                "id": i + 1,
                "symbol": "RELIANCE",
                "side": "BUY",
                "entry": 100.0,
                "initial_sl": 90.0,
                "target_price": 115.0,
                "realised_exit_price": 115.0,
                "exit_reason": "TAKE_PROFIT",
                "trade_mode": "INTRADAY",
                "signal_at": f"2026-07-{day:02d} 09:30:00",
                "exit_at": f"2026-07-{day:02d} 09:40:00",
                "bars": [
                    {"bar_time": f"2026-07-{day:02d} 09:30:00", "open": 100.0, "high": 106.0, "low": 99.5, "close": 105.0, "volume": 1000},
                    {"bar_time": f"2026-07-{day:02d} 09:35:00", "open": 105.0, "high": 115.5, "low": 104.0, "close": 115.0, "volume": 1000},
                ]
            })
        res = run_profit_harvest_ab_replay(trades=trades)
        self.assertEqual(res["total_trades"], 10)
        self.assertEqual(res["fidelity"]["fidelity_pct"], 100.0)
        self.assertEqual(res["recommendation"], "INSUFFICIENT_TRIGGERED_TRADES")
        self.assertIn("train", res["split"])
        self.assertIn("test", res["split"])
        self.assertEqual(res["split"]["train"]["trades_count"], 5)
        self.assertEqual(res["split"]["test"]["trades_count"], 5)

    def test_recorded_stagnation_exit_not_faithful_when_replay_differs(self):
        """A trade recorded as stagnation exit must not count as faithful when replay falls through."""
        trade = {
            "id": 1,
            "symbol": "INFY",
            "side": "BUY",
            "entry": 100.0,
            "initial_sl": 90.0,
            "target_price": 115.0,
            "realised_exit_price": 100.2,
            "exit_reason": "STAGNATION_GUARD",
            "trade_mode": "SWING",  # SWING skips stagnation guard in replay walk
            "signal_at": "2026-07-15 09:30:00",
            "exit_at": "2026-07-15 09:35:00",
            "bars": [
                {"bar_time": "2026-07-15 09:30:00", "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.2, "volume": 1000},
                {"bar_time": "2026-07-15 09:35:00", "open": 100.2, "high": 101.5, "low": 99.8, "close": 100.4, "volume": 1000},
            ]
        }
        res = run_profit_harvest_ab_replay(trades=[trade])
        self.assertEqual(res["total_trades"], 1)
        self.assertEqual(res["fidelity"]["fell_through_count"], 1)
        self.assertEqual(res["fidelity"]["fidelity_all_pct"], 0.0)
        self.assertEqual(res["recommendation"], "REPLAY_NOT_FAITHFUL")

    def test_fidelity_drops_when_replay_lacks_model_for_unmodelled_exit(self):
        """Fidelity must drop when recorded exit is an unmodelled exit like RMS_AUTO_SQUAREOFF."""
        trade = {
            "id": 1,
            "symbol": "TCS",
            "side": "BUY",
            "entry": 100.0,
            "initial_sl": 90.0,
            "target_price": 115.0,
            "realised_exit_price": 99.5,
            "exit_reason": "RMS_AUTO_SQUAREOFF",
            "trade_mode": "INTRADAY",
            "signal_at": "2026-07-15 09:30:00",
            "exit_at": "2026-07-15 09:40:00",
            "bars": [
                {"bar_time": "2026-07-15 09:30:00", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
                {"bar_time": "2026-07-15 09:35:00", "open": 101.0, "high": 102.5, "low": 99.2, "close": 99.5, "volume": 1000},
            ]
        }
        res = run_profit_harvest_ab_replay(trades=[trade])
        self.assertEqual(res["fidelity"]["fell_through_count"], 1)
        self.assertEqual(res["fidelity"]["fidelity_all_pct"], 0.0)
        self.assertEqual(res["recommendation"], "REPLAY_NOT_FAITHFUL")

    def test_100_plus_triggered_trades_helps_both_halves_returns_enable(self):
        """With 100+ triggered trades in test half where harvest helps in both halves, returns ENABLE."""
        trades = []
        for d in range(1, 21):
            day_str = f"2026-07-{d:02d}"
            for k in range(10):
                t_id = (d - 1) * 10 + k + 1
                trades.append({
                    "id": t_id,
                    "symbol": "NIFTY",
                    "side": "BUY",
                    "entry": 100.0,
                    "initial_sl": 90.0,
                    "target_price": 115.0,
                    "realised_exit_price": 100.1,  # Baseline Arm A hit breakeven stop
                    "exit_reason": "BREAKEVEN_STOP",
                    "trade_mode": "INTRADAY",
                    "signal_at": f"{day_str} 09:30:00",
                    "exit_at": f"{day_str} 10:00:00",
                    "bars": [
                        {"bar_time": f"{day_str} 09:30:00", "interval": "5minute", "open": 100.0, "high": 105.0, "low": 100.0, "close": 104.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:35:00", "interval": "5minute", "open": 104.5, "high": 110.0, "low": 104.0, "close": 109.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:40:00", "interval": "5minute", "open": 109.5, "high": 113.6, "low": 109.0, "close": 113.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:45:00", "interval": "5minute", "open": 111.5, "high": 114.0, "low": 110.5, "close": 111.0, "volume": 3500},
                        {"bar_time": f"{day_str} 09:50:00", "interval": "5minute", "open": 111.0, "high": 111.2, "low": 107.5, "close": 108.0, "volume": 3000},
                        {"bar_time": f"{day_str} 09:55:00", "interval": "5minute", "open": 108.0, "high": 108.2, "low": 98.0, "close": 99.0, "volume": 1000},
                        {"bar_time": f"{day_str} 10:00:00", "interval": "5minute", "open": 99.0, "high": 99.5, "low": 88.0, "close": 89.0, "volume": 1000},
                    ]
                })

        res = run_profit_harvest_ab_replay(trades=trades, exit_threshold=60.0)
        self.assertEqual(res["total_trades"], 200)
        self.assertGreaterEqual(res["split"]["test"]["triggered_count"], 100)
        self.assertGreater(res["split"]["test"]["ci_95"][0], 0.0)
        self.assertEqual(res["recommendation"], "ENABLE")

    def test_100_plus_triggered_trades_helps_only_train_returns_neutral(self):
        """With 100+ triggered trades in test half where harvest helps only in train half, returns NEUTRAL."""
        trades = []
        for d in range(1, 21):
            day_str = f"2026-07-{d:02d}"
            is_train = (d <= 10)
            for k in range(10):
                t_id = (d - 1) * 10 + k + 1
                if is_train:
                    bars = [
                        {"bar_time": f"{day_str} 09:30:00", "interval": "5minute", "open": 100.0, "high": 105.0, "low": 100.0, "close": 104.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:35:00", "interval": "5minute", "open": 104.5, "high": 110.0, "low": 104.0, "close": 109.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:40:00", "interval": "5minute", "open": 109.5, "high": 113.6, "low": 109.0, "close": 113.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:45:00", "interval": "5minute", "open": 111.5, "high": 114.0, "low": 110.5, "close": 111.0, "volume": 3500},
                        {"bar_time": f"{day_str} 09:50:00", "interval": "5minute", "open": 111.0, "high": 111.2, "low": 107.5, "close": 108.0, "volume": 3000},
                        {"bar_time": f"{day_str} 09:55:00", "interval": "5minute", "open": 108.0, "high": 108.2, "low": 98.0, "close": 99.0, "volume": 1000},
                        {"bar_time": f"{day_str} 10:00:00", "interval": "5minute", "open": 99.0, "high": 99.5, "low": 88.0, "close": 89.0, "volume": 1000},
                    ]
                    rec_exit = 100.1
                    rec_reason = "BREAKEVEN_STOP"
                    ex_at = f"{day_str} 10:00:00"
                else:
                    bars = [
                        {"bar_time": f"{day_str} 09:30:00", "interval": "5minute", "open": 100.0, "high": 108.0, "low": 100.0, "close": 107.0, "volume": 1000},
                        {"bar_time": f"{day_str} 09:35:00", "interval": "5minute", "open": 107.0, "high": 112.8, "low": 106.5, "close": 112.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:40:00", "interval": "5minute", "open": 112.5, "high": 115.5, "low": 112.0, "close": 115.0, "volume": 1000},
                    ]
                    rec_exit = 115.0
                    rec_reason = "TAKE_PROFIT"
                    ex_at = f"{day_str} 09:40:00"

                trades.append({
                    "id": t_id,
                    "symbol": "NIFTY",
                    "side": "BUY",
                    "entry": 100.0,
                    "initial_sl": 90.0,
                    "target_price": 115.0,
                    "realised_exit_price": rec_exit,
                    "exit_reason": rec_reason,
                    "trade_mode": "INTRADAY",
                    "signal_at": f"{day_str} 09:30:00",
                    "exit_at": ex_at,
                    "bars": bars,
                })

        res = run_profit_harvest_ab_replay(trades=trades, exit_threshold=60.0)
        self.assertEqual(res["total_trades"], 200)
        self.assertGreaterEqual(res["split"]["test"]["triggered_count"], 100)
        self.assertGreater(res["split"]["train"]["delta_mean_r"], 0.10)
        self.assertAlmostEqual(res["split"]["test"]["delta_mean_r"], 0.0, places=2)
        self.assertEqual(res["recommendation"], "NEUTRAL")

    def test_resolution_invariance_1m_vs_5m(self):
        """Resolution invariance: Same price path as 1m and as 5m bars gives the same arm decisions on target/stop exits.

        Documents why not on sub-minute rules:
        - On standard price-level exits (TAKE_PROFIT, STOP_LOSS, BREAKEVEN_STOP), 1m and 5m bars produce the same
          exit prices and arm decisions.
        - On sub-minute rules (90s EARLY_ADVERSE_CUT, 900s STAGNATION_GUARD), 5m bars cannot resolve intra-bar
          second-level durations without lookahead/interpolation bias. Replay explicitly skips 90s/900s checks
          when bar interval > 1m and marks those trades unmodelled in the fidelity report.
        """
        # Trade setup: BUY at 100.0, initial_sl 95.0, target 110.0
        trade = {
            "id": 1,
            "symbol": "INFY",
            "side": "BUY",
            "entry": 100.0,
            "initial_sl": 95.0,
            "target_price": 110.0,
            "trade_mode": "INTRADAY",
            "signal_at": "2026-07-01 09:30:00",
            "realised_exit_price": 110.0,
            "exit_reason": "TAKE_PROFIT",
        }

        # 1. Price path as 1-minute bars: reaches 110.5 on minute 3
        bars_1m = [
            {"bar_time": "2026-07-01 09:30:00", "interval": "1minute", "open": 100.0, "high": 102.0, "low": 99.8, "close": 101.5, "volume": 100},
            {"bar_time": "2026-07-01 09:31:00", "interval": "1minute", "open": 101.5, "high": 105.0, "low": 101.0, "close": 104.5, "volume": 100},
            {"bar_time": "2026-07-01 09:32:00", "interval": "1minute", "open": 104.5, "high": 108.0, "low": 104.0, "close": 107.5, "volume": 100},
            {"bar_time": "2026-07-01 09:33:00", "interval": "1minute", "open": 107.5, "high": 110.5, "low": 107.0, "close": 110.0, "volume": 100},
        ]

        # 2. Aggregated equivalent 5-minute bar: open 100.0, high 110.5, low 99.8, close 110.0
        bars_5m = [
            {"bar_time": "2026-07-01 09:30:00", "interval": "5minute", "open": 100.0, "high": 110.5, "low": 99.8, "close": 110.0, "volume": 400},
        ]

        res_1m = replay_trade_walk_forward(trade=trade, bars=bars_1m, harvest_enabled=False)
        res_5m = replay_trade_walk_forward(trade=trade, bars=bars_5m, harvest_enabled=False)

        # Decision invariance on target exit:
        self.assertEqual(res_1m["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(res_5m["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(res_1m["exit_price"], res_5m["exit_price"])
        self.assertAlmostEqual(res_1m["realized_r"], res_5m["realized_r"], places=2)

    def test_zero_modelled_trades_returns_replay_not_faithful(self):
        """When there are zero modelled-exit trades (len(core_trades) == 0), return REPLAY_NOT_FAITHFUL instead of passing at 100%."""
        # Recorded exits are all unmodelled (MANUAL_CLOSE)
        trades = [{
            "id": i + 1,
            "symbol": "TCS",
            "side": "BUY",
            "entry": 100.0,
            "initial_sl": 95.0,
            "target_price": 110.0,
            "realised_exit_price": 103.0,
            "exit_reason": "MANUAL_CLOSE",
            "trade_mode": "INTRADAY",
            "signal_at": f"2026-07-{i+1:02d} 09:30:00",
            "exit_at": f"2026-07-{i+1:02d} 09:31:00",
            "bars": [
                {"bar_time": f"2026-07-{i+1:02d} 09:30:00", "interval": "1minute", "open": 100.0, "high": 103.5, "low": 99.0, "close": 103.0, "volume": 100},
                {"bar_time": f"2026-07-{i+1:02d} 09:31:00", "interval": "1minute", "open": 103.0, "high": 103.2, "low": 102.8, "close": 103.0, "volume": 100},
            ],
        } for i in range(10)]

        res = run_profit_harvest_ab_replay(trades=trades)
        self.assertEqual(res["fidelity"]["modelled_trades_compared"], 0)
        self.assertEqual(res["recommendation"], "REPLAY_NOT_FAITHFUL")

    def test_enable_requires_both_test_ci_and_train_delta_positive(self):
        """ENABLE requires BOTH test CI lower bound > 0 AND train-half mean delta > 0."""
        # Simulated scenario: test CI lower bound > 0, but train delta is <= 0 -> MUST NOT ENABLE
        trades = []
        for d in range(1, 21):
            day_str = f"2026-07-{d:02d}"
            is_train = (d <= 10)
            for k in range(10):
                t_id = (d - 1) * 10 + k + 1
                if is_train:
                    # In train, harvest exit hurts (returns lower price than baseline)
                    bars = [
                        {"bar_time": f"{day_str} 09:30:00", "interval": "5minute", "open": 100.0, "high": 108.0, "low": 100.0, "close": 107.0, "volume": 1000},
                        {"bar_time": f"{day_str} 09:35:00", "interval": "5minute", "open": 107.0, "high": 113.0, "low": 106.5, "close": 112.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:40:00", "interval": "5minute", "open": 112.5, "high": 115.5, "low": 112.0, "close": 115.0, "volume": 1000},
                    ]
                    rec_exit = 115.0
                    rec_reason = "TAKE_PROFIT"
                    ex_at = f"{day_str} 09:40:00"
                else:
                    # In test, harvest helps
                    bars = [
                        {"bar_time": f"{day_str} 09:30:00", "interval": "5minute", "open": 100.0, "high": 105.0, "low": 100.0, "close": 104.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:35:00", "interval": "5minute", "open": 104.5, "high": 110.0, "low": 104.0, "close": 109.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:40:00", "interval": "5minute", "open": 109.5, "high": 113.6, "low": 109.0, "close": 113.5, "volume": 1000},
                        {"bar_time": f"{day_str} 09:45:00", "interval": "5minute", "open": 111.5, "high": 114.0, "low": 110.5, "close": 111.0, "volume": 3500},
                        {"bar_time": f"{day_str} 09:50:00", "interval": "5minute", "open": 111.0, "high": 111.2, "low": 107.5, "close": 108.0, "volume": 3000},
                        {"bar_time": f"{day_str} 09:55:00", "interval": "5minute", "open": 108.0, "high": 108.2, "low": 98.0, "close": 99.0, "volume": 1000},
                        {"bar_time": f"{day_str} 10:00:00", "interval": "5minute", "open": 99.0, "high": 99.5, "low": 88.0, "close": 89.0, "volume": 1000},
                    ]
                    rec_exit = 100.1
                    rec_reason = "BREAKEVEN_STOP"
                    ex_at = f"{day_str} 10:00:00"

                trades.append({
                    "id": t_id,
                    "symbol": "NIFTY",
                    "side": "BUY",
                    "entry": 100.0,
                    "initial_sl": 90.0,
                    "target_price": 115.0,
                    "realised_exit_price": rec_exit,
                    "exit_reason": rec_reason,
                    "trade_mode": "INTRADAY",
                    "signal_at": f"{day_str} 09:30:00",
                    "exit_at": ex_at,
                    "bars": bars,
                })

        res = run_profit_harvest_ab_replay(trades=trades, exit_threshold=60.0)
        # Even if test CI is positive, train delta is <= 0 so recommendation must NOT be ENABLE
        self.assertLessEqual(res["split"]["train"]["delta_mean_r"], 0.0)
        self.assertNotEqual(res["recommendation"], "ENABLE")


if __name__ == "__main__":
    unittest.main()
