"""Phase 1 & 1b Tests: Swing stop-loss, gap-aware fills, incremental evaluation,
price freshness guards, Upstox REST quote fallback, and holiday calendar integration.
"""
from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from backend.config import (
    PRICE_MAX_AGE_SECONDS,
    PRICE_MAX_AGE_SWING_SECONDS,
    STALE_DATA_EXIT_MINUTES,
    HELD_CONTRACT_STALE_TICK_MINUTES,
    SWING_MAX_STOP_ATR,
    SWING_MAX_POSITION_PCT,
    STALE_OPEN_GRACE_SECONDS,
)
from backend.position_manager import (
    PositionManager,
    is_market_hours,
    get_market_open_time,
    get_calendar_session,
    clear_calendar_cache,
    PriceResult,
)
from backend.swing_risk import (
    calculate_daily_atr,
    compute_swing_risk_parameters,
    SWING_REJECTED_THIN_HISTORY,
    SWING_REJECTED_ATR_CALCULATION,
    SWING_REJECTED_STOP_CAP_EXCEEDED,
    SWING_REJECTED_ZERO_QUANTITY,
)

IST = ZoneInfo("Asia/Kolkata")


class TestSwingStopsAndRisk(unittest.TestCase):
    def setUp(self):
        clear_calendar_cache()
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
            conn.execute(text("""
                CREATE TABLE exchange_trading_calendar (
                    exchange TEXT NOT NULL,
                    session_date DATE NOT NULL,
                    session_status TEXT NOT NULL,
                    opens_at TIME,
                    closes_at TIME,
                    source TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(exchange, session_date)
                );
            """))

            conn.execute(text("""
                CREATE TABLE monitoring_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT,
                    level TEXT,
                    message TEXT,
                    payload TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))

            conn.execute(text("""
                CREATE TABLE trade_candidate_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TIMESTAMP,
                    model_version TEXT,
                    exchange TEXT,
                    symbol TEXT,
                    instrument_id INTEGER,
                    signal INTEGER CHECK(typeof(signal) = 'integer'),
                    probability NUMERIC,
                    decision_price NUMERIC,
                    selector_stage TEXT,
                    accepted BOOLEAN,
                    rejection_reason TEXT,
                    trade_mode TEXT
                );
            """))

            # Seed instruments: Stock (1) and Option (2)
            conn.execute(text("""
                INSERT INTO instrument_master (id, instrument_token, symbol, exchange, instrument_type, is_active, underlying_symbol)
                VALUES (1, 1001, 'RELIANCE', 'NSE', 'EQ', 1, 'RELIANCE');
            """))
            conn.execute(text("""
                INSERT INTO instrument_master (id, instrument_token, symbol, exchange, instrument_type, is_active, underlying_symbol, strike)
                VALUES (2, 2001, 'RELIANCE 2600 CE', 'NFO', 'CE', 1, 'RELIANCE', 2600.0);
            """))

        self.mock_redis = MagicMock()
        self.mock_redis.get.return_value = None
        self.mock_redis.hget.return_value = None

    def tearDown(self):
        self.engine.dispose()

    # -------------------------------------------------------------
    # 1. Swing Risk Model & Fails-Closed Tests
    # -------------------------------------------------------------

    def test_swing_risk_fewer_than_15_candles_fails_closed(self):
        """calculate_daily_atr and compute_swing_risk_parameters must return None if < 15 daily candles."""
        thin_candles = [{"high": 2500, "low": 2450, "close": 2480} for _ in range(10)]
        self.assertIsNone(calculate_daily_atr(thin_candles, 14))

        res = compute_swing_risk_parameters(
            symbol="RELIANCE",
            side="BUY",
            entry_price=2500.0,
            daily_candles=thin_candles,
        )
        self.assertIsNone(res, "Must return None when history has fewer than 15 daily candles")

    def test_swing_risk_parameters_calculation_valid_history(self):
        """Test compute_swing_risk_parameters with 20 daily candles produces valid SL/TP and sizing."""
        daily_candles = [
            {"high": 2480 + i, "low": 2450 + i, "close": 2470 + i}
            for i in range(20)
        ]
        atr = calculate_daily_atr(daily_candles, period=14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, Decimal("0"))

        res = compute_swing_risk_parameters(
            symbol="RELIANCE",
            side="BUY",
            entry_price=2500.0,
            daily_candles=daily_candles,
            capital=1_000_000.0,
            risk_pct=0.01,
        )
        self.assertIsNotNone(res)
        self.assertLess(res["stop_loss_price"], 2500.0)
        self.assertGreater(res["take_profit_price"], 2500.0)
        self.assertGreaterEqual(res["risk_reward"], 1.5)
        self.assertGreater(res["quantity"], 0)

    def test_option_swing_risk_with_no_option_history_uses_underlying(self):
        """Option with 0 daily candles must calculate stop from underlying ATR & delta, and set invalidation."""
        und_candles = [
            {"high": 2480 + i, "low": 2450 + i, "close": 2470 + i}
            for i in range(20)
        ]
        res = compute_swing_risk_parameters(
            symbol="RELIANCE 2600 CE",
            side="BUY",
            entry_price=50.0,
            daily_candles=None,  # No option history!
            is_option=True,
            underlying_daily_candles=und_candles,
            option_type="CE",
            delta=0.50,
            lot_size=250,
            capital=1_000_000.0,
            risk_pct=0.01,
        )
        self.assertIsNotNone(res)
        self.assertLess(res["stop_loss_price"], 50.0)
        self.assertGreater(res["take_profit_price"], 50.0)
        self.assertIsNotNone(res["underlying_invalidation_level"])
        self.assertGreater(res["underlying_invalidation_level"], 2400.0)
        self.assertEqual(res["quantity"] % 250, 0, "Option sizing must be a multiple of lot size (250)")

    def test_option_swing_risk_with_thin_underlying_history_returns_none(self):
        """Option with < 15 underlying daily candles must fail closed and return None."""
        thin_und_candles = [{"high": 2500, "low": 2450, "close": 2480} for _ in range(5)]
        res = compute_swing_risk_parameters(
            symbol="RELIANCE 2600 CE",
            side="BUY",
            entry_price=50.0,
            is_option=True,
            underlying_daily_candles=thin_und_candles,
            option_type="CE",
        )
        self.assertIsNone(res)

    def test_swing_stop_capped_and_wide_structure_rejected(self):
        """If structural stop > NIVESH_SWING_MAX_STOP_ATR * ATR, reject the setup (return None)."""
        # ATR ~ 10, but 20-day swing low is 2300 (200 pts away >> 3 * 10 = 30 pts)
        daily_candles = [
            {"high": 2500, "low": 2490, "close": 2495} for _ in range(19)
        ]
        daily_candles.insert(0, {"high": 2320, "low": 2300, "close": 2310})  # Deep low

        res = compute_swing_risk_parameters(
            symbol="RELIANCE",
            side="BUY",
            entry_price=2500.0,
            daily_candles=daily_candles,
            max_stop_atr=3.0,
        )
        self.assertIsNone(res, "Setup must be rejected when structural stop exceeds 3x ATR")

    def test_swing_sizing_position_value_cap(self):
        """Position value must be capped at NIVESH_SWING_MAX_POSITION_PCT (default 20%)."""
        daily_candles = [
            {"high": 2505, "low": 2495, "close": 2500} for _ in range(20)
        ]
        # Entry at 2500, tight stop at 2490 (stop_distance = 10 pts).
        # Capital = 100,000. Risk 1% = 1,000 -> 100 shares.
        # But Max position value = 20% of 100,000 = 20,000.
        # At 2500 per share, 20,000 allows at most 8 shares!
        res = compute_swing_risk_parameters(
            symbol="RELIANCE",
            side="BUY",
            entry_price=2500.0,
            daily_candles=daily_candles,
            capital=100_000.0,
            risk_pct=0.01,
            max_position_pct=0.20,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["quantity"], 8, "Position quantity must be capped at 8 shares (₹20,000 / ₹2,500)")

    # -------------------------------------------------------------
    # 2. Gap-Aware Fills & Execution Tests
    # -------------------------------------------------------------

    def test_overnight_gap_down_fills_at_open(self):
        """Overnight gap-down exit must fill at open_price with GAP_DOWN_STOP, NOT at stop loss price."""
        signal_time = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    take_profit_price, signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2450.0, 2600.0, :sig_at, 'RECONCILED', 'SWING', '{}'
                )
            """), {"sig_at": signal_time})

            # Day 1 bar
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :b1, 2500.0, 2510.0, 2480.0, 2490.0, 'zerodha_kite'
                )
            """), {"b1": signal_time + timedelta(minutes=5)})

            # Day 2 Open gaps down severely to 2400 (well below stop loss of 2450)
            day2_open_time = signal_time + timedelta(days=1)
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :b2, 2400.0, 2410.0, 2390.0, 2405.0, 'zerodha_kite'
                )
            """), {"b2": day2_open_time})

        pm = PositionManager(database_url="sqlite:///:memory:", redis_client=self.mock_redis)
        pm.engine = self.engine

        pos = pm.get_open_positions()[0]
        eval_result = pm.evaluate_position(pos, day2_open_time)

        self.assertIsNotNone(eval_result)
        self.assertEqual(eval_result["status"], "CLOSED")
        self.assertEqual(eval_result["exit_reason"], "GAP_DOWN_STOP")
        self.assertEqual(eval_result["exit_price"], 2400.0)

    # -------------------------------------------------------------
    # 3. Exchange Calendar, Holiday & Open Grace Tests
    # -------------------------------------------------------------

    def test_holiday_suppresses_stale_exit_on_held_swing(self):
        """On an exchange holiday (e.g. Dussehra), is_market_hours is False and held swing position does NOT exit."""
        # Dussehra: 2026-10-20 (Tuesday, regular weekday!)
        holiday_date = date(2026, 10, 20)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO exchange_trading_calendar (exchange, session_date, session_status, source)
                VALUES ('NSE', :day, 'CLOSED', 'nse_circular');
            """), {"day": holiday_date})

        watermark_holiday = datetime(2026, 10, 20, 5, 30, tzinfo=timezone.utc)  # 11:00 AM IST on holiday
        self.assertFalse(is_market_hours(watermark_holiday, engine=self.engine))

        # Held swing position from 2 days prior
        signal_time = watermark_holiday - timedelta(days=2)
        last_bar_time = watermark_holiday - timedelta(hours=24)  # Old bar from prior day
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2400.0, :sig_at, 'RECONCILED', 'SWING', '{}'
                )
            """), {"sig_at": signal_time})
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2500.0, 2505.0, 2495.0, 2500.0, 'upstox_v3'
                )
            """), {"bt": last_bar_time})

        pm = PositionManager(database_url="sqlite:///:memory:", redis_client=self.mock_redis)
        pm.engine = self.engine

        pos = pm.get_open_positions()[0]
        res = pm.evaluate_position(pos, watermark_holiday)

        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "OPEN")
        self.assertFalse(res["stale_price"], "Stale price check must be suppressed on exchange holidays")

    def test_open_grace_period_suppresses_staleness_noise(self):
        """During the first 120 seconds after market open, stale price check is suppressed."""
        # Normal open at 09:15 IST (03:45 UTC). Watermark at 09:16 IST (60s after open).
        market_day = date(2026, 6, 3)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO exchange_trading_calendar (exchange, session_date, session_status, opens_at, closes_at, source)
                VALUES ('NSE', :day, 'OPEN', '09:15:00', '15:30:00', 'nse_circular');
            """), {"day": market_day})

        # Watermark at 09:16:00 IST (UTC 03:46:00)
        watermark_open = datetime(2026, 6, 3, 3, 46, 0, tzinfo=timezone.utc)
        # Yesterday's closing bar
        yesterday_bar = watermark_open - timedelta(hours=18)

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2400.0, :sig_at, 'RECONCILED', 'SWING', '{}'
                )
            """), {"sig_at": yesterday_bar})
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2500.0, 2505.0, 2495.0, 2500.0, 'upstox_v3'
                )
            """), {"bt": yesterday_bar})

        pm = PositionManager(database_url="sqlite:///:memory:", redis_client=self.mock_redis)
        pm.engine = self.engine
        pm.stale_open_grace_seconds = 120

        pos = pm.get_open_positions()[0]
        res = pm.evaluate_position(pos, watermark_open)
        self.assertIsNotNone(res)
        self.assertFalse(res["stale_price"], "Stale check must be suppressed in first 120s of session")

    def test_price_freshness_guard_detects_stale_and_exits_normal_day(self):
        """On a normal day after open grace, a truly dead feed flags STALE_PRICE and exits after timeout."""
        market_day = date(2026, 6, 3)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO exchange_trading_calendar (exchange, session_date, session_status, opens_at, closes_at, source)
                VALUES ('NSE', :day, 'OPEN', '09:15:00', '15:30:00', 'nse_circular');
            """), {"day": market_day})

        # 11:00 AM IST (UTC 05:30:00)
        watermark = datetime(2026, 6, 3, 5, 30, tzinfo=timezone.utc)
        bar_time = watermark - timedelta(seconds=120)

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2450.0, :sig_at, 'RECONCILED', 'INTRADAY', '{}'
                )
            """), {"sig_at": bar_time - timedelta(minutes=10)})
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2500.0, 2505.0, 2495.0, 2500.0, 'zerodha_kite'
                )
            """), {"bt": bar_time})

        pm = PositionManager(database_url="sqlite:///:memory:", redis_client=self.mock_redis)
        pm.engine = self.engine
        pm.price_max_age_seconds = 60
        pm.stale_data_exit_minutes = 5

        pos = pm.get_open_positions()[0]
        res1 = pm.evaluate_position(pos, watermark)
        self.assertIsNotNone(res1)
        self.assertTrue(res1["stale_price"])
        self.assertEqual(res1["status"], "OPEN")

        # 5 minutes later
        watermark_timeout = watermark + timedelta(minutes=5, seconds=5)
        res2 = pm.evaluate_position(pos, watermark_timeout)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["status"], "CLOSED")
        self.assertEqual(res2["exit_reason"], "STALE_DATA_EXIT")

    # -------------------------------------------------------------
    # 4. Option Underlying Invalidation Stop Test
    # -------------------------------------------------------------

    def test_option_underlying_invalidation_exit(self):
        """When underlying breaks invalidation level, option position exits with UNDERLYING_STOP."""
        signal_time = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
        note = json.dumps({"underlying_invalidation_level": 2450.0})

        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    2, 'BUY', 250, 50.0, 20.0, :sig_at, 'RECONCILED', 'SWING', :note
                )
            """), {"sig_at": signal_time, "note": note})

            # Option bar
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    2, '1minute', :bt, 50.0, 52.0, 48.0, 49.0, 'upstox_v3'
                )
            """), {"bt": signal_time + timedelta(minutes=10)})

            # Underlying stock breaks down below 2450 (close = 2440)
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2460.0, 2465.0, 2435.0, 2440.0, 'upstox_v3'
                )
            """), {"bt": signal_time + timedelta(minutes=10)})

        pm = PositionManager(database_url="sqlite:///:memory:", redis_client=self.mock_redis)
        pm.engine = self.engine

        pos = pm.get_open_positions()[0]
        res = pm.evaluate_position(pos, signal_time + timedelta(minutes=10))

        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "CLOSED")
        self.assertEqual(res["exit_reason"], "UNDERLYING_STOP")

    # -------------------------------------------------------------
    # 5. Upstox REST Quote Fallback & Critical Alert Test
    # -------------------------------------------------------------

    def test_upstox_rest_quote_fallback_and_critical_alert(self):
        """poll_held_positions_quote uses Upstox REST LTP, writes to Redis with timestamp,
        never inserts fake bars into live_market_bars, and only runs when ticks are stale."""
        from backend.live_stream_service import LiveStreamService

        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM live_market_bars;"))
            # Active open position for RELIANCE (id=1)
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, signal_at, audit_status, net_pnl
                ) VALUES (
                    1, 'BUY', 10, 2500.0, CURRENT_TIMESTAMP, 'RECONCILED', NULL
                );
            """))
            conn.execute(text("""
                INSERT INTO instrument_provider_keys (instrument_id, provider, provider_key, mode_hint, is_active)
                VALUES (1, 'upstox_v3', 'NSE_EQ|INE002A01018', 'full', 1);
            """))

        recorded_upstox_response = json.dumps({
            "status": "success",
            "data": {
                "NSE_EQ:RELIANCE": {
                    "instrument_token": "NSE_EQ|INE002A01018",
                    "last_price": 2515.50
                }
            }
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = recorded_upstox_response
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///:memory:",
            "REDIS_URL": "redis://localhost:6379/0",
            "NIVESH_MARKET_DATA_PROVIDER": "upstox",
            "UPSTOX_ACCESS_TOKEN": "mock_upstox_token",
            "LIVE_ELIGIBLE": "FALSE",
            "NIVESH_LIVE_TRADING_ENABLED": "0"
        }):
            with patch("backend.live_stream_service.PostgresBarAggregator"):
                with patch("redis.Redis.from_url") as mock_redis_cls:
                    mock_redis = MagicMock()
                    mock_pipe = MagicMock()
                    mock_redis.pipeline.return_value = mock_pipe
                    mock_redis_cls.return_value = mock_redis

                    service = LiveStreamService()
                    service.engine = self.engine
                    service.redis = mock_redis
                    service.market_open = MagicMock(return_value=True)

                    with patch("urllib.request.urlopen", return_value=mock_resp):
                        service.poll_held_positions_quote()

                        # Verify Redis write with price AND timestamp
                        mock_pipe.hset.assert_any_call("nivesh:ticks:latest", "1", "2515.5")
                        ts_calls = [c for c in mock_pipe.hset.call_args_list if c[0][0] == "nivesh:ticks:timestamp"]
                        self.assertTrue(len(ts_calls) > 0, "Must write timestamp to nivesh:ticks:timestamp")

                        # Verify NO bars written to live_market_bars!
                        with self.engine.connect() as check_conn:
                            bar_count = check_conn.execute(text("""
                                SELECT COUNT(*) FROM live_market_bars WHERE instrument_id = 1
                            """)).scalar()
                            self.assertEqual(bar_count, 0, "REST fallback must never insert fake bars into live_market_bars")

    # -------------------------------------------------------------
    # 6. Empty Calendar Table on Weekday Holiday Test
    # -------------------------------------------------------------

    def test_empty_calendar_on_weekday_holiday_suppresses_stale_data_exit(self):
        """When exchange_trading_calendar table is empty on a weekday holiday (e.g. Dussehra 2026-10-20),
        STALE_DATA_EXIT must NOT be executed; only a CRITICAL alert is raised."""
        clear_calendar_cache()
        # 2026-10-20 is Tuesday (weekday=1), Dussehra. Empty calendar table.
        watermark = datetime(2026, 10, 20, 6, 0, 0, tzinfo=timezone.utc)  # 11:30 AM IST
        bar_time = watermark - timedelta(hours=2)

        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM exchange_trading_calendar;"))
            conn.execute(text("""
                INSERT INTO shadow_execution_audits (
                    instrument_id, side, quantity, theoretical_fill_price, stop_loss_price,
                    signal_at, audit_status, trade_mode, improvement_note
                ) VALUES (
                    1, 'BUY', 10, 2500.0, 2400.0, :sig_at, 'RECONCILED', 'SWING', '{}'
                )
            """), {"sig_at": bar_time - timedelta(days=1)})
            conn.execute(text("""
                INSERT INTO live_market_bars (
                    instrument_id, interval, bar_time, open_price, high_price, low_price, close_price, source
                ) VALUES (
                    1, '1minute', :bt, 2500.0, 2505.0, 2495.0, 2500.0, 'upstox_v3'
                )
            """), {"bt": bar_time})

        pm = PositionManager(database_url="sqlite:///:memory:", redis_client=self.mock_redis)
        pm.engine = self.engine
        pm.price_max_age_seconds = 60
        pm.price_max_age_swing_seconds = 60
        pm.stale_data_exit_minutes = 5

        pos = pm.get_open_positions()[0]
        # Simulate tracker already past 5 minutes threshold
        pm.stale_positions_tracker[pos["id"]] = watermark - timedelta(minutes=6)

        with self.assertLogs("nivesh.position_manager", level="CRITICAL") as cm:
            res = pm.evaluate_position(pos, watermark)

        # Assert position remains OPEN and NOT exited with STALE_DATA_EXIT
        self.assertIsNotNone(res)
        self.assertEqual(res["status"], "OPEN")
        self.assertNotEqual(res.get("exit_reason"), "STALE_DATA_EXIT")
        # Assert CRITICAL alert was raised
        alert_found = any("Suppressing STALE_DATA_EXIT" in record.getMessage() for record in cm.records)
        self.assertTrue(alert_found, "Must raise CRITICAL alert warning about missing calendar row suppressing exit")

    def test_calendar_caching_and_single_missing_warning(self):
        """Calendar lookup is cached per IST date; missing calendar logs warning only once per date."""
        clear_calendar_cache()
        target_dt = datetime(2026, 10, 20, 5, 0, 0, tzinfo=timezone.utc)
        target_date = target_dt.astimezone(IST).date()

        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM exchange_trading_calendar;"))

        # First call logs warning and caches None
        with self.assertLogs("nivesh.position_manager", level="WARNING") as cm:
            res1 = is_market_hours(target_dt, engine=self.engine)
            res2 = get_market_open_time(target_dt, engine=self.engine)
            res3 = get_calendar_session(target_date, engine=self.engine)

        warn_count = sum(1 for r in cm.records if f"No exchange_trading_calendar row for NSE on {target_date}" in r.getMessage())
        self.assertTrue(res1, "10:30 AM IST on weekday falls back to open market hours")
        self.assertFalse(is_market_hours(datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc), engine=self.engine))
        self.assertEqual(res2, dt_time(9, 15))
        self.assertIsNone(res3)

    # -------------------------------------------------------------
    # 7. Swing Rejection Skips Candidate and Records Reason Test
    # -------------------------------------------------------------

    def test_rejected_swing_skips_candidate_and_records_reason(self):
        """When swing setup is rejected, candidate is skipped with reason code SWING_REJECTED_<why>."""
        # 1. compute_swing_risk_parameters returns SWING_REJECTED_THIN_HISTORY
        res, reason = compute_swing_risk_parameters(
            symbol="INFY",
            side="BUY",
            entry_price=1500.0,
            daily_candles=[{"high": 1510, "low": 1490, "close": 1500}],  # < 15 candles
            return_reason=True,
        )
        self.assertIsNone(res)
        self.assertEqual(reason, SWING_REJECTED_THIN_HISTORY)

        # 2. Stop cap exceeded returns SWING_REJECTED_STOP_CAP_EXCEEDED
        daily_candles = [
            {"high": 100 + i, "low": 90 + i, "close": 95 + i} for i in range(20)
        ]
        daily_candles_wide = list(daily_candles)
        daily_candles_wide[0] = {"high": 100, "low": 10, "close": 95}
        res2, reason2 = compute_swing_risk_parameters(
            symbol="INFY",
            side="BUY",
            entry_price=100.0,
            daily_candles=daily_candles_wide,
            max_stop_atr=2.0,
            return_reason=True,
        )
        self.assertIsNone(res2)
        self.assertEqual(reason2, SWING_REJECTED_STOP_CAP_EXCEEDED)

    def test_rejected_swing_candidate_audit_funnel_logging(self):
        """Test production method LiveInferenceService._record_swing_rejection_audit.
        
        Asserts:
        - Uses INTEGER signal (1 for BUY, -1 for SELL), strictly enforced by table CHECK constraint.
        - Updates existing candidate audit row or inserts if missing.
        - Correctly sets selector_stage='swing_risk', accepted=False, and rejection_reason=SWING_REJECTED_<why>.
        - Gracefully handles schemas with and without trade_mode.
        """
        from datetime import datetime, timezone
        from backend.live_inference import LiveInferenceService
        now_dt = datetime.now(timezone.utc)

        # Create service instance bound to test engine
        service = LiveInferenceService.__new__(LiveInferenceService)
        service.engine = self.engine

        # Pre-insert candidate that was initially marked accepted in top10 with INTEGER signal (1 for BUY)
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO trade_candidate_audits (
                    observed_at, model_version, exchange, symbol, instrument_id,
                    signal, probability, decision_price, selector_stage, accepted,
                    rejection_reason, trade_mode
                ) VALUES (
                    :dt, 'v1.0', 'NSE', 'TCS', 1, 1, 0.85, 3500.0, 'accepted_top10', 1, NULL, 'SWING'
                )
            """), {"dt": now_dt})

        # Test updating existing candidate via production method
        item = {
            "symbol": "TCS",
            "instrument_id": 1,
            "exchange": "NSE",
            "signal": 1,
            "session": {"timestamp": now_dt},
            "probability": 0.85,
        }
        target = {"side": "BUY", "price": 3500.0}

        service._record_swing_rejection_audit(
            item=item,
            target=target,
            model_version="v1.0",
            reject_code=SWING_REJECTED_THIN_HISTORY,
        )

        # Verify audit row state in database
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT selector_stage, accepted, rejection_reason, trade_mode, signal
                FROM trade_candidate_audits WHERE symbol = 'TCS'
            """)).mappings().one()

            self.assertEqual(row["selector_stage"], "swing_risk")
            self.assertEqual(int(row["accepted"]), 0)
            self.assertEqual(row["rejection_reason"], SWING_REJECTED_THIN_HISTORY)
            self.assertEqual(row["signal"], 1)

        # Test inserting a new candidate that did not exist previously (e.g. INFY SELL)
        item_new = {
            "symbol": "INFY",
            "instrument_id": 3,
            "exchange": "NSE",
            "session": {"timestamp": now_dt},
            "probability": 0.72,
        }
        target_new = {"side": "SELL", "price": 1800.0}

        service._record_swing_rejection_audit(
            item=item_new,
            target=target_new,
            model_version="v1.0",
            reject_code=SWING_REJECTED_STOP_CAP_EXCEEDED,
        )

        with self.engine.connect() as conn:
            row_new = conn.execute(text("""
                SELECT selector_stage, accepted, rejection_reason, trade_mode, signal
                FROM trade_candidate_audits WHERE symbol = 'INFY'
            """)).mappings().one()

            self.assertEqual(row_new["selector_stage"], "swing_risk")
            self.assertEqual(int(row_new["accepted"]), 0)
            self.assertEqual(row_new["rejection_reason"], SWING_REJECTED_STOP_CAP_EXCEEDED)
            self.assertEqual(row_new["signal"], -1, "SELL side must be recorded as integer -1")

    def test_record_swing_rejection_audit_without_trade_mode_column(self):
        """Test production method when trade_mode column is not present (pre-v3_17 schema)."""
        from datetime import datetime, timezone
        from backend.live_inference import LiveInferenceService
        now_dt = datetime.now(timezone.utc)

        # Separate engine without trade_mode column
        from sqlalchemy import create_engine
        legacy_engine = create_engine("sqlite:///:memory:")
        with legacy_engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE trade_candidate_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TIMESTAMP,
                    model_version TEXT,
                    exchange TEXT,
                    symbol TEXT,
                    instrument_id INTEGER,
                    signal INTEGER CHECK(typeof(signal) = 'integer'),
                    probability NUMERIC,
                    decision_price NUMERIC,
                    selector_stage TEXT,
                    accepted BOOLEAN,
                    rejection_reason TEXT
                );
            """))

        service = LiveInferenceService.__new__(LiveInferenceService)
        service.engine = legacy_engine

        item = {
            "symbol": "WIPRO",
            "instrument_id": 4,
            "exchange": "NSE",
            "session": {"timestamp": now_dt},
            "probability": 0.65,
        }
        target = {"side": "BUY", "price": 450.0}

        # Must succeed without referencing non-existent trade_mode column
        service._record_swing_rejection_audit(
            item=item,
            target=target,
            model_version="v1.0",
            reject_code=SWING_REJECTED_THIN_HISTORY,
        )

        with legacy_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT selector_stage, accepted, rejection_reason, signal
                FROM trade_candidate_audits WHERE symbol = 'WIPRO'
            """)).mappings().one()

            self.assertEqual(row["selector_stage"], "swing_risk")
            self.assertEqual(int(row["accepted"]), 0)
            self.assertEqual(row["signal"], 1)

    def test_record_swing_rejection_audit_logs_warning_on_sql_error(self):
        """Test that LiveInferenceService._record_swing_rejection_audit logs at WARNING on SQL failure."""
        from datetime import datetime, timezone
        from backend.live_inference import LiveInferenceService
        now_dt = datetime.now(timezone.utc)

        # Point engine to an empty in-memory engine where trade_candidate_audits table does not exist
        from sqlalchemy import create_engine
        broken_engine = create_engine("sqlite:///:memory:")

        service = LiveInferenceService.__new__(LiveInferenceService)
        service.engine = broken_engine

        item = {"symbol": "SBIN", "session": {"timestamp": now_dt}}
        target = {"side": "BUY", "price": 800.0}

        with self.assertLogs("nivesh", level="WARNING") as cm:
            service._record_swing_rejection_audit(
                item=item,
                target=target,
                model_version="v1.0",
                reject_code=SWING_REJECTED_THIN_HISTORY,
            )

        self.assertTrue(
            any("Failed to record swing rejection in candidate audit log for SBIN" in r.getMessage() for r in cm.records),
            "Must log at WARNING level on SQL failure"
        )

    def test_nse_holidays_2026_file_accuracy(self):
        """Verify data/nse_holidays_2026.json accuracy against official NSE schedule:
        - Asserts every holiday falls on the expected weekday.
        - Asserts no weekend-only holiday is marked as a weekday closure.
        - Models Nov 8 (Sunday) as a special Muhurat session with announced hours, not CLOSED.
        - Removes Aug 25, Mar 20, and Mar 27.
        - Confirms all 16 official weekday holidays.
        """
        import json
        from pathlib import Path
        holiday_file = Path(__file__).resolve().parent.parent / "data" / "nse_holidays_2026.json"
        self.assertTrue(holiday_file.exists(), f"Missing holiday file at {holiday_file}")

        with open(holiday_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["year"], 2026)
        self.assertIn("172/2025", data["source"])
        holidays = {h["date"]: h for h in data["holidays"]}

        # Specific dates that must NOT be present
        self.assertNotIn("2026-08-25", holidays, "Aug 25 is not an NSE holiday")
        self.assertNotIn("2026-03-20", holidays, "Mar 20 is not a holiday (Id-Ul-Fitr is Saturday Mar 21)")
        self.assertNotIn("2026-03-27", holidays, "Mar 27 is not a holiday (Ram Navami is Mar 26)")

        # Verify Diwali Laxmi Pujan (Muhurat Trading) on Nov 8 (Sunday)
        nov8 = holidays.get("2026-11-08")
        self.assertIsNotNone(nov8)
        self.assertEqual(nov8["status"], "SPECIAL", "Nov 8 must be SPECIAL Muhurat session, not CLOSED")
        self.assertEqual(nov8["opens_at"], "18:15:00")
        self.assertEqual(nov8["closes_at"], "19:15:00")
        self.assertEqual(nov8["day"], "Sunday")

        # Official 16 weekday holidays for 2026
        expected_weekday_holidays = {
            "2026-01-15": ("Thursday", "Municipal Corporation Election - Maharashtra"),
            "2026-01-26": ("Monday", "Republic Day"),
            "2026-03-03": ("Tuesday", "Holi"),
            "2026-03-26": ("Thursday", "Shri Ram Navami"),
            "2026-03-31": ("Tuesday", "Shri Mahavir Jayanti"),
            "2026-04-03": ("Friday", "Good Friday"),
            "2026-04-14": ("Tuesday", "Dr. Baba Saheb Ambedkar Jayanti"),
            "2026-05-01": ("Friday", "Maharashtra Day"),
            "2026-05-28": ("Thursday", "Bakri Id"),
            "2026-06-26": ("Friday", "Muharram"),
            "2026-09-14": ("Monday", "Ganesh Chaturthi"),
            "2026-10-02": ("Friday", "Mahatma Gandhi Jayanti"),
            "2026-10-20": ("Tuesday", "Dussehra"),
            "2026-11-10": ("Tuesday", "Diwali-Balipratipada"),
            "2026-11-24": ("Tuesday", "Prakash Gurpurb Sri Guru Nanak Dev"),
            "2026-12-25": ("Friday", "Christmas"),
        }

        weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        for date_str, (exp_day, exp_name) in expected_weekday_holidays.items():
            self.assertIn(date_str, holidays, f"Missing expected weekday holiday {date_str} ({exp_name})")
            h = holidays[date_str]
            self.assertEqual(h["status"], "CLOSED")
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            actual_day = weekday_names[dt.weekday()]
            self.assertEqual(actual_day, exp_day, f"{date_str} must be a {exp_day}, was {actual_day}")
            self.assertEqual(h["day"], exp_day)
            self.assertTrue(dt.weekday() < 5, f"{date_str} must be a weekday")

        # Weekend-only holidays in the file
        weekend_holidays = {
            "2026-02-15": "Sunday",    # Mahashivratri
            "2026-03-21": "Saturday",  # Id-Ul-Fitr
            "2026-08-15": "Saturday",  # Independence Day
        }
        for date_str, exp_day in weekend_holidays.items():
            self.assertIn(date_str, holidays)
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            self.assertTrue(dt.weekday() >= 5, f"{date_str} must be a weekend")
            self.assertEqual(weekday_names[dt.weekday()], exp_day)


if __name__ == "__main__":
    unittest.main()

