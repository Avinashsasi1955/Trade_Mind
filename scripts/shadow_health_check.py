#!/usr/bin/env python3
"""
scripts/shadow_health_check.py

Read-only start-up and operational health check for the shadow trading pipeline.
Prints:
  1. Market Schedule & Exchange Calendar (exchange_trading_calendar)
  2. Required Processes (Stream service, Celery worker, Celery beat)
  3. Broker Token Validation (UPSTOX_ACCESS_TOKEN read-only profile check)
  4. Stream connection status & Broker DB state
  5. Last tick age (Redis feed)
  6. Last bar age (live_market_bars)
  7. Last candidate generated (trade_candidate_audits)
  8. Last shadow trade opened and closed (shadow_execution_audits)
  9. Today's session status (shadow_session_daily_log)
 10. Data-gap and feed warnings

Exit code:
  - 1 if anything essential fails during market hours (or with --strict).
  - 0 if healthy or outside market hours.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, inspect, text

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_OPEN = time(9, 15, 0)
DEFAULT_CLOSE = time(15, 30, 0)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shadow_health_check")


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


def check_calendar(
    engine,
    now_ist: datetime,
    mock_status: Optional[str] = None,
    mock_source: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Query exchange_trading_calendar to determine if today is a trading day
    and if market is open right now, including special/muhurat sessions.
    Falls back to Mon-Fri 09:15-15:30 IST only if table has no row.
    """
    today_date = now_ist.date()
    current_time = now_ist.time()

    if mock_status:
        is_trading = mock_status.upper() == "OPEN"
        opens_at = DEFAULT_OPEN
        closes_at = DEFAULT_CLOSE
        is_open_now = is_trading and (opens_at <= current_time <= closes_at)
        return {
            "has_calendar_row": True,
            "session_date": today_date,
            "session_status": mock_status.upper(),
            "source": mock_source or "MOCK_SESSION",
            "opens_at": opens_at,
            "closes_at": closes_at,
            "is_trading_day": is_trading,
            "is_market_open_now": is_open_now,
            "fallback_used": False,
            "notes": "Injected mock calendar override",
        }

    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "exchange_trading_calendar" in insp.get_table_names():
                row = conn.execute(
                    text("SELECT * FROM exchange_trading_calendar WHERE exchange = 'NSE' AND session_date = :dt"),
                    {"dt": today_date},
                ).mappings().fetchone()

                if row:
                    status = (row.get("session_status") or "").upper()
                    source = row.get("source") or "EXCHANGE_CALENDAR"
                    opens_at = row.get("opens_at") or DEFAULT_OPEN
                    closes_at = row.get("closes_at") or DEFAULT_CLOSE

                    is_trading = status == "OPEN"
                    is_open_now = is_trading and (opens_at <= current_time <= closes_at)

                    return {
                        "has_calendar_row": True,
                        "session_date": today_date,
                        "session_status": status,
                        "source": source,
                        "opens_at": opens_at,
                        "closes_at": closes_at,
                        "is_trading_day": is_trading,
                        "is_market_open_now": is_open_now,
                        "fallback_used": False,
                        "notes": f"NSE exchange calendar row confirmed: {source}",
                    }
    except Exception as e:
        logger.warning(f"Error checking exchange_trading_calendar: {e}")

    # Fallback to Mon-Fri 09:15 - 15:30 IST
    is_trading = now_ist.weekday() < 5
    opens_at = DEFAULT_OPEN
    closes_at = DEFAULT_CLOSE
    is_open_now = is_trading and (opens_at <= current_time <= closes_at)

    return {
        "has_calendar_row": False,
        "session_date": today_date,
        "session_status": "OPEN" if is_trading else "CLOSED",
        "source": "MON_FRI_FALLBACK",
        "opens_at": opens_at,
        "closes_at": closes_at,
        "is_trading_day": is_trading,
        "is_market_open_now": is_open_now,
        "fallback_used": True,
        "notes": f"Falling back to Mon-Fri regular hours because exchange_trading_calendar has no row for {today_date}",
    }


