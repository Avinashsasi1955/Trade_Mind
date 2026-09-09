# Nivesh AI v3.8 model and gate report

Verified on 4 July 2026 IST against the running local PostgreSQL paper environment.

## Active paper baseline

- Model: `direction-v2.5-20260703T210440115936Z`
- Untouched accuracy: 51.47%
- Untouched log loss: 0.6937
- Walk-forward trades: 550
- Walk-forward return: +27.90%
- Win rate: 58.00%
- Profit factor: 1.31
- Maximum drawdown: -7.32%
- Status: active paper baseline; `live_eligible=false`

## Rejected v2.6 experiment

The cost-aware cross-sectional ranking, regime gate and strict no-trade policy were trained and tested without changing the untouched holdout.

- Model: `direction-v2.6-20260704T053159679116Z`
- Untouched log loss: 0.6937 (unchanged)
- Walk-forward trades: 397
- Walk-forward return: +4.91%
- Win rate: 53.40%
- Profit factor: 1.10
- Maximum drawdown: -7.36%
- Status: rejected; it did not outperform v2.5

The policy reduced trades but also removed profitable opportunities. It is retained as an auditable experiment and is not used by the active paper model.

## Software completed

- Candidate models can no longer replace the active baseline before validation.
- Training registers a candidate; walk-forward validation promotes only a superior candidate or marks it rejected.
- Cost-aware ranking, market-regime classification and explicit no-trade rejection are implemented as an experimental policy.
- Estimated shadow costs cannot satisfy the real-cost promotion gate.
- Real-cost evidence requires filled Zerodha ledger orders with reconciled contract-note fees.
- Derivative evidence requires active futures/options contracts, valid expiry, correct lot multiples and broker-connected shadow observations.
- NVIDIA NIM was added as an OpenAI-compatible, quota-dependent narrative provider.
- External language models remain unable to submit orders or alter deterministic risk controls.
- PostgreSQL, Redis, web, Celery worker and Celery Beat are healthy on image `nivesh-ai:paper-v3.8`.
- All 65 container tests pass.

## Genuine evidence still required

- Forward shadow sessions: 0/90
- Real contract-note reconciled sessions: 0/20
- Verified derivative shadow executions: 0/20
- Kite live market data and instrument session: not configured
- Licensed intraday/news data: not configured
- Untouched log loss gate: fails at 0.6937; required `<0.693`

These counters must be produced by future broker-connected observations. They cannot be safely generated from historical or simulated records. Autonomous live execution remains locked.

Historical results are research evidence, not a profit guarantee.
