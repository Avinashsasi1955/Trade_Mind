"""Phase 1 Tests: Swing stop-loss, gap-aware fills, incremental evaluation, and price freshness guards."""

import json
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from backend.config import (
    PRICE_MAX_AGE_SECONDS,
    PRICE_MAX_AGE_SWING_SECONDS,
    STALE_DATA_EXIT_MINUTES,
    HELD_CONTRACT_STALE_TICK_MINUTES,
)
from backend.position_manager import PositionManager, is_market_hours, PriceResult
from backend.swing_risk import calculate_daily_atr, compute_swing_risk_parameters

IST = ZoneInfo("Asia/Kolkata")


class TestSwingStopsAndRisk(unittest.TestCase):
    def setUp(self):
        # In-memory SQLite database for deterministic fast tests
        self.engine = create_engine("sqlite:///:memory:", future=True)
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE instrument_master (
                    id INTEGER PRIMARY KEY,
                    instrument_token INTEGER,
                    symbol TEXT,
                    exchange TEXT,
                    instrument_type TEXT,
                    expiry DATE,
                    strike NUMERIC,
                    is_active BOOLEAN DEFAULT 1,
                    is_fno_eligible BOOLEAN DEFAULT 1,
                    underlying_symbol TEXT
                );
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
                    volume INTEGER DEFAULT 100,
                    source TEXT
                );
            """))
            conn.execute(text("""
                CREATE TABLE shadow_execution_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    trade_mode TEXT DEFAULT 'SWING',
                    holding_days INTEGER DEFAULT 0,
                    max_holding_days INTEGER DEFAULT 15,
                    estimated_fees NUMERIC DEFAULT 0,
                    improvement_note TEXT,
                    reasoning_chain TEXT
                );
            """))
            conn.execute(text("""
                CREATE TABLE instrument_provider_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER,
                    provider TEXT,
                    provider_key TEXT,
                    mode_hint TEXT,
                    is_active BOOLEAN DEFAULT 1
                );
            """))

            # Seed instrument
            conn.execute(text("""
                INSERT INTO instrument_master (id, instrument_token, symbol, exchange, instrument_type, is_active, underlying_symbol)
                VALUES (1, 1001, 'RELIANCE', 'NSE', 'EQ', 1, 'RELIANCE');
            """))

    def tearDown(self):
        self.engine.dispose()

    def test_swing_risk_parameters_calculation(self):
        """Test compute_swing_risk_parameters with daily ATR, structure low/high, gap buffer, and sizing."""
        daily_candles = [
            {"high": 2500, "low": 2450, "close": 2480},
            {"high": 2520, "low": 2470, "close": 2510},
            {"high": 2530, "low": 2490, "close": 2500},
            {"high": 2510, "low": 2460, "close": 2470},
        ]
        atr = calculate_daily_atr(daily_candles, period=3)
        self.assertGreater(atr, Decimal("0"))

        # BUY side
        res = compute_swing_risk_parameters(
            symbol="RELIANCE",
            side="BUY",
            entry_price=2500.0,
            daily_candles=daily_candles,
            k=1.5,
            gap_buffer_pct=0.005,
            capital=1_000_000.0,
            risk_pct=0.01,
        )
        self.assertLess(res["stop_loss_price"], 2500.0)
        self.assertGreater(res["take_profit_price"], 2500.0)
        self.assertGreaterEqual(res["risk_reward"], 1.5)
        self.assertGreater(res["quantity"], 0)

        # SELL side
        res_sell = compute_swing_risk_parameters(
            symbol="RELIANCE",
            side="SELL",
            entry_price=2500.0,
            daily_candles=daily_candles,
            k=1.5,
            gap_buffer_pct=0.005,
            capital=1_000_000.0,
            risk_pct=0.01,
        )
        self.assertGreater(res_sell["stop_loss_price"], 2500.0)
        self.assertLess(res_sell["take_profit_price"], 2500.0)
        self.assertGreaterEqual(res_sell["risk_reward"], 1.5)

    def test_overnight_gap_down_fills_at_open(self):
        """Overnight gap-down exit must fill at open_price with GAP_DOWN_STOP, NOT at stop loss price."""
        # Entry at 2500, Stop Loss at 2450
        signal_time = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
        with self.engine.begin() as conn:
            audit_id = conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    take_profit_price, signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2450.0, 2600.0, :sig_at, 'RECONCILED', 'SWING', '{}'
                )
            """), {"sig_at": signal_time}).lastrowid

            # Day 1: normal bar within range (High 2510, Low 2480, Close 2490)
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :b1, 2500.0, 2510.0, 2480.0, 2490.0, 'zerodha_kite'
                )
            """), {"b1": signal_time + timedelta(minutes=5)})

            # Day 2 Open: GAPS DOWN severely to 2400 (well below stop loss of 2450!)
            day2_open_time = signal_time + timedelta(days=1)
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :b2, 2400.0, 2410.0, 2390.0, 2405.0, 'zerodha_kite'
                )
            """), {"b2": day2_open_time})

        pm = PositionManager(database_url="sqlite:///:memory:")
        pm.engine = self.engine

        pos = pm.get_open_positions()[0]
        eval_result = pm.evaluate_position(pos, day2_open_time)

        self.assertIsNotNone(eval_result)
        self.assertEqual(eval_result["status"], "CLOSED")
        self.assertEqual(eval_result["exit_reason"], "GAP_DOWN_STOP")
        # Critical verification: fill must be at open (2400.0), NOT at stop loss (2450.0)!
        self.assertEqual(eval_result["exit_price"], 2400.0)

    def test_price_freshness_guard_detects_stale_and_exits(self):
        """A position whose price feed stops during market hours is marked STALE_PRICE and exits after timeout."""
        # 11:00 AM IST on Wednesday (market hours) -> UTC 05:30
        watermark = datetime(2026, 6, 3, 5, 30, tzinfo=timezone.utc)
        self.assertTrue(is_market_hours(watermark))

        # Bar was 120 seconds ago (> 60s intraday threshold)
        bar_time = watermark - timedelta(seconds=120)

        with self.engine.begin() as conn:
            audit_id = conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2450.0, :sig_at, 'RECONCILED', 'INTRADAY', '{}'
                )
            """), {"sig_at": bar_time - timedelta(minutes=10)}).lastrowid

            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2500.0, 2505.0, 2495.0, 2500.0, 'zerodha_kite'
                )
            """), {"bt": bar_time})

        pm = PositionManager(database_url="sqlite:///:memory:")
        pm.engine = self.engine
        pm.price_max_age_seconds = 60
        pm.stale_data_exit_minutes = 5

        pos = pm.get_open_positions()[0]

        # First evaluation: triggers STALE_PRICE and marks tracker
        res1 = pm.evaluate_position(pos, watermark)
        self.assertIsNotNone(res1)
        self.assertTrue(res1["stale_price"])
        self.assertEqual(res1["status"], "OPEN")
        self.assertIn(pos["id"], pm.stale_positions_tracker)

        # Advance past stale_data_exit_minutes (5 minutes later)
        watermark_timeout = watermark + timedelta(minutes=5, seconds=5)
        res2 = pm.evaluate_position(pos, watermark_timeout)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["status"], "CLOSED")
        self.assertEqual(res2["exit_reason"], "STALE_DATA_EXIT")

    def test_incremental_evaluation_o_new_bars(self):
        """Incremental evaluation persists state and processes only newly added bars."""
        signal_time = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    id, instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    10, 1, 'BUY', 10, 2500.0, 2450.0, :sig_at, 'RECONCILED', 'SWING', '{}'
                )
            """), {"sig_at": signal_time})

            # Add initial 5 bars
            for i in range(1, 6):
                conn.execute(text("""
                    INSERT INTO live_market_bars (
                        instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                    ) VALUES (
                        1, '1minute', :bt, 2500.0, 2510.0, 2495.0, 2505.0, 'zerodha_kite'
                    )
                """), {"bt": signal_time + timedelta(minutes=i)})

        pm = PositionManager(database_url="sqlite:///:memory:")
        pm.engine = self.engine

        pos = pm.get_open_positions()[0]
        wm1 = signal_time + timedelta(minutes=5)
        res1 = pm.evaluate_position(pos, wm1)
        self.assertEqual(res1["status"], "OPEN")

        # Verify note updated with last_processed_bar_time
        with self.engine.connect() as conn:
            note_raw = conn.execute(text("SELECT improvement_note FROM shadow_execution_audits WHERE id = 10")).scalar_one()
            note_dict = json.loads(note_raw)
            self.assertIn("last_processed_bar_time", note_dict)
            self.assertIn("running_sl", note_dict)
            self.assertIn("best_favourable", note_dict)
            last_pbt = note_dict["last_processed_bar_time"]

        # Now spy on get_exit_bars during next evaluation with 1 new bar added
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2505.0, 2515.0, 2500.0, 2510.0, 'zerodha_kite'
                )
            """), {"bt": signal_time + timedelta(minutes=6)})

        pos_updated = pm.get_open_positions()[0]
        wm2 = signal_time + timedelta(minutes=6)

        with patch.object(pm, "get_exit_bars", wraps=pm.get_exit_bars) as spy_get_bars:
            pm.evaluate_position(pos_updated, wm2)
            # Verify get_exit_bars was called with exclusive_start=True starting from last_processed_bar_time
            spy_get_bars.assert_called_once()
            call_kwargs = spy_get_bars.call_args[1]
            self.assertTrue(call_kwargs.get("exclusive_start"))

    def test_held_position_subscription_guarantee_tier_minus_one(self):
        """LiveStreamService must include open reconciled positions at top priority Tier -1 with full mode."""
        from backend.live_stream_service import LiveStreamService

        with self.engine.begin() as conn:
            # Active open position for RELIANCE (id=1)
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, signal_at, audit_status, net_pnl
                ) VALUES (
                    1, 'BUY', 10, 2500.0, CURRENT_TIMESTAMP, 'RECONCILED', NULL
                );
            """))
            # Provider key for RELIANCE
            conn.execute(text("""
                INSERT INTO instrument_provider_keys (instrument_id, provider, provider_key, mode_hint, is_active)
                VALUES (1, 'upstox_v3', 'NSE_EQ|INE002A01018', 'full', 1);
            """))

        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///:memory:",
            "REDIS_URL": "redis://localhost:6379/0",
            "NIVESH_MARKET_DATA_PROVIDER": "upstox",
            "UPSTOX_ACCESS_TOKEN": "mock_token",
            "LIVE_ELIGIBLE": "FALSE",
            "NIVESH_LIVE_TRADING_ENABLED": "0"
        }):
            with patch("backend.live_stream_service.PostgresBarAggregator"):
                with patch("redis.Redis.from_url"):
                    service = LiveStreamService()
                    service.engine = self.engine
                    subs = service.subscriptions()
                    self.assertGreater(len(subs), 0)
                    # Held position must be at index 0 (Tier -1) with full mode!
                    self.assertEqual(subs[0][0], "NSE_EQ|INE002A01018")
                    self.assertEqual(subs[0][1], "full")


if __name__ == "__main__":
    unittest.main()
