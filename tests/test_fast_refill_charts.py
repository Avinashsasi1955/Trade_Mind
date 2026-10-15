"""Isolated unit tests for scripts/fast_refill_missing_charts.py safeguards.

Mocks all database and network interactions to ensure zero real database
connections or Upstox API network calls.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from scripts.fast_refill_missing_charts import (
    parse_target_date,
    validate_refill_database_target,
    refill_missing_charts,
    main,
)

IST = ZoneInfo("Asia/Kolkata")


class TestFastRefillSafeguards(unittest.TestCase):
    def setUp(self):
        self.today_ist = date(2026, 10, 16)

    # 1. Date Validation Safeguards
    def test_missing_or_empty_date_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_target_date("", today_ist=self.today_ist)
        self.assertIn("Missing required target date", str(ctx.exception))

        with self.assertRaises(ValueError):
            parse_target_date(None, today_ist=self.today_ist)

    def test_invalid_date_format_raises(self):
        for bad in ("2026/10/15", "15-10-2026", "invalid-date", "2026-02-30"):
            with self.assertRaises(ValueError) as ctx:
                parse_target_date(bad, today_ist=self.today_ist)
            self.assertIn("Invalid date format", str(ctx.exception))

    def test_future_date_rejected(self):
        future = (self.today_ist + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError) as ctx:
            parse_target_date(future, today_ist=self.today_ist)
        self.assertIn("cannot be in the future", str(ctx.exception))

    def test_valid_date_parsed_successfully(self):
        parsed = parse_target_date("2026-10-15", today_ist=self.today_ist)
        self.assertEqual(parsed, date(2026, 10, 15))

    # 2. Database Target Classification & Production Refusal
    def test_empty_database_url_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            validate_refill_database_target("")
        self.assertIn("DATABASE_URL is not set or empty", str(ctx.exception))

    def test_production_environment_flag_refused(self):
        with patch.dict("os.environ", {"NIVESH_ENV": "production"}):
            with self.assertRaises(RuntimeError) as ctx:
                validate_refill_database_target("postgresql://user:pass@localhost:5432/nivesh_dev")
            self.assertIn("SAFETY GATE TRIGGERED", str(ctx.exception))
            self.assertIn("NIVESH_ENV=production", str(ctx.exception))

    def test_production_host_patterns_rejected(self):
        bad_hosts = [
            "postgresql://user:pass@prod-db.example.com:5432/nivesh",
            "postgresql://user:pass@my-cluster.rds.amazonaws.com:5432/nivesh",
            "postgresql://user:pass@live-db:5432/nivesh",
        ]
        for url in bad_hosts:
            with self.assertRaises(RuntimeError) as ctx:
                validate_refill_database_target(url)
            self.assertIn("SAFETY GATE TRIGGERED", str(ctx.exception))

    def test_unknown_host_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            validate_refill_database_target("postgresql://user:pass@unknown-host:5432/nivesh_test")
        self.assertIn("FAIL-CLOSED: Host 'unknown-host' is not in approved", str(ctx.exception))

    def test_forbidden_database_names_rejected(self):
        for bad_db in ("postgres", "nivesh_production", "nivesh_prod"):
            url = f"postgresql://user:pass@localhost:5432/{bad_db}"
            with self.assertRaises(RuntimeError) as ctx:
                validate_refill_database_target(url)
            self.assertIn("SAFETY GATE TRIGGERED: Database name", str(ctx.exception))

    def test_approved_local_hosts_accepted(self):
        for host in ("localhost", "127.0.0.1", "postgres"):
            url = f"postgresql://nivesh:pass@{host}:5432/nivesh"
            info = validate_refill_database_target(url)
            self.assertEqual(info["scheme"], "postgresql")
            self.assertEqual(info["host"], host)
            self.assertEqual(info["classification"], "approved_local_dev_postgres")

    def test_sqlite_local_accepted(self):
        info = validate_refill_database_target("sqlite:///data/nivesh_dev.db")
        self.assertEqual(info["scheme"], "sqlite")
        self.assertEqual(info["classification"], "local_sqlite_dev")

    # 3. Dry-Run Default and Write Confirmation Invariants
    def test_dry_run_default_does_not_call_fetch_or_save_bars(self):
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [(101, 200)]  # <370 bars
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        mock_bf = MagicMock()
        mock_inst = MagicMock()
        mock_inst.instrument_id = 101
        mock_inst.symbol = "RELIANCE"
        mock_bf.select_instruments.return_value = [mock_inst]

        dev_url = "postgresql://user:pass@localhost:5432/nivesh_dev"
        report = refill_missing_charts(
            database_url=dev_url,
            target_date=date(2026, 10, 15),
            confirm_write=False,  # DRY RUN
            backfill_client=mock_bf,
            engine=mock_engine,
        )

        self.assertTrue(report["dry_run"])
        self.assertFalse(report["writes_executed"])
        self.assertEqual(report["instruments_needing_repair"], 1)
        self.assertEqual(report["needed_symbols"], ["RELIANCE"])

        # INVARIANT: Zero network fetches to Upstox and zero writes to DB in dry run
        mock_bf.fetch_history.assert_not_called()
        mock_bf.save_bars.assert_not_called()

    def test_confirm_write_uses_replace_false_preserving_live_provenance(self):
        """When write confirmation is given, save_bars MUST be invoked with replace=False."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [(101, 100)]
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        mock_bf = MagicMock()
        mock_inst = MagicMock()
        mock_inst.instrument_id = 101
        mock_inst.symbol = "TCS"
        mock_inst.provider_key = "NSE_EQ|INE467B01029"
        mock_bf.select_instruments.return_value = [mock_inst]

        # Return 1 candle on target date
        candle = {
            "bar_time": datetime(2026, 10, 15, 9, 15, tzinfo=IST),
            "open": 3000.0, "high": 3010.0, "low": 2990.0, "close": 3005.0, "volume": 1000,
        }
        mock_bf.fetch_history.return_value = [candle]
        mock_bf.save_bars.return_value = 1

        dev_url = "postgresql://user:pass@localhost:5432/nivesh_dev"
        report = refill_missing_charts(
            database_url=dev_url,
            target_date=date(2026, 10, 15),
            confirm_write=True,
            sleep_seconds=0.0,
            backfill_client=mock_bf,
            engine=mock_engine,
        )

        self.assertFalse(report["dry_run"])
        self.assertTrue(report["writes_executed"])
        self.assertEqual(report["successful_refills"], 1)

        # CRITICAL ASSERTION: replace=False MUST be passed to save_bars to DO NOTHING on conflict
        calls = mock_bf.save_bars.call_args_list
        self.assertEqual(len(calls), 2)  # 1m and 5m calls
        # 1m call
        self.assertEqual(calls[0][1]["replace"], False)
        self.assertEqual(calls[0][0][1], "1minute")
        # 5m call
        self.assertEqual(calls[1][1]["replace"], False)
        self.assertEqual(calls[1][0][1], "5minute")

    # 4. Main Entry Point CLI Safeguards
    def test_main_cli_missing_date_fails(self):
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit) as ctx:
                main([])
            self.assertNotEqual(ctx.exception.code, 0)

    def test_main_cli_refuses_production(self):
        with patch.dict("os.environ", {"NIVESH_ENV": "production", "DATABASE_URL": "postgresql://u:p@localhost:5432/db"}):
            with patch("sys.stderr"):
                code = main(["--date", "2026-10-15"])
                self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
