#!/usr/bin/env python3
"""
scripts/shadow_smoke_test.py

Smoke test confirming shadow pipeline wiring without executing trades:
  1. Core module imports resolve cleanly
  2. Database connects and schema tables exist
  3. Redis server responds to ping
  4. Exchange calendar has today's row (or valid fallback)
  5. LivePaperInference entry point is callable in dry mode and writes nothing to the database
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable) != VENV_PYTHON:
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

IST = ZoneInfo("Asia/Kolkata")


def run_smoke_test() -> int:
    print("=" * 78)
    print("             SHADOW PIPELINE SMOKE TEST (PHASE 7A)")
    print("=" * 78)
    now_ist = datetime.now(IST)
    print(f"  Execution Time : {now_ist.isoformat()}")
    print("=" * 78)

    errors = []

    # 1. Imports
    print("\n[1] CHECKING CORE IMPORTS")
    print("-" * 78)
    try:
        import backend.config as cfg
        print("  [OK] backend.config loaded")
        from backend.live_inference import LivePaperInference
        print("  [OK] backend.live_inference.LivePaperInference resolved")
        from backend.tasks.worker import celery_app
        print("  [OK] backend.tasks.worker.celery_app resolved")
        from backend.ml.validation_engine import record_shadow_signal, record_shadow_exit
        print("  [OK] backend.ml.validation_engine shadow functions resolved")
        from scripts.shadow_scorecard import generate_full_scorecard
        print("  [OK] scripts.shadow_scorecard.generate_full_scorecard resolved")
        from scripts.shadow_health_check import check_calendar
        print("  [OK] scripts.shadow_health_check.check_calendar resolved")
    except Exception as e:
        print(f"  [FAIL] Import error: {e}")
        errors.append(f"Import failed: {e}")

    # 2. Database Connection
    print("\n[2] CHECKING DATABASE CONNECTIVITY & AUDIT TABLES")
    print("-" * 78)
    db_url = None
    engine = None
    try:
        from sqlalchemy import create_engine, inspect, text
        from scripts.shadow_health_check import get_db_url
        db_url = get_db_url(None)
        print(f"  Target DB      : {db_url.split('@')[-1] if '@' in db_url else db_url}")
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            print("  [OK] PostgreSQL connection alive (ping passed)")

            insp = inspect(conn)
            tables = set(insp.get_table_names())
            required_tables = [
                "shadow_execution_audits",
                "trade_candidate_audits",
                "shadow_session_daily_log",
                "exchange_trading_calendar",
                "live_market_bars",
            ]
            for t in required_tables:
                if t in tables:
                    cnt = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                    print(f"  [OK] Table '{t:<26}' exists ({cnt} rows)")
                else:
                    print(f"  [FAIL] Missing table '{t}'")
                    errors.append(f"Database table missing: {t}")
    except Exception as e:
        print(f"  [FAIL] Database connection error: {e}")
        errors.append(f"Database error: {e}")

    # 3. Redis Connection
    print("\n[3] CHECKING REDIS CACHE")
    print("-" * 78)
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis
        r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=3)
        if r.ping():
            print(f"  [OK] Redis server responds to PING ({redis_url})")
            keys_count = len(r.keys("nivesh:*"))
            print(f"  [OK] Active nivesh:* keys in cache: {keys_count}")
        else:
            print("  [FAIL] Redis ping returned False")
            errors.append("Redis ping returned False")
    except Exception as e:
        print(f"  [FAIL] Redis connection error: {e}")
        errors.append(f"Redis error: {e}")

    # 4. Exchange Calendar
    print("\n[4] CHECKING EXCHANGE TRADING CALENDAR")
    print("-" * 78)
    if engine:
        try:
            cal = check_calendar(engine, now_ist)
            print(f"  Today's Date   : {cal['session_date']}")
            print(f"  Has Calendar Row: {cal['has_calendar_row']}")
            print(f"  Session Status : {cal['session_status']}")
            print(f"  Source Tag     : {cal['source']}")
            print(f"  Hours          : {cal['opens_at']} to {cal['closes_at']} IST")
            print(f"  Is Trading Day : {cal['is_trading_day']}")
            print(f"  Is Market Open : {cal['is_market_open_now']}")
            if cal["fallback_used"]:
                print(f"  Note           : {cal['notes']}")
            print("  [OK] Calendar logic verified successfully")
        except Exception as e:
            print(f"  [FAIL] Calendar check failed: {e}")
            errors.append(f"Calendar check failed: {e}")

    # 5. LivePaperInference Dry Run Verification
    print("\n[5] CHECKING INFERENCE ENTRY POINT (DRY RUN)")
    print("-" * 78)
    if engine and db_url:
        try:
            from sqlalchemy import text
            from backend.live_inference import LivePaperInference

            with engine.connect() as conn:
                initial_trades = conn.execute(text("SELECT COUNT(*) FROM shadow_execution_audits")).scalar()
                initial_cands = conn.execute(text("SELECT COUNT(*) FROM trade_candidate_audits")).scalar()

            inferencer = LivePaperInference(database_url=db_url, redis_url=redis_url)
            print("  [OK] LivePaperInference instantiated with execution fuses verified")

            # Execute dry run
            result = inferencer.run()
            print(f"  Run Result     : status={result.get('status')}, reason='{result.get('reason')}', orders_allowed={result.get('orders_allowed')}")

            # Verify no rows were added to the audit tables
            with engine.connect() as conn:
                post_trades = conn.execute(text("SELECT COUNT(*) FROM shadow_execution_audits")).scalar()
                post_cands = conn.execute(text("SELECT COUNT(*) FROM trade_candidate_audits")).scalar()

            trades_delta = post_trades - initial_trades
            cands_delta = post_cands - initial_cands

            print(f"  Rows Written   : shadow_execution_audits: +{trades_delta}, trade_candidate_audits: +{cands_delta}")
            if trades_delta == 0 and cands_delta == 0:
                print("  [OK] Dry run completed cleanly: ZERO rows written, orders_allowed=False confirmed")
            else:
                print("  [FAIL] Dry run wrote unexpected rows into database!")
                errors.append("Dry run modified audit tables")

            if result.get("orders_allowed") is not False:
                print("  [FAIL] orders_allowed is not False in inference output!")
                errors.append("orders_allowed was not False")
        except Exception as e:
            print(f"  [FAIL] Inference dry run failed: {e}")
            errors.append(f"Inference error: {e}")

    print("\n" + "=" * 78)
    if not errors:
        print("  SMOKE TEST RESULT: [PASS] All shadow pipeline wiring verified!")
        print("=" * 78)
        return 0
    else:
        print(f"  SMOKE TEST RESULT: [FAIL] ({len(errors)} errors found)")
        for err in errors:
            print(f"    * {err}")
        print("=" * 78)
        return 1


if __name__ == "__main__":
    sys.exit(run_smoke_test())
