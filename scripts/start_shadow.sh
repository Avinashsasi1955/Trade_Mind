#!/usr/bin/env bash
# scripts/start_shadow.sh
# Starts TradeMind shadow-trading pipeline components in strict order:
#   1. Live Stream Service
#   2. Celery Worker
#   3. Celery Beat Scheduler
# Wrapped in `caffeinate -dimsu` to keep macOS awake during trading hours.
# Refuses to start if live-order fuses are unlocked.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

echo "=============================================================================="
echo "               TRADEMIND SHADOW PIPELINE LAUNCHER"
echo "=============================================================================="

# 1. Verify Execution Fuses
# Load .env if present without overriding existing exported vars
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source <(grep -E '^(LIVE_ELIGIBLE|NIVESH_LIVE_TRADING_ENABLED|UPSTOX_ORDER_ACCESS_TOKEN)=' "$REPO_ROOT/.env" || true)
    set +a
fi

LIVE_ELIGIBLE="${LIVE_ELIGIBLE:-FALSE}"
NIVESH_LIVE_TRADING_ENABLED="${NIVESH_LIVE_TRADING_ENABLED:-0}"
UPSTOX_ORDER_ACCESS_TOKEN="${UPSTOX_ORDER_ACCESS_TOKEN:-}"

echo "Safety Fuses Check:"
echo "  LIVE_ELIGIBLE               : $LIVE_ELIGIBLE (must be FALSE)"
echo "  NIVESH_LIVE_TRADING_ENABLED : $NIVESH_LIVE_TRADING_ENABLED (must be 0)"
echo "  UPSTOX_ORDER_ACCESS_TOKEN   : length=${#UPSTOX_ORDER_ACCESS_TOKEN} (must be empty)"

LIVE_ELIGIBLE_UPPER="$(echo "$LIVE_ELIGIBLE" | tr '[:lower:]' '[:upper:]')"
if [ "$LIVE_ELIGIBLE_UPPER" != "FALSE" ]; then
    echo "ERROR: Refusing to start shadow trading! LIVE_ELIGIBLE must be FALSE (got: $LIVE_ELIGIBLE)" >&2
    exit 1
fi

if [ "$NIVESH_LIVE_TRADING_ENABLED" != "0" ]; then
    echo "ERROR: Refusing to start shadow trading! NIVESH_LIVE_TRADING_ENABLED must be 0 (got: $NIVESH_LIVE_TRADING_ENABLED)" >&2
    exit 1
fi

if [ -n "$UPSTOX_ORDER_ACCESS_TOKEN" ]; then
    echo "ERROR: Refusing to start shadow trading! UPSTOX_ORDER_ACCESS_TOKEN must be empty in shadow mode." >&2
    exit 1
fi

echo "  -> ALL LIVE-ORDER FUSES LOCKED. SAFE FOR SHADOW TRADING."
echo "------------------------------------------------------------------------------"

# 2. Python & Celery environment
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
    CELERY="$REPO_ROOT/.venv/bin/celery"
else
    PYTHON="$(command -v python3)"
    CELERY="$(command -v celery)"
fi

export PYTHONPATH="$REPO_ROOT"

# Resolve connectable database URL (e.g. localhost:5432 vs 5433)
DB_TARGET="$("$PYTHON" -c "from scripts.shadow_health_check import get_db_url; print(get_db_url(None))" 2>/dev/null || echo "")"
if [ -n "$DB_TARGET" ]; then
    export DATABASE_URL="$DB_TARGET"
    echo "Database: CONNECTED ($DB_TARGET)"
fi

# 3. Ensure Redis is reachable
if ! redis-cli ping > /dev/null 2>&1; then
    echo "Redis is not running. Starting local redis-server..."
    redis-server --daemonize yes
    sleep 1
    if ! redis-cli ping > /dev/null 2>&1; then
        echo "ERROR: Failed to connect to Redis on localhost:6379" >&2
        exit 1
    fi
fi
echo "Redis: REACHABLE (localhost:6379)"

# 4. Check if components are already running
if pgrep -f "backend.live_stream_service" > /dev/null 2>&1; then
    echo "WARNING: Stream service is already running. Run scripts/stop_shadow.sh first."
fi
if pgrep -f "celery.*worker" > /dev/null 2>&1; then
    echo "WARNING: Celery worker is already running. Run scripts/stop_shadow.sh first."
fi
if pgrep -f "celery.*beat" > /dev/null 2>&1; then
    echo "WARNING: Celery beat is already running. Run scripts/stop_shadow.sh first."
fi

echo "------------------------------------------------------------------------------"
echo "Starting Components under caffeinate -dimsu..."

# Component 1: Stream Service
echo "1. Starting Live Stream Service..."
caffeinate -dimsu "$PYTHON" -m backend.live_stream_service > "$LOG_DIR/stream.log" 2>&1 &
STREAM_PID=$!
echo "$STREAM_PID" > "$LOG_DIR/stream.pid"
sleep 2

if ! kill -0 "$STREAM_PID" 2>/dev/null; then
    echo "ERROR: Stream service failed to start! Check logs at $LOG_DIR/stream.log" >&2
    exit 1
fi
echo "   [STARTED] Stream Service (PID: $STREAM_PID) -> $LOG_DIR/stream.log"

# Component 2: Celery Worker
echo "2. Starting Celery Worker..."
caffeinate -dimsu "$CELERY" -A backend.tasks.worker:celery_app worker --loglevel=INFO --concurrency=1 --prefetch-multiplier=1 > "$LOG_DIR/worker.log" 2>&1 &
WORKER_PID=$!
echo "$WORKER_PID" > "$LOG_DIR/worker.pid"
sleep 2

if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "ERROR: Celery worker failed to start! Check logs at $LOG_DIR/worker.log" >&2
    exit 1
fi
echo "   [STARTED] Celery Worker (PID: $WORKER_PID) -> $LOG_DIR/worker.log"

# Component 3: Celery Beat
echo "3. Starting Celery Beat..."
caffeinate -dimsu "$CELERY" -A backend.tasks.worker:celery_app beat --loglevel=INFO > "$LOG_DIR/beat.log" 2>&1 &
BEAT_PID=$!
echo "$BEAT_PID" > "$LOG_DIR/beat.pid"
sleep 1

if ! kill -0 "$BEAT_PID" 2>/dev/null; then
    echo "ERROR: Celery beat failed to start! Check logs at $LOG_DIR/beat.log" >&2
    exit 1
fi
echo "   [STARTED] Celery Beat (PID: $BEAT_PID) -> $LOG_DIR/beat.log"

echo "=============================================================================="
echo "Shadow Pipeline is RUNNING. macOS sleep is prevented via caffeinate."
echo "To check status : python3 scripts/shadow_health_check.py"
echo "To stop pipeline: ./scripts/stop_shadow.sh"
echo "=============================================================================="
