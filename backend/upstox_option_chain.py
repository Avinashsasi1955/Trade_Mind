"""Read-only Upstox option-chain OI synchronisation.

The live WebSocket is still the preferred source for completed candles. This
module complements it by fetching option-chain REST snapshots so paper trading
can validate option OI, bid/ask spread and near-expiry liquidity before a trade.
It never places, modifies or cancels orders.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from sqlalchemy import create_engine, text

from .upstox_backfill import UPSTOX_API_BASE, _headers, upstox_token
from .upstox_stream import provider_token


OPTION_CHAIN_SOURCE = "upstox_v2_option_chain"
DEFAULT_UNDERLYINGS = ("NIFTY 50", "BANKNIFTY", "SENSEX", "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS")
OPTION_UNDERLYING_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BSE SENSEX": "SENSEX",
    "BSESENSEX": "SENSEX",
    "SENSEX": "SENSEX",
}


@dataclass(frozen=True)
class OptionChainRequest:
    underlying_id: int
    underlying_symbol: str
    provider_key: str
    expiry: date


def _decimal(value) -> Optional[Decimal]:
    if value in (None, "", "NaN"):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _integer(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalise_symbol(symbol: str) -> str:
    compact = str(symbol or "").upper().replace(" ", "")
    return {"NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "NIFTYBANK": "BANKNIFTY", "BSESENSEX": "SENSEX"}.get(compact, str(symbol or "").upper())


def _option_underlying_alias_sql() -> str:
    return """CASE
        WHEN REPLACE(u.underlying_symbol,' ','') IN ('NIFTY50','NIFTY') THEN 'NIFTY'
        WHEN REPLACE(u.underlying_symbol,' ','') IN ('NIFTYBANK','BANKNIFTY') THEN 'BANKNIFTY'
        WHEN REPLACE(u.underlying_symbol,' ','') IN ('BSESENSEX','SENSEX') THEN 'SENSEX'
        ELSE u.underlying_symbol
    END"""


def _option_payload(row: Dict, side: str) -> Dict:
    if side == "CE":
        value = row.get("call_options") or row.get("callOptions") or row.get("ce_options") or row.get("ceOption") or row.get("ce") or {}
    else:
        value = row.get("put_options") or row.get("putOptions") or row.get("pe_options") or row.get("peOption") or row.get("pe") or {}
    return value if isinstance(value, dict) else {}


def _market_data(option: Dict) -> Dict:
    market = option.get("market_data") or option.get("marketData") or option.get("quote") or {}
    return market if isinstance(market, dict) else option


def _option_greeks(option: Dict) -> Dict:
    greeks = option.get("option_greeks") or option.get("optionGreeks") or option.get("greeks") or {}
    return greeks if isinstance(greeks, dict) else {}


def _best_bid_ask(market: Dict) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[int], Optional[int]]:
    bid = _decimal(market.get("bid_price") or market.get("best_bid") or market.get("bid"))
    ask = _decimal(market.get("ask_price") or market.get("best_ask") or market.get("ask"))
    bid_qty = _integer(market.get("bid_qty") or market.get("bid_quantity") or market.get("best_bid_qty"))
    ask_qty = _integer(market.get("ask_qty") or market.get("ask_quantity") or market.get("best_ask_qty"))
    depth = market.get("depth") or {}
    if isinstance(depth, dict):
        buy_rows = depth.get("buy") or depth.get("bids") or []
        sell_rows = depth.get("sell") or depth.get("asks") or []
        if buy_rows and not bid:
            first = buy_rows[0] if isinstance(buy_rows[0], dict) else {}
            bid = _decimal(first.get("price") or first.get("bid_price"))
            bid_qty = _integer(first.get("quantity") or first.get("qty")) or bid_qty
        if sell_rows and not ask:
            first = sell_rows[0] if isinstance(sell_rows[0], dict) else {}
            ask = _decimal(first.get("price") or first.get("ask_price"))
            ask_qty = _integer(first.get("quantity") or first.get("qty")) or ask_qty
    return bid, ask, bid_qty, ask_qty


class UpstoxOptionChainSync:
    def __init__(self, database_url: str, redis_url: str = "", token: Optional[str] = None, sleep_seconds: float = 0.35):
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        self.engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self.token = token or upstox_token()
        self.sleep_seconds = max(0.0, sleep_seconds)
        self.redis = None
        if redis_url:
            try:
                import redis
                self.redis = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
            except Exception:
                self.redis = None

    def select_requests(self, limit: int = 8, underlyings: Iterable[str] = ()) -> List[OptionChainRequest]:
        wanted = [_normalise_symbol(item) for item in underlyings if item]
        if not wanted:
            wanted = list(DEFAULT_UNDERLYINGS)
        with self.engine.connect() as connection:
            rows = connection.execute(text(f"""
                WITH wanted(symbol, priority) AS (
                    SELECT symbol, ordinality
                    FROM unnest(CAST(:symbols AS text[])) WITH ORDINALITY AS wanted(symbol, ordinality)
                ),
                underlying AS (
                    SELECT DISTINCT ON (w.priority)
                        i.id underlying_id,
                        i.symbol underlying_symbol,
                        k.provider_key,
                        w.priority
                    FROM wanted w
                    JOIN instrument_master i
                      ON (i.symbol=w.symbol OR REPLACE(i.symbol,' ','')=REPLACE(w.symbol,' ',''))
                    JOIN instrument_provider_keys k
                      ON k.instrument_id=i.id AND k.provider='upstox_v3' AND k.is_active
                    WHERE i.is_active AND i.instrument_type IN ('EQ','INDEX')
                    ORDER BY w.priority,
                        CASE WHEN i.instrument_type='INDEX' THEN 0 ELSE 1 END,
                        CASE WHEN i.exchange='NSE' THEN 0 WHEN i.exchange='BSE' THEN 1 ELSE 2 END
                ),
                expiries AS (
                    SELECT u.underlying_id,u.underlying_symbol,u.provider_key,u.priority,MIN(o.expiry) expiry
                    FROM underlying u
                    JOIN instrument_master o
                      ON o.underlying_symbol IN (u.underlying_symbol, REPLACE(u.underlying_symbol,' ',''), {_option_underlying_alias_sql()})
                     AND o.instrument_type IN ('CE','PE')
                     AND o.exchange IN ('NFO','BFO')
                     AND o.is_active
                     AND o.expiry >= CURRENT_DATE
                    GROUP BY u.underlying_id,u.underlying_symbol,u.provider_key,u.priority
                )
                SELECT underlying_id, underlying_symbol, provider_key, expiry
                FROM expiries
                ORDER BY priority
                LIMIT :limit
            """), {"symbols": wanted, "limit": max(1, limit)}).mappings().all()
        return [OptionChainRequest(**dict(row)) for row in rows]

    def fetch_chain(self, request: OptionChainRequest) -> List[Dict]:
        query = urlencode({"instrument_key": request.provider_key, "expiry_date": request.expiry.isoformat()})
        response = requests.get(f"{UPSTOX_API_BASE}/v2/option/chain?{query}", headers=_headers(self.token), timeout=30)
        if response.status_code == 429:
            time.sleep(max(2.0, self.sleep_seconds * 8))
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            raise ValueError("Upstox option-chain response did not contain a data list")
        return data

    def _parse_chain_rows(self, request: OptionChainRequest, chain_rows: List[Dict], observed_at: datetime) -> List[Dict]:
        snapshots: List[Dict] = []
        for row in chain_rows:
            strike = _decimal(row.get("strike_price") or row.get("strike"))
            expiry_raw = row.get("expiry") or request.expiry.isoformat()
            expiry = date.fromisoformat(str(expiry_raw)[:10])
            if strike is None:
                continue
            for option_type in ("CE", "PE"):
                option = _option_payload(row, option_type)
                if not option:
                    continue
                instrument_key = str(option.get("instrument_key") or option.get("instrumentKey") or "").strip()
                market = _market_data(option)
                greeks = _option_greeks(option)
                bid, ask, bid_qty, ask_qty = _best_bid_ask(market)
                snapshots.append({
                    "provider_key": instrument_key,
                    "provider_token": provider_token(instrument_key) if instrument_key else None,
                    "underlying_id": request.underlying_id,
                    "underlying_symbol": request.underlying_symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "observed_at": observed_at,
                    "last_price": _decimal(market.get("ltp") or market.get("last_price") or market.get("lastPrice")),
                    "best_bid": bid,
                    "best_ask": ask,
                    "bid_quantity": bid_qty,
                    "ask_quantity": ask_qty,
                    "volume": _integer(market.get("volume") or market.get("volume_traded_today")),
                    "open_interest": _integer(market.get("oi") or market.get("open_interest")),
                    "previous_open_interest": _integer(market.get("prev_oi") or market.get("previous_open_interest")),
                    "implied_volatility": _decimal(greeks.get("iv") or greeks.get("implied_volatility")),
                    "delta": _decimal(greeks.get("delta")),
                    "gamma": _decimal(greeks.get("gamma")),
                    "theta": _decimal(greeks.get("theta")),
                    "vega": _decimal(greeks.get("vega")),
                    "raw_payload": json.dumps(option, default=str),
                })
        return snapshots

    def save_snapshots(self, snapshots: List[Dict]) -> Dict:
        if not snapshots:
            return {"snapshots": 0, "bars_updated": 0, "depth_cached": 0, "oi_rows": 0}
        depth_cached = 0
        with self.engine.begin() as connection:
            rows = connection.execute(text("""
                SELECT k.provider_key,i.id instrument_id,i.exchange,i.symbol
                FROM instrument_provider_keys k JOIN instrument_master i ON i.id=k.instrument_id
                WHERE k.provider='upstox_v3' AND k.provider_key = ANY(:keys)
            """), {"keys": [item["provider_key"] for item in snapshots if item.get("provider_key")]}).mappings().all()
            by_key = {row["provider_key"]: dict(row) for row in rows}
            payloads = []
            for item in snapshots:
                mapped = by_key.get(item.get("provider_key") or "")
                if not mapped:
                    continue
                oi = item.get("open_interest")
                previous_oi = item.get("previous_open_interest")
                payloads.append({
                    **item,
                    "instrument_id": mapped["instrument_id"],
                    "exchange": mapped["exchange"],
                    "symbol": mapped["symbol"],
                    "oi_change": (oi - previous_oi) if oi is not None and previous_oi is not None else None,
                })
            if payloads:
                connection.execute(text("""
                    INSERT INTO option_chain_oi_snapshots(
                        instrument_id,underlying_instrument_id,provider,observed_at,exchange,symbol,underlying_symbol,expiry,strike,option_type,
                        last_price,best_bid,best_ask,bid_quantity,ask_quantity,volume,open_interest,previous_open_interest,oi_change,
                        implied_volatility,delta,gamma,theta,vega,raw_payload
                    )
                    VALUES(
                        :instrument_id,:underlying_id,:provider,:observed_at,:exchange,:symbol,:underlying_symbol,:expiry,:strike,CAST(:option_type AS instrument_kind),
                        :last_price,:best_bid,:best_ask,:bid_quantity,:ask_quantity,:volume,:open_interest,:previous_open_interest,:oi_change,
                        :implied_volatility,:delta,:gamma,:theta,:vega,CAST(:raw_payload AS jsonb)
                    )
                    ON CONFLICT(instrument_id,observed_at,provider) DO UPDATE SET
                        last_price=EXCLUDED.last_price,best_bid=EXCLUDED.best_bid,best_ask=EXCLUDED.best_ask,
                        bid_quantity=EXCLUDED.bid_quantity,ask_quantity=EXCLUDED.ask_quantity,volume=EXCLUDED.volume,
                        open_interest=EXCLUDED.open_interest,previous_open_interest=EXCLUDED.previous_open_interest,oi_change=EXCLUDED.oi_change,
                        implied_volatility=EXCLUDED.implied_volatility,delta=EXCLUDED.delta,gamma=EXCLUDED.gamma,theta=EXCLUDED.theta,vega=EXCLUDED.vega,
                        raw_payload=EXCLUDED.raw_payload,received_at=CURRENT_TIMESTAMP
                """), [{**item, "provider": OPTION_CHAIN_SOURCE} for item in payloads])
                updated = connection.execute(text("""
                    WITH latest AS (
                        SELECT DISTINCT ON (b.instrument_id,b.interval)
                            b.instrument_id,b.interval,b.bar_time,s.open_interest,s.oi_change
                        FROM live_market_bars b
                        JOIN option_chain_oi_snapshots s ON s.instrument_id=b.instrument_id
                        WHERE s.observed_at=:observed_at
                          AND b.interval IN ('1minute','5minute')
                          AND b.source IN ('upstox_v3','upstox_rest_intraday','upstox_rest_5m')
                          AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:observed_at AT TIME ZONE 'Asia/Kolkata')::date
                        ORDER BY b.instrument_id,b.interval,b.bar_time DESC
                    )
                    UPDATE live_market_bars b
                    SET open_interest=latest.open_interest,
                        oi_change=latest.oi_change,
                        received_at=CURRENT_TIMESTAMP
                    FROM latest
                    WHERE b.instrument_id=latest.instrument_id
                      AND b.interval=latest.interval
                      AND b.bar_time=latest.bar_time
                      AND latest.open_interest IS NOT NULL
                """), {"observed_at": payloads[0]["observed_at"]}).rowcount
            else:
                updated = 0
        if self.redis:
            pipeline = self.redis.pipeline(transaction=False)
            for item in snapshots:
                if item.get("provider_token") and item.get("best_bid") and item.get("best_ask"):
                    payload = {
                        "instrument_token": item["provider_token"],
                        "best_bid": str(item["best_bid"]),
                        "best_ask": str(item["best_ask"]),
                        "bid_quantity": item.get("bid_quantity") or 0,
                        "ask_quantity": item.get("ask_quantity") or 0,
                        "source": OPTION_CHAIN_SOURCE,
                        "exchange_timestamp": item["observed_at"].isoformat(),
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    }
                    pipeline.setex(f"nivesh:depth:{int(item['provider_token'])}", 300, json.dumps(payload))
                    depth_cached += 1
            if depth_cached:
                pipeline.execute()
        return {
            "snapshots": len(snapshots),
            "matched_snapshots": len(payloads) if "payloads" in locals() else 0,
            "bars_updated": int(updated or 0),
            "depth_cached": depth_cached,
            "oi_rows": sum(1 for item in snapshots if item.get("open_interest") is not None),
        }

    def record_event(self, level: str, message: str, payload: Dict) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES('upstox_option_chain_oi',:level,:message,CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
            """), {"level": level, "message": message, "payload": json.dumps(payload, default=str)})

    def run(self, *, limit: int = 8, underlyings: Iterable[str] = ()) -> Dict:
        requests_ = self.select_requests(limit=limit, underlyings=underlyings)
        report = {
            "status": "success",
            "requested_limit": limit,
            "underlyings": [item.underlying_symbol for item in requests_],
            "chains_requested": len(requests_),
            "snapshots": 0,
            "matched_snapshots": 0,
            "oi_rows": 0,
            "bars_updated": 0,
            "depth_cached": 0,
            "failures": [],
            "orders_allowed": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.record_event("INFO", "Upstox option-chain OI sync started", {k: v for k, v in report.items() if k != "failures"})
        observed_at = datetime.now(timezone.utc)
        for request in requests_:
            try:
                chain = self.fetch_chain(request)
                snapshots = self._parse_chain_rows(request, chain, observed_at)
                saved = self.save_snapshots(snapshots)
                for key in ("snapshots", "matched_snapshots", "oi_rows", "bars_updated", "depth_cached"):
                    report[key] += int(saved.get(key) or 0)
                time.sleep(self.sleep_seconds)
            except Exception as exc:
                failure = {"underlying": request.underlying_symbol, "provider_key": request.provider_key, "expiry": request.expiry.isoformat(), "error": str(exc)[:300]}
                report["failures"].append(failure)
                self.record_event("ERROR", "Upstox option-chain OI sync failed for underlying", failure)
        report["status"] = "partial" if report["failures"] else "success"
        self.record_event("INFO" if not report["failures"] else "WARNING", "Upstox option-chain OI sync completed", report)
        return report


def run_option_chain_oi_sync(database_url: str, redis_url: str = "", **kwargs) -> Dict:
    return UpstoxOptionChainSync(
        database_url,
        redis_url=redis_url,
        sleep_seconds=float(os.getenv("NIVESH_UPSTOX_OPTION_CHAIN_SLEEP_SECONDS", "0.35")),
    ).run(**kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Upstox option-chain OI snapshots into PostgreSQL and Redis depth cache.")
    parser.add_argument("--limit", type=int, default=int(os.getenv("NIVESH_UPSTOX_OPTION_CHAIN_LIMIT", "8")))
    parser.add_argument("--underlyings", default=os.getenv("NIVESH_UPSTOX_OPTION_CHAIN_UNDERLYINGS", ""),
                        help="Optional comma-separated list, e.g. 'NIFTY 50,BANKNIFTY,SENSEX,RELIANCE'")
    args = parser.parse_args()
    underlyings = [item.strip() for item in args.underlyings.split(",") if item.strip()]
    report = run_option_chain_oi_sync(os.environ["DATABASE_URL"], os.getenv("REDIS_URL", ""), limit=args.limit, underlyings=underlyings)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
