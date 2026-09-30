#!/usr/bin/env python3
"""Phase B: Extract intraday_micro_v1 features from live_market_bars (Postgres).

This script:
1. Connects to Postgres (DATABASE_URL from .env)
2. Queries 5-minute bars per equity instrument
3. Computes the enriched intraday_micro_v1 features (VP Shape, ASI, Order Flow)
4. Writes results into the SQLite research DB via ml_pipeline.feature_rows()

Usage:
    .venv/bin/python scripts/extract_intraday_features.py [--interval 5minute] [--min-bars 80] [--dry-run]

Requirements:
    - Postgres must be running (Colima + Docker)
    - .env must contain DATABASE_URL
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import DATABASE_URL as _RAW_DB_URL
from backend.ml_pipeline import (
    ResearchStore,
    feature_rows,
    persist_features,
    now_iso,
)

# When running outside Docker, DATABASE_URL may be blank. Fall back to localhost.
DATABASE_URL = (_RAW_DB_URL
    or "postgresql+psycopg2://nivesh:nivesh-local-paper-only@127.0.0.1:5433/nivesh")


def _connect_postgres():
    """Create a SQLAlchemy engine from DATABASE_URL."""
    from sqlalchemy import create_engine
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set. Check your .env file.")
        sys.exit(1)
    engine = create_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    # Quick connectivity check
    with engine.connect() as conn:
        from sqlalchemy import text
        result = conn.execute(text("SELECT 1")).scalar()
        assert result == 1, "Postgres connectivity check failed"
    return engine


def _load_instruments(engine):
    """Load active equity instruments that have bars."""
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT i.id, i.exchange, i.symbol
            FROM instrument_master i
            JOIN live_market_bars b ON b.instrument_id = i.id
            WHERE i.is_active
              AND i.instrument_type = 'EQ'
              AND i.exchange IN ('NSE', 'BSE')
            ORDER BY i.exchange, i.symbol
        """)).mappings().all()
    return [dict(r) for r in rows]


def _load_bars(engine, instrument_id: int, interval: str, min_bars: int):
    """Load OHLCV bars from Postgres for one instrument, ordered chronologically."""
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT bar_time AS timestamp,
                   open_price AS open, high_price AS high,
                   low_price AS low, close_price AS close,
                   volume, COALESCE(open_interest, 0) AS oi
            FROM live_market_bars
            WHERE instrument_id = :iid AND interval = :interval
            ORDER BY bar_time ASC
        """), {"iid": instrument_id, "interval": interval}).mappings().all()
    bars = [{
        "timestamp": str(r["timestamp"]),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
        "volume": int(r["volume"]),
        "oi": int(r["oi"]),
    } for r in rows]
    return bars if len(bars) >= min_bars else []


def main():
    parser = argparse.ArgumentParser(description="Extract intraday_micro_v1 features from Postgres bars")
    parser.add_argument("--interval", default="5minute", help="Bar interval to extract (default: 5minute)")
    parser.add_argument("--min-bars", type=int, default=80, help="Minimum bars required per instrument (default: 80)")
    parser.add_argument("--dry-run", action="store_true", help="Count instruments and bars without extracting")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of instruments to process (0=all)")
    args = parser.parse_args()

    print(f"[Phase B] Intraday Feature Extraction")
    print(f"  Interval:  {args.interval}")
    print(f"  Min bars:  {args.min_bars}")
    print(f"  Dry run:   {args.dry_run}")
    print()

    # Connect to Postgres
    print("[1/4] Connecting to Postgres...")
    engine = _connect_postgres()
    print("  ✅ Connected")

    # Load instruments
    print("[2/4] Loading active equity instruments with bars...")
    instruments = _load_instruments(engine)
    print(f"  ✅ Found {len(instruments)} instruments")

    if args.limit > 0:
        instruments = instruments[:args.limit]
        print(f"  ⚠️  Limited to first {args.limit} instruments")

    if args.dry_run:
        # Just count bars per instrument
        print("\n[DRY RUN] Counting bars per instrument:")
        total_bars = 0
        for inst in instruments:
            bars = _load_bars(engine, inst["id"], args.interval, args.min_bars)
            count = len(bars)
            total_bars += count
            if count > 0:
                print(f"  {inst['exchange']}:{inst['symbol']:20s} → {count:>7,} bars")
            else:
                print(f"  {inst['exchange']}:{inst['symbol']:20s} → SKIP (< {args.min_bars} bars)")
        print(f"\n  Total bars: {total_bars:,}")
        print("  No features extracted (dry run). Remove --dry-run to extract.")
        return

    # Extract features
    print("[3/4] Extracting intraday_micro_v1 features...")
    store = ResearchStore()
    total_features = 0
    processed = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    for idx, inst in enumerate(instruments, 1):
        exchange = inst["exchange"]
        symbol = inst["symbol"]
        try:
            bars = _load_bars(engine, inst["id"], args.interval, args.min_bars)
            if not bars:
                skipped += 1
                continue

            rows = feature_rows(exchange, symbol, bars, feature_set="intraday_micro_v1")
            if rows:
                count = persist_features(store, rows, feature_set="intraday_micro_v1")
                total_features += count
                processed += 1
                elapsed = time.time() - start_time
                rate = processed / max(0.1, elapsed)
                print(f"  [{idx:>4}/{len(instruments)}] {exchange}:{symbol:20s} → {count:>6,} features  ({rate:.1f} instruments/s)")
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            print(f"  [{idx:>4}/{len(instruments)}] {exchange}:{symbol:20s} → ERROR: {exc}")

    elapsed = time.time() - start_time
    print(f"\n[4/4] Extraction complete!")
    print(f"  ✅ Processed:  {processed} instruments")
    print(f"  ⏭️  Skipped:    {skipped} (< {args.min_bars} bars)")
    print(f"  ❌ Errors:     {errors}")
    print(f"  📊 Features:   {total_features:,} rows written to {store.path}")
    print(f"  ⏱️  Duration:   {elapsed:.1f}s")
    print()
    print("Next step: retrain the model with:")
    print(f"  .venv/bin/python -c \"from backend.ml_pipeline import *; print(train_model(feature_set='intraday_micro_v1'))\"")


if __name__ == "__main__":
    main()
