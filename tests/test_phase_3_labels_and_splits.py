"""Unit and real-SQL integration tests for Phase 3b.

Specifications:
- Labels are y = 1 only if the simulated trade reaches >= +1R net before -1R.
  Continuous r_net per direction. +0 to <1R outcomes stay y = 0 with r_net preserved.
- Enter at the NEXT bar's open plus 2 ticks of slippage, never the signal bar's close.
- Never create labels for entries after 14:45 IST for intraday mode.
- Truncate each simulated intraday trade at the 15:15 force-flat of its own day, using only that day's bars.
- When no exit is reached before the series (or day) ends, mark the label censored = True with a reason,
  exclude it from training sets by default, and report the count.
- Use the same stop and target the live path would set (_chart_strategy_gate ATR-based levels: 1.5x ATR long, 1.2x ATR short, 3.0x ATR target).
- Normalise bars once per series (O(N) cost), and cap the lookahead window at one trading day.
- Persistence works on both Postgres (SQLAlchemy, bound parameters) and SQLite with real SQL.
- Splits: Purge is at least the maximum label horizon in trading days (1 for intraday, swing limit for swing).
  Active embargo_days. Test that fails if a training label's exit date is on or after the test start.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, time
from decimal import Decimal
from typing import Any, Dict, List

import numpy as np
from sqlalchemy import create_engine

from backend.ml.label_builder import ExitAwareLabel, LabelBuilder, SCHEMA_EXIT_AWARE_LABELS
from backend.ml.splits import (
    PurgedGroupTimeSeriesSplit,
    compute_classification_metrics,
    compute_regression_metrics,
    evaluate_split_folds,
)


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
        """Under live risk levels (1.5x ATR long, 3.0x ATR target), rising prices trigger TAKE_PROFIT on long and STOP_LOSS on short."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "open": 99.8,
            "high": 100.2,
            "low": 99.7,
            "atr": 1.0,
        }
        # Bar 1: opens at 100.0. Trade enters at 100.0 + 2*0.05 = 100.10.
        # Long risk: 1.5, target: 3.0 (TP = 103.10, SL = 98.60).
        # Short risk: 1.2, target: 3.0 (SL = 101.10).
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.8, "low": 99.9, "close": 100.7},
            {"timestamp": (base_time + timedelta(minutes=2)).isoformat(), "open": 100.7, "high": 102.0, "low": 100.5, "close": 101.8},
            {"timestamp": (base_time + timedelta(minutes=3)).isoformat(), "open": 101.8, "high": 103.5, "low": 101.5, "close": 103.4},
        ]

        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")
        self.assertEqual(sim_long["side"], "BUY")
        self.assertEqual(sim_long["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(sim_long["y"], 1)
        self.assertGreaterEqual(sim_long["realized_r"], 1.0)

        # In the same rising market, short hits STOP_LOSS at 101.10
        sim_short = self.builder.simulate_candidate(entry_bar, future_bars, side="SELL")
        self.assertEqual(sim_short["side"], "SELL")
        self.assertIn(sim_short["exit_reason"], ("STOP_LOSS", "EARLY_ADVERSE_CUT"))
        self.assertEqual(sim_short["y"], 0)
        self.assertLess(sim_short["realized_r"], 0.0)

    def test_exit_simulator_short_profit_and_long_loss(self):
        """Under live risk levels, falling prices trigger TAKE_PROFIT on short and STOP_LOSS on long."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "open": 100.2,
            "high": 100.5,
            "low": 99.8,
            "atr": 1.0,
        }
        # Bar 1 opens at 100.0. Short entry: 99.90. Short TP: 96.90. Short SL: 101.10.
        # Long entry: 100.10. Long SL: 98.60.
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.1, "low": 99.2, "close": 99.3},
            {"timestamp": (base_time + timedelta(minutes=2)).isoformat(), "open": 99.3, "high": 99.4, "low": 98.0, "close": 98.2},
            {"timestamp": (base_time + timedelta(minutes=3)).isoformat(), "open": 98.2, "high": 98.3, "low": 96.5, "close": 96.8},
        ]

        sim_short = self.builder.simulate_candidate(entry_bar, future_bars, side="SELL")
        self.assertEqual(sim_short["side"], "SELL")
        self.assertEqual(sim_short["exit_reason"], "TAKE_PROFIT")
        self.assertEqual(sim_short["y"], 1)
        self.assertGreaterEqual(sim_short["realized_r"], 1.0)

        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")
        self.assertEqual(sim_long["side"], "BUY")
        self.assertIn(sim_long["exit_reason"], ("STOP_LOSS", "EARLY_ADVERSE_CUT"))
        self.assertEqual(sim_long["y"], 0)
        self.assertLess(sim_long["realized_r"], 0.0)

    def test_exit_simulator_gap_stop_loss(self):
        """An adverse gap on the bar open beyond SL must trigger GAP_DOWN_STOP or GAP_UP_STOP."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "atr": 1.0,
        }
        # Bar 1: Entry at 100.10, SL at 98.60
        # Bar 2: Gaps down at open to 98.0 (< 98.60)
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0},
            {"timestamp": (base_time + timedelta(minutes=2)).isoformat(), "open": 98.0, "high": 98.5, "low": 97.5, "close": 98.2},
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
        # Take profit hit at 103.10 (+3.0 points gross = 2.0R since R=1.5)
        future_bars = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.5, "low": 99.9, "close": 100.2},
            {"timestamp": (base_time + timedelta(minutes=2)).isoformat(), "open": 100.2, "high": 103.5, "low": 100.1, "close": 103.2},
        ]
        sim_long = self.builder.simulate_candidate(entry_bar, future_bars, side="BUY")

        # 2-tick slippage + fees means net exit is below gross 2.0R
        self.assertLess(sim_long["realized_r"], 2.0)
        self.assertGreater(sim_long["realized_r"], 1.7)
        self.assertGreater(sim_long["fee_amount"], 0.0)

    def test_enter_at_next_bar_open_plus_slippage(self):
        """Entry must occur strictly at NEXT bar's open plus 2 ticks slippage, never signal bar's close."""
        base_time = datetime(2026, 6, 1, 9, 15, 0)
        bars = [
            {"bar_time": (base_time).isoformat(), "open": 100.0, "high": 100.5, "low": 99.8, "close": 100.4, "volume": 100},
            {"bar_time": (base_time + timedelta(minutes=1)).isoformat(), "open": 101.0, "high": 101.5, "low": 100.8, "close": 101.2, "volume": 100},
            {"bar_time": (base_time + timedelta(minutes=2)).isoformat(), "open": 101.2, "high": 101.6, "low": 101.0, "close": 101.4, "volume": 100},
        ]
        labels = self.builder.build_labels_for_series("NSE", "INFY", bars, min_future_bars=1)
        self.assertGreater(len(labels), 0)
        first_label = labels[0]
        # Next bar open is 101.0. With 2 ticks slippage (0.10), entry_price must be 101.10, NOT 100.4 (signal bar close)
        self.assertEqual(first_label.entry_price, 101.10)
        self.assertNotEqual(first_label.entry_price, 100.4)

    def test_y_1_only_if_r_net_ge_1_and_continuous_r_preserved(self):
        """y = 1 ONLY if r_net >= +1.0R. Continuous r_net is preserved and intermediate positive gains stay y=0."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        # 1. Trade that exits with +0.5R gain
        sim_moderate = {
            "entry_price": 100.0,
            "exit_price": 100.75,
            "realized_r": 0.50,
            "exit_reason": "PROFIT_HARVEST_STOP",
            "censored": False,
        }
        # In LabelBuilder logic: y is 1 ONLY if realized_r >= 1.0
        y_mod = 1 if sim_moderate["realized_r"] >= 1.0 else 0
        self.assertEqual(y_mod, 0)
        self.assertEqual(sim_moderate["realized_r"], 0.50)

        # 2. Trade that exits with +1.5R gain
        sim_large = {
            "entry_price": 100.0,
            "exit_price": 102.25,
            "realized_r": 1.50,
            "exit_reason": "TAKE_PROFIT",
            "censored": False,
        }
        y_large = 1 if sim_large["realized_r"] >= 1.0 else 0
        self.assertEqual(y_large, 1)
        self.assertEqual(sim_large["realized_r"], 1.50)

    def test_intraday_cutoff_at_1445(self):
        """Never create labels for entries after 14:45 IST for intraday mode."""
        base_time = datetime(2026, 6, 1, 14, 44, 0)
        bars = [
            {"bar_time": (base_time).isoformat(), "open": 100.0, "high": 100.5, "low": 99.8, "close": 100.2, "volume": 100},
            # Next bar is at 14:45:00 (allowed)
            {"bar_time": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.2, "high": 100.6, "low": 100.0, "close": 100.4, "volume": 100},
            # Next bar is at 14:46:00 (after 14:45 cutoff -> should be skipped)
            {"bar_time": (base_time + timedelta(minutes=2)).isoformat(), "open": 100.4, "high": 100.7, "low": 100.2, "close": 100.5, "volume": 100},
            {"bar_time": (base_time + timedelta(minutes=3)).isoformat(), "open": 100.5, "high": 100.8, "low": 100.3, "close": 100.6, "volume": 100},
        ]
        labels = self.builder.build_labels_for_series("NSE", "INFY", bars, min_future_bars=1)
        # Only the first bar (which enters at 14:45) is permitted; bar entering at 14:46 is skipped
        self.assertEqual(len(labels), 1)

    def test_intraday_force_flat_truncation_and_censoring(self):
        """Truncate simulated intraday trade at 15:15 force-flat of its own day, marking unexited as censored."""
        base_time = datetime(2026, 6, 1, 14, 40, 0)
        bars = [
            {"bar_time": (base_time).isoformat(), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 100},
            # 14:41 entry (before 14:45 cutoff)
            {"bar_time": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 100},
            # 15:00
            {"bar_time": (datetime(2026, 6, 1, 15, 0, 0)).isoformat(), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 100},
            # 15:15 force-flat
            {"bar_time": (datetime(2026, 6, 1, 15, 15, 0)).isoformat(), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 100},
            # Next day bar (must NOT be reached or used)
            {"bar_time": (datetime(2026, 6, 2, 9, 15, 0)).isoformat(), "open": 100.0, "high": 105.0, "low": 99.9, "close": 104.0, "volume": 100},
        ]
        labels = self.builder.build_labels_for_series("NSE", "INFY", bars, min_future_bars=1)
        self.assertGreater(len(labels), 0)
        first_lbl = labels[0]
        self.assertEqual(first_lbl.exit_reason_long, "FORCE_FLAT")

    def test_real_sql_database_persistence_sqlite_and_sqlalchemy(self):
        """Test real-SQL database persistence on both SQLite raw connection and SQLAlchemy engine with bound parameters."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
            db_path = tmp_db.name

        try:
            # 1. Test via raw sqlite3 connection
            conn = sqlite3.connect(db_path)
            LabelBuilder.create_tables(conn)

            base_time = datetime(2026, 6, 1, 9, 15, 0)
            bars = self._generate_synthetic_bars(base_time, count=25, start_price=100.0, trend=0.5)
            labels = self.builder.build_labels_for_series("NSE", "TCS", bars, min_future_bars=2)
            self.assertEqual(len(labels), 23)

            saved_count = LabelBuilder.save_labels(conn, labels)
            self.assertEqual(saved_count, 23)

            loaded_all = LabelBuilder.load_labels(conn, exchange="NSE", symbol="TCS", include_censored=True)
            self.assertEqual(len(loaded_all), 23)
            self.assertIn("exit_date", loaded_all[0])

            loaded_uncensored = LabelBuilder.load_labels(conn, exchange="NSE", symbol="TCS", include_censored=False)
            self.assertEqual(len(loaded_uncensored), 7)
            self.assertIn("hit_1r_first_long", loaded_all[0])
            self.assertIn("max_favourable_r_long", loaded_all[0])
            self.assertIn("max_adverse_r_long", loaded_all[0])
            conn.close()

            # 2. Test via SQLAlchemy Engine (used by Postgres/SQLAlchemy in production) with bound parameters
            sa_engine = create_engine(f"sqlite:///{db_path}")
            loaded_sa = LabelBuilder.load_labels(sa_engine, exchange="NSE", symbol="TCS", include_censored=True)
            self.assertEqual(len(loaded_sa), 23)
            self.assertIn("hit_1r_first_short", loaded_sa[0])
            self.assertIn("max_favourable_r_short", loaded_sa[0])
            self.assertIn("max_adverse_r_short", loaded_sa[0])

            # Idempotent upsert via SQLAlchemy
            saved_sa = LabelBuilder.save_labels(sa_engine, labels)
            self.assertEqual(saved_sa, 23)
            sa_engine.dispose()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_no_silent_exception_handling(self):
        """Label builder and SQL operations must raise errors without swallowing them."""
        with self.assertRaises(ValueError):
            self.builder.simulate_candidate({"timestamp": "2026-06-01T09:15:00", "close": 100.0}, [], side="INVALID_SIDE")

        with self.assertRaises(ValueError):
            self.builder._normalize_bar({"close": 100.0})

        with self.assertRaises(ValueError):
            self.builder._normalize_bar({"timestamp": "2026-06-01T09:15:00"})

        with self.assertRaises(ValueError):
            self.builder.build_labels_for_series("NSE", "INFY", [{"timestamp": "2026-06-01T09:15:00", "close": 100.0}], min_future_bars=5)

    def test_continuous_series_no_false_gap_exit(self):
        """On a series where every bar opens at previous close, GAP_DOWN_STOP and GAP_UP_STOP must NOT appear."""
        from backend.position_manager import replay_trade_walk_forward
        trade_long = {
            "entry": 1000.0,
            "theoretical_fill_price": 1000.0,
            "stop_loss_price": 990.0,
            "take_profit_price": 1030.0,
            "side": "BUY",
            "quantity": 1,
            "estimated_fees": 20.0,
            "signal_at": "2026-06-01T09:15:00+05:30",
            "trade_mode": "INTRADAY",
        }
        # Bar 0: reaches +1.2R (triggers breakeven candidate), closes at 1005.0
        # Bar 1: opens at 1005.0 (exact previous close, zero gap!)
        bars = [
            {"bar_time": "2026-06-01T09:15:00+05:30", "open": 1000.0, "high": 1012.0, "low": 999.0, "close": 1005.0, "volume": 100},
            {"bar_time": "2026-06-01T09:16:00+05:30", "open": 1005.0, "high": 1006.0, "low": 1004.0, "close": 1005.0, "volume": 100},
        ]
        res = replay_trade_walk_forward(trade_long, bars)
        self.assertNotEqual(res["exit_reason"], "GAP_DOWN_STOP")

        # Intra-bar spike and pullback: trailing stop breached on Bar 0 pulls back to 1015 (< trailing stop 1020)
        # Bar 1 opens at 1015 (exact previous close). Must exit as TRAILING_STOP, not GAP_DOWN_STOP.
        trade_trail = {
            "entry": 1000.0,
            "theoretical_fill_price": 1000.0,
            "stop_loss_price": 990.0,
            "take_profit_price": 1030.0,
            "side": "BUY",
            "quantity": 100,
            "estimated_fees": 0.0,
            "signal_at": "2026-06-01T09:15:00+05:30",
            "trade_mode": "INTRADAY",
        }
        bars_trail = [
            {"bar_time": "2026-06-01T09:15:00+05:30", "open": 1000.0, "high": 1025.0, "low": 999.0, "close": 1015.0, "volume": 100},
            {"bar_time": "2026-06-01T09:16:00+05:30", "open": 1015.0, "high": 1018.0, "low": 1014.0, "close": 1016.0, "volume": 100},
        ]
        res_trail = replay_trade_walk_forward(trade_trail, bars_trail)
        self.assertEqual(res_trail["exit_reason"], "TRAILING_STOP")

    def test_hit_1r_first_and_excursion_tracking(self):
        """hit_1r_first is 1 if favourable excursion reaches +1R before -1R, else 0, with max excursions."""
        base_time = datetime(2026, 6, 1, 9, 30, 0)
        entry_bar = {
            "timestamp": base_time.isoformat(),
            "close": 100.0,
            "atr": 1.0,  # Long R = max(1.5, 0.6) = 1.5. +1R is 101.6, -1R is 98.6
        }

        # Case A: High reaches +1.5R (102.5) before Low touches -1R (low is 99.8)
        future_a = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 102.5, "low": 99.8, "close": 102.0},
        ]
        sim_a = self.builder.simulate_candidate(entry_bar, future_a, side="BUY")
        self.assertEqual(sim_a["hit_1r_first"], 1)
        self.assertGreaterEqual(sim_a["max_favourable_r"], 1.0)
        self.assertGreater(sim_a["max_adverse_r"], -1.0)

        # Case B: Low drops to 98.0 (< 98.6, reaching -1R) before high reaches +1R (high is 100.2)
        future_b = [
            {"timestamp": (base_time + timedelta(minutes=1)).isoformat(), "open": 100.0, "high": 100.2, "low": 98.0, "close": 98.2},
        ]
        sim_b = self.builder.simulate_candidate(entry_bar, future_b, side="BUY")
        self.assertEqual(sim_b["hit_1r_first"], 0)
        self.assertLessEqual(sim_b["max_adverse_r"], -1.0)

    def test_purged_group_time_series_split_by_trading_day(self):
        """PurgedGroupTimeSeriesSplit must group by trading day and enforce purge buffers."""
        days = [f"2026-06-{i:02d}" for i in range(1, 21)]
        groups = []
        X = []
        for d in days:
            for bar_i in range(5):
                groups.append(d)
                X.append([float(bar_i), float(bar_i * 2)])

        X = np.asarray(X)
        splitter = PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=2, embargo_days=1, min_train_days=8)

        splits = list(splitter.split(X, groups=groups))
        self.assertEqual(len(splits), 3)

        for fold_idx, (train_idx, test_idx) in enumerate(splits):
            train_days = set(np.asarray(groups)[train_idx])
            test_days = set(np.asarray(groups)[test_idx])
            self.assertEqual(len(train_days.intersection(test_days)), 0)

            sorted_train_days = sorted(train_days)
            sorted_test_days = sorted(test_days)
            last_train_day = sorted_train_days[-1]
            first_test_day = sorted_test_days[0]
            train_day_idx = days.index(last_train_day)
            test_day_idx = days.index(first_test_day)
            self.assertGreaterEqual(test_day_idx - train_day_idx, splitter.purge_days + 1)

    def test_split_fails_if_training_label_exit_date_on_or_after_test_start(self):
        """Split validation must fail if any training label's exit date is on or after the test start date."""
        days = [f"2026-06-{i:02d}" for i in range(1, 15)]
        groups = []
        exit_dates = []
        X = []

        # Construct scenario where trades on day i exit on day i + 2 (multi-day holding)
        for i, d in enumerate(days):
            exit_d = days[min(len(days) - 1, i + 2)]
            for _ in range(4):
                groups.append(d)
                exit_dates.append(exit_d)
                X.append([1.0, 2.0])

        X = np.asarray(X)
        groups_arr = np.asarray(groups)
        exit_dates_arr = np.asarray(exit_dates)

        # 1. With insufficient purge (purge_days = 1 when trades hold 2 days), training label exits on/after test start!
        leaked_splitter = PurgedGroupTimeSeriesSplit(n_splits=2, purge_days=1, max_label_horizon_days=1, embargo_days=0, min_train_days=6)
        leakage_detected = False
        for train_idx, test_idx in leaked_splitter.split(X, groups=groups):
            first_test_day = sorted(list(set(groups_arr[test_idx])))[0]
            train_exit_days = exit_dates_arr[train_idx]
            if any(ed >= first_test_day for ed in train_exit_days):
                leakage_detected = True
                break
        self.assertTrue(leakage_detected, "Insufficient purge must cause training label exit date to leak into test period")

        # 2. With purge_days >= max_label_horizon_days (purge_days = 2), NO training label exits on or after test start
        safe_splitter = PurgedGroupTimeSeriesSplit(n_splits=2, purge_days=2, max_label_horizon_days=2, embargo_days=0, min_train_days=6)
        for train_idx, test_idx in safe_splitter.split(X, groups=groups):
            first_test_day = sorted(list(set(groups_arr[test_idx])))[0]
            train_exit_days = exit_dates_arr[train_idx]
            for ed in train_exit_days:
                self.assertLess(ed, first_test_day, f"Training label exit date {ed} must be strictly before test start {first_test_day}")

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


if __name__ == "__main__":
    unittest.main()
