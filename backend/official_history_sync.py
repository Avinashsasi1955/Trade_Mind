"""Resumable official NSE/BSE end-of-day archive ingestion.

This imports exchange-published bhavcopy files only. It deliberately does not
scrape quote pages or pretend that end-of-day bars are intraday observations.

Examples:
  python3 -m backend.official_history_sync --exchange NSE --days 365
  python3 -m backend.official_history_sync --exchange BOTH --start 2021-01-01
"""
import argparse
import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from .config import DATA_DIR
from .history_store import HistoryStore
from .security_master import load_security_master


SOURCE = "official_exchange_bhavcopy"
USER_AGENT = "NiveshAI-Research/1.0 (+local paper-trading research)"
RAW_DIR = DATA_DIR / "raw" / "exchange_bhavcopy"


def _dates(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            yield cursor
        cursor += timedelta(days=1)


def archive_urls(exchange: str, trade_date: date) -> List[str]:
    ymd = trade_date.strftime("%Y%m%d")
    if exchange == "NSE":
        old = trade_date.strftime("%d%b%Y").upper()
        return [
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
            f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{trade_date:%Y}/{trade_date.strftime('%b').upper()}/cm{old}bhav.csv.zip",
        ]
    old = trade_date.strftime("%d%m%y")
    return [
        f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.csv",
        f"https://www.bseindia.com/download/BhavCopy/Equity/EQ{old}_CSV.ZIP",
    ]


def _request(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,application/zip,application/octet-stream,*/*",
        "Referer": "https://www.nseindia.com/" if "nseindia" in url else "https://www.bseindia.com/",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    if not payload or payload[:20].lstrip().lower().startswith(b"<!doctype html"):
        raise ValueError("archive returned an HTML/missing-file response")
    return payload


def download_archive(exchange: str, trade_date: date, retries: int = 2) -> Tuple[str, bytes]:
    errors = []
    for url in archive_urls(exchange, trade_date):
        for attempt in range(retries + 1):
            try:
                return url, _request(url)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    break
                if attempt < retries:
                    time.sleep(0.6 * (attempt + 1))
    raise RuntimeError("; ".join(errors[-3:]))


def _csv_bytes(payload: bytes) -> bytes:
    if payload[:2] != b"PK":
        return payload
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith((".csv", ".txt"))]
        if len(files) != 1:
            raise ValueError("expected exactly one CSV in bhavcopy archive")
        item = files[0]
        if item.file_size > 50_000_000 or item.file_size > max(10_000_000, len(payload) * 100):
            raise ValueError("unsafe archive expansion ratio")
        return archive.read(item)


def _number(row: Dict, *names: str) -> float:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "-"):
            return float(str(value).replace(",", ""))
    return 0.0


def parse_archive(exchange: str, trade_date: date, payload: bytes, symbols: Sequence[str] = (),
                  eligible_symbols: Sequence[str] = (), aliases: Dict[str,str] = None) -> List[Dict]:
    text = _csv_bytes(payload).decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    wanted = {item.upper() for item in symbols}
    eligible = {item.upper() for item in eligible_symbols}
    aliases = {str(key).upper():str(value).upper() for key,value in (aliases or {}).items()}
    rows = []
    for row in reader:
        # UDiFF schema (current NSE/BSE) and legacy exchange schemas.
        raw_symbol = (row.get("TckrSymb") or row.get("SYMBOL") or row.get("SC_CODE") or "").strip().upper()
        isin = (row.get("ISIN") or "").strip().upper()
        symbol = aliases.get(raw_symbol) or aliases.get(isin) or raw_symbol
        instrument_type = (row.get("FinInstrmTp") or "STK").strip().upper()
        series = (row.get("SctySrs") or row.get("SERIES") or row.get("SC_GROUP") or "").strip().upper()
        if not symbol or instrument_type != "STK" or (exchange == "NSE" and series != "EQ"):
            continue
        if eligible and symbol not in eligible:
            continue
        if wanted and symbol not in wanted:
            continue
        open_price = _number(row, "OpnPric", "OPEN")
        high = _number(row, "HghPric", "HIGH")
        low = _number(row, "LwPric", "LOW")
        close = _number(row, "ClsPric", "CLOSE")
        if min(open_price, high, low, close) <= 0 or high < low:
            continue
        row_date = (row.get("TradDt") or row.get("TIMESTAMP") or "").strip()
        if len(row_date) >= 10 and row_date[4:5] == "-" and row_date[7:8] == "-" and row_date[:10] != trade_date.isoformat():
            raise ValueError(f"archive date {row_date[:10]} does not match requested {trade_date}")
        token = int(_number(row, "FinInstrmId", "SC_CODE"))
        rows.append({
            "symbol": symbol,
            "instrument_token": token,
            "timestamp": f"{trade_date.isoformat()}T00:00:00+05:30",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(_number(row, "TtlTradgVol", "TOTTRDQTY", "NO_OF_SHRS")),
            "oi": int(_number(row, "OpnIntrst")),
        })
    if not rows:
        raise ValueError("no eligible equity rows found in archive")
    return rows


def _cache_path(exchange: str, trade_date: date, payload: bytes) -> Path:
    suffix = ".zip" if payload[:2] == b"PK" else ".csv"
    return RAW_DIR / exchange.lower() / str(trade_date.year) / f"{trade_date.isoformat()}{suffix}"


def sync(exchange: str = "NSE", days: int = 365, start: str = "", end: str = "",
         workers: int = 4, keep_raw: bool = True, force: bool = False,
         symbols: Sequence[str] = ()) -> Dict:
    exchanges = ["NSE", "BSE"] if exchange.upper() == "BOTH" else [exchange.upper()]
    if any(item not in {"NSE", "BSE"} for item in exchanges):
        raise ValueError("exchange must be NSE, BSE or BOTH")
    end_date = date.fromisoformat(end) if end else date.today() - timedelta(days=1)
    start_date = date.fromisoformat(start) if start else end_date - timedelta(days=max(1, days))
    if start_date > end_date:
        raise ValueError("start date must not be after end date")
    store = HistoryStore()
    master = load_security_master()["securities"]
    equity_symbols = {venue: {item["symbol"].upper() for item in master if item["exchange"] == venue}
                      for venue in ("NSE", "BSE")}
    aliases={venue:{} for venue in ("NSE","BSE")}
    for item in master:
        venue=item["exchange"]; canonical=item["symbol"].upper()
        for value in (item.get("symbol"),item.get("code"),item.get("isin")):
            if value: aliases[venue][str(value).upper()]=canonical
    report = {"start": start_date.isoformat(), "end": end_date.isoformat(), "exchanges": {}, "source": SOURCE}
    for venue in exchanges:
        requested = [day for day in _dates(start_date, end_date)
                     if force or not store.archive_imported(venue, day.isoformat(), SOURCE)]
        result = {"requested_sessions": len(requested), "imported_sessions": 0, "missing_sessions": 0,
                  "rows": 0, "symbols": 0, "errors": []}
        with ThreadPoolExecutor(max_workers=max(1, min(8, workers))) as pool:
            futures = {pool.submit(download_archive, venue, day): day for day in requested}
            for completed, future in enumerate(as_completed(futures), 1):
                day = futures[future]
                stamp = datetime.now(timezone.utc).isoformat()
                try:
                    url, payload = future.result()
                    rows = parse_archive(venue, day, payload, symbols, equity_symbols[venue],aliases[venue])
                    digest = hashlib.sha256(payload).hexdigest()
                    store.save_archive(venue, day.isoformat(), rows, SOURCE, url, digest, len(payload), stamp)
                    if keep_raw:
                        path = _cache_path(venue, day, payload)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(payload)
                    result["imported_sessions"] += 1
                    result["rows"] += len(rows)
                    print(f"[{venue} {completed}/{len(requested)}] {day} · {len(rows):,} equities", flush=True)
                except Exception as exc:
                    url = archive_urls(venue, day)[0]
                    store.record_archive_failure(venue, day.isoformat(), SOURCE, url, str(exc), stamp)
                    result["missing_sessions"] += 1
                    if len(result["errors"]) < 20:
                        result["errors"].append({"date": day.isoformat(), "error": str(exc)[:240]})
                    print(f"[{venue} {completed}/{len(requested)}] {day} · unavailable", flush=True)
        stamp = datetime.now(timezone.utc).isoformat()
        result["pruned_non_equity_rows"] = store.prune_symbols(venue, equity_symbols[venue])
        result["symbols"] = store.rebuild_coverage(venue, "day", stamp)
        report["exchanges"][venue] = result
    report["warehouse"] = store.stats()
    return report


def main():
    parser = argparse.ArgumentParser(description="Import official NSE/BSE daily equity bhavcopies")
    parser.add_argument("--exchange", choices=["NSE", "BSE", "BOTH"], default="NSE")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-raw", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--symbols", default="", help="optional comma-separated symbols/codes")
    args = parser.parse_args()
    report = sync(args.exchange, args.days, args.start, args.end, args.workers, not args.no_raw,
                  args.force, [item.strip() for item in args.symbols.split(",") if item.strip()])
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
