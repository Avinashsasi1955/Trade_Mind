import json
from functools import lru_cache
from pathlib import Path
from typing import Dict

from .config import ROOT


MASTER_PATH = ROOT / "data" / "security_master" / "security_master.json"

INDEX_SECURITIES = [
    {"exchange": "NSE", "symbol": "NIFTY 50", "code": "NIFTY50", "name": "Nifty 50 Index", "series": "INDEX", "isin": "", "status": "ACTIVE"},
    {"exchange": "NSE", "symbol": "BANKNIFTY", "code": "BANKNIFTY", "name": "Nifty Bank Index", "series": "INDEX", "isin": "", "status": "ACTIVE"},
    {"exchange": "NSE", "symbol": "INDIA VIX", "code": "INDIAVIX", "name": "India VIX Index", "series": "INDEX", "isin": "", "status": "ACTIVE"},
    {"exchange": "BSE", "symbol": "SENSEX", "code": "SENSEX", "name": "BSE Sensex Index", "series": "INDEX", "isin": "", "status": "ACTIVE"},
]


@lru_cache(maxsize=1)
def load_security_master() -> Dict:
    if not MASTER_PATH.is_file():
        return {"as_of": None, "sources": {}, "counts": {"NSE": 0, "BSE": 0, "total": 0}, "securities": []}
    return json.loads(MASTER_PATH.read_text(encoding="utf-8"))


def security_master_stats() -> Dict:
    master = load_security_master()
    return {"as_of": master["as_of"], "sources": master["sources"], "counts": master["counts"]}


def search_securities(query: str = "", exchange: str = "ALL", limit: int = 50, offset: int = 0) -> Dict:
    master = load_security_master()
    query = query.strip().upper()
    exchange = exchange.upper() if exchange.upper() in {"NSE", "BSE"} else "ALL"
    securities = INDEX_SECURITIES + master["securities"]
    filtered = [item for item in securities if
                (exchange == "ALL" or item["exchange"] == exchange) and
                (not query or query in item["symbol"].upper() or query in item["code"] or query in item["name"].upper() or query in item.get("isin", "").upper())]
    limit = min(100, max(1, limit))
    offset = max(0, offset)
    return {"items": filtered[offset:offset + limit], "total": len(filtered), "offset": offset,
            "limit": limit, "exchange": exchange, "query": query, "master": security_master_stats(),
            "note": "Security-master inclusion does not mean live quote or derivatives eligibility. The algorithm trades only instruments that pass data, liquidity, risk, and broker checks."}


def resolve_security(symbol: str, exchange: str = ""):
    raw = symbol.strip().upper()
    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        if prefix in {"NSE", "BSE", "NFO", "BFO"}:
            exchange = prefix
            raw = rest.strip()
    
    # Common symbol aliases & index mappings
    alias_map = {
        "NIFTY": "NIFTY 50",
        "NIFTY50": "NIFTY 50",
        "BANK NIFTY": "BANKNIFTY",
        "NIFTY BANK": "BANKNIFTY",
        "INDIAVIX": "INDIA VIX",
        "VIX": "INDIA VIX",
    }
    target_symbol = alias_map.get(raw, raw)

    items = INDEX_SECURITIES + load_security_master()["securities"]
    matches = [
        item for item in items
        if (item["symbol"].upper() == target_symbol or item["code"] == target_symbol or item["symbol"].upper() == raw or item["code"] == raw)
        and (not exchange or item["exchange"] == exchange)
    ]
    if not matches:
        return None
    return next((item for item in matches if item["exchange"] == "NSE"), matches[0])
