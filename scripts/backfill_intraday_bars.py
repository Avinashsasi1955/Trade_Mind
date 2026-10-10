#!/usr/bin/env python3
"""Backfill Upstox v3 historical 1-minute candles for NIFTY 50, BANKNIFTY, and top 20 F&O stocks.

Features:
- Underlyings: NIFTY 50, BANKNIFTY
- Top 20 F&O stocks on NSE
- Reads Upstox access token from environment (UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN)
- Backfills last 60 trading days (chunked into 30-day windows)
- Rate-limited (configurable sleep, default 0.5s; automatic backoff on 429)
- Resumable: writes to live_market_bars with source='upstox_v3' and interval='1minute'
- Skips existing rows via PostgreSQL `ON CONFLICT (instrument_id, interval, bar_time) DO NOTHING`
- Includes `--dry-run` mode that prints the plan without database execution
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_intraday_bars")

IST = ZoneInfo("Asia/Kolkata")
UPSTOX_API_BASE = os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com").rstrip("/")

DEFAULT_INSTRUMENTS: List[Dict[str, Any]] = [
    {"symbol": "NIFTY 50", "exchange": "NSE", "instrument_type": "INDEX", "provider_key": "NSE_INDEX|Nifty 50", "instrument_id": 24118},
    {"symbol": "BANKNIFTY", "exchange": "NSE", "instrument_type": "INDEX", "provider_key": "NSE_INDEX|Nifty Bank", "instrument_id": 52942},
    {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE002A01018", "instrument_id": 6367},
    {"symbol": "TCS", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE467B01029", "instrument_id": 6750},
    {"symbol": "HDFCBANK", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE040A01034", "instrument_id": 5503},
    {"symbol": "ICICIBANK", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE090A01021", "instrument_id": 5565},
    {"symbol": "BHARTIARTL", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE397D01024", "instrument_id": 4965},
    {"symbol": "INFY", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE009A01021", "instrument_id": 5628},
    {"symbol": "ITC", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE154A01025", "instrument_id": 5661},
    {"symbol": "SBIN", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE062A01020", "instrument_id": 6489},
    {"symbol": "LT", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE018A01030", "instrument_id": 5883},
    {"symbol": "HINDUNILVR", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE030A01027", "instrument_id": 5533},
    {"symbol": "AXISBANK", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE238A01034", "instrument_id": 4884},
    {"symbol": "KOTAKBANK", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE237A01036", "instrument_id": 5800},
    {"symbol": "BAJFINANCE", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE296A01032", "instrument_id": 4904},
    {"symbol": "MARUTI", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE585B01010", "instrument_id": 5946},
    {"symbol": "SUNPHARMA", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE044A01036", "instrument_id": 6681},
    {"symbol": "TATASTEEL", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE081A01020", "instrument_id": 6740},
    {"symbol": "NTPC", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE733E01010", "instrument_id": 6131},
    {"symbol": "POWERGRID", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE752E01010", "instrument_id": 6263},
    {"symbol": "TITAN", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE280A01028", "instrument_id": 6796},
    {"symbol": "M&M", "exchange": "NSE", "instrument_type": "EQ", "provider_key": "NSE_EQ|INE101A01026", "instrument_id": 5895},
]


def get_upstox_token() -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN") or ""
    return token.strip()


def calculate_trading_day_chunks(num_trading_days: int = 60, end_date: Optional[date] = None) -> List[tuple[date, date]]:
    """Generate 30-day date windows covering approximately `num_trading_days` trading days."""
    if end_date is None:
        end_date = datetime.now(IST).date()

    # Approximate 60 trading days = ~90 calendar days
    calendar_days_needed = int(num_trading_days * 1.5)
    start_date = end_date - timedelta(days=calendar_days_needed)

    # Chunk into 30-day windows because Upstox historical-candle endpoint returns max 30 days per call
    chunks: List[tuple[date, date]] = []
    curr_start = start_date
    while curr_start < end_date:
        curr_end = min(end_date, curr_start + timedelta(days=30))
        chunks.append((curr_start, curr_end))
        curr_start = curr_end + timedelta(days=1)

    return chunks


def fetch_upstox_candles(
    token: str,
    provider_key: str,
    from_date: date,
    to_date: date,
    interval: str = "1minute",
    sleep_seconds: float = 0.5,
) -> List[Dict[str, Any]]:
    """Fetch 1-minute historical candles from Upstox v3 REST API."""
    encoded_key = quote(provider_key, safe="")
    url = f"{UPSTOX_API_BASE}/v2/historical-candle/{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 429:
                backoff = max(2.0, sleep_seconds * (2 ** (attempt + 2)))
                logger.warning(f"Rate limited (429) on {provider_key}. Backing off for {backoff:.1f}s...")
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            data = resp.json()
            candles = data.get("data", {}).get("candles", [])
            parsed = []
            for c in candles:
                if len(c) >= 5:
                    ts_str = str(c[0])
                    parsed.append({
                        "bar_time": ts_str,
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": int(c[5]) if len(c) > 5 and c[5] is not None else 0,
                        "oi": int(c[6]) if len(c) > 6 and c[6] is not None else None,
                    })
            time.sleep(sleep_seconds)
            return parsed
        except Exception as err:
            if attempt == max_retries - 1:
                logger.error(f"Failed to fetch candles for {provider_key} ({from_date} to {to_date}): {err}")
                raise
            time.sleep(sleep_seconds * 2)

    return []


def save_bars_to_postgres(engine: Any, instrument_id: int, bars: List[Dict[str, Any]]) -> int:
    """Save bars to live_market_bars skipping existing rows."""
    if not bars:
        return 0
    from sqlalchemy import text
    sql = text("""
        INSERT INTO live_market_bars (
            instrument_id, interval, bar_time,
            open_price, high_price, low_price, close_price,
            volume, open_interest, source, exchange_timestamp
        ) VALUES (
            :instrument_id, CAST('1minute' AS bar_interval), CAST(:bar_time AS timestamptz),
            :open, :high, :low, :close,
            :volume, :oi, 'upstox_v3', CAST(:bar_time AS timestamptz)
        )
        ON CONFLICT (instrument_id, interval, bar_time) DO NOTHING
    """)

    rows = [{
        "instrument_id": instrument_id,
        "bar_time": b["bar_time"],
        "open": b["open"],
        "high": b["high"],
        "low": b["low"],
        "close": b["close"],
        "volume": b["volume"],
        "oi": b["oi"],
    } for b in bars]

    with engine.begin() as conn:
        res = conn.execute(sql, rows)
        return int(res.rowcount or 0)


def main():
    parser = argparse.ArgumentParser(description="Backfill 1-minute historical bars from Upstox v3 into live_market_bars")
    parser.add_argument("--dry-run", action="store_true", help="Print execution plan without making API calls or writing to DB")
    parser.add_argument("--days", type=int, default=60, help="Number of trading days to backfill (default: 60)")
    parser.add_argument("--rate-limit", type=float, default=0.5, help="Sleep seconds between HTTP requests (default: 0.5s)")
    parser.add_argument("--db-url", type=str, default=os.getenv("DATABASE_URL", "postgresql://avinash@localhost:5432/nivesh_v3_staging"), help="PostgreSQL connection URI")
    args = parser.parse_args()

    chunks = calculate_trading_day_chunks(num_trading_days=args.days)
    instruments = DEFAULT_INSTRUMENTS

    print("=" * 80)
    print("UPSTOX V3 INTRADAY 1-MINUTE CANDLE BACKFILL PLAN")
    print("=" * 80)
    print(f"Target Database:    {args.db_url}")
    print(f"Trading Days:       {args.days}")
    print(f"Calendar Windows:   {len(chunks)} chunks:")
    for i, (c_start, c_end) in enumerate(chunks, 1):
        print(f"  Chunk {i}: {c_start} to {c_end}")
    print(f"Total Instruments:  {len(instruments)} (2 Indices + 20 F&O Stocks)")
    print(f"Rate Limiting:      {args.rate_limit}s sleep per request")
    print(f"Estimated Calls:    {len(instruments) * len(chunks)} API requests")
    print("-" * 80)
    print("INSTRUMENTS TO BACKFILL:")
    for inst in instruments:
        print(f"  ID: {inst['instrument_id']:<6} | {inst['symbol']:<12} | Key: {inst['provider_key']:<26} | Type: {inst['instrument_type']}")
    print("=" * 80)

    if args.dry_run:
        print("\n[DRY RUN MODE] Plan printed successfully. No HTTP requests made, no database writes executed.")
        return 0

    token = get_upstox_token()
    if not token:
        logger.error("UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN environment variable is required.")
        sys.exit(1)

    from sqlalchemy import create_engine
    engine = create_engine(args.db_url)

    total_inserted = 0
    total_fetched = 0

    for inst in instruments:
        sym = inst["symbol"]
        inst_id = inst["instrument_id"]
        pkey = inst["provider_key"]
        logger.info(f"Starting backfill for {sym} (ID {inst_id})...")

        for c_start, c_end in chunks:
            try:
                candles = fetch_upstox_candles(
                    token=token,
                    provider_key=pkey,
                    from_date=c_start,
                    to_date=c_end,
                    interval="1minute",
                    sleep_seconds=args.rate_limit,
                )
                total_fetched += len(candles)
                inserted = save_bars_to_postgres(engine, inst_id, candles)
                total_inserted += inserted
                logger.info(f"  {sym} [{c_start} to {c_end}]: fetched {len(candles)} bars, inserted {inserted} new bars")
            except Exception as e:
                logger.error(f"  {sym} [{c_start} to {c_end}] failed: {e}")

    logger.info(f"Backfill complete! Total fetched: {total_fetched}, total new bars inserted: {total_inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
