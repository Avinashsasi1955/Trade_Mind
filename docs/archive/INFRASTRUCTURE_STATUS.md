# Infrastructure completion status

Updated: 2 July 2026 IST

## Completed locally

- PostgreSQL 17.10 installed and running through Homebrew.
- Redis 8.8 installed and running through Homebrew.
- Isolated `nivesh_v3_staging` database created.
- V3 PostgreSQL schema applied successfully.
- Database drawdown trigger tested at -8%; it locked trading against the -7.32% hard threshold.
- Security master migrated: 7,020 instruments.
- Historical warehouse migrated and reconciled: 6,435,334 bars, 6,940 covered instruments, 2021-06-30 through 2026-06-29.
- PostgreSQL OHLC integrity check: zero invalid rows.
- Local PostgreSQL size after import: approximately 1.2 GB.
- Project-local Python environment created with PostgreSQL, Redis, Celery, PyTorch and Transformers dependencies.
- Celery worker started with concurrency 1 and passed a Redis ping.
- ProsusAI/FinBERT downloaded and completed a two-headline CPU inference smoke test.
- Bulk importer added at `scripts/migrate_history_to_postgres.py`.
- Application schema migration applied for authentication, portfolios, settings, holdings, trades, agent runs, analysis, sentiment, AI audit logs, chat, risk controls, kill switches, order intents/events and broker session state.
- Application SQLite data migrated and reconciled into PostgreSQL with `scripts/migrate_app_to_postgres.py`.
- PostgreSQL compatibility layer uses a bounded threaded connection pool with health checks; authenticated API/dashboard smoke tests passed against PostgreSQL.
- Kite instrument-master synchronization and deterministic 1-minute/5-minute bar aggregation are implemented, including derivative expiry, strike, option type, lot size, OI/OI change and isolated India VIX storage.
- Celery worker heartbeat writes to PostgreSQL and verifies Redis; exactly-one-Beat scheduling and worker concurrency `1` are configured.
- Structured JSON logs and deduplicated incident-webhook paging are implemented. The external pager webhook still needs to be supplied.
- Duplicate-order idempotency, partial-fill reconciliation and broker-outage state preservation are covered by automated tests.
- PostgreSQL custom-format backup, SHA-256 verification and full restore drill passed: 6,435,334 of 6,435,334 market bars restored and reconciled.
- Full automated suite passes: 46 tests (two PostgreSQL integration tests conditionally skipped without `DATABASE_URL`), including stale-token, derivative routing, execution-fuse, promotion-ledger, remote-restore and shadow-ledger controls.
- Live/autonomous submission remains disabled by default and requires explicit operator enablement after promotion gates.
- `HistoryStore` and `ResearchStore` now route natively to PostgreSQL when `DATABASE_URL` is configured.
- Research migration reconciled 3,488,676 feature rows, 12,588 corporate actions, 11 historical validations and 11,408 pre-v3.2 shadow records.
- Fresh model training and walk-forward validation completed on PostgreSQL; the portfolio result reproduced but the model remains `live_eligible=false`.
- AWS RDS/ElastiCache Multi-AZ, TLS, KMS, Secrets Manager, backup and alarm infrastructure is defined in `infra/aws-ha` and passes OpenTofu provider validation.
- Final 989 MB compressed PostgreSQL recovery drill restored and reconciled all 6,435,334 bars and 3,488,676 feature rows.

## Blocked by local/operator prerequisites

- Docker CLI and Colima are installed without Docker Desktop. The 437 MB Linux/amd64 `nivesh-ai:v3.4` CPU image built and passed its internal dependency/restore-tool smoke test.
- SQLite files should be retained temporarily as rollback archives, but production runtime repositories no longer require them when `DATABASE_URL` is configured.
- Celery Beat is intentionally not left running because no licensed news API key is configured and no production feature-observation stream exists yet.
- Zerodha login, read-only WebSocket and instrument/OI/VIX feeds require the user's Kite application credentials and daily interactive login.
- Contract-note reconciliation requires real broker contract notes.
- Ninety forward sessions require elapsed market time and cannot be backfilled from historical labels.
- Microscopic-capital trading requires user authorization, broker/regulatory readiness and supervised approval.
- Autonomous execution remains locked.

## Operator actions still required for deployment

1. Authenticate the AWS CLI and explicitly approve the billable OpenTofu plan; local Homebrew services remain staging-only.
2. Supply production `DATABASE_URL`, `REDIS_URL`, JWT secret, TLS/HTTPS domain and secret-manager entries.
3. Supply a licensed news API key and an incident-management webhook; then enable one Beat instance and verify real FinBERT ingestion alerts.
4. Supply Kite API credentials and complete the daily interactive Zerodha login. Start with read-only instruments/WebSocket/holdings reconciliation.
5. Provide contract notes so estimated brokerage, STT, GST, SEBI/exchange charges and slippage can be reconciled against actual fees.
6. Arrange managed PostgreSQL HA/PITR and Redis HA; repeat restore and database-failover drills in that environment.
7. Complete the 90-session shadow period, supervised microscopic-capital phase and compliance/broker approvals. Credentials do not waive these gates.
