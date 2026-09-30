"""Put-Call Ratio (PCR) and Root Market Analytics Engine.

Calculates Total PCR, Volume PCR, ATM ±3 strikes PCR, and Max Pain
to identify institutional accumulation, distribution, and option traps.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from sqlalchemy import text


def calculate_pcr(
    chain_rows: List[Dict[str, Any]],
    spot_price: float,
    strike_step: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute comprehensive PCR metrics from option chain records."""
    if not chain_rows or spot_price <= 0:
        return {
            "total_pcr": 1.0,
            "volume_pcr": 1.0,
            "atm_pcr": 1.0,
            "max_pain": spot_price,
            "bias": "NEUTRAL",
            "total_call_oi": 0,
            "total_put_oi": 0,
            "total_call_volume": 0,
            "total_put_volume": 0,
            "atm_strikes": [],
        }

    strikes = sorted({float(r["strike"]) for r in chain_rows if r.get("strike")})
    if not strikes:
        return {"total_pcr": 1.0, "atm_pcr": 1.0, "bias": "NEUTRAL"}

    # Find closest ATM strike
    atm_strike = min(strikes, key=lambda s: abs(s - spot_price))
    atm_idx = strikes.index(atm_strike)
    start_idx = max(0, atm_idx - 3)
    end_idx = min(len(strikes), atm_idx + 4)
    atm_strike_set = set(strikes[start_idx:end_idx])

    total_call_oi = 0
    total_put_oi = 0
    total_call_vol = 0
    total_put_vol = 0

    atm_call_oi = 0
    atm_put_oi = 0

    oi_by_strike = {}

    for row in chain_rows:
        strike = float(row.get("strike") or 0)
        otype = str(row.get("option_type") or "").upper()
        oi = int(row.get("open_interest") or 0)
        vol = int(row.get("volume") or 0)

        if strike not in oi_by_strike:
            oi_by_strike[strike] = {"CE": 0, "PE": 0}

        if otype in {"CE", "CALL"}:
            total_call_oi += oi
            total_call_vol += vol
            oi_by_strike[strike]["CE"] += oi
            if strike in atm_strike_set:
                atm_call_oi += oi
        elif otype in {"PE", "PUT"}:
            total_put_oi += oi
            total_put_vol += vol
            oi_by_strike[strike]["PE"] += oi
            if strike in atm_strike_set:
                atm_put_oi += oi

    total_pcr = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else 1.0
    volume_pcr = round(total_put_vol / total_call_vol, 4) if total_call_vol > 0 else 1.0
    atm_pcr = round(atm_put_oi / atm_call_oi, 4) if atm_call_oi > 0 else total_pcr

    # Max Pain calculation: strike minimizing total buyer payout
    pain_loss = {}
    for settle_k in strikes:
        total_payout = 0.0
        for k, ois in oi_by_strike.items():
            if settle_k > k:
                total_payout += (settle_k - k) * ois["CE"]
            elif settle_k < k:
                total_payout += (k - settle_k) * ois["PE"]
        pain_loss[settle_k] = total_payout

    max_pain_strike = min(pain_loss.keys(), key=lambda k: pain_loss[k]) if pain_loss else atm_strike

    # Institutional Bias Classification
    if atm_pcr >= 1.40:
        bias = "EXTREME_BULLISH_OVERSOLD"  # High Put OI, heavy support, squeeze potential
    elif atm_pcr >= 1.05:
        bias = "MODERATE_BULLISH"
    elif atm_pcr >= 0.80:
        bias = "NEUTRAL"
    elif atm_pcr >= 0.55:
        bias = "MODERATE_BEARISH"
    else:
        bias = "EXTREME_BEARISH_OVERBOUGHT"  # Heavy Call OI resistance, dump potential

    return {
        "total_pcr": total_pcr,
        "volume_pcr": volume_pcr,
        "atm_pcr": atm_pcr,
        "atm_strike": atm_strike,
        "max_pain": max_pain_strike,
        "bias": bias,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
        "atm_strikes": sorted(list(atm_strike_set)),
        "spot_price": spot_price,
    }


def get_latest_pcr(connection, underlying_symbol: str, spot_price: Optional[float] = None) -> Dict[str, Any]:
    """Query PostgreSQL for latest option chain snapshot of underlying and return PCR metrics."""
    clean_sym = underlying_symbol.replace(" ", "").upper()
    sym_alias = {"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY", "BSESENSEX": "SENSEX"}.get(clean_sym, clean_sym)

    rows = connection.execute(text("""
        WITH latest_obs AS (
            SELECT MAX(observed_at) as max_time
            FROM option_chain_oi_snapshots
            WHERE REPLACE(underlying_symbol, ' ', '') = :sym OR underlying_symbol = :alias
        )
        SELECT strike, option_type, open_interest, volume, last_price
        FROM option_chain_oi_snapshots s
        JOIN latest_obs o ON s.observed_at = o.max_time
        WHERE REPLACE(s.underlying_symbol, ' ', '') = :sym OR s.underlying_symbol = :alias
    """), {"sym": clean_sym, "alias": sym_alias}).mappings().all()

    if not rows:
        return {"total_pcr": 1.0, "atm_pcr": 1.0, "bias": "NEUTRAL", "no_data": True}

    if spot_price is None or spot_price <= 0:
        # Use median strike as baseline proxy if spot not provided
        strikes = [float(r["strike"]) for r in rows if r.get("strike")]
        spot_price = sorted(strikes)[len(strikes) // 2] if strikes else 100.0

    return calculate_pcr([dict(r) for r in rows], spot_price)
