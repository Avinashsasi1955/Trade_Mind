import math
from datetime import date
from typing import Dict, List


BASE_MARKET = [
    ("RELIANCE", "Reliance Industries", 2993.20, 1.53, 63, 1.8, 1.2, 0.9),
    ("HDFCBANK", "HDFC Bank", 1672.70, -0.56, 44, 1.1, -0.4, -0.3),
    ("INFY", "Infosys", 1526.30, 2.60, 67, 1.7, 1.6, 1.1),
    ("LT", "Larsen & Toubro", 3612.10, 0.77, 58, 1.3, 0.8, 0.5),
    ("SUNPHARMA", "Sun Pharma", 1518.75, 1.49, 61, 1.4, 1.0, 0.8),
    ("MARUTI", "Maruti Suzuki", 12218.50, -0.98, 41, 0.9, -0.7, -0.6),
    ("BEL", "Bharat Electronics", 314.80, 3.84, 71, 2.4, 2.8, 1.9),
    ("TRENT", "Trent Ltd", 5462.15, 2.18, 66, 1.9, 2.2, 1.4),
    ("COALINDIA", "Coal India", 487.65, 1.42, 59, 1.8, 1.7, 0.9),
    ("ICICIBANK", "ICICI Bank", 1228.40, 0.96, 57, 1.5, 1.1, 0.7),
    ("SBIN", "State Bank of India", 842.30, 1.10, 62, 1.6, 1.4, 0.8),
    ("TCS", "Tata Consultancy Services", 3982.70, 0.74, 55, 1.2, 0.6, 0.5),
    ("BHARTIARTL", "Bharti Airtel", 1484.20, 1.66, 64, 1.7, 1.5, 1.0),
    ("AXISBANK", "Axis Bank", 1260.15, -0.24, 46, 1.0, -0.1, -0.2),
    ("TATAMOTORS", "Tata Motors", 1024.50, 2.12, 72, 2.0, 1.8, 1.2),
]

INDICES = [
    {"symbol": "NIFTY 50", "price": 24835.40, "change_pct": 0.62},
    {"symbol": "SENSEX", "price": 81354.92, "change_pct": 0.48},
    {"symbol": "BANKNIFTY", "price": 56124.15, "change_pct": -0.17},
    {"symbol": "INDIA VIX", "price": 13.72, "change_pct": -2.04},
]


def market_snapshot(run_number: int = 0) -> List[Dict]:
    """Deterministic NSE-like data for paper trading; replace with ZerodhaMarketDataAdapter later."""
    phase = date.today().toordinal() + run_number
    rows = []
    for idx, row in enumerate(BASE_MARKET):
        symbol, name, base, change, rsi, volume_ratio, breakout_pct, vwap_pct = row
        drift = math.sin((phase + idx * 3) * 0.37) * 0.0015
        rows.append({
            "symbol": symbol, "name": name, "price": round(base * (1 + drift), 2),
            "change_pct": round(change + drift * 100, 2), "rsi": round(rsi + drift * 80, 1),
            "volume_ratio": round(volume_ratio + abs(drift) * 15, 2),
            "breakout_pct": round(breakout_pct + drift * 50, 2),
            "vwap_pct": round(vwap_pct + drift * 40, 2),
            "volatility": round(1.1 + (idx % 5) * 0.25, 2),
        })
    return rows


def price_map(run_number: int = 0) -> Dict[str, float]:
    return {item["symbol"]: item["price"] for item in market_snapshot(run_number)}
