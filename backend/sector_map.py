"""Small deterministic NSE/BSE sector mapper for paper-risk concentration.

The broker/instrument master does not always carry an industry field, especially
for derivatives.  This helper gives the live-paper selector a conservative
sector bucket so it can avoid opening too many trades in one theme at once.
Unknown symbols deliberately fall into ``OTHER`` rather than being fabricated.
"""
from __future__ import annotations

from typing import Dict


SECTOR_SYMBOLS: Dict[str, set[str]] = {
    "IT": {
        "INFY", "TCS", "HCLTECH", "WIPRO", "TECHM", "LTIM", "LTTS", "MPHASIS",
        "COFORGE", "PERSISTENT", "OFSS", "TATAELXSI",
    },
    "BANKING": {
        "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK",
        "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB", "AUBANK", "BANDHANBNK",
    },
    "FINANCIALS": {
        "BAJFINANCE", "BAJAJFINSV", "SBILIFE", "HDFCLIFE", "ICICIPRULI", "LICI",
        "CHOLAFIN", "MUTHOOTFIN", "SHRIRAMFIN", "JIOFIN", "RECLTD", "PFC",
    },
    "ENERGY": {
        "RELIANCE", "ONGC", "IOC", "BPCL", "HINDPETRO", "GAIL", "OIL", "PETRONET",
        "NTPC", "POWERGRID", "TATAPOWER", "ADANIGREEN", "ADANIPOWER", "JSWENERGY",
    },
    "AUTO": {
        "MARUTI", "M&M", "TATAMOTORS", "EICHERMOT", "BAJAJ-AUTO", "HEROMOTOCO",
        "TVSMOTOR", "ASHOKLEY", "BALKRISIND", "MRF", "APOLLOTYRE", "FORCEMOT",
    },
    "PHARMA": {
        "SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "LUPIN", "AUROPHARMA",
        "TORNTPHARM", "ZYDUSLIFE", "BIOCON", "ALKEM", "GLAND",
    },
    "FMCG": {
        "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO",
        "GODREJCP", "COLPAL", "TATACONSUM", "UBL", "VBL",
    },
    "METALS": {
        "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "NATIONALUM", "SAIL",
        "JINDALSTEL", "NMDC", "HINDCOPPER",
    },
    "INFRA": {
        "LT", "ADANIENT", "ADANIPORTS", "GRASIM", "ULTRACEMCO", "AMBUJACEM",
        "ACC", "DLF", "LODHA", "IRCTC", "IRFC", "RVNL", "GMRINFRA",
    },
    "CONSUMER": {
        "DMART", "TRENT", "TITAN", "NYKAA", "JUBLFOOD", "INDHOTEL",
        "ZOMATO", "DELHIVERY", "NAUKRI",
    },
    "TELECOM": {"BHARTIARTL", "IDEA", "INDUSTOWER", "TATACOMM"},
    "INDEX": {"NIFTY", "NIFTY50", "NIFTY 50", "BANKNIFTY", "BANK NIFTY", "SENSEX", "INDIA VIX"},
}


def sector_for_symbol(symbol: str, instrument_type: str = "") -> str:
    cleaned = str(symbol or "").upper().strip()
    compact = cleaned.replace(" ", "")
    if str(instrument_type or "").upper() == "INDEX":
        return "INDEX"
    for sector, symbols in SECTOR_SYMBOLS.items():
        if cleaned in symbols or compact in symbols:
            return sector
    return "OTHER"
