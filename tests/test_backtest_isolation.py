"""Tests for Phase 2.1: Backtest Isolation, Synthetic-Data Safety, and Chronological Split Corrections."""

import copy
import unittest
import numpy as np

from backend.backtesting import run_backtest, load_history, InsufficientHistoryError
from backend.backtest_miner import mine_golden_patterns_from_backtests
from backend.trade_memory import DEFAULT_GOLDEN_PATTERNS, get_golden_trade_library, find_matching_golden_trade
from backend.ml.splits import PurgedGroupTimeSeriesSplit, evaluate_split_folds
from backend.ml.trading_policy import score_candidate


class TestPhase21BacktestIsolation(unittest.TestCase):
    """P0-A: Prove that backtests and pattern mining never write to live inference memory."""

    def setUp(self):
        self.initial_golden_patterns = copy.deepcopy(DEFAULT_GOLDEN_PATTERNS)
        self.initial_count = len(DEFAULT_GOLDEN_PATTERNS)

    def tearDown(self):
        DEFAULT_GOLDEN_PATTERNS.clear()
        DEFAULT_GOLDEN_PATTERNS.extend(self.initial_golden_patterns)

    def test_backtest_with_profitable_trades_does_not_modify_default_golden_patterns(self):
        report = run_backtest("TEST_WINNER", "momentum", allow_simulated=True)
        self.assertIn("out_of_sample", report)
        self.assertEqual(len(DEFAULT_GOLDEN_PATTERNS), self.initial_count)
        self.assertEqual(DEFAULT_GOLDEN_PATTERNS, self.initial_golden_patterns)

    def test_backtest_mining_does_not_write_to_live_vector_memory(self):
        result = mine_golden_patterns_from_backtests(
            symbols=["TEST_SYM1", "TEST_SYM2"],
            allow_simulated=True,
            opt_in_research_memory=False,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(DEFAULT_GOLDEN_PATTERNS), self.initial_count)
        self.assertEqual(DEFAULT_GOLDEN_PATTERNS, self.initial_golden_patterns)

    def test_explicitly_enabled_research_memory_writes_only_to_isolated_destination(self):
        research_sink = []
        result = mine_golden_patterns_from_backtests(
            symbols=["TEST_SYM1"],
            allow_simulated=True,
            opt_in_research_memory=True,
            research_destination=research_sink,
        )
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["research_memory"]["enabled"])
        self.assertEqual(result["research_memory"]["destination"], "list")
        self.assertEqual(len(research_sink), result["research_memory"]["patterns_stored"])
        self.assertEqual(len(DEFAULT_GOLDEN_PATTERNS), self.initial_count)
        self.assertEqual(DEFAULT_GOLDEN_PATTERNS, self.initial_golden_patterns)

    def test_research_destination_cannot_silently_fallback_to_live_if_missing(self):
        with self.assertRaises(ValueError) as ctx:
            mine_golden_patterns_from_backtests(
                symbols=["TEST_SYM1"],
                allow_simulated=True,
                opt_in_research_memory=True,
                research_destination=None,
            )
        self.assertIn("Explicit research_destination list is required", str(ctx.exception))
        self.assertEqual(len(DEFAULT_GOLDEN_PATTERNS), self.initial_count)

    def test_failed_research_memory_write_does_not_trigger_live_memory_fallback(self):
        with self.assertRaises(ValueError):
            mine_golden_patterns_from_backtests(
                symbols=["TEST_SYM1"],
                allow_simulated=True,
                opt_in_research_memory=True,
                research_destination="not_a_list",
            )
        self.assertEqual(len(DEFAULT_GOLDEN_PATTERNS), self.initial_count)

    def test_live_inference_scoring_and_thresholds_unchanged_by_running_backtest(self):
        cand = {
            "symbol": "TATAMOTORS",
            "features": {
                "market_return_20d": 0.03,
                "market_breadth": 0.08,
                "market_volatility_20d": 0.015,
                "atr_14": 0.02,
            }
        }
        score_before = score_candidate(cand, probability=0.80, raw_probability=0.75, estimated_cost_bps=12.0)

        run_backtest("TATAMOTORS", "ensemble", allow_simulated=True)

        score_after = score_candidate(cand, probability=0.80, raw_probability=0.75, estimated_cost_bps=12.0)
        self.assertEqual(score_before.audit(), score_after.audit())
        self.assertEqual(score_before.accepted, score_after.accepted)
        self.assertEqual(score_before.score, score_after.score)

    def test_standard_backtest_entry_point_does_not_ingest_oos_winning_trades_into_live_memory(self):
        report = run_backtest("RELIANCE", "breakout", allow_simulated=True)
        self.assertFalse(report["research_memory"]["enabled"])
        self.assertIsNone(report["research_memory"]["destination"])
        self.assertEqual(report["research_memory"]["patterns_captured"], 0)
        self.assertEqual(len(DEFAULT_GOLDEN_PATTERNS), self.initial_count)


