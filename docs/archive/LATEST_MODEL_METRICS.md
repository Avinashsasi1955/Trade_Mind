# Latest model metrics

Verified from the running PostgreSQL paper environment on 4 July 2026 IST. The model was retrained, HMAC-signed and independently revalidated in this environment.

## Model

- Version: `direction-v2.5-20260703T210440115936Z`
- Algorithm: histogram gradient boosting, flexible candidate, isotonic calibration
- Training period: 13 September 2021 to 2 January 2025
- Training samples: 408,893
- Artifact SHA-256: `aaf72b8bfbcb90ee1ae954baf05a1e0edd9d56deeb9c9d2d2b6666095cdf5571`
- Local artifact signature: verified (paper-environment key; production signing remains required)
- Status: research/shadow only; `live_eligible=false`

## Dataset metrics

| Dataset | Samples | Accuracy | Precision | Recall | Log loss | Brier |
|---|---:|---:|---:|---:|---:|---:|
| Training | 408,893 | 69.97% | 78.96% | 64.61% | 0.6144 | 0.2116 |
| Calibration | 54,500 | 57.34% | 59.72% | 61.07% | 0.6724 | 0.2400 |
| Untouched holdout | 116,358 | 51.47% | 54.55% | 26.99% | 0.6937 | 0.2502 |

Untouched period: 14 July 2025 to 25 June 2026. The required promotion log loss is `<0.693`, so this gate fails.

## Purged walk-forward portfolio

- Period: 8 August 2023 to 25 June 2026
- Six chronological folds, 1,940 symbols, 550 trades
- Starting capital: ₹10,00,000
- Ending capital: ₹12,79,040.12
- Net return: +27.90%
- Win rate: 58.00%
- Profit factor: 1.31
- Maximum drawdown: −7.32%
- Policy: top ten signals, ten-session non-overlapping rebalance, 5% per position and 50% gross cap
- Costs: brokerage/charges, slippage and participation impact included in the simulation
- Fold directional accuracy: 75.00%, 57.50%, 45.83%, 70.21%, 44.17%, 82.61%

The fresh run reproduced the previous deterministic result. That is evidence of pipeline repeatability, not evidence of future profitability. These are historical research results, not a profit guarantee. Forward shadow, real-cost and derivative-executability gates remain incomplete.

## Promotion blockers

- Forward shadow sessions: 0/90
- Live cost-reconciled sessions: 0/20
- Verified derivative shadow executions: 0/20
- Untouched log loss: 0.6937; required `<0.693`
- Live forward profit factor and drawdown: not yet available

## Runtime verification

- Preview health endpoint: passing
- Authenticated ML-status endpoint: passing
- PostgreSQL bars: 6,435,334 across 6,940 complete symbol series
- Engineered feature rows: 3,488,676
- Corporate actions: 12,588
- Latest validation stored against the exact model version above
- PostgreSQL, Redis, web, Celery worker and Celery Beat containers: running

The local preview is ready for analysis and paper/shadow operation. It is deliberately not approved for autonomous live execution.

## v2.6 candidate result

The v2.6 cost-aware ranking/regime/no-trade candidate was independently tested and rejected. It produced 397 trades, +4.91% return, 53.40% win rate, 1.10 profit factor and -7.36% maximum drawdown. The stronger v2.5 result remains the active paper baseline. See `V3_8_MODEL_AND_GATE_REPORT.md` for the complete gate audit.
