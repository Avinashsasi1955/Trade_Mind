# Promotion engine v3.4 report

Updated: 3 July 2026 IST

## Implemented

- PostgreSQL `system_promotion_ledger` holds all five gates and cannot set `live_eligible=true` unless every numerical and Boolean constraint passes.
- A database trigger rejects attempts to set `risk_control_state.trading_enabled=true` while the promotion ledger is ineligible.
- Broker submission now requires both environment fuses and the PostgreSQL promotion ledger.
- The 90-session ledger credits only complete live sessions; historical dates and incomplete bars/VIX/OI/prediction coverage are rejected.
- Bearish ML cash signals route to an active derivative contract or produce an immutable rejection event.
- The live stream caches contemporaneous best bid/ask depth in Redis for signal-time audit capture.
- `shadow_execution_audits` stores depth, one-tick adverse fill, estimated fees, reconciliation status and derivative verification.
- A 4:00 PM IST Celery task reconciles pending shadow costs and reevaluates every promotion gate.
- Zerodha cost rates were rechecked: NSE equity transaction charge corrected to 0.00307%, 2026 futures/options STT retained, STT now rounds to the nearest rupee, and IPFT is represented. Estimates remain non-authoritative until contract notes exist.

## Threshold/meta-label finding

The proposed fixed 0.35/0.65 directional cutoff is available as an abstention experiment, but it is not activated as a claim of improved log loss. On the active model's 2,485 latest probabilities:

- maximum probability: 0.6257415064;
- probabilities ≥0.65: 0;
- probabilities ≤0.35: 3;
- resulting directional coverage: approximately 0.12%.

Therefore a 0.65 cutoff would eliminate every bullish signal and leave only three bearish observations. It cannot provide statistically credible evidence and does not alter the original holdout log loss. A secondary meta-model must be trained on leakage-free out-of-fold primary predictions and judged on a separate chronological holdout before promotion.

The chronological calibration experiment also compared identity, Platt, beta and isotonic calibration without selecting on the retrospective holdout. Identity was best, with selection log loss `0.720002` and retrospective holdout log loss `0.710944`; Platt (`0.732096`), beta (`0.732132`) and isotonic (`0.749983`) were worse on that holdout. Calibration alone therefore does not clear the `<0.693` gate.

## Required model-quality solution

1. Preserve a genuinely unseen future period; the previously reported holdout is now retrospective evidence and must not be reused for promotion.
2. Generate purged, embargoed out-of-fold primary-model probabilities across chronological folds.
3. Train the meta-label model only on those out-of-fold probabilities plus regime, liquidity, spread, volatility, sentiment freshness and derivative-availability features.
4. Tune abstention thresholds against expected value after costs, with minimum trade-count and market-regime coverage constraints—not win rate alone.
5. Freeze the pipeline and evaluate once on the new untouched period. Promote only if log loss, profit factor, drawdown, cost and coverage gates all pass.

## Current immutable gate state

- Completed forward sessions: 0/90 — failed.
- Forward profit factor/drawdown: unavailable — failed.
- Untouched log loss: 0.6937 versus required <0.693 — failed.
- Reconciled live-cost sessions: 0/20 — failed.
- Verified derivative shadow executions: 0/20 — failed.
- `live_eligible`: false.

The PostgreSQL trigger was tested and successfully blocked manual trading enablement.

## Verification

- 46 tests pass; two PostgreSQL-only tests skip unless `DATABASE_URL` is supplied.
- v3.4 database archive fully restored 6,435,334 bars and 3,488,676 features.
- Archive: `postgres-20260702T194218Z.dump`, 989,190,999 bytes, SHA-256 `aa7d88e60e500040fc0c9d7e0d2ba59e54ef3044e49874b72412a6c7665a7ce4`.
- Linux/amd64 `nivesh-ai:v3.4` image smoke test passes with CPU-only PyTorch.
- AWS/OpenTofu infrastructure remains valid and execution services default to zero replicas.

Reference rates: [Zerodha charges](https://zerodha.com/charges/), [April 2026 STT revision](https://zerodha.com/marketintel/bulletin/445377/revision-in-stt-securities-transaction-tax-from-1st-april-2026).
