# Live-paper phases 1–3 completion report

Updated: 4 July 2026 IST

## Completed

- One reusable `nivesh-ai:paper-v3.6` image serves Web, Worker, Beat and the credential-gated Stream process.
- PostgreSQL, Redis, one Celery Worker, exactly one Celery Beat and Uvicorn Web run under Compose health/dependency gates.
- The read-only Stream is isolated behind the `live-paper` profile and refuses to start unless both execution fuses are locked.
- Mixed Kite `full`/`quote` subscriptions are capped at 3,000 tokens per connection and 9,000 total.
- Equity/derivative and short index packets are decoded; India VIX index timestamps are retained.
- One-minute and five-minute candles close by exchange time. In-progress candles are discarded on shutdown.
- Disconnects, recoveries and bounded priority-candle backfills are audited.
- Live feature snapshots store feature JSON, SHA-256 hash, source bar, causal watermark, price, model and instrument.
- Shadow predictions are idempotent by model, instrument, timeframe and watermark.
- The daily v2.5 model is forbidden from intraday inference before the final completed bar to prevent train/serve mismatch.
- Dashboard quotes switch to completed PostgreSQL Kite bars when available and label stale bars explicitly.
- Forward sessions require 350 one-minute buckets, 70 five-minute buckets, 50 instruments, VIX, 50 OI observations, 10 predictions and no critical errors.
- Exchange calendar records can mark ordinary, closed and special sessions.
- Both order fuses remain disabled and no live-order endpoint is called by the replay pipeline.

## Verification evidence

- 61/61 automated tests passed against PostgreSQL and Redis.
- Disposable replay: 8 real-history instruments produced 8 feature snapshots and 8 predictions; the same watermark produced 0 duplicates.
- Docker health: PostgreSQL healthy, Redis healthy, Web healthy, Worker heartbeat operational, Beat operational.
- Restored container baseline: 6,435,334 bars and 3,488,676 feature rows.
- Active model: `direction-v2.5-20260702T111144315292Z`.
- Promotion ledger: `live_eligible=false`.

## Credential-dependent validation remaining

- Daily Kite instrument synchronization and live WebSocket handshake.
- Real tick, VIX, depth and derivative-OI comparison against Kite reference data.
- Licensed news ingestion and FinBERT production throughput.
- The formal 90-session forward clock.
- Contract-note reconciliation and any broker-write workflow.

## Local commands

Credential-free paper stack:

```bash
./scripts/compose.sh up -d postgres redis migrate web worker beat
```

After supported Kite authentication, start the read-only stream while both fuses remain off:

```bash
./scripts/compose.sh --profile live-paper up -d stream
```

Stop the stack without deleting database volumes:

```bash
./scripts/compose.sh stop
```
