"""Leakage-safe ML utilities shared by the research pipeline."""
from .calibration import apply_calibration, calibration_metrics, fit_isotonic
from .label_builder import ExitAwareLabel, LabelBuilder, SCHEMA_EXIT_AWARE_LABELS
from .splits import (
    PurgedGroupTimeSeriesSplit,
    compute_classification_metrics,
    compute_regression_metrics,
    evaluate_split_folds,
)
from .trading_policy import policy_manifest, rank_candidates, score_candidate

__all__ = [
    "apply_calibration",
    "calibration_metrics",
    "fit_isotonic",
    "ExitAwareLabel",
    "LabelBuilder",
    "SCHEMA_EXIT_AWARE_LABELS",
    "PurgedGroupTimeSeriesSplit",
    "compute_classification_metrics",
    "compute_regression_metrics",
    "evaluate_split_folds",
    "policy_manifest",
    "rank_candidates",
    "score_candidate",
]
