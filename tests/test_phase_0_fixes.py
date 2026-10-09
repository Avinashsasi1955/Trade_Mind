"""Unit tests for Phase 0 and Phase 0b core data and execution blockers."""
import os
import unittest
from datetime import datetime, date, time as dt_time, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from backend.market_ingestion_v3 import PostgresBarAggregator, IST
from backend.execution import (
    emergency_cancel_open_orders,
    reconcile_intents,
    submit_intent,
    get_broker_adapter,
    lookup_upstox_provider_key,
    set_execution_engine,
)
from backend.upstox_adapter import UpstoxAdapter
from backend.position_manager import PositionManager, execute_exit
from backend.live_market_view import snapshot as live_market_snapshot


class Phase0BlockerTests(unittest.TestCase):
    def test_execute_exit_with_password_url_uses_existing_engine(self):
        """Phase 0b.1: execute_exit must take the existing engine directly and not reconstruct from str(url) (which masks password with ***)."""
        password_url_str = "postgresql+psycopg2://trade_trader:SuperSecretP@ss99!@localhost:5432/trademind_prod"
        sa_url = make_url(password_url_str)

        # In SQLAlchemy 2.x, str(url) masks password
        self.assertIn("***", str(sa_url))
        self.assertNotIn("SuperSecretP@ss99!", str(sa_url))

        # Test module-level execute_exit with existing engine
        mock_engine = MagicMock()
        mock_engine.url = sa_url

        with patch("backend.position_manager.record_shadow_exit") as mock_record:
            res = execute_exit(mock_engine, audit_id=42, price=2500.5, reason="STOP_LOSS")
            self.assertTrue(res)
            mock_record.assert_called_once()
            call_args = mock_record.call_args
            # Verify the exact engine instance was passed, without recreating from str(engine.url)
            self.assertIs(call_args[0][0], mock_engine)
            self.assertEqual(call_args[0][1], 42)
            self.assertEqual(call_args[0][2], Decimal("2500.5"))
            self.assertEqual(call_args[0][3], "STOP_LOSS")

        # Test check_instant_exit_breach in PostgresBarAggregator uses self.engine directly
        agg = PostgresBarAggregator.__new__(PostgresBarAggregator)
        agg.engine = mock_engine
        agg.open_trades_cache = {
            1: [
                {
                    "id": 101,
                    "side": "BUY",
                    "stop_loss_price": Decimal("100.0"),
                    "take_profit_price": Decimal("150.0"),
                }
            ]
        }
        with patch("backend.position_manager.record_shadow_exit") as mock_record_exit:
            now_dt = datetime.now(timezone.utc)
            # Price breaches stop loss (99.0 <= 100.0)
            agg.check_instant_exit_breach(1, Decimal("99.0"), now_dt)
            mock_record_exit.assert_called_once()
            # Verified passed agg.engine directly
            self.assertIs(mock_record_exit.call_args[0][0], mock_engine)
            self.assertEqual(mock_record_exit.call_args[0][1], 101)
            self.assertEqual(mock_record_exit.call_args[0][3], "STOP_LOSS")

    def test_volume_baseline_daily_session_rollover(self):
        """Phase 0b.2: At new IST session open (09:15), baseline = 0 so opening bar keeps opening-auction volume."""
        agg = PostgresBarAggregator.__new__(PostgresBarAggregator)
        agg.states = {}
        agg.tokens = {101: 1}
        agg.token_types = {101: "EQ"}
        agg.vix_tokens = set()
        agg.open_trades_cache = {}
        agg.last_open_trades_sync = 0.0
        agg.prev_cumulative_volume = {}
        agg.last_completed_bar_time = {}
        agg.sanitizer = MagicMock()
        agg.sanitizer.validate_tick.return_value = (True, None)
        agg._save_bars = MagicMock()
        agg.engine = MagicMock()

        # Day 1: 2026-10-08 09:15 IST (03:45 UTC) - cumulative volume 10,000
        t1_day1 = datetime(2026, 10, 8, 3, 45, 0, tzinfo=timezone.utc)
        tick1 = {"instrument_token": 101, "last_price": 100.0, "volume": 10000, "exchange_timestamp": t1_day1}
        agg.ingest([tick1])

        # Day 1: 2026-10-08 09:16 IST (03:46 UTC) - cumulative volume 12,500
        t2_day1 = datetime(2026, 10, 8, 3, 46, 0, tzinfo=timezone.utc)
        tick2 = {"instrument_token": 101, "last_price": 101.0, "volume": 12500, "exchange_timestamp": t2_day1}
        agg.ingest([tick2])

        # Verify Day 1 cached volume for instrument 1 is (2026-10-08, 12500)
        cached_date, cached_vol = agg.prev_cumulative_volume[1]
        self.assertEqual(cached_date, date(2026, 10, 8))
        self.assertEqual(cached_vol, 12500)

        agg.engine = MagicMock()
        # Day 2: 2026-10-09 09:15:00 IST (03:45 UTC) - opening tick arrives at session open with 500 auction volume
        t1_day2 = datetime(2026, 10, 9, 3, 45, 0, tzinfo=timezone.utc)
        tick_day2_1 = {"instrument_token": 101, "last_price": 102.0, "volume": 500, "exchange_timestamp": t1_day2}
        agg.ingest([tick_day2_1])

        # At session open (09:15 IST), baseline is set to 0 to preserve opening-auction volume
        state_0915 = agg.states[(1, "1minute")]
        self.assertEqual(state_0915["baseline_volume"], 0)
        self.assertEqual(state_0915["volume"], 500)

        # Day 2: 2026-10-09 09:15:30 IST - volume increases to 800 (last tick in 09:15 bar)
        t2_day2 = datetime(2026, 10, 9, 3, 45, 30, tzinfo=timezone.utc)
        tick_day2_2 = {"instrument_token": 101, "last_price": 103.0, "volume": 800, "exchange_timestamp": t2_day2}
        agg.ingest([tick_day2_2])

        # Day 2: 2026-10-09 09:16:00 IST - tick in next minute triggers rollover of 09:15 bar
        t3_day2 = datetime(2026, 10, 9, 3, 46, 0, tzinfo=timezone.utc)
        tick_day2_3 = {"instrument_token": 101, "last_price": 104.0, "volume": 900, "exchange_timestamp": t3_day2}
        agg.ingest([tick_day2_3])

        # Verify completed 1-minute bar for Day 2 09:15 equals the cumulative volume at its last tick (800)
        saved_calls = agg._save_bars.call_args_list
        self.assertTrue(len(saved_calls) > 0)
        saved_bars = saved_calls[-1][0][0]
        completed_1m = [b for b in saved_bars if b["interval"] == "1minute" and b["bar_time"].date() == date(2026, 10, 9)]
        self.assertEqual(len(completed_1m), 1)
        self.assertEqual(completed_1m[0]["volume"], 800)

        # Mid-session process start test: 11:30 IST
        # Mid-session process start sets baseline = first tick's raw volume
        t_mid1 = datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)  # 11:30 IST
        agg.states.clear()
        agg.last_completed_bar_time.clear()
        agg.tokens[202] = 2
        agg.token_types[202] = "EQ"
        agg.ingest([{"instrument_token": 202, "last_price": 50.0, "volume": 1500, "exchange_timestamp": t_mid1}])
        state_mid = agg.states[(2, "1minute")]
        self.assertEqual(state_mid["baseline_volume"], 1500)
        self.assertEqual(state_mid["volume"], 0)

        t_mid2 = datetime(2026, 10, 9, 6, 0, 30, tzinfo=timezone.utc)
        agg.ingest([{"instrument_token": 202, "last_price": 50.5, "volume": 1700, "exchange_timestamp": t_mid2}])
        self.assertEqual(agg.states[(2, "1minute")]["volume"], 200)

    def test_upstox_submit_intent_uses_instrument_provider_keys(self):
        """Phase 0b.3: submit_intent must look up provider_key from instrument_provider_keys and reject if not found."""
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchone.side_effect = [
            # 1. order_intents row
            {
                "id": 1,
                "user_id": 1,
                "status": "APPROVED",
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "transaction_type": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "product": "CNC",
                "limit_price": 2500.0,
            },
            # 2. system_promotion_ledger row
            {"live_eligible": True},
        ]

        mock_adapter = MagicMock(spec=UpstoxAdapter)
        mock_adapter.configured = True
        mock_adapter.place_order.return_value = {"order_id": "UP-9876"}

        with patch("backend.execution.LIVE_TRADING_ENABLED", True), \
             patch("backend.execution.LIVE_ELIGIBLE", True), \
             patch("backend.execution.get_broker_adapter", return_value=mock_adapter):

            # Case A: provider_key is missing in instrument_provider_keys -> must reject intent
            with patch("backend.execution.lookup_upstox_provider_key", return_value=None):
                with self.assertRaises(ValueError) as ctx:
                    submit_intent(mock_db, user_id=1, intent_id=1)
                self.assertIn("Missing Upstox provider_key in instrument_provider_keys", str(ctx.exception))
                self.assertIn("NSE:RELIANCE", str(ctx.exception))

            # Case B: provider_key is found -> must pass it as instrument_token
            mock_db.execute.return_value.fetchone.side_effect = [
                {
                    "id": 1,
                    "user_id": 1,
                    "status": "APPROVED",
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "transaction_type": "BUY",
                    "quantity": 10,
                    "order_type": "LIMIT",
                    "product": "CNC",
                    "limit_price": 2500.0,
                },
                {"live_eligible": True},
            ]
            with patch("backend.execution.lookup_upstox_provider_key", return_value="NSE_EQ|INE002A01018"), \
                 patch("backend.execution.intent_detail", return_value={"id": 1, "status": "SUBMITTED"}):
                res = submit_intent(mock_db, user_id=1, intent_id=1)
                self.assertEqual(res["status"], "SUBMITTED")
                # Assert place_order received instrument_token="NSE_EQ|INE002A01018"
                mock_adapter.place_order.assert_called_once()
                self.assertEqual(mock_adapter.place_order.call_args[1].get("instrument_token"), "NSE_EQ|INE002A01018")

    def test_upstox_adapter_requires_explicit_instrument_token(self):
        """Phase 0b.3: Upstox place_order must reject calls without explicit instrument_token (no key guessing)."""
        adapter = UpstoxAdapter(api_key="test_key", access_token="test_token")
        adapter._request = MagicMock(return_value={"order_id": "UP-1234"})

        # Without explicit instrument_token -> raises ValueError
        with self.assertRaises(ValueError) as ctx:
            adapter.place_order(symbol="RELIANCE", action="BUY", quantity=1, price=2500.0, exchange="NSE")
        self.assertIn("Explicit Upstox instrument_token required", str(ctx.exception))

        # With explicit instrument_token -> passes to payload
        adapter.place_order(
            symbol="RELIANCE",
            action="BUY",
            quantity=1,
            price=2500.0,
            exchange="NSE",
            instrument_token="NSE_EQ|INE002A01018",
        )
        payload = adapter._request.call_args[1]["data"]
        self.assertEqual(payload["instrument_token"], "NSE_EQ|INE002A01018")

    def test_upstox_order_access_token_no_fallback(self):
        """Phase 0b.4: UPSTOX_ORDER_ACCESS_TOKEN must not fall back to UPSTOX_ACCESS_TOKEN or analytics token."""
        # When UPSTOX_ORDER_ACCESS_TOKEN is missing, routing to Upstox raises ValueError
        with patch("backend.execution.BROKER_ROUTING", "upstox"), \
             patch("backend.execution.UPSTOX_ORDER_ACCESS_TOKEN", ""):
            with self.assertRaises(ValueError) as ctx:
                get_broker_adapter(user_id=1)
            self.assertIn("UPSTOX_ORDER_ACCESS_TOKEN is missing", str(ctx.exception))

        # When creating UpstoxAdapter with empty token, _request raises RuntimeError
        unauthed_adapter = UpstoxAdapter(api_key="key", access_token="")
        self.assertFalse(unauthed_adapter.configured)
        with self.assertRaises(RuntimeError) as ctx:
            unauthed_adapter._request("/order/retrieve-all")
        self.assertIn("UPSTOX_ORDER_ACCESS_TOKEN is missing or not configured", str(ctx.exception))

    def test_emergency_cancel_preserves_untagged_and_manual_orders(self):
        """Phase 0.2: emergency_cancel_open_orders must never cancel untagged or manual user orders."""
        mock_adapter = MagicMock()
        mock_adapter.configured = True
        mock_adapter.orders.return_value = [
            {"order_id": "ORD-1", "status": "OPEN", "tag": "TradeMind", "variety": "regular"},
            {"order_id": "ORD-2", "status": "OPEN", "tag": "NIVESH_AI", "variety": "regular"},
            {"order_id": "ORD-3", "status": "OPEN", "tag": "MANUAL_TRADE", "variety": "regular"},
            {"order_id": "ORD-4", "status": "OPEN", "tag": None, "variety": "regular"},
            {"order_id": "ORD-5", "status": "COMPLETE", "tag": "TradeMind", "variety": "regular"},
        ]

        with patch("backend.execution.LIVE_TRADING_ENABLED", True), \
             patch("backend.execution.get_broker_adapter", return_value=mock_adapter):
            result = emergency_cancel_open_orders(MagicMock(), user_id=1)

        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["cancelled"], 2)
        # Assert ORD-1 and ORD-2 were cancelled, ORD-3 (manual) and ORD-4 (untagged) were untouched
        cancelled_ids = [c[0][0] for c in mock_adapter.cancel_order.call_args_list]
        self.assertEqual(set(cancelled_ids), {"ORD-1", "ORD-2"})

    def test_upstox_position_field_normalization(self):
        """Phase 0.3: reconcile_intents must parse Upstox trading_symbol and Kite tradingsymbol."""
        mock_db = MagicMock()
        mock_db.execute.return_value.fetchall.side_effect = [
            [],  # no open pending intents to update
            [{"symbol": "RELIANCE", "quantity": 10}, {"symbol": "INFY", "quantity": 5}],  # local holdings
        ]
        mock_adapter = MagicMock()
        mock_adapter.configured = True
        mock_adapter.orders.return_value = []
        # Upstox returns trading_symbol (with underscore), Kite returns tradingsymbol
        mock_adapter.positions.return_value = [
            {"trading_symbol": "RELIANCE", "quantity": 10},
            {"tradingsymbol": "INFY", "quantity": 5},
        ]

        with patch("backend.execution.LIVE_TRADING_ENABLED", True), \
             patch("backend.execution.get_broker_adapter", return_value=mock_adapter):
            result = reconcile_intents(mock_db, user_id=1)

        self.assertEqual(result["position_discrepancies"], [])
        self.assertEqual(result["reason"], "Reconciled")

    def test_security_master_fails_closed_in_production(self):
        """Phase 0.5: In production/staging, missing security_master.json must raise RuntimeError."""
        from backend.security_master import load_security_master
        with patch("pathlib.Path.is_file", return_value=False), \
             patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            load_security_master.cache_clear()
            with self.assertRaises(RuntimeError) as ctx:
                load_security_master()
            self.assertIn("Fail-closed invariant triggered", str(ctx.exception))
            load_security_master.cache_clear()

    def test_production_database_outage_returns_explicit_degraded_status(self):
        """Phase 0.8: In production, live_market_view outage must return explicit degraded payload."""
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            # Connect to invalid port 59999 to guarantee connection failure
            result = live_market_snapshot("postgresql://invalid:invalid@localhost:59999/invalid")
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["data_mode"], "database_unavailable")
            self.assertFalse(result["orders_allowed"])