def check_required_processes() -> Dict[str, Dict[str, Any]]:
    """
    Check if required daemon processes are alive:
      1. Stream service (backend.live_stream_service)
      2. Celery worker (celery ... worker)
      3. Celery beat (celery ... beat)
    """
    targets = {
        "stream_service": [re.compile(r"backend\.live_stream_service"), re.compile(r"live_stream_service\.py")],
        "celery_worker": [re.compile(r"celery.*worker")],
        "celery_beat": [re.compile(r"celery.*beat")],
    }
    results = {k: {"alive": False, "pids": [], "evidence": []} for k in targets}

    try:
        out = subprocess.check_output(["ps", "-eo", "pid,args"]).decode("utf-8", errors="ignore")
        for line in out.splitlines()[1:]:
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                pid, cmd = parts
                if "ps -eo" in cmd or "grep" in cmd:
                    continue
                for k, patterns in targets.items():
                    if any(p.search(cmd) for p in patterns):
                        results[k]["alive"] = True
                        results[k]["pids"].append(pid)
                        results[k]["evidence"].append(f"PID {pid}: {cmd[:90]}")
    except Exception as e:
        logger.warning(f"Process check failed: {e}")

    return results


def check_upstox_token() -> Dict[str, Any]:
    """
    Validate UPSTOX_ACCESS_TOKEN:
      - Print only length (hide secret)
      - Make one read-only Upstox profile call
      - Report: valid, expired, or missing
    """
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        try:
            from backend.config import UPSTOX_ACCESS_TOKEN
            token = UPSTOX_ACCESS_TOKEN.strip()
        except Exception:
            pass

    if not token:
        return {
            "status": "MISSING",
            "length": 0,
            "valid": False,
            "message": "UPSTOX_ACCESS_TOKEN is not set",
        }

    token_len = len(token)
    req = urllib.request.Request(
        "https://api.upstox.com/v2/user/profile",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "TradeMind/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            user_data = data.get("data", {})
            user_name = user_data.get("user_name") or user_data.get("user_id") or "Active User"
            return {
                "status": "VALID",
                "length": token_len,
                "valid": True,
                "message": f"Token valid (authenticated as {user_name})",
            }
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return {
                "status": "EXPIRED",
                "length": token_len,
                "valid": False,
                "message": f"Token expired or unauthorized (HTTP {exc.code})",
            }
        return {
            "status": f"HTTP_ERROR_{exc.code}",
            "length": token_len,
            "valid": False,
            "message": f"Upstox API returned HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "UNREACHABLE",
            "length": token_len,
            "valid": False,
            "message": f"Connection error: {exc}",
        }


def check_stream_and_ticks(redis_url: str, now_ist: datetime) -> Tuple[bool, Optional[datetime], Optional[float], Dict[str, Any]]:
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
    if cli_db:
        url = cli_db
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url

    candidates = []
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        candidates.append(env_url)
    try:
        from backend.config import DATABASE_URL
        if DATABASE_URL and DATABASE_URL not in candidates:
            candidates.append(DATABASE_URL)
    except Exception:
        pass

    candidates.extend([
        "postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging",
        "postgresql://avinash@localhost:5432/nivesh_v3_staging",
        "postgresql://localhost:5432/nivesh_v3_staging",
    ])

    for cand in candidates:
        cand_norm = cand.replace("postgresql://", "postgresql+psycopg2://", 1) if cand.startswith("postgresql://") else cand
        try:
            test_eng = create_engine(cand_norm, pool_pre_ping=True)
            with test_eng.connect():
                return cand_norm
        except Exception:
            continue

    return "postgresql+psycopg2://avinash@localhost:5432/nivesh_v3_staging"


def run_health_check(
    db_url: str,
    redis_url: str,
    now_ist: Optional[datetime] = None,
    mock_calendar_status: Optional[str] = None,
    mock_calendar_source: Optional[str] = None,
    strict: bool = False,
) -> int:
    if now_ist is None:
        now_ist = datetime.now(IST)

    engine = create_engine(db_url, pool_pre_ping=True)

    # 1. Calendar Evaluation
    cal = check_calendar(
        engine=engine,
        now_ist=now_ist,
        mock_status=mock_calendar_status,
        mock_source=mock_calendar_source,
    )
    is_market_open = cal["is_market_open_now"]
    is_trading_day = cal["is_trading_day"]

    print("=" * 78)
    print("             SHADOW TRADING PIPELINE HEALTH CHECK")
    print("=" * 78)
    print(f"  Check Timestamp : {now_ist.isoformat()}")
    print(f"  Trading Day     : {'YES' if is_trading_day else 'NO'} ({cal['session_status']})")
    print(f"  Market Session  : {'[LIVE - MARKET IS OPEN]' if is_market_open else '[CLOSED / OFF-MARKET]'}")
    print(f"  Schedule Source : {cal['source']} (Hours: {cal['opens_at']} - {cal['closes_at']} IST)")
    if cal["fallback_used"]:
        print(f"  NOTE            : {cal['notes']}")
    print(f"  Database Target : {db_url.split('@')[-1] if '@' in db_url else db_url}")
    print("=" * 78)

    stale_items = []
    process_failures = []

    # 2. Required Processes Check
    print("\n[1] REQUIRED PROCESSES")
    print("-" * 78)
    procs = check_required_processes()
    for proc_name, label in [
        ("stream_service", "Live Stream Service (backend.live_stream_service)"),
        ("celery_worker", "Celery Worker       (celery worker)"),
        ("celery_beat", "Celery Beat         (celery beat)"),
    ]:
        pinfo = procs.get(proc_name, {})
        if pinfo.get("alive"):
            pids_str = ", ".join(pinfo.get("pids", []))
            print(f"  [RUNNING] {label:<45} (PID: {pids_str})")
        else:
            print(f"  [STOPPED] {label:<45} (No active process found)")
            process_failures.append(f"{label} is not running")

    if is_market_open and process_failures:
        for pf in process_failures:
            stale_items.append(pf)

    # 3. Upstox Token Check
    print("\n[2] BROKER TOKEN VALIDATION (UPSTOX_ACCESS_TOKEN)")
    print("-" * 78)
    token_info = check_upstox_token()
    token_len_msg = f"(length: {token_info['length']})" if token_info["length"] > 0 else "(length: 0)"
    status_tag = "[PASS]" if token_info["valid"] else "[FAIL]"
    print(f"  {status_tag} Token Status   : {token_info['status']} {token_len_msg}")
    print(f"         Verification   : {token_info['message']}")
    if is_market_open and not token_info["valid"]:
        stale_items.append(f"Broker token invalid: {token_info['status']} - {token_info['message']}")

    # 4. Stream & Redis status
    print("\n[3] STREAM & BROKER CONNECTION STATE")
    print("-" * 78)
    stream_connected, latest_tick_dt, tick_age_sec, stream_meta = check_stream_and_ticks(redis_url, now_ist)
    broker_details = ""
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "broker_connections" in insp.get_table_names():
                b_row = conn.execute(
                    text("SELECT broker, status, updated_at FROM broker_connections ORDER BY user_id LIMIT 1")
                ).mappings().fetchone()
                if b_row:
                    broker_details = f"Broker: {b_row.get('broker')}, Status: {b_row.get('status')}, Last Updated: {b_row.get('updated_at')}"
    except Exception as e:
        broker_details = f"DB broker query error: {e}"

    stream_ok = stream_connected and tick_age_sec is not None and tick_age_sec < 60
    if stream_ok:
        print(f"  Stream Feed     : CONNECTED (Active live tick stream)")
    elif stream_meta.get("redis_available"):
        print(f"  Stream Feed     : DISCONNECTED (Redis connected, but 0 live streaming ticks)")
    else:
        print(f"  Stream Feed     : DISCONNECTED (Redis unreachable)")

    if broker_details:
        print(f"  Broker DB State : {broker_details}")
    else:
        print(f"  Broker DB State : Disconnected / No active broker connection record")

    if not stream_connected or tick_age_sec is None or tick_age_sec > 60:
        if is_market_open:
            stale_items.append("Live market stream is down or receiving no ticks")

    # 5. Tick Freshness
    print("\n[4] TICK FRESHNESS")
    print("-" * 78)
    if latest_tick_dt and tick_age_sec is not None:
        print(f"  Last Tick Time  : {latest_tick_dt.isoformat()} ({format_age(tick_age_sec)})")
        print(f"  Active Symbols  : {stream_meta.get('total_tick_keys', 0)} in Redis cache")
        if is_market_open and tick_age_sec > 60:
            stale_items.append(f"Tick data is stale ({format_age(tick_age_sec)})")
    else:
        print(f"  Last Tick Time  : NONE RECORDED (Redis tick cache is empty)")
        if is_market_open:
            stale_items.append("No live ticks available")

    # 6. Bar Freshness
    print("\n[5] BAR FRESHNESS (live_market_bars)")
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

    if is_market_open and (bar_age_sec is None or bar_age_sec > 900):  # 15 minutes
        stale_items.append(f"Bar data is stale ({format_age(bar_age_sec)})")

    # 7. Candidate Generation
    print("\n[6] CANDIDATE GENERATION (trade_candidate_audits)")
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

    # 8. Shadow Trades
    print("\n[7] SHADOW TRADES (shadow_execution_audits)")
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

    # 9. Today's Session Status
    print("\n[8] TODAY'S SESSION STATUS (shadow_session_daily_log)")
    print("-" * 78)
    session_warnings = []
    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            if "shadow_session_daily_log" in insp.get_table_names():
                today_log = conn.execute(
                    text("SELECT * FROM shadow_session_daily_log WHERE session_date = :today"),
                    {"today": cal["session_date"]},
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
                    latest_log = conn.execute(
                        text("SELECT * FROM shadow_session_daily_log ORDER BY session_date DESC LIMIT 1")
                    ).mappings().fetchone()
                    if latest_log:
                        print(f"  Today ({cal['session_date']}) : NO SESSION LOG ROW YET")
                        print(f"  Latest Log Date : {latest_log.get('session_date')} | Status: {latest_log.get('status')} | Valid: {latest_log.get('valid')}")
                        session_warnings = latest_log.get("warnings") or []
                    else:
                        print("  No session log entries found in table.")
    except Exception as e:
        print(f"  Error querying shadow_session_daily_log: {e}")

    # 10. Data-Gap Warnings
    print("\n[9] DATA-GAP & FEED WARNINGS")
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
    if is_market_open:
        if stale_items:
            print("  HEALTH CHECK VERDICT: [FAILED - ESSENTIAL SERVICE STALE DURING MARKET HOURS]")
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
        if stale_items or process_failures:
            print("  Off-market diagnostic notes (services pending start before next session):")
            for item in set(stale_items + process_failures):
                print(f"    - {item}")
        print("=" * 78)
        if strict and (stale_items or process_failures):
            return 1
        return 0


def main():
    parser = argparse.ArgumentParser(description="Read-only shadow trading start-up and operational health check.")
    parser.add_argument("--db", default=None, help="Optional database URL override")
    parser.add_argument("--redis", default=None, help="Optional Redis URL override")
    parser.add_argument("--mock-time", default=None, help="Injected mock time in ISO format (e.g. 2026-10-12T10:00:00)")
    parser.add_argument("--mock-calendar-status", choices=["OPEN", "CLOSED"], default=None, help="Injected calendar session_status override")
    parser.add_argument("--mock-calendar-source", default=None, help="Injected calendar source override (e.g. SPECIAL_MUHURAT)")
    parser.add_argument("--strict", action="store_true", help="Exit code 1 on missing items even outside market hours")
    args = parser.parse_args()

    now_ist = None
    if args.mock_time:
        try:
            dt = datetime.fromisoformat(args.mock_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            now_ist = dt
        except ValueError:
            logger.error(f"Invalid mock time format: {args.mock_time}. Expected ISO format.")
            sys.exit(1)

    db_url = get_db_url(args.db)
    redis_url = args.redis or os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    exit_code = run_health_check(
        db_url=db_url,
        redis_url=redis_url,
        now_ist=now_ist,
        mock_calendar_status=args.mock_calendar_status,
        mock_calendar_source=args.mock_calendar_source,
        strict=args.strict,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
