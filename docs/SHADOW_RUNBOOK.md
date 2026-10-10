# Shadow-Trading Operational Runbook (Phase 7a)

This runbook documents the exact procedures, commands, and operational sequences to manage the paper-only shadow trading pipeline in TradeMind.

---

## 1. Paper-Only Safety Guarantees

Before starting any service, verify that both execution fuses remain locked:

| Setting / Flag | Required Value | Actual Current Value | Purpose |
| :--- | :--- | :--- | :--- |
| `NIVESH_LIVE_TRADING_ENABLED` | `0` | `0` | Disables broker order submission in `backend/execution.py` |
| `LIVE_ELIGIBLE` | `FALSE` | `FALSE` | Safety interlock in `backend/live_inference.py:194` and `backend/tasks/worker.py:185` |
| `UPSTOX_ORDER_ACCESS_TOKEN` | `""` (empty) | `""` (empty) | Interactive write token required for order placement |

> **Safety Interlock:** `LivePaperInference.__init__` in `backend/live_inference.py` raises `RuntimeError("Live-paper inference requires both execution fuses locked")` if either `LIVE_ELIGIBLE != "FALSE"` or `NIVESH_LIVE_TRADING_ENABLED != "0"`. No live order can be submitted from the shadow pipeline.

---

## 2. Component Startup Sequence

Start components strictly in the following order:

```
[1. Redis] ──> [2. PostgreSQL Database] ──> [3. Live Stream Service] ──> [4. Celery Worker] ──> [5. Celery Beat]
```

### Component 1: Redis Server
Redis acts as the message broker for Celery and the real-time cache for live ticks (`nivesh:ticks:*`).

```bash
# Start Redis (system service or daemon)
brew services start redis
# Or start directly:
redis-server --daemonize yes

# Verification:
redis-cli ping
# Expected output: PONG
```

### Component 2: PostgreSQL Database
Houses audit trails (`shadow_execution_audits`, `trade_candidate_audits`, `shadow_session_daily_log`, `exchange_trading_calendar`).

```bash
# Start PostgreSQL (local service)
brew services start postgresql@14

# Verification:
python3 -c "from sqlalchemy import create_engine, text; eng = create_engine('postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging'); print(eng.connect().execute(text('SELECT 1')).scalar())"
# Expected output: 1
```

### Component 3: Live Market Stream Service
Maintains the WebSocket market data feed (Upstox/Kite), builds 1m and 5m candle aggregates, and updates tick cache.

```bash
# Ensure log directory exists
mkdir -p logs reports

# Foreground execution (for debugging):
.venv/bin/python -m backend.live_stream_service

# Background daemon execution:
nohup .venv/bin/python -m backend.live_stream_service > logs/stream.log 2>&1 &

# Verification:
ps aux | grep backend.live_stream_service | grep -v grep
```

### Component 4: Celery Worker
Executes periodic paper inference, bar repair, position defense, and news ingestion tasks.

```bash
# Foreground execution:
.venv/bin/celery -A backend.tasks.worker:celery_app worker --loglevel=INFO --concurrency=1 --prefetch-multiplier=1

# Background daemon execution:
nohup .venv/bin/celery -A backend.tasks.worker:celery_app worker --loglevel=INFO --concurrency=1 --prefetch-multiplier=1 > logs/worker.log 2>&1 &

# Verification:
.venv/bin/celery -A backend.tasks.worker:celery_app status
```

### Component 5: Celery Beat Scheduler
Dispatches scheduled cron tasks (pre-market readiness, 5-minute live paper inference, eod reporting).

```bash
# Foreground execution:
.venv/bin/celery -A backend.tasks.worker:celery_app beat --loglevel=INFO

# Background daemon execution:
nohup .venv/bin/celery -A backend.tasks.worker:celery_app beat --loglevel=INFO > logs/beat.log 2>&1 &

# Verification:
ps aux | grep "celery.*beat" | grep -v grep
```

---

## 3. Pre-Open Checklist (Run before 09:15 IST)

Execute these checks every morning between **08:30 and 09:00 IST**:

### Step 1: Upstox Daily Interactive Token
Obtain the daily market data token from Upstox and export it in the shell environment:
```bash
export UPSTOX_ACCESS_TOKEN="<your_daily_upstox_token>"
```

### Step 2: Run Pipeline Smoke Test
Confirms wiring, Redis, Postgres, calendar row, and verifies dry-run inference writes zero rows:
```bash
python3 scripts/shadow_smoke_test.py
```
*Expected verdict:* `SMOKE TEST RESULT: [PASS] All shadow pipeline wiring verified!`

### Step 3: Run Start-Up Health Check
Verifies exchange calendar session status, required processes, and tests Upstox token validity:
```bash
python3 scripts/shadow_health_check.py
```
*Verification criteria:*
- `Trading Day: YES (OPEN)`
- `Schedule Source: NSE_REGULAR_SESSION (Hours: 09:15:00 - 15:30:00 IST)`
- `Required Processes: [RUNNING]` for Stream, Worker, and Beat
- `Token Status: [PASS] VALID (length: > 0)`

---

## 4. End-of-Day Routine (Run after 15:30 IST)

Perform these steps after market close between **15:35 and 16:00 IST**:

### Step 1: Run Post-Market Health Check
Verifies today's session summary row and checks for any WebSocket gaps:
```bash
python3 scripts/shadow_health_check.py
```

### Step 2: Generate Shadow Statistical Scorecard
Computes sample counts, win rate, payoff ratio, profit factor, bootstrap 95% CI expectancy, and evaluates promotion gates:
```bash
python3 scripts/shadow_scorecard.py \
  --since 2026-06-01 \
  --json reports/scorecard_$(date +%Y-%m-%d).json
```

### Step 3: Save and Archive Scorecard
Save a plain-text copy of the scorecard for audit history:
```bash
python3 scripts/shadow_scorecard.py --since 2026-06-01 > reports/scorecard_$(date +%Y-%m-%d).txt
cat reports/scorecard_$(date +%Y-%m-%d).txt
```

---

## 5. Shutdown Routine

To gracefully stop all background shadow processes:

```bash
# 1. Stop Celery Beat scheduler
pkill -f "celery.*beat"

# 2. Stop Celery Worker gracefully (allows active tasks to finish)
pkill -TERM -f "celery.*worker"

# 3. Stop Live Stream Service
pkill -f "backend.live_stream_service"

# 4. Verify all trading processes are terminated
ps aux | grep -E "celery|live_stream_service" | grep -v grep

# 5. Optional: Stop local Redis / PostgreSQL if stopping local development environment
# brew services stop redis
# brew services stop postgresql@14
```
