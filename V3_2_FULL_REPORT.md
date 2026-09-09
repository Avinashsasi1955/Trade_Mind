# Nivesh AI v3.4 completion and validation report

Updated: 2 July 2026 IST

## Executive verdict

The software/data platform is now v3.4. `HistoryStore` and `ResearchStore` route natively to PostgreSQL whenever `DATABASE_URL` is configured; their SQLite files remain rollback archives, not production runtime dependencies. The production AWS HA/TLS infrastructure is defined and provider-validated but has not been provisioned because AWS authentication and approval for billable resources have not been supplied.

The freshly trained ML model remains version 2.5 because this update changed storage/infrastructure, not model methodology. It is reproducible but **not live eligible**. Autonomous trading remains disabled.

## PostgreSQL migration and integrity

- Market bars: 6,435,334 across 6,940 instruments, 30 June 2021–29 June 2026.
- Invalid OHLC rows: 0.
- History sync failures: 0.
- Feature rows: 3,488,676 across 6,467 exchange/symbol listings, 13 September 2021–29 June 2026.
- Labels: 1,469,081 positive; 1,456,812 negative; 562,783 unresolved/neutral barrier outcomes.
- Duplicate feature primary keys: 0.
- Rows missing core feature fields: 0.
- Corporate actions: 12,588 across 2,481 exchange/symbol listings.
- Runtime routing tests confirmed `PostgresHistoryStore` and `PostgresResearchStore`.

## Fresh training result

- Model: `direction-v2.5-20260702T111144315292Z`.
- Selected algorithm: flexible histogram gradient boosting with isotonic calibration.
- Training samples: 408,893.
- Training: 69.97% accuracy, 78.96% precision, 64.61% recall, 0.6144 log loss, 0.2116 Brier score.
- Calibration block: 57.34% accuracy, 59.72% precision, 61.07% recall, 0.6724 log loss, 0.2400 Brier score.
- Untouched holdout: 116,358 samples; 51.47% accuracy, 54.55% precision, 26.99% recall, 0.6937 log loss, 0.2502 Brier score.

The holdout log loss is slightly worse than the approximately 0.693 random binary baseline. Therefore this model must not be described as reliably predictive despite its profitable simulated portfolio path.

## Fresh walk-forward portfolio result

- Method: six-fold purged expanding walk-forward validation.
- Period: 8 August 2023–25 June 2026.
- Symbols: 1,940 liquid eligible listings.
- Trades: 550.
- Starting capital: ₹10,00,000.
- Ending capital: ₹12,79,040.12.
- Net return: +27.90% after estimated charges, slippage and participation impact.
- Win rate: 58.00%.
- Profit factor: 1.31.
- Maximum drawdown: −7.32%.
- Policy: top 10 signals, 10-session non-overlapping rebalance, 5% per position, 50% maximum gross exposure.

This reproduces the previous result, which is useful evidence of deterministic migration. It is not evidence of guaranteed future profit.

## Promotion gates

Passed: positive simulated return, profit factor ≥1.20, drawdown above −15%, and at least five folds.

Failed/pending: untouched holdout log loss below random, resolved forward-shadow months, and broker-instrument executability. Overall gate: failed; `live_eligible=false`.

Fresh shadow generation created 2,485 predictions with `orders_allowed=false`. Drift status is currently stable across all 21 monitored features, but this is a small current window and does not replace forward validation.

## HA/TLS infrastructure

`infra/aws-ha` contains a validated OpenTofu stack for:

- private RDS PostgreSQL 17 Multi-AZ;
- forced PostgreSQL TLS with certificate hostname verification;
- 35-day backups, storage autoscaling, performance insights, deletion protection and final snapshots;
- a three-node ElastiCache Redis replication group with Multi-AZ automatic failover;
- Redis TLS/auth, KMS encryption at rest and snapshots;
- security-group isolation, generated credentials in Secrets Manager and CloudWatch/SNS alarms.

OpenTofu formatting and provider-schema validation pass using AWS provider 5.100.0 and random provider 3.9.0. Actual provisioning and failover testing remain operator tasks because they create chargeable cloud resources.

## Verification completed

- 44 standard tests pass; two PostgreSQL-only tests intentionally skip without `DATABASE_URL`.
- Both PostgreSQL integration tests pass when run against staging.
- Python compilation passes.
- PostgreSQL runtime repository reads pass.
- OpenTofu configuration validation passes.
- Final v3.4 custom-format PostgreSQL backup was SHA-256 verified and fully restored: 6,435,334/6,435,334 bars and 3,488,676/3,488,676 feature rows reconciled. The compressed archive is 989,190,999 bytes with SHA-256 `aa7d88e60e500040fc0c9d7e0d2ba59e54ef3044e49874b72412a6c7665a7ce4`.

## Still required before deployment

1. Supply AWS networking/pager inputs and approve the chargeable `tofu apply`.
2. Provision the stack, mount the current RDS CA bundle, apply migrations and import the reconciled data.
3. Execute real RDS failover, Redis primary promotion, restore and application reconnect drills.
4. Supply licensed news and Zerodha credentials; validate read-only WebSocket/VIX/OI/instrument flows.
5. Reconcile actual contract-note charges and slippage.
6. Complete 90 forward shadow sessions and supervised microscopic-capital validation.
7. Complete broker/exchange/compliance approvals.

Do not enable `NIVESH_LIVE_TRADING_ENABLED` until every failed/pending promotion gate is independently reviewed and passed.
