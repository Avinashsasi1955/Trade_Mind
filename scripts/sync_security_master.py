#!/usr/bin/env python3
"""Sync exchange instrument master to data/security_master/security_master.json."""
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import ROOT as CONFIG_ROOT, UPSTOX_INSTRUMENTS_URL
from backend.security_master import CORE_EQUITY_SECURITIES, MASTER_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_security_master")


def sync_security_master(output_path: Path = MASTER_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    securities = []
    source = "Builtin Core Equities"

    if UPSTOX_INSTRUMENTS_URL:
        try:
            logger.info("Attempting to fetch instruments from Upstox: %s", UPSTOX_INSTRUMENTS_URL)
            req = Request(UPSTOX_INSTRUMENTS_URL, headers={"User-Agent": "TradeMind/1.0"})
            with urlopen(req, timeout=15) as resp:
                data = resp.read()
                if UPSTOX_INSTRUMENTS_URL.endswith(".gz"):
                    data = gzip.decompress(data)
                raw_items = json.loads(data.decode("utf-8"))
                for item in raw_items:
                    exchange = str(item.get("exchange") or "").upper()
                    if exchange in {"NSE_EQ", "NSE"}:
                        securities.append({
                            "exchange": "NSE",
                            "symbol": str(item.get("trading_symbol") or item.get("symbol") or "").upper(),
                            "code": str(item.get("instrument_key") or item.get("trading_symbol") or "").upper(),
                            "name": str(item.get("name") or ""),
                            "series": "EQ",
                            "isin": str(item.get("isin") or ""),
                            "lot": int(item.get("lot_size") or 1),
                            "status": "Active" if item.get("trading_status") != "SUSPENDED" else "Suspended",
                        })
                if securities:
                    source = "Upstox Complete Instruments Feed"
                    logger.info("Successfully parsed %d instruments from Upstox", len(securities))
        except Exception as exc:
            logger.warning("Could not download remote Upstox instruments (%s); falling back to core blue-chips", exc)

    if not securities:
        logger.info("Writing canonical core securities master (%d scrips)", len(CORE_EQUITY_SECURITIES))
        securities = list(CORE_EQUITY_SECURITIES)

    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "sources": {"NSE": source, "BSE": source},
        "counts": {
            "NSE": sum(1 for s in securities if s.get("exchange") == "NSE"),
            "BSE": sum(1 for s in securities if s.get("exchange") == "BSE"),
            "total": len(securities),
        },
        "securities": securities,
    }

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Wrote security master to %s (total: %d)", output_path, len(securities))
    return output_path


if __name__ == "__main__":
    sync_security_master()
