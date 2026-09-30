# Nivesh v3 production deployment gate

This document prepares production infrastructure; it does not certify the ML model for autonomous capital. ML v2.5 remains `live_eligible=false` until every gate below is evidenced.

## 1. Regulatory and broker readiness

1. Review SEBI circular `SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013` and the latest exchange/broker operational requirements with a qualified compliance professional.
2. Confirm Zerodha supports the intended retail-algo workflow, static-IP requirements, order-rate limits, tagging and approval/registration requirements for this account.
3. Register a backend-only Kite redirect URL over HTTPS. Keep the API secret and daily access token in a secret manager, never in the browser, database, image or repository.
4. Keep `NIVESH_LIVE_TRADING_ENABLED=0`; PostgreSQL/Redis availability must never toggle it.

References: [SEBI retail algo circular](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html), [Kite Connect introduction](https://kite.trade/docs/connect/v3/), [Kite order lifecycle](https://kite.trade/docs/connect/v3/orders/).

## 2. Provision and validate infrastructure

1. Use managed PostgreSQL/Redis for production. `docker-compose.v3.yml` is for an isolated staging host, not high availability.
2. Generate independent random PostgreSQL, Redis, JWT and broker secrets. Restrict database/Redis network access to application and worker security groups.
3. Apply `migrations/postgres/v3_0_production.sql` using a dedicated migration role. Verify constraints, trigger behavior and point-in-time restore before importing data.
4. Run exactly one Celery Beat instance. Start FinBERT workers with concurrency `1`; scale by adding explicitly sized workers rather than increasing concurrency inside a memory-constrained process.
5. Configure PostgreSQL backups, WAL archiving, TLS, connection limits and alerts. Configure Redis authentication, TLS/persistence and memory eviction policy appropriate for a task broker.

Staging command after secrets are configured:

```bash
docker compose -f docker-compose.v3.yml config
docker compose -f docker-compose.v3.yml up -d postgres redis worker beat
```

## 3. Migrate SQLite data with reconciliation

1. Stop the in-process scheduler and take consistent copies of `nivesh.db`, `market_history.db` and `ml_research.db`.
2. Import instrument master first, resolving every record to `(exchange,symbol)` and preserving cross-listed ISINs as separate listings.
3. Import bars in date partitions/batches using Decimal-safe text or binary COPY. Reconcile row counts, per-symbol min/max timestamps, OHLC constraints and SHA-256 source provenance.
4. Import model metadata, feature observations, shadow predictions and immutable order events. Never import API secrets or expired access tokens.
5. Perform dual-read comparisons for at least five sessions. SQLite remains the rollback source until PostgreSQL counts, checksums and portfolio balances match.
6. The application/auth/portfolio/order repositories now support PostgreSQL and their staging migration reconciles. Do not remove SQLite yet: the historical `HistoryStore` and ML `ResearchStore` execution paths still need their final PostgreSQL port and dual-read validation.

Verified local recovery command:

```bash
python -m backend.postgres_backup nivesh_v3_staging --restore-drill
```

This produced a SHA-256-verified custom archive and reconciled all 6,435,334 bars after a full temporary-database restore.

## 4. Read-only Zerodha validation

1. Connect Kite login and WebSocket in read-only mode. Subscribe in batches using instrument tokens from the daily instrument master.
2. Store exchange timestamps and receive timestamps separately. Reject stale, out-of-order and impossible ticks. Aggregate 1-minute bars once and derive 5-minute bars deterministically.
3. Reconcile instruments, holdings, positions, margins and order updates without placing orders. Alert on token changes, lot-size changes, expiry rollover and broker/local position mismatches.
4. Collect India VIX in `india_vix_history`; collect OI only for derivative instruments. Never backfill missing VIX/OI with zero.

## 5. Transaction-drag validation

1. Use `backend.transaction_costs.estimate_zerodha_costs` only as a pre-trade estimate. Rates are segment, exchange, date and side dependent.
2. Run supervised paper/replay orders and compare estimates with Zerodha brokerage calculator and actual contract notes.
3. Persist actual fees and actual fill slippage in `order_execution_ledger`. Investigate every material variance before enabling another segment.
4. Rates encoded for the current deployment include the F&O STT revision effective 1 April 2026. Revalidate rates on every tax/exchange circular change. [Zerodha charges](https://zerodha.com/charges), [STT revision](https://zerodha.com/marketintel/bulletin/445377/revision-in-stt-securities-transaction-tax-from-1st-april-2026).

## 6. Ninety-session supervised shadow gate

For 90 completed trading sessions:

- Ingest official/live data and generate predictions before outcomes exist.
- Keep all broker submission disabled; resolve outcomes only after the barrier horizon completes.
- Track coverage, stale data, calibration, PSI, win rate, profit factor, drawdown, turnover, slippage and sector/regime concentration.
- Validate bullish signals separately from shorts. A short is executable only when mapped to an eligible futures/SLB instrument with verified lot size, margin and expiry.
- Require stable results across folds and forward months; the historical 1.31 profit factor is not a promised forward value.

## 7. Microscopic-capital progression

After regulatory approval and all shadow gates pass:

1. Enable broker-connected supervised orders with human approval, one liquid equity, minimum quantity and a stricter daily loss limit than the hard −7.32% disaster stop.
2. Require LIMIT/SL orders, fresh quotes, idempotent client IDs, margin checks, order-update reconciliation and operator-visible kill controls.
3. Test rejection, partial fill, network partition, duplicate callback, broker outage, stale token, Redis failure, PostgreSQL failover and emergency cancellation.
4. Expand symbols and derivatives only after contract-note reconciliation and several clean weeks at the previous stage.
5. Fully autonomous execution is a separate final approval. It must not be enabled solely because credentials exist or because a backtest passes.

The PostgreSQL trigger permanently locks trading when recorded daily account drawdown reaches −7.32%. The existing application default is stricter (2% realised daily loss) and should remain the ordinary operating limit.
