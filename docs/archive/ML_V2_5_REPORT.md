# ML v2.5 calibration experiment

Generated: 1 July 2026 (IST)

## Implemented

- JSON-serializable isotonic probability calibration.
- Strict development → calibration → selection → untouched-final chronology with ten-session purges.
- Raw and calibrated candidate metrics for logistic regression and two histogram-gradient-boosting variants.
- Fold-local calibration in expanding walk-forward validation.
- Calibrated eligibility with raw-confidence tie-breaking for isotonic probability plateaus.
- Identity fallback for research fixtures without enough calibration observations.
- Live-order access remains disabled.

## Selected model

- Version: `direction-v2.5-20260701T073458745271Z`
- Algorithm: flexible histogram gradient boosting + isotonic calibration
- Untouched-final samples: 116,358
- Untouched-final accuracy: 51.47%
- Precision: 54.55%
- Recall: 26.99%
- Log loss: 0.6937
- Brier score: 0.2502

The selection block improved from 0.6923 to 0.6772 log loss and from 0.2481 to 0.2421 Brier score after calibration. The untouched-final improvement was much smaller, so the model is not considered calibrated well enough for live promotion.

## Corrected walk-forward result

- Folds: 6
- Symbols: 1,940
- Trades: 550
- Cost-adjusted return: +27.90%
- Win rate: 58.00%
- Profit factor: 1.31
- Maximum drawdown: -7.32%
- Fold directional accuracy: 75.00%, 57.50%, 45.83%, 70.21%, 44.17%, 82.61%

The profit-factor and drawdown gates pass, but fold dispersion remains wide.

## Shadow filter check

The first v2.5 shadow run stored 2,485 predictions with broker access disabled: 39 bullish, 14 bearish and 2,432 neutral. This is substantially more selective than the previous high-recall classifier, but all observations remain pending until future outcomes are available.

## Promotion blockers

- Untouched-final log loss is not below the 0.693 gate.
- No 90-session resolved forward shadow record exists.
- Point-in-time/delisted universe records are absent.
- India VIX history and derivatives open interest are not connected.
- Short signals have not been mapped to executable futures/SLB instruments.
- Costs remain estimates rather than reconciled Zerodha contract-note charges.

The model remains for research and paper trading only and cannot place or approve broker orders.
