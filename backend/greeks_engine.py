"""Institutional Black-Scholes Greeks & Implied Volatility (IV) Engine.

Calculates real-time analytical Greeks:
- Delta (Δ): Directional exposure (dPrice/dSpot)
- Gamma (Γ): Delta rate of change (dDelta/dSpot)
- Theta (Θ): 1-day calendar time decay in INR (dPrice/dt)
- Vega (ν): 1% Implied Volatility sensitivity (dPrice/dIV)
- Rho (ρ): 1% Interest rate sensitivity (dPrice/dr)
- IV Rank & IV Percentile against historical volatility bounds
"""
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function approximation (Abramowitz & Stegun)."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def calculate_black_scholes_greeks(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float,
    rate: float = 0.065,  # RBI Repo Rate ~ 6.5%
    dividend_yield: float = 0.012,  # NIFTY average dividend yield ~ 1.2%
    option_type: str = "CE",
) -> Dict[str, float]:
    """Computes exact analytical Black-Scholes Greeks for European/Indian options."""
    if spot <= 0 or strike <= 0 or iv <= 0:
        return {"price": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    t = max(0.5 / 365.0, dte_days / 365.0)  # Min half a day to avoid singularity at expiry
    sigma = max(0.01, iv)
    r = rate
    q = dividend_yield

    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)

    pdf_d1 = _norm_pdf(d1)
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)

    is_call = option_type.upper() in ("CE", "CALL", "C")

    if is_call:
        price = spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -(spot * sigma * disc_q * pdf_d1) / (2.0 * math.sqrt(t))
            - r * strike * disc_r * _norm_cdf(d2)
            + q * spot * disc_q * _norm_cdf(d1)
        ) / 365.0
        rho = (strike * t * disc_r * _norm_cdf(d2)) / 100.0
    else:
        price = strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta = (
            -(spot * sigma * disc_q * pdf_d1) / (2.0 * math.sqrt(t))
            + r * strike * disc_r * _norm_cdf(-d2)
            - q * spot * disc_q * _norm_cdf(-d1)
        ) / 365.0
        rho = (-strike * t * disc_r * _norm_cdf(-d2)) / 100.0

    gamma = (disc_q * pdf_d1) / (spot * sigma * math.sqrt(t))
    vega = (spot * disc_q * math.sqrt(t) * pdf_d1) / 100.0  # 1% IV shift

    return {
        "price": round(max(0.05, price), 2),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),  # ₹ decay per day
        "vega": round(vega, 2),    # ₹ change per 1% IV
        "rho": round(rho, 4),
        "intrinsic_value": round(max(0.0, (spot - strike) if is_call else (strike - spot)), 2),
        "extrinsic_value": round(max(0.0, price - (max(0.0, (spot - strike) if is_call else (strike - spot)))), 2),
    }


