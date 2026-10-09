"""Sync NSE Trading Calendar into exchange_trading_calendar table.

Loads committed NSE holiday schedule (e.g. data/nse_holidays_2026.json)
and populates the full session schedule (trading days, weekends, holidays, special sessions).
Fails loudly if today has no row in production or when run with --verify-today.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sync_trading_calendar")

IST = ZoneInfo("Asia/Kolkata")
ROOT_DIR = Path(__file__).resolve().parent.parent


def get_calendar_file(year: int = 2026) -> Path:
    return ROOT_DIR / "data" / f"nse_holidays_{year}.json"


def ensure_calendar_table(engine: Any) -> None:
    """Ensure exchange_trading_calendar table exists (works on SQLite and PostgreSQL)."""
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS exchange_trading_calendar(
                    exchange TEXT NOT NULL CHECK(exchange IN ('NSE','BSE')),
                    session_date DATE NOT NULL,
                    session_status TEXT NOT NULL CHECK(session_status IN ('OPEN','CLOSED','SPECIAL')),
                    opens_at TIME,
                    closes_at TIME,
                    source TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(exchange,session_date),
                    CHECK(session_status='CLOSED' OR (opens_at IS NOT NULL AND closes_at IS NOT NULL AND opens_at<closes_at))
                );
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS exchange_trading_calendar(
                    exchange TEXT NOT NULL,
                    session_date DATE NOT NULL,
                    session_status TEXT NOT NULL,
                    opens_at TIME,
                    closes_at TIME,
                    source TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(exchange,session_date)
                );
            """))


def sync_calendar(
    engine: Any,
    calendar_file: Optional[Path] = None,
    year: int = 2026,
    exchange: str = "NSE"
) -> int:
    """Populate or update exchange_trading_calendar with all 365/366 dates of the given year."""
    ensure_calendar_table(engine)

    if calendar_file is None:
        calendar_file = get_calendar_file(year)

    if not calendar_file.exists():
        raise FileNotFoundError(f"Trading calendar file not found: {calendar_file}")

    with open(calendar_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    source_desc = data.get("source") or calendar_file.name
    holidays_map: Dict[str, Dict[str, Any]] = {
        h["date"]: h for h in data.get("holidays", [])
    }

    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    cur_date = start_date

    records = []
    while cur_date <= end_date:
        d_str = cur_date.isoformat()
        is_weekend = cur_date.weekday() >= 5  # Saturday = 5, Sunday = 6

        if is_weekend:
            # Check if there is a special weekend session (e.g. Muhurat trading)
            if d_str in holidays_map and holidays_map[d_str].get("status") == "SPECIAL":
                h_info = holidays_map[d_str]
                records.append({
                    "exchange": exchange,
                    "session_date": cur_date,
                    "session_status": "SPECIAL",
                    "opens_at": h_info.get("opens_at", "18:15:00"),
                    "closes_at": h_info.get("closes_at", "19:15:00"),
                    "source": f"{source_desc} ({h_info.get('name', 'Special')})",
                })
            else:
                records.append({
                    "exchange": exchange,
                    "session_date": cur_date,
                    "session_status": "CLOSED",
                    "opens_at": None,
                    "closes_at": None,
                    "source": source_desc,
                })
        elif d_str in holidays_map:
            h_info = holidays_map[d_str]
            h_status = h_info.get("status", "CLOSED").upper()
            records.append({
                "exchange": exchange,
                "session_date": cur_date,
                "session_status": h_status,
                "opens_at": h_info.get("opens_at") if h_status != "CLOSED" else None,
                "closes_at": h_info.get("closes_at") if h_status != "CLOSED" else None,
                "source": f"{source_desc} ({h_info.get('name', 'Holiday')})",
            })
        else:
            # Standard trading weekday
            records.append({
                "exchange": exchange,
                "session_date": cur_date,
                "session_status": "OPEN",
                "opens_at": "09:15:00",
                "closes_at": "15:30:00",
                "source": source_desc,
            })

        cur_date += timedelta(days=1)

    upsert_sql = text("""
        INSERT INTO exchange_trading_calendar (
            exchange, session_date, session_status, opens_at, closes_at, source, updated_at
        ) VALUES (
            :exchange, :session_date, :session_status, :opens_at, :closes_at, :source, CURRENT_TIMESTAMP
        )
        ON CONFLICT (exchange, session_date) DO UPDATE SET
            session_status = EXCLUDED.session_status,
            opens_at = EXCLUDED.opens_at,
            closes_at = EXCLUDED.closes_at,
            source = EXCLUDED.source,
            updated_at = CURRENT_TIMESTAMP
    """)

    with engine.begin() as conn:
        for rec in records:
            conn.execute(upsert_sql, rec)

    logger.info(f"Successfully synced {len(records)} session dates for {exchange} {year} from {calendar_file.name}")
    return len(records)


def verify_today_calendar(engine: Any, exchange: str = "NSE", fail_loudly: bool = True) -> bool:
    """Verify that today's date has a valid exchange_trading_calendar row.
    
    In production, fails loudly if missing.
    """
    ensure_calendar_table(engine)
    today_ist = datetime.now(IST).date()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT session_status, opens_at, closes_at, source FROM exchange_trading_calendar WHERE exchange = :ex AND session_date = :day"),
            {"ex": exchange, "day": today_ist}
        ).mappings().one_or_none()

    if row is None:
        msg = (
            f"CRITICAL: No exchange_trading_calendar row found for {exchange} on today ({today_ist})! "
            f"Run scripts/sync_trading_calendar.py immediately to populate session calendar."
        )
        logger.critical(msg)
        if fail_loudly:
            raise RuntimeError(msg)
        return False

    logger.info(f"Verified {exchange} session for today ({today_ist}): {row['session_status']} ({row['source']})")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync NSE trading calendar from committed schedule.")
    parser.add_argument("--year", type=int, default=2026, help="Calendar year to sync (default: 2026)")
    parser.add_argument("--db-url", type=str, default=None, help="Database connection URL (defaults to env DATABASE_URL)")
    parser.add_argument("--verify-today", action="store_true", help="Fail loudly if today's date has no row")
    parser.add_argument("--file", type=str, default=None, help="Custom calendar JSON file")
    args = parser.parse_args()

    db_url = args.db_url or os.getenv("DATABASE_URL")
    if not db_url:
        sqlite_path = ROOT_DIR / "data" / "nivesh.db"
        db_url = f"sqlite:///{sqlite_path}"
        logger.info(f"No DATABASE_URL set; using local SQLite: {db_url}")

    engine = create_engine(db_url, future=True)
    custom_file = Path(args.file) if args.file else None

    # Sync calendar
    synced = sync_calendar(engine, calendar_file=custom_file, year=args.year)
    logger.info(f"Calendar sync complete: {synced} rows.")

    # Check today
    is_prod = os.getenv("APP_ENV") == "production" or os.getenv("IS_PRODUCTION", "0") in ("1", "true", "True")
    if args.verify_today or is_prod:
        verify_today_calendar(engine, fail_loudly=True)


if __name__ == "__main__":
    main()
