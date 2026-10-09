"""Upstox REST candle backfill for training and shadow-paper fallback.

The backfill is intentionally non-destructive by default.  The production
``live_market_bars`` primary key is ``(instrument_id, interval, bar_time)``, so
the table cannot keep two candle sources for the same instrument/time.  To
protect the existing historical warehouse, this module fills missing bars with
Upstox REST data and only overwrites when ``replace=True`` is explicitly used.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import create_engine, text


IST = ZoneInfo("Asia/Kolkata")
UPSTOX_API_BASE = os.getenv("UPSTOX_API_BASE_URL", "https://api.upstox.com").rstrip("/")
REST_HISTORY_SOURCE = "upstox_rest_history"
REST_INTRADAY_SOURCE = "upstox_rest_intraday"
REST_5M_SOURCE = "upstox_rest_5m"
REST_1S_PROXY_SOURCE = "upstox_rest_1s_proxy"

PRIORITY_SYMBOLS = (
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "LT", "ITC",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "HAL", "NTPC", "ONGC", "POWERGRID", "ULTRACEMCO",
    "TITAN", "ADANIENT", "ADANIPORTS", "WIPRO", "TECHM", "JSWSTEEL",
    "TATASTEEL", "COALINDIA", "HCLTECH", "BEL",
)


@dataclass(frozen=True)
class BackfillInstrument:
    instrument_id: int
    provider_key: str
    exchange: str
    symbol: str
    instrument_type: str
    is_fno_eligible: bool


def upstox_token() -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN") or ""
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN is required")
    return token


def _headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc)


def _valid_ohlc(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> bool:
    return open_ > 0 and high > 0 and low > 0 and close > 0 and high >= max(open_, low, close) and low <= min(open_, high, close)


def parse_upstox_candles(payload: Dict) -> List[Dict]:
    candles = payload.get("data", {}).get("candles", []) if isinstance(payload, dict) else []
    parsed: List[Dict] = []
    for raw in candles:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        open_, high, low, close = (Decimal(str(raw[1])), Decimal(str(raw[2])), Decimal(str(raw[3])), Decimal(str(raw[4])))
        if not _valid_ohlc(open_, high, low, close):
            continue
        parsed.append({
            "bar_time": _parse_timestamp(raw[0]),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(raw[5] or 0) if len(raw) > 5 else 0,
            "oi": int(raw[6] or 0) if len(raw) > 6 and raw[6] is not None else None,
        })
    parsed.sort(key=lambda item: item["bar_time"])
    return parsed


def aggregate_5minute(candles: Iterable[Dict]) -> List[Dict]:
    buckets: Dict[datetime, List[Dict]] = {}
    for candle in candles:
        local = candle["bar_time"].astimezone(IST)
        floored = local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)
        buckets.setdefault(floored.astimezone(timezone.utc), []).append(candle)
    output = []
    for bucket_time, rows in sorted(buckets.items()):
        rows.sort(key=lambda item: item["bar_time"])
        output.append({
            "bar_time": bucket_time,
            "open": rows[0]["open"],
            "high": max(item["high"] for item in rows),
            "low": min(item["low"] for item in rows),
            "close": rows[-1]["close"],
            "volume": sum(item["volume"] for item in rows),
            "oi": rows[-1].get("oi"),
        })
    return output


def _lerp_decimal(start: Decimal, end: Decimal, step: int, steps: int) -> Decimal:
    if steps <= 0:
        return end
    return start + (end - start) * Decimal(step) / Decimal(steps)


def synthesize_1second_from_1minute(candles: Iterable[Dict]) -> List[Dict]:
    """Create honest proxy 1-second bars from 1-minute OHLC candles.

    Upstox REST does not provide the true tick path through each minute.  This
    function therefore creates a deterministic OHLC-preserving path and the
    caller stores it with source ``upstox_rest_1s_proxy``.  It is suitable for
    chart continuity and explicit gap-repair visibility, but not for formal
    live microstructure validation.
    """
    output: List[Dict] = []
    for candle in candles:
        start = candle["bar_time"]
        open_, high, low, close = candle["open"], candle["high"], candle["low"], candle["close"]
        if not _valid_ohlc(open_, high, low, close):
            continue
        # Make a plausible path that always touches both extremes.  Green
        # candles usually visit low first then high; red candles high first then
        # low.  This keeps every generated minute aggregating back to the REST
        # 1m OHLC.
        path = (
            [(0, open_), (15, low), (30, high), (59, close)]
            if close >= open_
            else [(0, open_), (15, high), (30, low), (59, close)]
        )
        values: List[Decimal] = [open_] * 60
        for (left_second, left_value), (right_second, right_value) in zip(path, path[1:]):
            span = max(1, right_second - left_second)
            for second in range(left_second, right_second + 1):
                values[second] = _lerp_decimal(left_value, right_value, second - left_second, span)
        volume = int(candle.get("volume") or 0)
        base_volume = volume // 60 if volume > 0 else 0
        remainder = volume - base_volume * 60
        for second in range(60):
            current = values[second]
            previous = values[second - 1] if second else open_
            output.append({
                "bar_time": start + timedelta(seconds=second),
                "open": previous,
                "high": max(previous, current),
                "low": min(previous, current),
                "close": current,
                "volume": base_volume + (1 if second < remainder else 0),
                "oi": candle.get("oi"),
            })
    return output


class UpstoxRestBackfill:
    def __init__(self, database_url: str, token: Optional[str] = None, sleep_seconds: float = 0.25):
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self.token = token or upstox_token()
        self.sleep_seconds = max(0.0, sleep_seconds)

    def select_instruments(self, limit: int = 100, include_indices: bool = True, fno_only: bool = True) -> List[BackfillInstrument]:
        hard_limit = "" if limit <= 0 else "LIMIT :limit"
        with self.engine.connect() as connection:
            rows = connection.execute(text(f"""
                SELECT i.id instrument_id,k.provider_key,i.exchange,i.symbol,i.instrument_type,i.is_fno_eligible
                FROM instrument_provider_keys k JOIN instrument_master i ON i.id=k.instrument_id
                WHERE i.is_active AND k.is_active AND k.provider='upstox_v3'
                  AND (
                    (:include_indices AND i.instrument_type='INDEX' AND REPLACE(i.symbol,' ','') IN ('NIFTY50','BANKNIFTY','SENSEX','INDIAVIX'))
                    OR (i.exchange IN ('NSE','BSE') AND i.instrument_type='EQ' AND (:fno_only=FALSE OR i.is_fno_eligible))
                  )
                ORDER BY CASE
                    WHEN REPLACE(i.symbol,' ','') IN ('NIFTY50','BANKNIFTY','SENSEX','INDIAVIX') THEN 0
                    WHEN i.symbol = ANY(:priority) THEN 1
                    WHEN i.exchange='NSE' AND i.instrument_type='EQ' AND i.is_fno_eligible THEN 2
                    WHEN i.exchange='BSE' AND i.instrument_type='EQ' AND i.is_fno_eligible THEN 3
                    ELSE 4 END,
                    i.exchange,i.symbol
                {hard_limit}
            """), {"limit": limit, "include_indices": include_indices, "fno_only": fno_only, "priority": list(PRIORITY_SYMBOLS)}).mappings().all()
        return [BackfillInstrument(**dict(row)) for row in rows]

    def _get(self, path: str) -> Dict:
        response = requests.get(f"{UPSTOX_API_BASE}{path}", headers=_headers(self.token), timeout=30)
        if response.status_code == 429:
            time.sleep(max(2.0, self.sleep_seconds * 8))
        response.raise_for_status()
        return response.json()

    def fetch_intraday(self, provider_key: str, interval: str = "1minute") -> List[Dict]:
        encoded = quote(provider_key, safe="")
        payload = self._get(f"/v2/historical-candle/intraday/{encoded}/{interval}")
        return parse_upstox_candles(payload)

    def fetch_history(self, provider_key: str, interval: str, from_date: date, to_date: date) -> List[Dict]:
        encoded = quote(provider_key, safe="")
        payload = self._get(f"/v2/historical-candle/{encoded}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}")
        return parse_upstox_candles(payload)

    def save_bars(self, instrument_id: int, interval: str, bars: Iterable[Dict], source: str, replace: bool = False) -> int:
        rows = list(bars)
        if not rows:
            return 0
        conflict = """DO UPDATE SET open_price=EXCLUDED.open_price,high_price=EXCLUDED.high_price,low_price=EXCLUDED.low_price,
            close_price=EXCLUDED.close_price,volume=EXCLUDED.volume,open_interest=EXCLUDED.open_interest,source=EXCLUDED.source,
            exchange_timestamp=EXCLUDED.exchange_timestamp,received_at=CURRENT_TIMESTAMP""" if replace else "DO NOTHING"
        with self.engine.begin() as connection:
            result = connection.execute(text(f"""
                INSERT INTO live_market_bars(instrument_id,interval,bar_time,open_price,high_price,low_price,close_price,volume,open_interest,source,exchange_timestamp)
                VALUES(:instrument_id,CAST(:interval AS bar_interval),:bar_time,:open,:high,:low,:close,:volume,:oi,:source,:bar_time)
                ON CONFLICT(instrument_id,interval,bar_time) {conflict}
            """), [{
                "instrument_id": instrument_id,
                "interval": interval,
                "bar_time": item["bar_time"],
                "open": item["open"],
                "high": item["high"],
                "low": item["low"],
                "close": item["close"],
                "volume": item["volume"],
                "oi": item.get("oi"),
                "source": source,
            } for item in rows])
        return int(result.rowcount or 0)

    def record_event(self, level: str, message: str, payload: Dict) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES('upstox_rest_backfill',:level,:message,CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
            """), {"level": level, "message": message, "payload": json.dumps(payload, default=str)})

    def run(self, *, limit: int = 100, mode: str = "intraday", days: int = 365, fno_only: bool = True, replace: bool = False, repair_1s_proxy: bool = False, target_date: Optional[date] = None) -> Dict:
        instruments = self.select_instruments(limit=limit, include_indices=True, fno_only=fno_only)
        report = {
            "status": "success",
            "mode": mode,
            "requested_limit": limit,
            "instruments": len(instruments),
            "replace_existing": replace,
            "inserted_1minute": 0,
            "inserted_5minute": 0,
            "inserted_1second_proxy": 0,
            "one_second_proxy_source": REST_1S_PROXY_SOURCE if repair_1s_proxy else None,
            "inserted_day": 0,
            "failures": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.record_event("INFO", "Upstox REST backfill started", {k: v for k, v in report.items() if k != "failures"})
        from_date = target_date or (date.today() - timedelta(days=max(1, days)))
        to_date = target_date or date.today()
        for instrument in instruments:
            try:
                if mode in {"history", "both"}:
                    daily = self.fetch_history(instrument.provider_key, "day", from_date, to_date)
                    report["inserted_day"] += self.save_bars(instrument.instrument_id, "day", daily, REST_HISTORY_SOURCE, replace=replace)
                    time.sleep(self.sleep_seconds)
                if mode in {"intraday", "both"}:
                    one_minute = self.fetch_history(instrument.provider_key, "1minute", target_date, target_date) if target_date else self.fetch_intraday(instrument.provider_key, "1minute")
                    report["inserted_1minute"] += self.save_bars(instrument.instrument_id, "1minute", one_minute, REST_INTRADAY_SOURCE, replace=replace)
                    five_minute = aggregate_5minute(one_minute)
                    report["inserted_5minute"] += self.save_bars(instrument.instrument_id, "5minute", five_minute, REST_5M_SOURCE, replace=replace)
                    if repair_1s_proxy:
                        one_second_proxy = synthesize_1second_from_1minute(one_minute)
                        report["inserted_1second_proxy"] += self.save_bars(instrument.instrument_id, "1second", one_second_proxy, REST_1S_PROXY_SOURCE, replace=replace)
                    time.sleep(self.sleep_seconds)
            except Exception as exc:  # Keep the batch going; report every bad symbol.
                failure = {"symbol": instrument.symbol, "exchange": instrument.exchange, "provider_key": instrument.provider_key, "error": str(exc)[:240]}
                report["failures"].append(failure)
                self.record_event("ERROR", "Upstox REST backfill instrument failed", failure)
        report["status"] = "partial" if report["failures"] else "success"
        self.record_event("INFO" if not report["failures"] else "WARNING", "Upstox REST backfill completed", report)
        return report


def run_backfill(database_url: str, **kwargs) -> Dict:
    return UpstoxRestBackfill(database_url, sleep_seconds=float(os.getenv("NIVESH_UPSTOX_REST_SLEEP_SECONDS", "0.25"))).run(**kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Upstox REST candles into PostgreSQL without deleting existing history.")
    parser.add_argument("--mode", choices=["intraday", "history", "both"], default=os.getenv("NIVESH_UPSTOX_BACKFILL_MODE", "intraday"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("NIVESH_UPSTOX_BACKFILL_LIMIT", "100")), help="0 means all eligible instruments")
    parser.add_argument("--days", type=int, default=int(os.getenv("NIVESH_UPSTOX_BACKFILL_DAYS", "365")))
    parser.add_argument("--all-nse", action="store_true", help="Legacy alias: include all NSE/BSE cash equities, not only F&O names")
    parser.add_argument("--all-cash", action="store_true", help="Include all NSE/BSE cash equities, not only F&O names")
    parser.add_argument("--replace", action="store_true", help="Overwrite existing bars for matching instrument/interval/time")
    parser.add_argument("--date", type=str, default="", help="Specific IST session date to repair, YYYY-MM-DD")
    parser.add_argument("--repair-1s-proxy", action="store_true", help="Create source-tagged proxy 1-second bars from restored 1-minute REST candles")
    args = parser.parse_args()
    target_date = date.fromisoformat(args.date) if args.date else None
    report = run_backfill(
        os.environ["DATABASE_URL"],
        limit=args.limit,
        mode=args.mode,
        days=args.days,
        fno_only=not (args.all_nse or args.all_cash),
        replace=args.replace,
        repair_1s_proxy=args.repair_1s_proxy,
        target_date=target_date,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
