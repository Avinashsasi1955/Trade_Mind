"""Professional quant-analysis lenses for the trading stack.

These models are intentionally advisory.  They add evidence for Senior/ML/risk
decisions, but they do not place orders, bypass gates, or promote models.

The first implementation uses robust, dependency-light proxies so the web app
can explain quant context quickly.  Heavier implementations such as full
statsmodels VAR/VECM, HMMs, or GARCH can be added later after the shadow data is
stable and benchmarked.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Sequence, Tuple

from sqlalchemy import create_engine, text

from .config import DATABASE_URL


QUANT_MODEL_REGISTRY: List[Dict] = [
    {
        "id": "var_lead_lag",
        "name": "VAR lead-lag proxy",
        "family": "Vector autoregression",
        "role": "Detects whether index returns are leading or conflicting with stock/sector movement.",
        "status": "advisory_proxy",
        "uses": ["1minute", "5minute"],
    },
    {
        "id": "vecm_cointegration",
        "name": "VECM / cointegration proxy",
        "family": "Cointegrated time-series",
        "role": "Checks if related pairs are stretched away from their normal relationship.",
        "status": "advisory_proxy",
        "uses": ["1minute", "5minute", "day"],
    },
    {
        "id": "stat_arb_pairs",
        "name": "Statistical arbitrage pairs",
        "family": "Mean reversion / spread z-score",
        "role": "Highlights relative-value opportunities such as INFY/TCS or HDFCBANK/ICICIBANK.",
        "status": "advisory_proxy",
        "uses": ["5minute", "day"],
    },
    {
        "id": "regime_hmm_proxy",
        "name": "Market regime classifier",
        "family": "Hidden-Markov-style regime proxy",
        "role": "Labels the session as trend-up, trend-down, range, or volatile before selecting strategies.",
        "status": "advisory_proxy",
        "uses": ["1minute", "5minute"],
    },
    {
        "id": "garch_volatility_proxy",
        "name": "Volatility forecast proxy",
        "family": "GARCH-style volatility clustering",
        "role": "Warns when expected noise is high enough to widen/avoid weak stops.",
        "status": "advisory_proxy",
        "uses": ["1minute", "5minute"],
    },
    {
        "id": "cross_sectional_ranker",
        "name": "Cross-sectional momentum/quality ranker",
        "family": "Cross-sectional ranking",
        "role": "Ranks live instruments by momentum, stability, and liquidity so Senior sees the best candidates first.",
        "status": "implemented_advisory",
        "uses": ["1minute", "5minute"],
    },
    {
        "id": "factor_investing_model",
        "name": "Factor investing model",
        "family": "Momentum / quality / liquidity factors",
        "role": "Scores each live stock/index by factor strength so Senior can prefer cleaner, liquid names instead of random signals.",
        "status": "implemented_advisory",
        "uses": ["5minute", "day"],
    },
    {
        "id": "ml_prediction_bridge",
        "name": "Machine-learning prediction model bridge",
        "family": "Active model + recent shadow predictions",
        "role": "Connects the active ML model, latest probabilities, and strongest recent signals into the quant dashboard.",
        "status": "implemented_context_bridge",
        "uses": ["shadow_predictions", "model_versions"],
    },
    {
        "id": "portfolio_optimization_model",
        "name": "Portfolio optimization model",
        "family": "Risk-weighted allocation / exposure control",
        "role": "Converts factor-ranked candidates into capped paper exposure guidance while avoiding concentration.",
        "status": "implemented_advisory",
        "uses": ["5minute", "risk"],
    },
    {
        "id": "microstructure_1s_gate",
        "name": "1-second microstructure gate",
        "family": "Execution microstructure",
        "role": "Confirms the final entry/exit timing from 1-second bars before paper execution.",
        "status": "confirmation_only",
        "uses": ["1second"],
    },
]


PAIR_UNIVERSE: Tuple[Tuple[str, str], ...] = (
    ("INFY", "TCS"),
    ("HDFCBANK", "ICICIBANK"),
    ("NIFTY 50", "BANKNIFTY"),
    ("RELIANCE", "NIFTY 50"),
    ("SBIN", "BANKNIFTY"),
)


INDEX_PRIORITY = ("NIFTY 50", "BANKNIFTY", "SENSEX", "INDIA VIX")


def _to_float(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _returns(values: Sequence[float]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(values, values[1:]):
        if prev:
            out.append((cur - prev) / prev)
    return out


def _corr(left: Sequence[float], right: Sequence[float]) -> float:
    n = min(len(left), len(right))
    if n < 5:
        return 0.0
    x = list(left[-n:])
    y = list(right[-n:])
    mx, my = mean(x), mean(y)
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(vx * vy)


def _zscore(values: Sequence[float]) -> float:
    if len(values) < 8:
        return 0.0
    sd = pstdev(values)
    if sd <= 0:
        return 0.0
    return (values[-1] - mean(values)) / sd


def _volatility(returns: Sequence[float]) -> float:
    if len(returns) < 5:
        return 0.0
    return pstdev(returns)


def _iso(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _compact_score(value: float) -> float:
    return round(float(value), 4)


def quant_model_status() -> Dict:
    return {
        "status": "available",
        "mode": "advisory_quant_context",
        "orders_allowed": False,
        "models": QUANT_MODEL_REGISTRY,
        "architecture_role": (
            "Quant models produce context for Senior layer, ML inference, risk governor, "
            "AI Copilot, Operations monitoring and Backtest Lab. They do not place orders "
            "or override paper-trading rules."
        ),
        "recommended_use": [
            "Use regime + volatility before choosing strategy family.",
            "Use cross-sectional ranker to shortlist liquid live-feed instruments.",
            "Use factor investing to prefer stronger momentum/quality/liquidity names.",
            "Use ML prediction bridge to compare chart/Senior logic with active model probabilities.",
            "Use portfolio optimization to keep paper exposure diversified and capped.",
            "Use VAR/stat-arb as confirmation or conflict evidence.",
            "Use 1-second microstructure only for final timing until 1s storage is proven stable.",
        ],
    }


def _load_recent_bars(database_url: str, limit: int) -> List[Dict]:
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT i.symbol,
                               i.exchange,
                               i.instrument_type,
                               b.source,
                               b.interval,
                               b.bar_time,
                               b.open_price,
                               b.high_price,
                               b.low_price,
                               b.close_price,
                               COALESCE(b.volume, 0) AS volume,
                               ROW_NUMBER() OVER (
                                   PARTITION BY i.id, b.interval
                                   ORDER BY b.bar_time DESC
                               ) rn
                        FROM live_market_bars b
                        JOIN instrument_master i ON i.id=b.instrument_id
                        WHERE b.interval IN ('1second','1minute','5minute','day')
                          AND b.source IN ('upstox_v3','upstox_rest_intraday','upstox_rest_5m','upstox_rest_history','upstox_rest_1s_proxy','zerodha_kite','kite_gap_backfill')
                          AND i.instrument_type IN ('EQ','INDEX')
                          AND (i.exchange='NSE' OR i.instrument_type='INDEX')
                          AND b.bar_time >= CURRENT_TIMESTAMP - INTERVAL '10 days'
                    )
                    SELECT *
                    FROM ranked
                    WHERE rn <= :limit
                    ORDER BY symbol, interval, bar_time
                    """
                ),
                {"limit": max(20, min(int(limit or 80), 500))},
            ).mappings().all()
            return [dict(row) for row in rows]
    finally:
        engine.dispose()


