"""Unit tests for Phase 0 core data and execution blockers."""
import os
import unittest
from datetime import datetime, date, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from backend.market_ingestion_v3 import PostgresBarAggregator, IST
from backend.execution import emergency_cancel_open_orders, reconcile_intents, get_broker_adapter
from backend.upstox_adapter import UpstoxAdapter
from backend.position_manager import PositionManager
from backend.live_market_view import snapshot as live_market_snapshot


class Phase0BlockerTests(unittest.TestCase):
    def test_volume_baseline_daily_session_rollover(self):
        """Phase 0.1: Cumulative volume reset across days or counter reset must not erase bar volume."""
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
        # Day 2: 2026-10-09 09:15:00 IST (03:45 UTC) - exchange resets daily volume to 500
        t1_day2 = datetime(2026, 10, 9, 3, 45, 0, tzinfo=timezone.utc)
        tick_day2_1 = {"instrument_token": 101, "last_price": 102.0, "volume": 500, "exchange_timestamp": t1_day2}
        agg.ingest([tick_day2_1])

        # Verify Day 2 baseline reset to 500 rather than subtracting Day 1's 12500 (which would produce volume 0)
        cached_date_d2, cached_vol_d2 = agg.prev_cumulative_volume[1]
        self.assertEqual(cached_date_d2, date(2026, 10, 9))
        self.assertEqual(cached_vol_d2, 500)

        # Day 2: 2026-10-09 09:15:30 IST - volume increases to 800 (delta 300) inside 09:15 bar
        t2_day2 = datetime(2026, 10, 9, 3, 45, 30, tzinfo=timezone.utc)
        tick_day2_2 = {"instrument_token": 101, "last_price": 103.0, "volume": 800, "exchange_timestamp": t2_day2}
        agg.ingest([tick_day2_2])

        # Day 2: 2026-10-09 09:16:00 IST - tick in next minute triggers rollover of 09:15 bar
        t3_day2 = datetime(2026, 10, 9, 3, 46, 0, tzinfo=timezone.utc)
        tick_day2_3 = {"instrument_token": 101, "last_price": 104.0, "volume": 900, "exchange_timestamp": t3_day2}
        agg.ingest([tick_day2_3])

        # Verify completed 1-minute bar for Day 2 09:15 has volume 300 (800 - 500)
        saved_calls = agg._save_bars.call_args_list
        self.assertTrue(len(saved_calls) > 0)
        saved_bars = saved_calls[-1][0][0]
        completed_1m = [b for b in saved_bars if b["interval"] == "1minute" and b["bar_time"].date() == date(2026, 10, 9)]
        self.assertEqual(len(completed_1m), 1)
        self.assertEqual(completed_1m[0]["volume"], 300)

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

    def test_upstox_adapter_derivative_instrument_key(self):
        """Phase 0.3: Upstox place_order formats F&O keys as NSE_FO|... and equities as NSE_EQ|..."""
        adapter = UpstoxAdapter(api_key="test", access_token="test_token")
        adapter._request = MagicMock(return_value={"order_id": "UP-1234"})

        # Equity
        res_eq = adapter.place_order(symbol="RELIANCE", action="BUY", quantity=1, price=2500.0, exchange="NSE")
        payload_eq = adapter._request.call_args_list[0][1]["data"]
        self.assertEqual(payload_eq["instrument_token"], "NSE_EQ|RELIANCE")

        # F&O option
        res_opt = adapter.place_order(symbol="NIFTY26OCT25000CE", action="BUY", quantity=50, price=100.0, exchange="NFO")
        payload_opt = adapter._request.call_args_list[1][1]["data"]
        self.assertEqual(payload_opt["instrument_token"], "NSE_FO|NIFTY26OCT25000CE")

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


if __name__ == "__main__":
    unittest.main()