def option_chain_greeks_surface(
    symbol: str,
    spot: float,
    dte_days: float = 4.0,
    base_iv: float = 0.145,  # 14.5% IV baseline
    strikes_count: int = 7,
    strike_step: Optional[float] = None,
) -> Dict:
    """Generates a complete options matrix with ATM, ITM, OTM strikes, Greeks, and IV skew."""
    if not strike_step:
        strike_step = 50.0 if spot < 5000 else 100.0 if spot < 25000 else 500.0

    atm_strike = round(spot / strike_step) * strike_step
    half = strikes_count // 2
    strikes = [atm_strike + (i - half) * strike_step for i in range(strikes_count)]

    chain = []
    for k in strikes:
        moneyness = (k - spot) / spot
        # Realistic Volatility Smile / Skew model: OTM Puts have higher IV (skew), OTM Calls have smile
        call_iv = base_iv + max(0.0, (k - spot) / spot) * 0.15 + (0.01 if k > spot else 0.0)
        put_iv = base_iv + max(0.0, (spot - k) / spot) * 0.25 + (0.02 if k < spot else 0.0)

        ce_greeks = calculate_black_scholes_greeks(spot, k, dte_days, call_iv, option_type="CE")
        pe_greeks = calculate_black_scholes_greeks(spot, k, dte_days, put_iv, option_type="PE")

        chain.append({
            "strike": k,
            "is_atm": abs(k - atm_strike) < 1e-3,
            "is_itm_call": k < spot,
            "is_itm_put": k > spot,
            "call": {
                **ce_greeks,
                "iv": round(call_iv * 100, 2),
                "iv_rank": round(min(100.0, max(0.0, (call_iv - 0.11) / (0.28 - 0.11) * 100)), 1),
            },
            "put": {
                **pe_greeks,
                "iv": round(put_iv * 100, 2),
                "iv_rank": round(min(100.0, max(0.0, (put_iv - 0.11) / (0.28 - 0.11) * 100)), 1),
            }
        })

    # Summary metrics
    atm_call = next((c["call"] for c in chain if c["is_atm"]), chain[half]["call"])
    atm_put = next((c["put"] for c in chain if c["is_atm"]), chain[half]["put"])
    iv_skew = round(atm_put["iv"] - atm_call["iv"], 2)

    return {
        "symbol": symbol,
        "spot_price": round(spot, 2),
        "atm_strike": atm_strike,
        "dte_days": dte_days,
        "base_iv": round(base_iv * 100, 2),
        "iv_skew_pts": iv_skew,
        "iv_regime": "High IV (Favor Credit Spreads / Selling)" if base_iv > 0.18 else "Low IV (Favor Debit Spreads / Buying)" if base_iv < 0.13 else "Normal IV",
        "chain": chain,
    }


def get_live_or_analytical_greeks(
    engine_or_conn,
    instrument_id: int,
    spot: float,
    strike: float,
    dte_days: float = 4.0,
    option_type: str = "CE",
    fallback_iv: float = 0.145,
) -> Dict[str, Any]:
    """Retrieves real-time Greeks from option_chain_oi_snapshots if fresh (<= 15m).
    Falls back cleanly to analytical Black-Scholes if live data is unavailable.
    """
    if not engine_or_conn or not instrument_id:
        res = calculate_black_scholes_greeks(spot, strike, dte_days, fallback_iv, option_type=option_type)
        res["source"] = "analytical_black_scholes"
        res["implied_volatility"] = fallback_iv
        return res

    try:
        from sqlalchemy import text, create_engine
        conn_context = None
        if hasattr(engine_or_conn, "connect"):
            conn_context = engine_or_conn.connect()
            conn = conn_context
        elif isinstance(engine_or_conn, str):
            conn_context = create_engine(engine_or_conn).connect()
            conn = conn_context
        else:
            conn = engine_or_conn

        try:
            row = conn.execute(text("""
                SELECT delta, gamma, theta, vega, implied_volatility, last_price, observed_at
                FROM option_chain_oi_snapshots
                WHERE instrument_id = :instrument_id
                  AND delta IS NOT NULL
                  AND observed_at >= NOW() - INTERVAL '15 minutes'
                ORDER BY observed_at DESC
                LIMIT 1
            """), {"instrument_id": int(instrument_id)}).mappings().one_or_none()

            if row and row.get("delta") is not None:
                iv = float(row.get("implied_volatility") or fallback_iv)
                return {
                    "price": float(row.get("last_price") or 0.0),
                    "delta": round(float(row["delta"]), 4),
                    "gamma": round(float(row.get("gamma") or 0.0), 6),
                    "theta": round(float(row.get("theta") or 0.0), 2),
                    "vega": round(float(row.get("vega") or 0.0), 2),
                    "rho": 0.0,
                    "implied_volatility": iv,
                    "source": "upstox_v3_depth",
                    "observed_at": row["observed_at"].isoformat() if row.get("observed_at") else None,
                }
        finally:
            if conn_context:
                conn_context.close()
    except Exception:
        pass

    res = calculate_black_scholes_greeks(spot, strike, dte_days, fallback_iv, option_type=option_type)
    res["source"] = "analytical_black_scholes"
    res["implied_volatility"] = fallback_iv
    return res