class Phase0cProviderKeySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        cls.engine = create_engine("sqlite:///:memory:")
        with cls.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE instrument_master (
                    id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1
                );
            """))
            conn.execute(text("""
                CREATE TABLE instrument_provider_keys (
                    id INTEGER PRIMARY KEY,
                    instrument_id INTEGER NOT NULL REFERENCES instrument_master(id),
                    provider TEXT NOT NULL,
                    provider_key TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    last_synced_at TEXT
                );
            """))
            # Populate test data:
            # 1. RELIANCE (active instrument, active key)
            conn.execute(text("INSERT INTO instrument_master (id, symbol, exchange, is_active) VALUES (1, 'RELIANCE', 'NSE', 1);"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (1, 1, 'upstox_v3', 'NSE_EQ|INE002A01018', 1, '2026-10-08 09:15:00');"))

            # 2. SBIN (multiple keys, newest last_synced_at wins)
            conn.execute(text("INSERT INTO instrument_master (id, symbol, exchange, is_active) VALUES (2, 'SBIN', 'NSE', 1);"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (2, 2, 'upstox_v3', 'SBIN_OLD_KEY', 1, '2026-09-01 10:00:00');"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (3, 2, 'upstox_v3', 'SBIN_NEW_KEY', 1, '2026-10-09 10:00:00');"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (4, 2, 'upstox_v3', 'SBIN_NULL_DATE', 1, NULL);"))

            # 3. TCS (inactive key ignored)
            conn.execute(text("INSERT INTO instrument_master (id, symbol, exchange, is_active) VALUES (3, 'TCS', 'NSE', 1);"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (5, 3, 'upstox_v3', 'TCS_INACTIVE_KEY', 0, '2026-10-08 09:15:00');"))

            # 4. WIPRO (inactive instrument ignored)
            conn.execute(text("INSERT INTO instrument_master (id, symbol, exchange, is_active) VALUES (4, 'WIPRO', 'NSE', 0);"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (6, 4, 'upstox_v3', 'WIPRO_KEY', 1, '2026-10-08 09:15:00');"))

            # 5. INFY (non-upstox provider ignored)
            conn.execute(text("INSERT INTO instrument_master (id, symbol, exchange, is_active) VALUES (5, 'INFY', 'NSE', 1);"))
            conn.execute(text("INSERT INTO instrument_provider_keys (id, instrument_id, provider, provider_key, is_active, last_synced_at) VALUES (7, 5, 'zerodha', '256265', 1, '2026-10-08 09:15:00');"))

    def test_sql_lookup_found(self):
        """Phase 0c.2: Real SQL lookup returns active Upstox provider_key."""
        key = lookup_upstox_provider_key("RELIANCE", "NSE", engine=self.engine)
        self.assertEqual(key, "NSE_EQ|INE002A01018")
        # Case insensitive
        key_lower = lookup_upstox_provider_key("reliance", "nse", engine=self.engine)
        self.assertEqual(key_lower, "NSE_EQ|INE002A01018")

    def test_sql_lookup_newest_last_synced_at_wins(self):
        """Phase 0c.2: Real SQL query chooses newest last_synced_at row (DESC NULLS LAST)."""
        key = lookup_upstox_provider_key("SBIN", "NSE", engine=self.engine)
        self.assertEqual(key, "SBIN_NEW_KEY")

    def test_sql_lookup_inactive_key_ignored(self):
        """Phase 0c.2: Real SQL query ignores inactive provider keys."""
        key = lookup_upstox_provider_key("TCS", "NSE", engine=self.engine)
        self.assertIsNone(key)

    def test_sql_lookup_inactive_instrument_ignored(self):
        """Phase 0c.2: Real SQL query ignores inactive instruments."""
        key = lookup_upstox_provider_key("WIPRO", "NSE", engine=self.engine)
        self.assertIsNone(key)

    def test_sql_lookup_not_found(self):
        """Phase 0c.2: Real SQL query returns None when instrument is missing."""
        key = lookup_upstox_provider_key("HDFCBANK", "NSE", engine=self.engine)
        self.assertIsNone(key)
        # Non-upstox provider
        key_infy = lookup_upstox_provider_key("INFY", "NSE", engine=self.engine)
        self.assertIsNone(key_infy)

    def test_sql_lookup_db_error_path(self):
        """Phase 0c.2: Real SQL query failure raises and logs RuntimeError."""
        from sqlalchemy import create_engine
        empty_engine = create_engine("sqlite:///:memory:")  # has no tables
        with self.assertRaises(RuntimeError) as ctx:
            lookup_upstox_provider_key("RELIANCE", "NSE", engine=empty_engine)
        self.assertIn("Database error while looking up Upstox provider key", str(ctx.exception))

    def test_sql_lookup_unconfigured_database_raises_explicitly(self):
        """Phase 0c.1: Empty DATABASE_URL raises explicit database unavailable RuntimeError."""
        set_execution_engine(None)
        with patch("backend.config.DATABASE_URL", ""):
            with self.assertRaises(RuntimeError) as ctx:
                lookup_upstox_provider_key("RELIANCE", "NSE", engine=None)
            self.assertIn("DATABASE_URL is not configured", str(ctx.exception))

    def test_submit_intent_with_real_sql_execution(self):
        """Phase 0c.2: submit_intent exercises real SQL for found, missing, and db error paths without mocking lookup."""
        set_execution_engine(self.engine)

        mock_db = MagicMock()
        mock_adapter = MagicMock(spec=UpstoxAdapter)
        mock_adapter.configured = True
        mock_adapter.place_order.return_value = {"order_id": "UP-REAL-SQL-1"}

        with patch("backend.execution.LIVE_TRADING_ENABLED", True), \
             patch("backend.execution.LIVE_ELIGIBLE", True), \
             patch("backend.execution.get_broker_adapter", return_value=mock_adapter), \
             patch("backend.execution.intent_detail", return_value={"id": 1, "status": "SUBMITTED"}):

            # 1. Found path (RELIANCE): passes real token from SQL
            mock_db.execute.return_value.fetchone.side_effect = [
                {
                    "id": 1,
                    "user_id": 1,
                    "status": "APPROVED",
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "transaction_type": "BUY",
                    "quantity": 10,
                    "order_type": "LIMIT",
                    "product": "CNC",
                    "limit_price": 2500.0,
                },
                {"live_eligible": True},
            ]
            res = submit_intent(mock_db, user_id=1, intent_id=1)
            self.assertEqual(res["status"], "SUBMITTED")
            mock_adapter.place_order.assert_called_once()
            self.assertEqual(mock_adapter.place_order.call_args[1].get("instrument_token"), "NSE_EQ|INE002A01018")

            # 2. Missing path (TCS - inactive key in SQL): raises ValueError (broker rejection guard)
            mock_adapter.place_order.reset_mock()
            mock_db.execute.return_value.fetchone.side_effect = [
                {
                    "id": 2,
                    "user_id": 1,
                    "status": "APPROVED",
                    "symbol": "TCS",
                    "exchange": "NSE",
                    "transaction_type": "BUY",
                    "quantity": 5,
                    "order_type": "LIMIT",
                    "product": "CNC",
                    "limit_price": 3500.0,
                },
                {"live_eligible": True},
            ]
            with self.assertRaises(ValueError) as ctx:
                submit_intent(mock_db, user_id=1, intent_id=2)
            self.assertIn("Missing Upstox provider_key in instrument_provider_keys for NSE:TCS", str(ctx.exception))
            mock_adapter.place_order.assert_not_called()

            # 3. Database outage path: raises RuntimeError (distinguishable from missing key)
            from sqlalchemy import create_engine
            broken_engine = create_engine("sqlite:///:memory:")  # no tables
            set_execution_engine(broken_engine)
            mock_db.execute.return_value.fetchone.side_effect = [
                {
                    "id": 3,
                    "user_id": 1,
                    "status": "APPROVED",
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "transaction_type": "BUY",
                    "quantity": 10,
                    "order_type": "LIMIT",
                    "product": "CNC",
                    "limit_price": 2500.0,
                },
                {"live_eligible": True},
            ]
            with self.assertRaises(RuntimeError) as ctx:
                submit_intent(mock_db, user_id=1, intent_id=3)
            self.assertIn("Database error while looking up Upstox provider key", str(ctx.exception))
            mock_adapter.place_order.assert_not_called()

            # Reset engine
            set_execution_engine(None)


if __name__ == "__main__":
    unittest.main()
