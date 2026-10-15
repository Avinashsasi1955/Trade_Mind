import argparse
import os
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, text

from backend.upstox_backfill import (
    UpstoxRestBackfill,
    aggregate_5minute,
    REST_INTRADAY_SOURCE,
    REST_5M_SOURCE,
)

IST = ZoneInfo("Asia/Kolkata")

APPROVED_DEV_HOSTS: Set[str] = {"localhost", "127.0.0.1", "postgres"}
FORBIDDEN_DB_NAMES: Set[str] = {"postgres", "nivesh_production", "nivesh_prod"}
PRODUCTION_HOST_PATTERNS = ("amazonaws.com", "rds.", "prod", "live")


def parse_target_date(date_str: Optional[str], today_ist: Optional[date] = None) -> date:
    """Parse and strictly validate target trading date in YYYY-MM-DD format."""
    if not date_str or not isinstance(date_str, str) or not date_str.strip():
        raise ValueError("Missing required target date. Provide --date YYYY-MM-DD.")
    clean = date_str.strip()
    try:
        parsed = datetime.strptime(clean, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date format '{clean}'. Must be strictly YYYY-MM-DD.")

    now_date = today_ist or datetime.now(IST).date()
    if parsed > now_date:
        raise ValueError(f"Target date {parsed.isoformat()} cannot be in the future (current IST date is {now_date.isoformat()}).")

    return parsed


def validate_refill_database_target(database_url: Optional[str]) -> Dict[str, str]:
    """Strictly validate database connection target for safe non-production chart refills.

    Fail-closed policy:
    1. Rejects if empty or unconfigured.
    2. Rejects if environment indicates production (NIVESH_ENV=production, APP_ENV=production).
    3. Rejects unknown or remote hosts unless explicitly in APPROVED_DEV_HOSTS or NIVESH_APPROVED_DEV_DB_HOSTS.
    4. Rejects cloud/RDS endpoints and forbidden administrative/production DB names.
    """
    if not database_url or not isinstance(database_url, str) or not database_url.strip():
        raise RuntimeError("DATABASE_URL is not set or empty. Cannot operate without an explicit database target.")

    # 1. Environment check
    for env_var in ("NIVESH_ENV", "APP_ENV", "ENVIRONMENT"):
        if os.getenv(env_var, "").strip().lower() == "production":
            raise RuntimeError(f"SAFETY GATE TRIGGERED: Refusing to run chart refill when {env_var}=production.")

    clean_dsn = database_url.strip()

    # Handle sqlite
    if clean_dsn.startswith("sqlite"):
        return {
            "scheme": "sqlite",
            "host": "local_file",
            "dbname": clean_dsn.split("///")[-1],
            "classification": "local_sqlite_dev",
        }

    # PostgreSQL validation
    clean = clean_dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
    if not clean.startswith("postgresql://"):
        raise RuntimeError("FAIL-CLOSED: Disallowed database scheme. Only PostgreSQL or SQLite permitted.")

    try:
        parsed = urllib.parse.urlsplit(clean)
    except Exception as exc:
        raise RuntimeError(f"FAIL-CLOSED: Malformed database connection URL: {exc}")

    try:
        host = (parsed.hostname or "").lower()
    except Exception:
        raise RuntimeError("FAIL-CLOSED: Malformed host in database connection URL.")

    if not host:
        raise RuntimeError("FAIL-CLOSED: Database URL is missing a valid hostname.")

    # Cloud/RDS pattern rejection
    for pattern in PRODUCTION_HOST_PATTERNS:
        if pattern in host:
            raise RuntimeError(f"SAFETY GATE TRIGGERED: Host '{host}' matches production/cloud pattern '{pattern}'. Refusing execution.")

    # Explicit allowlist check
    custom_allowed = {h.strip().lower() for h in os.getenv("NIVESH_APPROVED_DEV_DB_HOSTS", "").split(",") if h.strip()}
    allowed_hosts = APPROVED_DEV_HOSTS | custom_allowed

    if host not in allowed_hosts:
        raise RuntimeError(
            f"FAIL-CLOSED: Host '{host}' is not in approved non-production development hosts ({sorted(allowed_hosts)}). "
            f"Refusing to execute against unverified database target."
        )

    dbname = parsed.path.lstrip("/").split("?")[0].strip()
    if not dbname:
        raise RuntimeError("FAIL-CLOSED: Database URL is missing database name.")

    if dbname in FORBIDDEN_DB_NAMES or dbname.endswith("_prod") or dbname.endswith("_production"):
        raise RuntimeError(f"SAFETY GATE TRIGGERED: Database name '{dbname}' is forbidden. Refusing execution.")

    return {
        "scheme": "postgresql",
        "host": host,
        "dbname": dbname,
        "classification": "approved_local_dev_postgres",
    }


def refill_missing_charts(
    database_url: str,
    target_date: date,
    confirm_write: bool = False,
    sleep_seconds: float = 0.15,
    backfill_client: Optional[Any] = None,
    engine: Optional[Any] = None,
) -> Dict[str, Any]:
    """Identify and optionally refill missing 1m and 5m bars for a given date.

    - Default behavior is DRY-RUN (read-only audit).
    - Preserves existing live bars by strictly using `replace=False` (`DO NOTHING` on conflict).
    - Returns audit report dictionary.
    """
    db_info = validate_refill_database_target(database_url)
    db_engine = engine or create_engine(database_url, pool_pre_ping=True, future=True)
    bf = backfill_client or UpstoxRestBackfill(database_url, sleep_seconds=sleep_seconds)

    universe = bf.select_instruments(limit=0, include_indices=True, fno_only=True)

    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT b.instrument_id, count(*) as cnt
            FROM live_market_bars b
            WHERE b.interval = '1minute' AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date = :d
            GROUP BY b.instrument_id
        """), {"d": target_date}).fetchall()
        counts = {r[0]: r[1] for r in rows}

    needed = [i for i in universe if counts.get(i.instrument_id, 0) < 370]

    report: Dict[str, Any] = {
        "target_date": target_date.isoformat(),
        "database_target": db_info,
        "total_universe_instruments": len(universe),
        "instruments_needing_repair": len(needed),
        "needed_symbols": [i.symbol for i in needed],
        "dry_run": not confirm_write,
        "writes_executed": confirm_write,
        "successful_refills": 0,
        "failures": [],
        "total_1m_bars_inserted": 0,
        "total_5m_bars_inserted": 0,
    }

    if not confirm_write:
        return report

    # Live write execution requested
    fetch_to = target_date + timedelta(days=2)
    success = 0
    failures = []
    total_1m = 0
    total_5m = 0

    for idx, inst in enumerate(needed, 1):
        try:
            raw = bf.fetch_history(inst.provider_key, "1minute", target_date, fetch_to)
            one_min = [c for c in raw if c["bar_time"].astimezone(IST).date() == target_date]
            if not one_min:
                continue

            # CRITICAL: replace=False ensures DO NOTHING on existing bars, strictly preserving live provenance
            ins_1m = bf.save_bars(inst.instrument_id, "1minute", one_min, REST_INTRADAY_SOURCE, replace=False)
            five_min = aggregate_5minute(one_min)
            ins_5m = bf.save_bars(inst.instrument_id, "5minute", five_min, REST_5M_SOURCE, replace=False)

            total_1m += ins_1m
            total_5m += ins_5m
            success += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except Exception as exc:
            failures.append({"symbol": inst.symbol, "error": str(exc)[:200]})

    report["successful_refills"] = success
    report["failures"] = failures
    report["total_1m_bars_inserted"] = total_1m
    report["total_5m_bars_inserted"] = total_5m
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely identify and refill missing 1m and 5m market bars for historical charts."
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Target trading date in YYYY-MM-DD format (required, non-future)",
    )
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        default=False,
        help="Explicit confirmation flag required to execute database writes. Defaults to read-only dry-run.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.15,
        help="Rate-limit pause between Upstox API requests in seconds (default: 0.15)",
    )

    args = parser.parse_args(argv)

    try:
        target_date = parse_target_date(args.date)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    database_url = os.environ.get("DATABASE_URL")
    try:
        db_info = validate_refill_database_target(database_url)
    except RuntimeError as exc:
        print(f"DATABASE SECURITY ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"--- Fast Chart Refill Audit ---")
    print(f"  Target Date       : {target_date.isoformat()}")
    print(f"  Database Host     : {db_info['host']}")
    print(f"  Database Name     : {db_info['dbname']}")
    print(f"  Classification    : {db_info['classification']}")
    print(f"  Mode              : {'LIVE WRITE (confirm-write enabled)' if args.confirm_write else 'READ-ONLY DRY-RUN'}")

    report = refill_missing_charts(
        database_url=database_url,
        target_date=target_date,
        confirm_write=args.confirm_write,
        sleep_seconds=args.sleep,
    )

    print(f"  Total Universe    : {report['total_universe_instruments']}")
    print(f"  Need Repair (<370): {report['instruments_needing_repair']}")

    if not args.confirm_write:
        print("\n[DRY-RUN] No network requests to Upstox or database writes were executed.")
        print("To execute missing bar refill, re-run with explicit --confirm-write.")
        return 0

    print(f"\n--- Refill Execution Finished ---")
    print(f"  Successful        : {report['successful_refills']} / {report['instruments_needing_repair']}")
    print(f"  Total 1m Inserted : {report['total_1m_bars_inserted']}")
    print(f"  Total 5m Inserted : {report['total_5m_bars_inserted']}")
    print(f"  Failures          : {len(report['failures'])}")

    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