def _build_series(rows: Iterable[Dict]) -> Dict[Tuple[str, str], List[Dict]]:
    series: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for row in rows:
        series[(str(row.get("symbol") or ""), str(row.get("interval") or ""))].append(row)
    return series


def _close_series(series: Dict[Tuple[str, str], List[Dict]], symbol: str, interval: str) -> List[float]:
    return [_to_float(row.get("close_price")) for row in series.get((symbol, interval), []) if _to_float(row.get("close_price")) > 0]


def _latest_time(series: Dict[Tuple[str, str], List[Dict]], interval: str) -> str | None:
    latest = None
    for (symbol, tf), rows in series.items():
        if tf != interval or not rows:
            continue
        candidate = rows[-1].get("bar_time")
        if candidate and (latest is None or candidate > latest):
            latest = candidate
    return _iso(latest)


def _market_regime(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    closes = _close_series(series, "NIFTY 50", "5minute") or _close_series(series, "NIFTY 50", "1minute")
    if len(closes) < 12:
        return {"label": "insufficient", "confidence": 0, "reason": "Need at least 12 recent NIFTY candles."}
    rets = _returns(closes)
    cumulative = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0
    vol = _volatility(rets)
    up_ratio = sum(1 for x in rets[-12:] if x > 0) / max(1, min(12, len(rets)))
    if vol > 0.004:
        label = "volatile"
    elif cumulative > 0.0025 and up_ratio >= 0.58:
        label = "trend_up"
    elif cumulative < -0.0025 and up_ratio <= 0.42:
        label = "trend_down"
    else:
        label = "range"
    confidence = min(95, 45 + abs(cumulative) * 12000 + abs(up_ratio - 0.5) * 80)
    return {
        "label": label,
        "confidence": round(confidence, 1),
        "cumulative_return_pct": round(cumulative * 100, 3),
        "volatility_bps": round(vol * 10000, 2),
        "up_ratio": round(up_ratio, 2),
        "reason": "NIFTY 5m/1m trend, volatility and candle direction mix.",
    }


def _lead_lag(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    benchmark = _returns(_close_series(series, "NIFTY 50", "1minute"))
    candidates = []
    for (symbol, interval), rows in series.items():
        if interval != "1minute" or symbol == "NIFTY 50":
            continue
        returns = _returns([_to_float(row.get("close_price")) for row in rows])
        corr_now = _corr(benchmark, returns)
        corr_lag = _corr(benchmark[:-1], returns[1:]) if len(benchmark) > 6 and len(returns) > 6 else 0.0
        if abs(corr_now) >= 0.15 or abs(corr_lag) >= 0.15:
            candidates.append({
                "symbol": symbol,
                "corr_with_nifty": _compact_score(corr_now),
                "nifty_lead_corr": _compact_score(corr_lag),
                "signal": "supports_index_flow" if corr_lag > 0.25 else "conflict_or_independent" if corr_lag < -0.25 else "weak",
            })
    candidates.sort(key=lambda item: abs(item["nifty_lead_corr"]), reverse=True)
    return {
        "status": "ready" if len(benchmark) >= 12 else "insufficient",
        "benchmark": "NIFTY 50",
        "top_relationships": candidates[:8],
        "explanation": "VAR proxy: checks whether NIFTY movement is leading stock/index movement.",
    }


def _pairs(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    rows = []
    for left, right in PAIR_UNIVERSE:
        l = _close_series(series, left, "5minute") or _close_series(series, left, "1minute")
        r = _close_series(series, right, "5minute") or _close_series(series, right, "1minute")
        n = min(len(l), len(r))
        if n < 12:
            continue
        l, r = l[-n:], r[-n:]
        spreads = [math.log(a) - math.log(b) for a, b in zip(l, r) if a > 0 and b > 0]
        z = _zscore(spreads)
        corr = _corr(_returns(l), _returns(r))
        rows.append({
            "pair": f"{left}/{right}",
            "correlation": _compact_score(corr),
            "spread_zscore": round(z, 2),
            "state": "stretched" if abs(z) >= 1.5 else "normal",
            "idea": "mean_reversion_watch" if abs(z) >= 1.5 and corr > 0.35 else "no_trade_context",
        })
    return {
        "status": "ready" if rows else "insufficient",
        "pairs": rows,
        "explanation": "VECM/stat-arb proxy: related pairs should not be treated as independent signals.",
    }


def _cross_sectional_rank(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    ranked = []
    for (symbol, interval), rows in series.items():
        if interval != "5minute" or len(rows) < 8:
            continue
        closes = [_to_float(row.get("close_price")) for row in rows if _to_float(row.get("close_price")) > 0]
        if len(closes) < 8:
            continue
        rets = _returns(closes)
        momentum = (closes[-1] - closes[max(0, len(closes) - 8)]) / closes[max(0, len(closes) - 8)] if closes[max(0, len(closes) - 8)] else 0
        vol = _volatility(rets[-12:])
        volume = mean([_to_float(row.get("volume")) for row in rows[-8:]]) if rows else 0.0
        liquidity_bonus = min(20, math.log10(max(volume, 1)))
        score = momentum * 10000 - vol * 4500 + liquidity_bonus
        ranked.append({
            "symbol": symbol,
            "score": round(score, 2),
            "direction": "CALL_bias" if momentum > 0 else "PUT_bias" if momentum < 0 else "neutral",
            "momentum_pct": round(momentum * 100, 3),
            "volatility_bps": round(vol * 10000, 2),
            "liquidity_score": round(liquidity_bonus, 2),
        })
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return {
        "status": "ready" if ranked else "insufficient",
        "top_long_bias": ranked[:8],
        "top_short_bias": sorted(ranked, key=lambda item: item["score"])[:8],
        "explanation": "Ranks live instruments so Senior reviews strongest and weakest structure first.",
    }


def _factor_investing(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    ranked = []
    for (symbol, interval), rows in series.items():
        if interval != "5minute" or len(rows) < 10:
            continue
        closes = [_to_float(row.get("close_price")) for row in rows if _to_float(row.get("close_price")) > 0]
        if len(closes) < 10:
            continue
        returns = _returns(closes)
        recent = closes[-1]
        base_6 = closes[-min(7, len(closes))]
        base_12 = closes[-min(13, len(closes))]
        momentum_6 = (recent - base_6) / base_6 if base_6 else 0
        momentum_12 = (recent - base_12) / base_12 if base_12 else momentum_6
        downside = [abs(item) for item in returns[-12:] if item < 0]
        downside_vol = pstdev(downside) if len(downside) >= 3 else _volatility(returns[-12:])
        quality = 1 / (1 + downside_vol * 1000)
        volume = mean([_to_float(row.get("volume")) for row in rows[-10:]]) if rows else 0.0
        liquidity = min(1.0, math.log10(max(volume, 1)) / 7)
        persistence = sum(1 for item in returns[-8:] if item > 0) / max(1, min(8, len(returns)))
        raw_score = momentum_6 * 45 + momentum_12 * 35 + quality * 12 + liquidity * 8 + (persistence - 0.5) * 10
        ranked.append({
            "symbol": symbol,
            "factor_score": round(raw_score, 3),
            "bias": "long_factor_bias" if raw_score > 0 else "short_factor_bias" if raw_score < -0.2 else "neutral",
            "momentum_6_bar_pct": round(momentum_6 * 100, 3),
            "momentum_12_bar_pct": round(momentum_12 * 100, 3),
            "quality_score": round(quality, 3),
            "liquidity_score": round(liquidity, 3),
            "persistence": round(persistence, 3),
        })
    ranked.sort(key=lambda item: item["factor_score"], reverse=True)
    return {
        "status": "ready" if ranked else "insufficient",
        "top_factor_long_bias": ranked[:10],
        "top_factor_short_bias": sorted(ranked, key=lambda item: item["factor_score"])[:10],
        "explanation": "Factor model: momentum + quality + liquidity + persistence. Advisory only; Senior/risk gates still decide trades.",
    }


def _ml_prediction_context(database_url: str) -> Dict:
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            active = connection.execute(
                text(
                    """
                    SELECT version, feature_set, algorithm, status, created_at
                    FROM model_versions
                    WHERE status='active'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
            ).mappings().one_or_none()
            summary = connection.execute(
                text(
                    """
                    SELECT COUNT(*) predictions,
                           COUNT(DISTINCT COALESCE(instrument_id::text, exchange || ':' || symbol)) instruments,
                           MAX(created_at) latest_prediction,
                           AVG(probability) avg_probability,
                           MAX(probability) max_probability,
                           MIN(probability) min_probability
                    FROM shadow_predictions
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '3 days'
                    """
                )
            ).mappings().one()
            strongest = connection.execute(
                text(
                    """
                    SELECT exchange, symbol, probability, signal, created_at
                    FROM shadow_predictions
                    WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '3 days'
                    ORDER BY ABS(probability - 0.5) DESC, created_at DESC
                    LIMIT 10
                    """
                )
            ).mappings().all()
    finally:
        engine.dispose()
    return {
        "status": "ready" if active else "insufficient",
        "active_model": dict(active) if active else None,
        "recent_prediction_summary": {key: _iso(value) if isinstance(value, datetime) else value for key, value in dict(summary).items()},
        "strongest_recent_signals": [
            {
                "exchange": row["exchange"],
                "symbol": row["symbol"],
                "probability": round(_to_float(row["probability"]), 4),
                "direction": "BUY/CALL bias" if int(row["signal"] or 0) > 0 else "SELL/PUT bias",
                "created_at": _iso(row["created_at"]),
            }
            for row in strongest
        ],
        "explanation": "ML bridge: shows the active prediction model and recent probability evidence for Senior/Agentic AI review.",
    }


def _portfolio_optimization(series: Dict[Tuple[str, str], List[Dict]], factor_model: Dict) -> Dict:
    candidates = list(factor_model.get("top_factor_long_bias") or [])
    if not candidates:
        return {
            "status": "insufficient",
            "allocations": [],
            "explanation": "Need factor-ranked 5m instruments before portfolio optimization can advise exposure.",
        }
    scored = []
    for item in candidates:
        symbol = item["symbol"]
        closes = _close_series(series, symbol, "5minute")
        vol = max(_volatility(_returns(closes[-24:])), 0.0005)
        factor_strength = max(0.01, abs(float(item.get("factor_score") or 0)))
        inverse_risk_score = factor_strength / vol
        scored.append({**item, "risk_score": inverse_risk_score, "volatility_bps": round(vol * 10000, 2)})
    total = sum(item["risk_score"] for item in scored) or 1.0
    allocations = []
    residual = 1.0
    for item in scored[:8]:
        weight = min(0.18, item["risk_score"] / total)
        residual -= weight
        allocations.append({
            "symbol": item["symbol"],
            "suggested_weight_pct": round(weight * 100, 2),
            "bias": item["bias"],
            "factor_score": item["factor_score"],
            "volatility_bps": item["volatility_bps"],
            "rule": "paper exposure guidance only; no direct order placement",
        })
    return {
        "status": "ready",
        "allocations": allocations,
        "cash_buffer_pct": round(max(0.0, residual) * 100, 2),
        "max_single_name_weight_pct": 18,
        "explanation": "Portfolio optimizer: inverse-risk weighted paper allocation guidance with concentration caps.",
    }


def _volatility_snapshot(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    rows = []
    for symbol in INDEX_PRIORITY:
        closes = _close_series(series, symbol, "5minute") or _close_series(series, symbol, "1minute")
        if len(closes) < 8:
            continue
        vol = _volatility(_returns(closes[-24:]))
        rows.append({
            "symbol": symbol,
            "volatility_bps": round(vol * 10000, 2),
            "state": "high_noise" if vol > 0.0025 else "normal",
        })
    return {
        "status": "ready" if rows else "insufficient",
        "index_volatility": rows,
        "explanation": "GARCH-style proxy: high clustered noise should tighten entry quality or avoid tiny-profit trades.",
    }


def _microstructure(series: Dict[Tuple[str, str], List[Dict]]) -> Dict:
    one_second = {symbol: rows for (symbol, interval), rows in series.items() if interval == "1second"}
    instruments = len(one_second)
    latest = _latest_time(series, "1second")
    return {
        "status": "ready" if instruments else "not_available",
        "instruments": instruments,
        "latest_bar": latest,
        "role": "final entry/exit confirmation only",
        "rule": "Do not train/promote a 1s model until full-session 1s coverage and gap repair are proven.",
    }


def run_quant_snapshot(database_url: str | None = None, limit: int = 80) -> Dict:
    database_url = database_url or DATABASE_URL
    base = quant_model_status()
    if not database_url:
        return {**base, "status": "unavailable", "reason": "DATABASE_URL is not configured"}
    try:
        rows = _load_recent_bars(database_url, limit)
        series = _build_series(rows)
        snapshot = {
            "bar_rows_loaded": len(rows),
            "symbols_loaded": len({symbol for symbol, _ in series.keys()}),
            "latest_1m": _latest_time(series, "1minute"),
            "latest_5m": _latest_time(series, "5minute"),
            "latest_1s": _latest_time(series, "1second"),
        }
        factor_model = _factor_investing(series)
        models = {
            "regime_hmm_proxy": _market_regime(series),
            "var_lead_lag": _lead_lag(series),
            "vecm_cointegration": _pairs(series),
            "stat_arb_pairs": _pairs(series),
            "garch_volatility_proxy": _volatility_snapshot(series),
            "cross_sectional_ranker": _cross_sectional_rank(series),
            "factor_investing_model": factor_model,
            "ml_prediction_bridge": _ml_prediction_context(database_url),
            "portfolio_optimization_model": _portfolio_optimization(series, factor_model),
            "microstructure_1s_gate": _microstructure(series),
        }
        ready_count = sum(1 for value in models.values() if value.get("status") == "ready")
        return {
            **base,
            "status": "success",
            "snapshot": snapshot,
            "ready_models": ready_count,
            "model_outputs": models,
            "senior_layer_use": "confirmation_and_conflict_context_only",
            "ml_model_use": "feature/research context; not active promotion evidence by itself",
            "risk_use": "tighten or avoid entries when regime/volatility/microstructure conflict",
            "orders_allowed": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            **base,
            "status": "error",
            "reason": str(exc),
            "orders_allowed": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
