"""Real-time Dealer Gamma Exposure (GEX) & Zero-Gamma Flip Point Calculator.

Phase C Institutional Implementation:
1. Computes strike-by-strike and aggregate dealer Gamma Exposure (GEX).
   - Call GEX = + Gamma * Call_OI * Spot^2 * 0.01
   - Put GEX  = - Gamma * Put_OI  * Spot^2 * 0.01
   - Net GEX  = Call GEX + Put GEX
2. Zero-Gamma Flip Point:
   - The strike price where cumulative net dealer gamma flips sign.
   - When Spot < Zero Gamma Flip Point:
     Dealers must sell into drops and buy into rallies, expanding volatility.
     Action: Widen stops by 1.5x and reduce position sizes by 50%.
3. Scoped to Nifty, BankNifty, and Top 5 heavyweights (Reliance, HDFC Bank, ICICI Bank, Infy, TCS).
4. Refresh cadence: Cached with 3-minute TTL to prevent API rate-limiting.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

from .config import DATABASE_URL

logger = logging.getLogger("nivesh.gex_engine")

TOP_HEAVYWEIGHTS = ("NIFTY", "BANKNIFTY", "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS")
LOT_SIZES: Dict[str, int] = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "RELIANCE": 250,
    "HDFCBANK": 550,
    "ICICIBANK": 700,
    "INFY": 400,
    "TCS": 175,
}

FALLBACK_SPOTS: Dict[str, float] = {
    "NIFTY": 24500.0,
    "NIFTY 50": 24500.0,
    "NIFTY50": 24500.0,
    "BANKNIFTY": 52000.0,
    "NIFTY BANK": 52000.0,
    "BANK NIFTY": 52000.0,
    "FINNIFTY": 23500.0,
    "MIDCPNIFTY": 12500.0,
    "RELIANCE": 2950.0,
    "HDFCBANK": 1650.0,
    "ICICIBANK": 1250.0,
    "INFY": 1850.0,
    "TCS": 4250.0,
}

FALLBACK_STEPS: Dict[str, float] = {
    "NIFTY": 50.0,
    "NIFTY 50": 50.0,
    "NIFTY50": 50.0,
    "BANKNIFTY": 100.0,
    "NIFTY BANK": 100.0,
    "BANK NIFTY": 100.0,
    "FINNIFTY": 50.0,
    "MIDCPNIFTY": 25.0,
    "RELIANCE": 20.0,
    "HDFCBANK": 10.0,
    "ICICIBANK": 10.0,
    "INFY": 20.0,
    "TCS": 50.0,
}

_GEX_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_MODULE_ENGINE = None
CACHE_TTL_SECONDS = 180.0  # 3 minutes


@dataclass
class StrikeGEX:
    strike: float
    call_oi: int
    put_oi: int
    call_gamma: float
    put_gamma: float
    call_gex: float
    put_gex: float
    net_gex: float


@dataclass
class GEXProfile:
    symbol: str
    spot_price: float
    total_call_gex: float
    total_put_gex: float
    net_gex: float
    regime: str  # LONG_GAMMA, SHORT_GAMMA, VOLATILITY_FLIP_DEFENSE
    stop_multiplier: float  # 1.0x normal, 1.5x when below flip point
    size_multiplier: float  # 1.0x normal, 0.5x when below flip point
    max_pain_strike: float
    zero_gamma_flip: Optional[float] = None
    strikes: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "spot_price": round(self.spot_price, 2),
            "total_call_gex": round(self.total_call_gex, 2),
            "total_put_gex": round(self.total_put_gex, 2),
            "net_gex": round(self.net_gex, 2),
            "zero_gamma_flip": round(self.zero_gamma_flip, 2) if self.zero_gamma_flip is not None else None,
            "regime": self.regime,
            "stop_multiplier": round(self.stop_multiplier, 2),
            "size_multiplier": round(self.size_multiplier, 2),
            "max_pain_strike": round(self.max_pain_strike, 2),
            "strikes_count": len(self.strikes),
            "timestamp": self.timestamp,
        }


def compute_black_scholes_gamma(
    spot: float, strike: float, dte_days: float, iv: float = 0.15, r: float = 0.07
) -> float:
    """Calculate option Black-Scholes gamma."""
    if spot <= 0 or strike <= 0 or dte_days <= 0 or iv <= 0:
        return 0.0
    t = max(dte_days / 365.0, 1e-4)
    vt = iv * math.sqrt(t)
    if vt <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / vt
    pdf = (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * d1 * d1)
    return pdf / (spot * vt)


def calculate_gex_profile(
    symbol: str,
    spot_price: float,
    options_data: List[Dict[str, Any]],
    lot_size: Optional[int] = None,
) -> GEXProfile:
    """Calculate complete GEX profile, Net GEX, and Zero Gamma Flip Point.

    options_data expects a list of dictionaries with:
      {'strike': float, 'call_oi': int, 'put_oi': int, 'call_iv': float, 'put_iv': float, 'dte': float}
    """
    sym = symbol.upper()
    resolved_lot = lot_size or LOT_SIZES.get(sym, 25)

    strike_records: List[StrikeGEX] = []
    total_call_gex = 0.0
    total_put_gex = 0.0

    for opt in options_data:
        strike = float(opt.get("strike", 0.0))
        if strike <= 0:
            continue
        call_oi = int(opt.get("call_oi", 0) or 0)
        put_oi = int(opt.get("put_oi", 0) or 0)
        dte = float(opt.get("dte", 1.0) or 1.0)
        call_iv = float(opt.get("call_iv", 0.15) or 0.15)
        put_iv = float(opt.get("put_iv", 0.15) or 0.15)

        # Gamma is identical for call & put at same strike under Black-Scholes,
        # but IV skew can make call_gamma slightly different from put_gamma.
        cg = float(opt.get("call_gamma") or compute_black_scholes_gamma(spot_price, strike, dte, call_iv))
        pg = float(opt.get("put_gamma") or compute_black_scholes_gamma(spot_price, strike, dte, put_iv))

        # GEX formula (in ₹ Crore / spot units):
        # Call GEX is positive dealer gamma; Put GEX is negative dealer gamma
        call_gex = cg * call_oi * resolved_lot * (spot_price ** 2) * 0.01 / 1e7
        put_gex = -pg * put_oi * resolved_lot * (spot_price ** 2) * 0.01 / 1e7
        net_gex = call_gex + put_gex

        total_call_gex += call_gex
        total_put_gex += put_gex

        strike_records.append(
            StrikeGEX(
                strike=strike,
                call_oi=call_oi,
                put_oi=put_oi,
                call_gamma=cg,
                put_gamma=pg,
                call_gex=call_gex,
                put_gex=put_gex,
                net_gex=net_gex,
            )
        )

    # Sort strikes ascending
    strike_records.sort(key=lambda x: x.strike)
    net_gex_total = total_call_gex + total_put_gex

    # Find Zero Gamma Flip Strike: where cumulative net gamma transitions
    flip_strike = spot_price
    if strike_records:
        cum_gex = 0.0
        prev_cum = 0.0
        found_flip = False
        flip_strike = None

        flip_direction = None
        for rec in strike_records:
            cum_gex += rec.net_gex
            if prev_cum < 0 <= cum_gex:
                flip_strike = rec.strike
                flip_direction = "NEG_TO_POS"
                found_flip = True
                break
            elif prev_cum > 0 >= cum_gex:
                flip_strike = rec.strike
                flip_direction = "POS_TO_NEG"
                found_flip = True
                break
            prev_cum = cum_gex

        if not found_flip:
            flip_strike = None
            flip_direction = None

    # Max Pain calculation: strike that minimizes total intrinsic payoff to option holders
    # (Call intrinsic: (k - strike) * call_oi when k > strike; Put intrinsic: (strike - k) * put_oi when strike > k)
    max_pain_strike = spot_price
    if strike_records:
        min_loss = float("inf")
        for candidate in strike_records:
            k = candidate.strike
            loss = sum(
                max(0.0, k - s.strike) * s.call_oi + max(0.0, s.strike - k) * s.put_oi
                for s in strike_records
            )
            if loss < min_loss:
                min_loss = loss
                max_pain_strike = k

    # Regimes & Risk Multipliers
    in_negative_flip_side = False
    if flip_strike is not None:
        if flip_direction == "NEG_TO_POS" and spot_price < flip_strike:
            in_negative_flip_side = True
        elif flip_direction == "POS_TO_NEG" and spot_price > flip_strike:
            in_negative_flip_side = True

    if in_negative_flip_side:
        regime = "VOLATILITY_FLIP_DEFENSE"
        stop_multiplier = 1.5
        size_multiplier = 0.5
    elif net_gex_total >= 0:
        regime = "LONG_GAMMA_MEAN_REVERSION"
        stop_multiplier = 1.0
        size_multiplier = 1.0
    else:
        regime = "SHORT_GAMMA_VOLATILITY_EXPANSION"
        stop_multiplier = 1.25
        size_multiplier = 0.75

    return GEXProfile(
        symbol=sym,
        spot_price=spot_price,
        total_call_gex=total_call_gex,
        total_put_gex=total_put_gex,
        net_gex=net_gex_total,
        zero_gamma_flip=flip_strike,
        regime=regime,
        stop_multiplier=stop_multiplier,
        size_multiplier=size_multiplier,
        max_pain_strike=max_pain_strike,
        strikes=[
            {
                "strike": s.strike,
                "call_oi": s.call_oi,
                "put_oi": s.put_oi,
                "call_gex": round(s.call_gex, 4),
                "put_gex": round(s.put_gex, 4),
                "net_gex": round(s.net_gex, 4),
            }
            for s in strike_records
        ],
    )


def get_cached_or_compute_gex(
    symbol: str = "NIFTY",
    database_url: Optional[str] = None,
    engine: Optional[Any] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Retrieve 3-minute cached GEX profile or compute from database/synthetic strikes."""
    sym = symbol.upper()
    now = time.time()

    is_default_target = (database_url is None)
    if is_default_target and not force_refresh and sym in _GEX_CACHE:
        cache_time, cached_data = _GEX_CACHE[sym]
        if now - cache_time < CACHE_TTL_SECONDS:
            if cached_data.get("has_live_spot"):
                s_raw = cached_data.get("spot_bar_time")
                is_fresh = False
                if s_raw:
                    try:
                        if isinstance(s_raw, str):
                            s_dt = datetime.fromisoformat(s_raw.replace("Z", "+00:00"))
                        elif hasattr(s_raw, "timestamp"):
                            s_dt = s_raw
                        else:
                            s_dt = None
                        if s_dt is not None:
                            if not s_dt.tzinfo:
                                s_dt = s_dt.replace(tzinfo=timezone.utc)
                            if abs((datetime.now(timezone.utc) - s_dt).total_seconds()) <= 900:
                                is_fresh = True
                    except Exception:
                        is_fresh = False
                if is_fresh:
                    return cached_data
                downgraded = dict(cached_data)
                downgraded["has_live_spot"] = False
                downgraded["regime"] = "UNAVAILABLE"
                downgraded["zero_gamma_flip"] = None
                downgraded["stop_multiplier"] = 1.0
                downgraded["size_multiplier"] = 1.0
                _GEX_CACHE[sym] = (now, downgraded)
                return downgraded
            else:
                return cached_data

    owned_engine = None
    if database_url:
        if engine is not None:
            eng = engine
        else:
            eng = create_engine(database_url)
            owned_engine = eng
    else:
        global _MODULE_ENGINE
        if engine is not None:
            eng = engine
        else:
            if _MODULE_ENGINE is None and DATABASE_URL:
                _MODULE_ENGINE = create_engine(DATABASE_URL)
            eng = _MODULE_ENGINE

    spot = FALLBACK_SPOTS.get(sym, 1000.0)
    has_live_spot = False
    spot_bar_time = None
    options_rows: List[Dict[str, Any]] = []

    if eng:
        try:
            with eng.connect() as conn:
                # 1. Fetch latest spot price strictly for INDEX or EQ instruments
                symbols_to_match = [sym]
                if sym == "NIFTY":
                    symbols_to_match.extend(["NIFTY 50", "NIFTY50"])
                elif sym == "BANKNIFTY":
                    symbols_to_match.extend(["NIFTY BANK", "BANK NIFTY", "NIFTYBANK"])

                since = datetime.now(timezone.utc) - timedelta(days=1)
                spot_row = conn.execute(
                    text("""
                    SELECT b.close_price, b.bar_time FROM live_market_bars b
                    JOIN instrument_master i ON i.id = b.instrument_id
                    WHERE (i.symbol = ANY(:syms) OR i.underlying_symbol = ANY(:syms))
                      AND i.instrument_type IN ('INDEX', 'EQ')
                      AND b.interval IN ('1minute', '5minute')
                      AND b.bar_time >= :since
                    ORDER BY b.bar_time DESC LIMIT 1
                    """),
                    {"syms": symbols_to_match, "since": since},
                ).fetchone()
                if spot_row and spot_row[0] and float(spot_row[0]) > 0.0:
                    cand_spot = float(spot_row[0])
                    cand_time = spot_row[1]
                    is_fresh = False
                    if cand_time:
                        try:
                            s_dt = cand_time if hasattr(cand_time, "timestamp") else datetime.fromisoformat(str(cand_time).replace("Z", "+00:00"))
                            if not s_dt.tzinfo:
                                s_dt = s_dt.replace(tzinfo=timezone.utc)
                            if abs((datetime.now(timezone.utc) - s_dt).total_seconds()) <= 900:
                                is_fresh = True
                        except Exception:
                            is_fresh = False
                    if is_fresh:
                        spot = cand_spot
                        spot_bar_time = cand_time
                        has_live_spot = True

                # 2. Fetch option chain strikes & OI from database (prefer nearest expiry)
                db_opts = []
                try:
                    with conn.begin_nested():
                        db_opts = conn.execute(
                            text("""
                            WITH nearest_exp AS (
                                SELECT MIN(expiry) as exp
                                FROM option_chain_oi_snapshots
                                WHERE (underlying_symbol = :sym OR symbol = :sym)
                                  AND expiry >= CURRENT_DATE
                                  AND observed_at >= :since
                            ),
                            latest_obs AS (
                                SELECT DISTINCT ON (strike, option_type)
                                       strike, option_type, open_interest, implied_volatility, expiry
                                FROM option_chain_oi_snapshots, nearest_exp
                                WHERE (underlying_symbol = :sym OR symbol = :sym)
                                  AND expiry = nearest_exp.exp
                                  AND observed_at >= :since
                                ORDER BY strike, option_type, observed_at DESC
                            )
                            SELECT strike,
                                   SUM(CASE WHEN CAST(option_type AS text) = 'CE' THEN open_interest ELSE 0 END) as call_oi,
                                   SUM(CASE WHEN CAST(option_type AS text) = 'PE' THEN open_interest ELSE 0 END) as put_oi,
                                   AVG(implied_volatility) as avg_iv,
                                   MAX(expiry) as expiry
                            FROM latest_obs
                            GROUP BY strike
                            HAVING SUM(open_interest) > 0
                            ORDER BY strike ASC
                            """),
                            {"sym": sym, "since": since},
                        ).fetchall()
                except Exception as exc:
                    logger.info("option_chain_oi_snapshots query skipped: %s", exc)
                    db_opts = []

                if not db_opts:
                    try:
                        with conn.begin_nested():
                            db_opts = conn.execute(
                                text("""
                                WITH nearest_exp AS (
                                    SELECT MIN(expiry) as exp
                                    FROM instrument_master
                                    WHERE (underlying_symbol = :sym OR symbol = :sym)
                                      AND instrument_type IN ('CE', 'PE')
                                      AND expiry >= CURRENT_DATE
                                ),
                                latest_bars AS (
                                    SELECT DISTINCT ON (i.id)
                                           i.strike, i.instrument_type, b.open_interest, i.expiry
                                    FROM instrument_master i
                                    JOIN live_market_bars b ON b.instrument_id = i.id
                                    JOIN nearest_exp n ON i.expiry = n.exp
                                    WHERE (i.underlying_symbol = :sym OR i.symbol = :sym)
                                      AND i.instrument_type IN ('CE', 'PE')
                                      AND b.bar_time >= :since
                                    ORDER BY i.id, b.bar_time DESC
                                )
                                SELECT strike,
                                       SUM(CASE WHEN instrument_type = 'CE' THEN open_interest ELSE 0 END) as call_oi,
                                       SUM(CASE WHEN instrument_type = 'PE' THEN open_interest ELSE 0 END) as put_oi,
                                       0.15 as avg_iv,
                                       MAX(expiry) as expiry
                                FROM latest_bars
                                GROUP BY strike
                                HAVING SUM(open_interest) > 0
                                ORDER BY strike ASC
                                """),
                                {"sym": sym, "since": since},
                            ).fetchall()
                    except Exception:
                        db_opts = []

                for row in db_opts:
                    if row[0]:
                        exp_val = row[4] if len(row) > 4 else None
                        dte = 2.0
                        if exp_val:
                            try:
                                if isinstance(exp_val, str):
                                    exp_d = datetime.strptime(str(exp_val)[:10], "%Y-%m-%d").date()
                                else:
                                    exp_d = exp_val if hasattr(exp_val, "strftime") else datetime.now(timezone.utc).date()
                                dte = max(0.5, float((exp_d - datetime.now(timezone.utc).date()).days))
                            except Exception:
                                dte = 2.0

                        options_rows.append({
                            "strike": float(row[0]),
                            "call_oi": int(row[1] or 0),
                            "put_oi": int(row[2] or 0),
                            "call_iv": float(row[3] or 0.15) if row[3] else 0.15,
                            "put_iv": float(row[3] or 0.15) if row[3] else 0.15,
                            "dte": dte,
                        })
        except Exception as exc:
            logger.warning("Database GEX query failed, using synthetic options ladder: %s", exc)
        finally:
            if owned_engine is not None:
                try:
                    owned_engine.dispose()
                except Exception:
                    pass

    source = "db"
    # If DB has no option bars yet, generate standard ATM +/- 10 strike ladder
    if not options_rows:
        source = "synthetic"
        step = FALLBACK_STEPS.get(sym, 20.0)
        atm = round(spot / step) * step
        for i in range(-10, 11):
            k = atm + i * step
            # Distance from ATM drives synthetic OI bell-curve
            dist = abs(i)
            call_oi = max(5000, int(150000 * math.exp(-0.25 * (dist ** 1.5))))
            put_oi = max(5000, int(140000 * math.exp(-0.22 * (dist ** 1.5))))
            options_rows.append({
                "strike": k,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "call_iv": 0.14 + 0.005 * abs(i),
                "put_iv": 0.15 + 0.006 * abs(i),
                "dte": 2.0,
            })

    profile = calculate_gex_profile(sym, spot, options_rows)
    result = profile.to_dict()
    result["source"] = source
    result["has_live_spot"] = has_live_spot
    result["spot_bar_time"] = spot_bar_time.isoformat() if hasattr(spot_bar_time, "isoformat") else (str(spot_bar_time) if spot_bar_time else None)
    if source == "synthetic" or not has_live_spot:
        result["stop_multiplier"] = 1.0
        result["size_multiplier"] = 1.0
        result["regime"] = "UNAVAILABLE"
        if not has_live_spot:
            result["zero_gamma_flip"] = None
    if is_default_target:
        _GEX_CACHE[sym] = (now, result)
    return result
