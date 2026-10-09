"""Unit and real-SQL integration tests for Phase 3.1 & 3.2.

Covers:
- Exit-aware labels using replay_trade_walk_forward as exit simulator
- Separate y_long, y_short classification targets and r_net regression target
- Fees and slippage inclusion in r_net
- PurgedGroupTimeSeriesSplit by trading day with purge and embargo
- Per-fold metrics reporting
- Real-SQL database persistence with temporary database
- Strict error propagation with no silent exception handling
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List

import numpy as np

from backend.ml.label_builder import ExitAwareLabel, LabelBuilder, SCHEMA_EXIT_AWARE_LABELS
from backend.ml.splits import (
    PurgedGroupTimeSeriesSplit,
    compute_classification_metrics,
    compute_regression_metrics,
    evaluate_split_folds,
)
from backend.position_manager import replay_trade_walk_forward


class TestPhase3LabelsAndSplits(unittest.TestCase):
    def setUp(self):
        self.builder = LabelBuilder(
            tick_size=0.05,
            target_r=2.0,
            sl_pct=0.01,
            quantity=1,
            trade_mode="INTRADAY",
        )

    def _generate_synthetic_bars(
        self,
        start_time: datetime,
        count: int,
        start_price: float = 100.0,
        trend: float = 0.0,
        volatility: float = 0.2,
    ) -> List[Dict[str, Any]]:
        bars = []
        price = start_price
        for i in range(count):
            t = start_time + timedelta(minutes=i)
            o = price
            price += trend + (0.05 if (i % 2 == 0) else -0.05)
            h = max(o, price) + volatility
            l = min(o, price) - volatility
            c = price
            bars.append({
                "bar_time": t.isoformat(),
                "timestamp": t.isoformat(),
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": 1000 + (i * 10),
            })
        return bars

    def test_exit_simulator_long_profit_and_short_loss(self):
        """A rising price series must trigger TAKE_PROFIT on long and STOP_LOSS on short."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        # Entry at 100.0, initial SL at 99.0 (R=1.0), TP at 102.0
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "open": 99.8,
            "high": 100.2,
            "low": 99.7,
            "atr": 1.0,
        }
        # Rising future bars reaching 102.5
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.8, "low": 99.9, "close": 100.7},
            {"timestamp": (base_time + timedelta(minutes=2)).isoformat(), "open": 100.7, "high": 101.5, "low": 100.5, "close": 101.4},
            {"timestamp": (base_time + timedelta(minutes=3)).isoformat(), "open": 101.4, "high": 102.6, "low": 101.2, "close": 102.5},
        ]

        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")
        self.assertEqual(sim_long["side"], "BUY")
        self.assertEqual(sim_long["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(sim_long["y"], 1)
        self.assertGreater(sim_long["realized_r"], 0.0)

        # In the same rising market, a short trade must hit STOP_LOSS
        sim_short = self.builder.simulate_candidate(entry_bar, future_bars, side="SELL")
        self.assertEqual(sim_short["side"], "SELL")
        self.assertIn(sim_short["exit_reason"], ("STOP_LOSS", "EARLY_ADVERSE_CUT"))
        self.assertEqual(sim_short["y"], 0)
        self.assertLess(sim_short["realized_r"], 0.0)

    def test_exit_simulator_short_profit_and_long_loss(self):
        """A falling price series must trigger TAKE_PROFIT on short and STOP_LOSS on long."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "open": 100.2,
            "high": 100.5,
            "low": 99.8,
            "atr": 1.0,
        }
        # Falling future bars reaching 97.5 (Short TP is 98.0)
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.1, "low": 99.2, "close": 99.3},
            {"timestamp": (base_time + timedelta(minutes=2)).isoformat(), "open": 99.3, "high": 99.4, "low": 98.4, "close": 98.5},
            {"timestamp": (base_time + timedelta(minutes=3)).isoformat(), "open": 98.5, "high": 98.6, "low": 97.5, "close": 97.8},
        ]

        sim_short = self.builder.simulate_candidate(entry_bar, future_bars, side="SELL")
        self.assertEqual(sim_short["side"], "SELL")
        self.assertEqual(sim_short["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(sim_short["y"], 1)
        self.assertGreater(sim_short["realized_r"], 0.0)

        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")
        self.assertEqual(sim_long["side"], "BUY")
        self.assertIn(sim_long["exit_reason"], ("STOP_LOSS", "EARLY_ADVERSE_CUT"))
        self.assertEqual(sim_long["y"], 0)
        self.assertLess(sim_long["realized_r"], 0.0)

    def test_exit_simulator_gap_stop_loss(self):
        """An adverse gap on the next bar open must trigger GAP_DOWN_STOP or GAP_UP_STOP."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "atr": 1.0,
        }
        # Gap down to 98.0 on next bar open (SL was 99.0)
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 98.0, "high": 98.5, "low": 97.5, "close": 98.2},
        ]
        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")
        self.assertEqual(sim_long["exit_reason"], "GAP_DOWN_STOP")
        self.assertEqual(sim_long["y"], 0)
        self.assertLess(sim_long["realized_r"], 0.0)

    def test_fees_and_slippage_included_in_r_net(self):
        """r_net must deduct transaction costs and slippage from gross return."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "atr": 1.0,
        }
        # Take profit hit at 102.0 (+2.0 points gross)
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 102.2, "low": 99.9, "close": 102.0},
        ]
        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")

        # 2-tick slippage (2 * 0.05 = 0.10) + fees means net exit is 101.90 minus fees
        # realized_r must be strictly less than gross 2.0R
        self.assertLess(sim_long["realized_r"], 2.0)
        self.assertGreater(sim_long["realized_r"], 1.7)
        self.assertGreater(sim_long["fee_amount"], 0.0)

    def test_real_sql_database_persistence_and_query(self):
        """Real-SQL test with a temporary SQLite database testing table creation, insert, and query."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
            db_path = tmp_db.name

        try:
            conn = sqlite3.connect(db_path)
            # 1. Create table
            LabelBuilder.create_tables(conn)

            # Verify table exists in SQLite schema
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='exit_aware_labels'")
            self.assertIsNotNone(cursor.fetchone())

            # 2. Build labels from synthetic bars
            base_time = datetime(2026, 6, 1, 9, 15, 0)
            bars = self._generate_synthetic_bars(base_time, count=15, start_price=100.0, trend=0.2)
            labels = self.builder.build_labels_for_series("NSE", "TCS", bars, min_future_bars=2)
            self.assertEqual(len(labels), 13)

            # 3. Save labels to temporary real SQL database
            saved_count = LabelBuilder.save_labels(conn, labels)
            self.assertEqual(saved_count, 13)

            # 4. Load back and verify data fidelity
            loaded = LabelBuilder.load_labels(conn, exchange="NSE", symbol="TCS")
            self.assertEqual(len(loaded), 13)

            first = loaded[0]
            self.assertEqual(first["exchange"], "NSE")
            self.assertEqual(first["symbol"], "TCS")
            self.assertIn("y_long", first)
            self.assertIn("y_short", first)
            self.assertIn("r_net", first)
            self.assertIn("r_net_long", first)
            self.assertIn("r_net_short", first)
            self.assertIn("exit_reason_long", first)
            self.assertIn("exit_reason_short", first)

            # Verify idempotence (saving again updates without error)
            saved_again = LabelBuilder.save_labels(conn, labels)
            self.assertEqual(saved_again, 13)
            loaded_after = LabelBuilder.load_labels(conn, exchange="NSE", symbol="TCS")
            self.assertEqual(len(loaded_after), 13)

            conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_no_silent_exception_handling(self):
        """Label builder and SQL operations must raise errors without swallowing them."""
        # Invalid side must raise ValueError
        with self.assertRaises(ValueError):
            self.builder.simulate_candidate({"timestamp": "2026-06-01T09:15:00", "close": 100.0}, [], side="INVALID_SIDE")

        # Missing timestamp must raise ValueError
        with self.assertRaises(ValueError):
            self.builder._normalize_bar({"close": 100.0})

        # Missing close must raise ValueError
        with self.assertRaises(ValueError):
            self.builder._normalize_bar({"timestamp": "2026-06-01T09:15:00"})

        # Insufficient bars for build_labels_for_series must raise ValueError
        with self.assertRaises(ValueError):
            self.builder.build_labels_for_series("NSE", "INFY", [{"timestamp": "2026-06-01T09:15:00", "close": 100.0}], min_future_bars=5)

        # Database save to non-existent table must raise sqlite3.OperationalError (not caught or swallowed)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
            db_path = tmp_db.name
        try:
            conn = sqlite3.connect(db_path)
            dummy_label = ExitAwareLabel(
                exchange="NSE",
                symbol="TEST",
                timestamp="2026-06-01T09:15:00",
                trade_date="2026-06-01",
                entry_price=100.0,
                y_long=1,
                y_short=0,
                r_net=1.5,
                r_net_long=1.5,
                r_net_short=-1.0,
                exit_reason_long="TAKE_PROFIT",
                exit_reason_short="STOP_LOSS",
                exit_price_long=102.0,
                exit_price_short=101.0,
                net_pnl_long=2.0,
                net_pnl_short=-1.0,
                created_at="2026-06-01T09:15:00",
            )
            with self.assertRaises(sqlite3.OperationalError):
                LabelBuilder.save_labels(conn, [dummy_label])
            conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_purged_group_time_series_split_by_trading_day(self):
        """PurgedGroupTimeSeriesSplit must group by trading day and enforce purge buffers."""
        # 20 trading days with 5 intraday bars each
        days = [f"2026-06-{i:02d}" for i in range(1, 21)]
        groups = []
        X = []
        for d in days:
            for bar_i in range(5):
                groups.append(d)
                X.append([float(bar_i), float(bar_i * 2)])

        X = np.asarray(X)
        splitter = PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=2, embargo_days=1, min_train_days=8)

        # Missing groups must raise ValueError
        with self.assertRaises(ValueError):
            list(splitter.split(X, groups=None))

        # Split generation
        splits = list(splitter.split(X, groups=groups))
        self.assertEqual(len(splits), 3)

        for fold_idx, (train_idx, test_idx) in enumerate(splits):
            train_days = set(np.asarray(groups)[train_idx])
            test_days = set(np.asarray(groups)[test_idx])

            # 1. No overlap between train and test days
            self.assertEqual(len(train_days.intersection(test_days)), 0)

            # 2. Check purge window: last train day must be at least purge_days before first test day
            sorted_train_days = sorted(train_days)
            sorted_test_days = sorted(test_days)
            last_train_day = sorted_train_days[-1]
            first_test_day = sorted_test_days[0]

            train_day_idx = days.index(last_train_day)
            test_day_idx = days.index(first_test_day)
            # Test start minus train end must be > purge_days
            self.assertGreaterEqual(test_day_idx - train_day_idx, splitter.purge_days + 1)

    def test_per_fold_metrics_report(self):
        """evaluate_split_folds must compute and report per-fold classification and regression metrics."""
        np.random.seed(42)
        days = [f"2026-06-{i:02d}" for i in range(1, 25)]
        groups = []
        X_list = []
        y_long_list = []
        y_short_list = []
        r_net_list = []

        for d in days:
            for _ in range(10):
                groups.append(d)
                f1 = np.random.randn()
                f2 = np.random.randn()
                X_list.append([f1, f2])

                # Long setup target correlated with f1
                y_l = 1 if (f1 + np.random.randn() * 0.5) > 0 else 0
                y_s = 1 if (f2 + np.random.randn() * 0.5) > 0 else 0
                r = (f1 * 0.8) + (np.random.randn() * 0.3)

                y_long_list.append(y_l)
                y_short_list.append(y_s)
                r_net_list.append(r)

        X = np.asarray(X_list)
        y_long = np.asarray(y_long_list)
        y_short = np.asarray(y_short_list)
        r_net = np.asarray(r_net_list)

        splitter = PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=2, embargo_days=1, min_train_days=10)
        report = evaluate_split_folds(splitter, X, groups, y_long, y_short, r_net)

        self.assertEqual(report["n_folds"], 3)
        self.assertIn("mean_y_long_accuracy", report)
        self.assertIn("mean_y_short_accuracy", report)
        self.assertIn("mean_r_net_rmse", report)
        self.assertIn("mean_r_net_mae", report)
        self.assertEqual(len(report["folds"]), 3)

        for fold_report in report["folds"]:
            self.assertIn("metrics_y_long", fold_report)
            self.assertIn("metrics_y_short", fold_report)
            self.assertIn("metrics_r_net", fold_report)
            self.assertGreater(fold_report["train_days_count"], 0)
            self.assertGreater(fold_report["test_days_count"], 0)
            self.assertGreater(fold_report["train_samples"], 0)
            self.assertGreater(fold_report["test_samples"], 0)
            self.assertIn("accuracy", fold_report["metrics_y_long"])
            self.assertIn("rmse", fold_report["metrics_r_net"])
            self.assertIn("profit_factor", fold_report["metrics_r_net"])


if __name__ == "__main__":
    unittest.main()
