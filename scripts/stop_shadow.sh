#!/usr/bin/env bash
# scripts/stop_shadow.sh
# Gracefully stops all TradeMind shadow trading components and caffeinate wrappers.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"

echo "=============================================================================="
echo "               STOPPING TRADEMIND SHADOW PIPELINE"
echo "=============================================================================="

# Stop Celery Beat
echo "1. Stopping Celery Beat..."
if [ -f "$LOG_DIR/beat.pid" ]; then
    PID=$(cat "$LOG_DIR/beat.pid")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
    fi
    rm -f "$LOG_DIR/beat.pid"
fi
pkill -f "celery.*beat" 2>/dev/null || true
echo "   [STOPPED] Celery Beat"

# Stop Celery Worker
echo "2. Stopping Celery Worker..."
if [ -f "$LOG_DIR/worker.pid" ]; then
    PID=$(cat "$LOG_DIR/worker.pid")
    if kill -0 "$PID" 2>/dev/null; then
        kill -TERM "$PID" 2>/dev/null || true
    fi
    rm -f "$LOG_DIR/worker.pid"
fi
pkill -TERM -f "celery.*worker" 2>/dev/null || true
echo "   [STOPPED] Celery Worker"

# Stop Live Stream Service
echo "3. Stopping Live Stream Service..."
if [ -f "$LOG_DIR/stream.pid" ]; then
    PID=$(cat "$LOG_DIR/stream.pid")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
    fi
    rm -f "$LOG_DIR/stream.pid"
fi
pkill -f "backend.live_stream_service" 2>/dev/null || true
echo "   [STOPPED] Live Stream Service"

# Clean up caffeinate wrappers if any remain
pkill -f "caffeinate -dimsu" 2>/dev/null || true

sleep 1

echo "------------------------------------------------------------------------------"
echo "Checking remaining processes:"
REMAINING=$(ps aux | grep -E "celery|live_stream_service" | grep -v grep || true)
if [ -n "$REMAINING" ]; then
    echo "WARNING: Some processes may still be lingering:"
    echo "$REMAINING"
else
    echo "All shadow trading pipeline components have cleanly stopped."
fi
echo "=============================================================================="
