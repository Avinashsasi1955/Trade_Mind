#!/usr/bin/env python3
"""
scripts/shadow_health_check.py

Read-only start-up and operational health check for the shadow trading pipeline.
Prints:
  1. Stream connection status (Broker + Redis feed)
  2. Last tick age
  3. Last bar age
  4. Last candidate generated
  5. Last shadow trade opened and closed
  6. Today's session status
  7. Data-gap and feed warnings

Exit code:
  - 1 if anything essential is stale during Indian market hours (Mon-Fri 09:15 - 15:30 IST).
  - 0 if healthy or outside market hours (unless --strict is specified).
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, inspect, text

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15, 0)
MARKET_CLOSE = time(15, 30, 0)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_health_check")


def is_market_hours(now_ist: datetime) -> bool:
    """Return True if now_ist is within regular NSE market hours (Mon-Fri 09:15-15:30 IST)."""
    # Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    if now_ist.weekday() >= 5:
        return False
    current_time = now_ist.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "N/A"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds:.1f}s ago"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m ago ({int(seconds)}s)"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f}h ago"
    days = hours / 24.0
    return f"{days:.1f} days ago ({int(hours)}h)"


def check_stream_and_ticks(redis_url: str, now_ist: datetime) -> Tuple[bool, Optional[datetime], Optional[float], Dict[str, Any]]:
    """
    Check Redis tick cache.
    Returns: (is_connected, latest_tick_dt, tick_age_seconds, metadata)
    """
    metadata: Dict[str, Any] = {"redis_available": False, "total_tick_keys": 0}
    try:
        import redis
        r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=3)
        if not r.ping():
            return False, None, None, metadata
        metadata["redis_available"] = True
    except Exception as e:
        metadata["redis_error"] = str(e)
        return False, None, None, metadata

    try:
        ts_hash = r.hgetall("nivesh:ticks:timestamp")
        metadata["total_tick_keys"] = len(ts_hash)
        if not ts_hash:
            return False, None, None, metadata

        latest_dt = None
        for inst_id, ts_raw in ts_hash.items():
            try:
                dt = datetime.fromisoformat(ts_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_ist = dt.astimezone(IST)
                if latest_dt is None or dt_ist > latest_dt:
                    latest_dt = dt_ist
            except Exception:
                continue

        if latest_dt:
            age_sec = (now_ist - latest_dt).total_seconds()
            return True, latest_dt, age_sec, metadata
        return True, None, None, metadata
    except Exception as e:
        metadata["hash_error"] = str(e)
        return True, None, None, metadata


def get_db_url(cli_db: Optional[str]) -> str:
    db_url = cli_db or os.environ.get("DATABASE_URL")
    if not db_url:
        candidates = [
            "postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging",
            "postgresql://avinash@localhost:5432/nivesh_v3_staging",
            "postgresql://localhost:5432/nivesh_v3_staging",
        ]
        try:
            from backend.config import DATABASE_URL
            if DATABASE_URL:
                candidates.insert(0, DATABASE_URL)
        except Exception:
            pass

        for cand in candidates:
            cand_norm = cand.replace("postgresql://", "postgresql+psycopg2://", 1) if cand.startswith("postgresql://") else cand
            try:
                test_eng = create_engine(cand_norm, pool_pre_ping=True)
                with test_eng.connect():
                    db_url = cand_norm
                    break
            except Exception:
                continue

        if not db_url:
            db_url = "postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging"

    if db_url and db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return db_url


def run_health_check(db_url: str, redis_url: str, strict: bool = False) -> int:
    now_ist = datetime.now(IST)
    in_market = is_market_hours(now_ist)
    today_date = now_ist.date()

    print("=" * 78)
    print("             SHADOW TRADING PIPELINE HEALTH CHECK")
    print("=" * 78)
    print(f"  Check Timestamp : {now_ist.isoformat()}")
    print(f"  Market Status   : {'[IN MARKET HOURS]' if in_market else '[CLOSED / OUTSIDE MARKET HOURS]'}")
    print(f"  Market Schedule : Mon-Fri 09:15 - 15:30 IST (Today: {now_ist.strftime('%A')})")
    print(f"  Database Target : {db_url.split('@')[-1] if '@' in db_url else db_url}")
    print("=" * 78)

    stale_items = []

    # 1. Broker & Stream status
    stream_connected, latest_tick_dt, tick_age_sec, stream_meta = check_stream_and_ticks(redis_url, now_ist)

    engine = create_engine(db_url, pool_pre_ping=True)
    broker_status = "UNKNOWN"
    broker_details = ""
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            tables = insp.get_table_names()
            if "broker_connections" in tables:
                b_row = conn.execute(
                    text("SELECT broker, status, updated_at FROM broker_connections ORDER BY user_id LIMIT 1")
                ).mappings().fetchone()
                if b_row:
                    broker_status = b_row.get("status") or "UNKNOWN"
                    broker_details = f"Broker: {b_row.get('broker')}, Status: {broker_status}, Last Updated: {b_row.get('updated_at')}"
    except Exception as e:
        broker_details = f"DB broker query error: {e}"

    print("\n[1] STREAM & BROKER STATUS")
    print("-" * 78)
    stream_ok = stream_connected and tick_age_sec is not None and tick_age_sec < 60
    if stream_ok:
        print(f"  Stream Status   : CONNECTED (Active live feed)")
    elif stream_meta.get("redis_available"):
        print(f"  Stream Status   : DISCONNECTED (Redis up, but 0 live streaming ticks)")
    else:
        print(f"  Stream Status   : DISCONNECTED (Redis unreachable)")
    if broker_details:
        print(f"  Broker State    : {broker_details}")
    else:
        print(f"  Broker State    : Disconnected / No active broker connection record")

    if not stream_connected or tick_age_sec is None or tick_age_sec > 60:
        stale_items.append("Live market stream is down or receiving no ticks")

    # 2. Tick Age
    print("\n[2] TICK FRESHNESS")
    print("-" * 78)
    if latest_tick_dt and tick_age_sec is not None:
        print(f"  Last Tick Time  : {latest_tick_dt.isoformat()} ({format_age(tick_age_sec)})")
        print(f"  Active Symbols  : {stream_meta.get('total_tick_keys', 0)} in Redis cache")
        if tick_age_sec > 60:
            stale_items.append(f"Tick data is stale ({format_age(tick_age_sec)})")
    else:
        print(f"  Last Tick Time  : NONE RECORDED (Redis tick cache is empty)")
        stale_items.append("No live ticks available")

    # 3. Bar Age
    print("\n[3] BAR FRESHNESS (live_market_bars)")
    print("-" * 78)
    latest_bar_dt = None
    bar_age_sec = None
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "live_market_bars" in insp.get_table_names():
                b_rows = conn.execute(
                    text("SELECT interval, MAX(bar_time) as max_time, COUNT(*) as cnt FROM live_market_bars GROUP BY interval ORDER BY max_time DESC NULLS LAST")
                ).mappings().all()
                if b_rows:
                    for r in b_rows:
                        b_time = r.get("max_time")
                        age = None
                        if b_time:
                            if b_time.tzinfo is None:
                                b_time = b_time.replace(tzinfo=IST)
                            age = (now_ist - b_time).total_seconds()
                            if latest_bar_dt is None or b_time > latest_bar_dt:
                                latest_bar_dt = b_time
                                bar_age_sec = age
                        print(f"  Interval: {r.get('interval'):<10} | Max Bar: {str(b_time):<30} | Age: {format_age(age):<18} | Rows: {r.get('cnt')}")
                else:
                    print("  No bars found in live_market_bars table.")
            else:
                print("  Table live_market_bars does not exist.")
    except Exception as e:
        print(f"  Error querying live_market_bars: {e}")

    if bar_age_sec is None or bar_age_sec > 900:  # 15 minutes
        stale_items.append(f"Bar data is stale ({format_age(bar_age_sec)})")

    # 4. Candidate Generation
    print("\n[4] CANDIDATE GENERATION (trade_candidate_audits)")
    print("-" * 78)
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "trade_candidate_audits" in insp.get_table_names():
                c_row = conn.execute(
                    text("SELECT COUNT(*) as cnt, MAX(observed_at) as last_cand FROM trade_candidate_audits")
                ).mappings().fetchone()
                c_cnt = c_row.get("cnt") if c_row else 0
                c_last = c_row.get("last_cand") if c_row else None
                if c_cnt > 0 and c_last:
                    if c_last.tzinfo is None:
                        c_last = c_last.replace(tzinfo=IST)
                    c_age = (now_ist - c_last).total_seconds()
                    print(f"  Total Candidates: {c_cnt}")
                    print(f"  Last Candidate  : {c_last.isoformat()} ({format_age(c_age)})")
                else:
                    print("  Total Candidates: 0 (No candidates generated)")
            else:
                print("  Table trade_candidate_audits does not exist.")
    except Exception as e:
        print(f"  Error querying trade_candidate_audits: {e}")

    # 5. Shadow Trades
    print("\n[5] SHADOW TRADES (shadow_execution_audits)")
    print("-" * 78)
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "shadow_execution_audits" in insp.get_table_names():
                t_row = conn.execute(
                    text("""SELECT COUNT(*) as total_cnt,
                                   COUNT(*) FILTER (WHERE exit_at IS NULL) as open_cnt,
                                   MAX(signal_at) as last_open,
                                   MAX(exit_at) as last_close
                            FROM shadow_execution_audits""")
                ).mappings().fetchone()
                total_cnt = t_row.get("total_cnt") if t_row else 0
                open_cnt = t_row.get("open_cnt") if t_row else 0
                last_open = t_row.get("last_open") if t_row else None
                last_close = t_row.get("last_close") if t_row else None

                print(f"  Total Trades    : {total_cnt} (Open: {open_cnt}, Closed: {total_cnt - open_cnt})")
                if last_open:
                    if last_open.tzinfo is None:
                        last_open = last_open.replace(tzinfo=IST)
                    print(f"  Last Trade Open : {last_open.isoformat()} ({format_age((now_ist - last_open).total_seconds())})")
                else:
                    print(f"  Last Trade Open : NONE")

                if last_close:
                    if last_close.tzinfo is None:
                        last_close = last_close.replace(tzinfo=IST)
                    print(f"  Last Trade Close: {last_close.isoformat()} ({format_age((now_ist - last_close).total_seconds())})")
                else:
                    print(f"  Last Trade Close: NONE")
            else:
                print("  Table shadow_execution_audits does not exist.")
    except Exception as e:
        print(f"  Error querying shadow_execution_audits: {e}")

    # 6. Today's Session Status
    print("\n[6] TODAY'S SESSION STATUS (shadow_session_daily_log)")
    print("-" * 78)
    session_warnings = []
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "shadow_session_daily_log" in insp.get_table_names():
                today_log = conn.execute(
                    text("SELECT * FROM shadow_session_daily_log WHERE session_date = :today"),
                    {"today": today_date}
                ).mappings().fetchone()

                if today_log:
                    print(f"  Date            : {today_log.get('session_date')}")
                    print(f"  Session Status  : {today_log.get('status')}")
                    print(f"  Valid Session   : {today_log.get('valid')}")
                    print(f"  Trades Taken    : {today_log.get('trades_taken')}")
                    rejections = today_log.get("rejection_reasons") or []
                    if rejections:
                        print(f"  Rejections      : {rejections}")
                    session_warnings = today_log.get("warnings") or []
                else:
                    # Look up most recent session log
                    latest_log = conn.execute(
                        text("SELECT * FROM shadow_session_daily_log ORDER BY session_date DESC LIMIT 1")
                    ).mappings().fetchone()
                    if latest_log:
                        print(f"  Today ({today_date}) : NO SESSION LOG ROW YET")
                        print(f"  Latest Log Date : {latest_log.get('session_date')} | Status: {latest_log.get('status')} | Valid: {latest_log.get('valid')}")
                        session_warnings = latest_log.get("warnings") or []
                    else:
                        print("  No session log entries found in table.")
                        if in_market:
                            stale_items.append("No session log found for active trading day")
    except Exception as e:
        print(f"  Error querying shadow_session_daily_log: {e}")

    # 7. Data-Gap Warnings
    print("\n[7] DATA-GAP & FEED WARNINGS")
    print("-" * 78)
    gap_count = 0
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "market_data_gaps" in insp.get_table_names():
                gap_row = conn.execute(
                    text("SELECT COUNT(*) as total_gaps FROM market_data_gaps")
                ).mappings().fetchone()
                gap_count = gap_row.get("total_gaps") if gap_row else 0
                print(f"  Recorded Market Gaps: {gap_count}")
    except Exception as e:
        print(f"  Error querying market_data_gaps: {e}")

    if session_warnings:
        print(f"  Session Warnings ({len(session_warnings)}):")
        for w in session_warnings:
            print(f"    - {w}")
    else:
        print("  Session Warnings    : None")

    print("\n" + "=" * 78)
    if in_market:
        if stale_items:
            print("  HEALTH CHECK VERDICT: [FAILED - STALE DATA DURING MARKET HOURS]")
            for item in stale_items:
                print(f"    * {item}")
            print("=" * 78)
            return 1
        else:
            print("  HEALTH CHECK VERDICT: [PASS - ALL ESSENTIAL SERVICES HEALTHY]")
            print("=" * 78)
            return 0
    else:
        print("  HEALTH CHECK VERDICT: [OFF-MARKET - NO ACTIONS REQUIRED]")
        if stale_items:
            print("  Note: The following items would trigger alerts during live market hours:")
            for item in stale_items:
                print(f"    - {item}")
        print("=" * 78)
        if strict and stale_items:
            return 1
        return 0


def main():
    parser = argparse.ArgumentParser(description="Read-only shadow trading start-up and operational health check.")
    parser.add_argument("--db", default=None, help="Optional database URL override")
    parser.add_argument("--redis", default=None, help="Optional Redis URL override")
    parser.add_argument("--strict", action="store_true", help="Exit code 1 on stale items even outside market hours")
    args = parser.parse_args()

    db_url = get_db_url(args.db)
    redis_url = args.redis or os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    exit_code = run_health_check(db_url=db_url, redis_url=redis_url, strict=args.strict)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
