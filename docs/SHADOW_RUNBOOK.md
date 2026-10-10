# Shadow-Trading Operational Runbook (Phase 7b)

This runbook documents the procedures, commands, launchers, and verification steps for operating the paper-only shadow trading pipeline in TradeMind.

---

## 1. Paper-Only Safety Guarantees

Before launching any component, verify that all execution fuses remain locked:

| Setting / Flag | Required Value | Purpose |
| :--- | :--- | :--- |
| `NIVESH_LIVE_TRADING_ENABLED` | `0` | Disables broker order submission in `backend/execution.py` |
| `LIVE_ELIGIBLE` | `FALSE` | Safety interlock in `backend/live_inference.py:194` and `backend/tasks/worker.py:185` |
| `UPSTOX_ORDER_ACCESS_TOKEN` | `""` (empty) | Interactive write token required for order placement |
| `NIVESH_MARKET_DATA_PROVIDER` | `upstox` | Must match configured Upstox market data stream credentials |

> **Safety Interlock:** `scripts/start_shadow.sh`, `backend/live_inference.py:194`, `backend/tasks/worker.py:185`, and `backend/live_stream_service.py:32` all refuse to start and raise `RuntimeError("Live-paper inference requires both execution fuses locked")` if either `LIVE_ELIGIBLE != "FALSE"` or `NIVESH_LIVE_TRADING_ENABLED != "0"`.

---

## 2. One-Command Launcher & Process Management

### Automated Start: `scripts/start_shadow.sh`
Starts Redis (if needed), Live Stream Service, Celery Worker, and Celery Beat in strict sequential order. Each background daemon is wrapped with `caffeinate -dimsu` to prevent macOS system, disk, and display sleep during market hours:

```bash
./scripts/start_shadow.sh
```

**Verification:**
- Logs written to `logs/stream.log`, `logs/worker.log`, and `logs/beat.log`.
- Active PIDs saved in `logs/stream.pid`, `logs/worker.pid`, and `logs/beat.pid`.
- Confirms `LIVE_ELIGIBLE == FALSE` and `NIVESH_LIVE_TRADING_ENABLED == 0` before starting any process.

### Automated Stop: `scripts/stop_shadow.sh`
Gracefully signals and terminates beat scheduler, worker, stream service, and associated caffeinate wrappers:

```bash
./scripts/stop_shadow.sh
```

---

## 3. Pre-Open Checklist (Run Daily: 08:30 – 09:00 IST)

### Step 1: Upstox Token Configuration
Set the daily interactive access token or verify that `UPSTOX_ANALYTICS_TOKEN` is present:
```bash
export UPSTOX_ACCESS_TOKEN="<your_token>"
```

### Step 2: Pipeline Smoke Test
Validates imports, database connectivity, Redis ping, calendar row, provider alignment, and verifies that inference dry-run writes zero rows:
```bash
python3 scripts/shadow_smoke_test.py
```
*Expected verdict:* `SMOKE TEST RESULT: [PASS] All shadow pipeline wiring verified!`

### Step 3: Start-Up Health Check
Validates calendar hours, tests Upstox token using read-only market-data LTP call (`GET /v2/market-quote/ltp?instrument_key=NSE_INDEX|Nifty 50`), and inspects process state:
```bash
python3 scripts/shadow_health_check.py
```
*Verification criteria:*
- `Active Provider: upstox`
- `Trading Day: YES (OPEN)`
- `Token Status: [PASS] VALID [HTTP 200]`
- `Provider Config: [PASS] upstox`

### Step 4: Launch Pipeline
```bash
./scripts/start_shadow.sh
```

---

## 4. First-Session Watch List (Monday 09:15 – 10:15 IST)

During the first hour of trading, verify that each milestone occurs in real time:

### 1. Live Ticks Arriving (09:15:05+ IST)
Check that WebSocket ticks are populating the Redis timestamp cache:
```bash
python3 -c "import redis; r = redis.Redis(); print('Cached instruments:', len(r.hgetall('nivesh:ticks:timestamp')))"
```
*Expected:* Count increases to subscribed symbol limit (50–100+ instruments) within seconds of market open.

### 2. Forming & Completed Bars Appearing (09:16:00+ and 09:20:00+ IST)
Verify that `PostgresBarAggregator` closes 1-minute and 5-minute bars with source `upstox_v3`:
```bash
psql -d nivesh_v3_staging -c "
SELECT interval, source, COUNT(*), MAX(bar_time) AS latest_bar
FROM live_market_bars
WHERE bar_time >= CURRENT_DATE
GROUP BY interval, source;
"
```
*Expected:* Rows appear for `1minute` (from 09:16 IST) and `5minute` (from 09:20 IST) with `source = 'upstox_v3'`.

### 3. First Candidate Audit Recorded (09:20:00+ IST)
Check candidate selection evaluations from `LivePaperInference`:
```bash
psql -d nivesh_v3_staging -c "
SELECT observed_at, symbol, side, signal_probability, gate_passed, rejection_reason
FROM trade_candidate_audits
ORDER BY observed_at DESC
LIMIT 5;
"
```
*Expected:* Entries appear every 5 minutes showing candidate symbols, probability scores, and gate pass/rejection reasons.

### 4. Daily Session Log Status (09:20:00+ IST)
Inspect the active session state tracked in `shadow_session_daily_log`:
```bash
psql -d nivesh_v3_staging -c "
SELECT session_date, status, valid, trades_taken, updated_at
FROM shadow_session_daily_log
WHERE session_date = CURRENT_DATE;
"
```
*Expected:* Row exists for today's date with status `WAITING` (until full minimum bucket and trade evidence thresholds are met).

---

## 5. End-of-Day Routine (Run Daily: 15:35 – 16:00 IST)

### Step 1: Health Check Review
```bash
python3 scripts/shadow_health_check.py
```

### Step 2: Generate Statistical Scorecard
```bash
python3 scripts/shadow_scorecard.py \
  --since 2026-06-01 \
  --json reports/scorecard_$(date +%Y-%m-%d).json > reports/scorecard_$(date +%Y-%m-%d).txt

cat reports/scorecard_$(date +%Y-%m-%d).txt
```

### Step 3: Stop Shadow Pipeline
```bash
./scripts/stop_shadow.sh
```
