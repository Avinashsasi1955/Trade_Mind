"""Unit tests validating candle volume aggregation, single-tick illiquid volume, and late-tick protection."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from backend.market_ingestion_v3 import PostgresBarAggregator


class TestMarketIngestionVolume(unittest.TestCase):
    def setUp(self):
        # Initialize aggregator with a mocked DB engine
        with patch.object(PostgresBarAggregator, "refresh_tokens"):
            self.agg = PostgresBarAggregator("sqlite:///:memory:", source="upstox_v3")
        self.agg.tokens = {101: 1}  # Token 101 -> Instrument 1
        self.agg.token_types = {101: "OPT"}
        self.agg._save_bars = MagicMock()
        self.agg.refresh_open_trades = MagicMock()

    def test_single_tick_illiquid_contract_volume(self):
        """A contract that ticks once a minute must carry previous cumulative volume and record actual trades."""
        # 10:00:30 tick with 100 contracts cumulative volume
        t1 = datetime(2026, 6, 29, 4, 30, 30, tzinfo=timezone.utc)  # 10:00:30 IST is continuous market
        # Note: 10:00 IST is 04:30 UTC. Let's make sure _is_continuous_market_bar accepts it:
        # IST 10:00 is Monday-Friday weekday, between 09:15 and 15:30.
        tick1 = {"instrument_token": 101, "last_price": 50.0, "volume": 100, "exchange_timestamp": t1}
        self.agg.ingest([tick1])

        # 10:01:45 tick with 500 cumulative contracts (400 contracts traded in minute 10:01)
        t2 = datetime(2026, 6, 29, 4, 31, 45, tzinfo=timezone.utc)
        tick2 = {"instrument_token": 101, "last_price": 55.0, "volume": 500, "exchange_timestamp": t2}
        self.agg.ingest([tick2])

        # Rolling over into 10:02
        t3 = datetime(2026, 6, 29, 4, 32, 10, tzinfo=timezone.utc)
        tick3 = {"instrument_token": 101, "last_price": 56.0, "volume": 550, "exchange_timestamp": t3}
        self.agg.ingest([tick3])

        # Check completed bars saved
        saved_bars = []
        for call_args in self.agg._save_bars.call_args_list:
            saved_bars.extend(call_args[0][0])

        minute_bars = [b for b in saved_bars if b["interval"] == "1minute"]
        self.assertGreaterEqual(len(minute_bars), 2)
        # Minute 1 (10:01:00) bar must have recorded 500 - 100 = 400 volume!
        bar_10_01 = next(b for b in minute_bars if b["bar_time"] == datetime(2026, 6, 29, 4, 31, 0, tzinfo=timezone.utc))
        self.assertEqual(bar_10_01["volume"], 400)

    def test_late_tick_does_not_reopen_or_overwrite_bar(self):
        """A late tick within sanitizer 3-second tolerance arriving after bar rollover must not reopen completed bar."""
        # 10:00:10 tick
        t1 = datetime(2026, 6, 29, 4, 30, 10, tzinfo=timezone.utc)
        tick1 = {"instrument_token": 101, "last_price": 100.0, "volume": 1000, "exchange_timestamp": t1}
        self.agg.ingest([tick1])

        # 10:00:55 tick: volume reaches 1500 (500 traded in 10:00)
        t2 = datetime(2026, 6, 29, 4, 30, 55, tzinfo=timezone.utc)
        tick2 = {"instrument_token": 101, "last_price": 102.0, "volume": 1500, "exchange_timestamp": t2}
        self.agg.ingest([tick2])

        # 10:01:01 tick rolls over to 10:01 bar
        t3 = datetime(2026, 6, 29, 4, 31, 1, tzinfo=timezone.utc)
        tick3 = {"instrument_token": 101, "last_price": 103.0, "volume": 1520, "exchange_timestamp": t3}
        self.agg.ingest([tick3])

        # Verify 10:00:00 minute bar was completed with 500 volume
        saved_bars = []
        for call_args in self.agg._save_bars.call_args_list:
            saved_bars.extend(call_args[0][0])
        bar_10_00 = next(b for b in saved_bars if b["interval"] == "1minute" and b["bar_time"] == datetime(2026, 6, 29, 4, 30, 0, tzinfo=timezone.utc))
        self.assertEqual(bar_10_00["volume"], 500)

        # Clear mock history
        self.agg._save_bars.reset_mock()

        # Now late tick arrives: timestamp is 10:00:59 (within 3 seconds of t3, so sanitizer passes it)
        t_late = datetime(2026, 6, 29, 4, 30, 59, tzinfo=timezone.utc)
        tick_late = {"instrument_token": 101, "last_price": 102.0, "volume": 1505, "exchange_timestamp": t_late}
        self.agg.ingest([tick_late])

        # It must NOT save a new duplicate 10:00 bar or evict 10:01 bar
        self.agg._save_bars.assert_not_called()
        key_1m = (1, "1minute")
        self.assertEqual(self.agg.states[key_1m]["bar_time"], datetime(2026, 6, 29, 4, 31, 0, tzinfo=timezone.utc))

    def test_partial_snapshots_and_flush_closed(self):
        """Partial snapshots and flush_closed must correctly compute volume from baseline."""
        t1 = datetime(2026, 6, 29, 4, 30, 10, tzinfo=timezone.utc)
        tick1 = {"instrument_token": 101, "last_price": 100.0, "volume": 500, "exchange_timestamp": t1}
        self.agg.ingest([tick1])

        t2 = datetime(2026, 6, 29, 4, 30, 40, tzinfo=timezone.utc)
        tick2 = {"instrument_token": 101, "last_price": 101.0, "volume": 650, "exchange_timestamp": t2}
        self.agg.ingest([tick2])

        snaps = self.agg.partial_snapshots()
        snap_1m = next(s for s in snaps if s["interval"] == "1minute")
        self.assertEqual(snap_1m["volume"], 150)

        # Flushing at 10:31:05 (past 10:30 bar closure)
        watermark = datetime(2026, 6, 29, 4, 31, 5, tzinfo=timezone.utc)
        flushed_count = self.agg.flush_closed(watermark)
        self.assertGreaterEqual(flushed_count, 1)

        saved_bars = []
        for call_args in self.agg._save_bars.call_args_list:
            saved_bars.extend(call_args[0][0])
        bar_10_00 = next(b for b in saved_bars if b["interval"] == "1minute" and b["bar_time"] == datetime(2026, 6, 29, 4, 30, 0, tzinfo=timezone.utc))
        self.assertEqual(bar_10_00["volume"], 150)


if __name__ == "__main__":
    unittest.main()
