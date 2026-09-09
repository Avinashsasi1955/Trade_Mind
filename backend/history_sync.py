"""Resumable Kite daily-history sync.

Usage after completing the daily Kite login:
  KITE_API_KEY=... KITE_ACCESS_TOKEN=... python3 -m backend.history_sync --days 365
"""
import argparse
import time
from datetime import date, datetime, timedelta, timezone

from .config import KITE_ACCESS_TOKEN, KITE_API_KEY
from .history_store import HistoryStore
from .security_master import load_security_master
from .zerodha_adapter import ZerodhaAdapter


def _windows(start: date, end: date, interval: str):
    chunk_days = 30 if interval == "minute" else 90 if interval != "day" else 1500
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=chunk_days - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def sync(days: int = 365, since: str = "", limit: int = 0, delay: float = .38, interval: str = "day"):
    adapter = ZerodhaAdapter(KITE_API_KEY, KITE_ACCESS_TOKEN)
    store = HistoryStore()
    end = date.today()
    start = date.fromisoformat(since) if since else end - timedelta(days=days)
    master_keys = {(item["exchange"], item["symbol"]): item for item in load_security_master()["securities"]}
    instruments = [item for item in adapter.instruments() if item.get("exchange") in {"NSE", "BSE"} and item.get("instrument_type") == "EQ" and (item["exchange"], item["tradingsymbol"]) in master_keys]
    if limit:
        instruments = instruments[:limit]
    if interval not in {"minute","3minute","5minute","10minute","15minute","30minute","60minute","day"}:
        raise ValueError("Unsupported Kite interval")
    print(f"Syncing {len(instruments)} equities at {interval} from {start} through {end}; existing work is upserted safely.")
    for index, item in enumerate(instruments, 1):
        exchange, symbol, token = item["exchange"], item["tradingsymbol"], int(item["instrument_token"])
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            rows=[]
            for window_start,window_end in _windows(start,end,interval):
                rows.extend(adapter.historical(token, interval, window_start, window_end))
                time.sleep(max(.34, delay))
            saved = store.save(exchange, symbol, token, interval, rows, stamp)
            print(f"[{index}/{len(instruments)}] {exchange}:{symbol} · {saved} bars")
        except Exception as exc:
            store.failure(exchange, symbol, token, "day", str(exc), stamp)
            print(f"[{index}/{len(instruments)}] {exchange}:{symbol} · failed: {exc}")
        time.sleep(max(.34, delay))
    print(store.stats())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--since", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=.38)
    parser.add_argument("--interval", default="day")
    args = parser.parse_args()
    sync(args.days, args.since, args.limit, args.delay, args.interval)