class TestPhase21SyntheticDataSafety(unittest.TestCase):
    """P0-B: Reject insufficient real historical data and prevent synthetic sinusoidal fallbacks."""

    def test_missing_history_raises_documented_insufficient_history_error(self):
        with self.assertRaises(InsufficientHistoryError) as ctx:
            load_history("NONEXISTENT_XYZ_SYMBOL", 300, allow_simulated=False)
        err = ctx.exception
        self.assertEqual(err.symbol, "NONEXISTENT_XYZ_SYMBOL")
        self.assertEqual(err.timeframe, "day")
        self.assertEqual(err.available, 0)
        self.assertEqual(err.required, 80)
        self.assertIn("Insufficient genuine historical data for NONEXISTENT_XYZ_SYMBOL", str(err))

    def test_run_backtest_fails_closed_on_missing_history(self):
        with self.assertRaises(InsufficientHistoryError) as ctx:
            run_backtest("NONEXISTENT_TICKER_123", "ensemble", allow_simulated=False)
        self.assertEqual(ctx.exception.symbol, "NONEXISTENT_TICKER_123")

    def test_history_below_minimum_does_not_silently_invoke_simulated_history(self):
        with self.assertRaises(InsufficientHistoryError):
            load_history("SPARSE_FAKE_SYM", min_bars=100, allow_simulated=False)

    def test_synthetic_fixture_works_only_through_explicit_allow_simulated(self):
        history = load_history("TEST_MOCK_SYMBOL", 100, allow_simulated=True)
        self.assertEqual(history["data_mode"], "synthetic_fixture")
        self.assertIn("SYNTHETIC TEST FIXTURE ONLY", history["provenance"])

    def test_backtest_report_marks_synthetic_fixture_warning(self):
        report = run_backtest("TEST_MOCK_SYMBOL", "ensemble", allow_simulated=True)
        self.assertEqual(report["data_mode"], "synthetic_fixture")
        self.assertIn("SYNTHETIC TEST FIXTURE ONLY", report["warning"])

    def test_miner_handles_insufficient_history_safely_without_swallowing_metrics(self):
        result = mine_golden_patterns_from_backtests(
            symbols=["NON_EXISTENT_ALPHA_999"],
            allow_simulated=False,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mined_patterns_count"], 0)
        self.assertEqual(result["total_trades_evaluated"], 0)


class TestPhase21ChronologicalSplits(unittest.TestCase):
    """P1-C: Enforce future-only expanding-window walk-forward splits with purge and embargo."""

    def setUp(self):
        self.days = [f"2026-05-{i:02d}" for i in range(1, 31)]
        self.groups = []
        self.X = []
        for d in self.days:
            for bar_i in range(4):
                self.groups.append(d)
                self.X.append([float(bar_i), float(bar_i * 2)])
        self.X = np.asarray(self.X)

    def test_every_trading_evaluation_fold_trains_only_on_earlier_observations(self):
        splitter = PurgedGroupTimeSeriesSplit(
            n_splits=3,
            purge_days=2,
            embargo_days=1,
            min_train_days=10,
            cv_mode="expanding"
        )
        for fold, (train_idx, test_idx) in enumerate(splitter.split(self.X, groups=self.groups)):
            train_days = sorted(list(set(np.asarray(self.groups)[train_idx])))
            test_days = sorted(list(set(np.asarray(self.groups)[test_idx])))

            self.assertLess(train_days[-1], test_days[0], f"Fold {fold} trained on future observations!")

            last_train_day_idx = self.days.index(train_days[-1])
            first_test_day_idx = self.days.index(test_days[0])
            self.assertGreaterEqual(first_test_day_idx - last_train_day_idx, splitter.purge_days + 1)

    def test_expanding_training_windows_grow_monotonically(self):
        splitter = PurgedGroupTimeSeriesSplit(
            n_splits=3,
            purge_days=2,
            embargo_days=1,
            min_train_days=10,
            cv_mode="expanding"
        )
        prev_train_len = 0
        for fold, (train_idx, test_idx) in enumerate(splitter.split(self.X, groups=self.groups)):
            train_days = set(np.asarray(self.groups)[train_idx])
            self.assertGreaterEqual(len(train_days), prev_train_len)
            prev_train_len = len(train_days)

    def test_dynamic_purge_and_embargo_derivation(self):
        splitter = PurgedGroupTimeSeriesSplit(
            n_splits=3,
            max_label_horizon_days=3,
            max_holding_period_days=4,
        )
        self.assertEqual(splitter.purge_days, 4)
        self.assertEqual(splitter.embargo_days, 2)

    def test_missing_horizon_and_purge_fails_safely(self):
        with self.assertRaises(ValueError) as ctx:
            PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=None)
        self.assertIn("Purge buffer cannot be determined", str(ctx.exception))

    def test_shuffled_or_future_inclusive_split_rejected_for_trading(self):
        with self.assertRaises(ValueError) as ctx:
            PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=2, cv_mode="kfold")
        self.assertIn("Trading-strategy evaluation requires future-only expanding-window splits", str(ctx.exception))

        research_splitter = PurgedGroupTimeSeriesSplit(
            n_splits=3,
            purge_days=2,
            cv_mode="kfold",
            allow_non_chronological=True
        )
        splits = list(research_splitter.split(self.X, groups=self.groups))
        self.assertEqual(len(splits), 3)

    def test_preprocessing_fitted_on_training_partition_only(self):
        np.random.seed(42)
        y_long = np.random.randint(0, 2, size=len(self.X))
        y_short = np.random.randint(0, 2, size=len(self.X))
        r_net = np.random.randn(len(self.X))

        splitter = PurgedGroupTimeSeriesSplit(
            n_splits=3,
            purge_days=2,
            embargo_days=1,
            min_train_days=10,
            cv_mode="expanding"
        )
        summary = evaluate_split_folds(splitter, self.X, self.groups, y_long, y_short, r_net)
        self.assertEqual(summary["n_folds"], 3)
        for fold in summary["folds"]:
            self.assertEqual(fold["preprocessing"], "StandardScaler fitted on X_train only")
            self.assertLess(fold["train_date_end"], fold["test_date_start"])

    def test_malformed_or_missing_dates_fail_clearly(self):
        bad_groups = list(self.groups)
        bad_groups[5] = None
        splitter = PurgedGroupTimeSeriesSplit(n_splits=2, purge_days=2, min_train_days=8)
        with self.assertRaises(ValueError) as ctx:
            list(splitter.split(self.X, groups=bad_groups))
        self.assertIn("Invalid trading day group", str(ctx.exception))

    def test_insufficient_unique_days_fails_clearly(self):
        few_groups = ["2026-05-01", "2026-05-02"] * 2
        few_X = np.ones((len(few_groups), 2))
        splitter = PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=5, embargo_days=2)
        with self.assertRaises(ValueError) as ctx:
            list(splitter.split(few_X, groups=few_groups))
        self.assertIn("Insufficient unique trading days", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
