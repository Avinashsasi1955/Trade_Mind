"""Serializable probability calibration for chronological financial models."""
from bisect import bisect_right
from typing import Dict, Iterable, List


def fit_isotonic(probabilities: Iterable[float], labels: Iterable[int]) -> Dict:
    """Fit isotonic regression and return a JSON-serializable calibration map."""
    import numpy as np
    from sklearn.isotonic import IsotonicRegression

    x = np.asarray(list(probabilities), dtype=np.float64)
    y = np.asarray(list(labels), dtype=np.int8)
    if len(x) < 50 or len(set(y.tolist())) < 2:
        raise ValueError("Calibration requires at least 50 observations from both classes")
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=1e-4, y_max=1-1e-4).fit(x, y)
    return {
        "method": "isotonic",
        "x_thresholds": [float(value) for value in calibrator.X_thresholds_],
        "y_thresholds": [float(value) for value in calibrator.y_thresholds_],
        "samples": int(len(x)),
    }


def apply_calibration(probability: float, calibration: Dict) -> float:
    """Apply the stored piecewise-linear isotonic map without unpickling code."""
    if not calibration or calibration.get("method") != "isotonic":
        return float(probability)
    xs: List[float] = calibration["x_thresholds"]
    ys: List[float] = calibration["y_thresholds"]
    value = float(probability)
    if not xs or len(xs) != len(ys):
        return value
    if value <= xs[0]:
        return ys[0]
    if value >= xs[-1]:
        return ys[-1]
    right = bisect_right(xs, value)
    left = right - 1
    span = xs[right] - xs[left]
    if span <= 1e-12:
        return ys[right]
    weight = (value - xs[left]) / span
    return ys[left] + weight * (ys[right] - ys[left])


def calibration_metrics(labels: Iterable[int], probabilities: Iterable[float]) -> Dict:
    labels, probabilities = list(labels), list(probabilities)
    if not labels:
        return {"brier_score": None, "log_loss": None}
    import math
    clipped = [max(1e-9, min(1-1e-9, float(value))) for value in probabilities]
    brier = sum((probability-label)**2 for probability, label in zip(clipped, labels))/len(labels)
    loss = -sum(label*math.log(probability)+(1-label)*math.log(1-probability)
                for probability, label in zip(clipped, labels))/len(labels)
    return {"brier_score": round(brier, 4), "log_loss": round(loss, 4)}
