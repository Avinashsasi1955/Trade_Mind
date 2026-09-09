"""Trade-aware sentiment decision layer.

This module converts scored provider news into a deterministic paper-trading
signal that the ML selector, senior rulebook and AI copilot can all read.
It is advisory by default.  It only blocks trades when the operator explicitly
enables the sentiment gate and asks for verified sentiment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Optional

from sqlalchemy import text


INDEX_ALIASES = {
    "NIFTY 50": {"NIFTY 50", "NIFTY50", "NIFTY"},
    "NIFTY50": {"NIFTY 50", "NIFTY50", "NIFTY"},
    "NIFTY": {"NIFTY 50", "NIFTY50", "NIFTY"},
    "BANKNIFTY": {"BANKNIFTY", "BANK NIFTY", "NIFTY BANK"},
    "BANK NIFTY": {"BANKNIFTY", "BANK NIFTY", "NIFTY BANK"},
    "SENSEX": {"SENSEX", "BSESENSEX"},
    "INDIA VIX": {"INDIA VIX", "INDIAVIX", "VIX"},
    "INDIAVIX": {"INDIA VIX", "INDIAVIX", "VIX"},
}


def _clean_symbol(value: object) -> str:
    return str(value or "").upper().strip()


def _trade_direction(item: Mapping, target: Optional[Mapping] = None) -> int:
    """Return +1 bullish exposure, -1 bearish exposure, 0 unknown.

    For options, this is the underlying-market direction, not merely the order
    side.  Example: BUY CE and SELL PE are bullish; BUY PE and SELL CE bearish.
    """
    side = _clean_symbol((target or {}).get("side") or item.get("option_side") or item.get("side"))
    kind = _clean_symbol((target or {}).get("kind") or item.get("option_type") or item.get("instrument_type"))
    if kind == "CE" and side == "BUY":
        return 1
    if kind == "PE" and side == "SELL":
        return 1
    if kind == "PE" and side == "BUY":
        return -1
    if kind == "CE" and side == "SELL":
        return -1
    signal = int(item.get("signal") or item.get("paper_probe_signal") or 0)
    if signal:
        return 1 if signal > 0 else -1
    if side == "BUY":
        return 1
    if side == "SELL":
        return -1
    return 0


def _symbol_candidates(item: Mapping, target: Optional[Mapping] = None) -> list[str]:
    values = [
        item.get("symbol"),
        item.get("underlying_symbol"),
        item.get("underlying"),
        (target or {}).get("underlying_symbol"),
        (target or {}).get("symbol"),
    ]
    symbols: set[str] = set()
    for raw in values:
        symbol = _clean_symbol(raw)
        if not symbol:
            continue
        symbol = symbol.split(".")[0]
        symbols.add(symbol)
        compact = symbol.replace(" ", "")
        symbols.add(compact)
        symbols.update(INDEX_ALIASES.get(symbol, set()))
        symbols.update(INDEX_ALIASES.get(compact, set()))
    return sorted(symbols)


def _label(score: Decimal) -> str:
    if score >= Decimal("15"):
        return "bullish"
    if score <= Decimal("-15"):
        return "bearish"
    return "neutral"


def _alignment(direction: int, label: str) -> str:
    if not direction or label == "neutral":
        return "neutral"
    if direction > 0 and label == "bullish":
        return "support"
    if direction < 0 and label == "bearish":
        return "support"
    return "conflict"


def _selector_adjustment(alignment: str, confidence: Decimal, articles: int, gate_enabled: bool) -> Decimal:
    if articles <= 0:
        return Decimal("0")
    strong = confidence >= Decimal("70")
    if alignment == "support":
        return Decimal("7") if strong else Decimal("4")
    if alignment == "conflict":
        # Advisory conflict should hurt ranking; enabled gate can make it harsher.
        return Decimal("-14") if gate_enabled and strong else Decimal("-8") if strong else Decimal("-4")
    return Decimal("0")


def evaluate_trade_sentiment(
    engine,
    item: Mapping,
    target: Optional[Mapping] = None,
    *,
    max_age_hours: int = 24,
    min_confidence: Decimal = Decimal("60"),
    gate_enabled: bool = False,
    require_verified: bool = False,
) -> Dict:
    """Return sentiment evidence for a candidate trade.

    The returned object is intentionally JSON-serialisable so it can be stored
    inside candidate audits and shadow execution notes.
    """
    symbols = _symbol_candidates(item, target)
    direction = _trade_direction(item, target)
    if not symbols or not direction:
        return {
            "accepted": True,
            "enabled": gate_enabled,
            "available": False,
            "action": "NO_DIRECTION",
            "reason": "no directional trade or symbol for sentiment analysis",
            "symbols": symbols,
            "selector_adjustment": "0",
            "orders_allowed": False,
        }
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT n.provider,n.headline,n.source_name,n.published_at,
                       s.positive_probability,s.neutral_probability,s.negative_probability,s.scored_at
                FROM news_articles_v3 n
                JOIN news_sentiment_scores s ON s.article_id=n.id
                WHERE n.symbols && CAST(:symbols AS text[])
                  AND n.published_at >= CURRENT_TIMESTAMP - (:hours || ' hours')::interval
                ORDER BY n.published_at DESC, s.scored_at DESC
                LIMIT 12
            """), {"symbols": symbols, "hours": int(max_age_hours)}).mappings().all()
    except Exception as exc:
        accepted = not require_verified
        return {
            "accepted": accepted,
            "enabled": gate_enabled,
            "available": False,
            "action": "UNAVAILABLE",
            "reason": f"sentiment store unavailable: {type(exc).__name__}",
            "symbols": symbols,
            "selector_adjustment": "0",
            "orders_allowed": False,
        }
    if not rows:
        accepted = not require_verified
        return {
            "accepted": accepted,
            "enabled": gate_enabled,
            "available": False,
            "action": "NO_RECENT_NEWS",
            "reason": "no verified recent scored news sentiment" if require_verified else "no recent scored news; neutral advisory fallback",
            "symbols": symbols,
            "articles": 0,
            "selector_adjustment": "0",
            "orders_allowed": False,
        }
    articles = len(rows)
    positive = sum(Decimal(str(row["positive_probability"] or 0)) for row in rows) / Decimal(articles)
    negative = sum(Decimal(str(row["negative_probability"] or 0)) for row in rows) / Decimal(articles)
    neutral = sum(Decimal(str(row["neutral_probability"] or 0)) for row in rows) / Decimal(articles)
    score = (positive - negative) * Decimal("100")
    confidence = max(positive, negative, neutral) * Decimal("100")
    label = _label(score)
    alignment = _alignment(direction, label)
    verified = any(str(row["provider"]).lower() in {"finnhub", "configured_news_api", "newsapi"} for row in rows)
    strong_conflict = alignment == "conflict" and confidence >= min_confidence and verified
    accepted = not (gate_enabled and strong_conflict)
    adjustment = _selector_adjustment(alignment, confidence, articles, gate_enabled)
    latest = max(row["published_at"] for row in rows if row["published_at"])
    if isinstance(latest, datetime):
        latest_value = latest.astimezone(timezone.utc).isoformat()
    else:
        latest_value = str(latest)
    action = "SUPPORTS_TRADE" if alignment == "support" else "CONFLICTS_WITH_TRADE" if alignment == "conflict" else "NEUTRAL"
    reason = (
        f"sentiment {label} supports trade direction"
        if alignment == "support" else
        f"sentiment {label} conflicts with trade direction"
        if alignment == "conflict" else
        "sentiment is neutral; no directional effect"
    )
    if gate_enabled and strong_conflict:
        reason += "; blocking because sentiment gate is enabled"
    return {
        "accepted": accepted,
        "enabled": gate_enabled,
        "available": True,
        "action": action,
        "label": label,
        "alignment": alignment,
        "direction": "bullish" if direction > 0 else "bearish",
        "score": str(round(score, 2)),
        "confidence": str(round(confidence, 2)),
        "articles": articles,
        "verified_provider": verified,
        "symbols": symbols,
        "latest_published_at": latest_value,
        "selector_adjustment": str(adjustment),
        "provider_mix": sorted({str(row["provider"]) for row in rows}),
        "headlines": [
            {
                "headline": str(row["headline"]),
                "source": str(row["source_name"]),
                "provider": str(row["provider"]),
                "published_at": row["published_at"].isoformat() if isinstance(row["published_at"], datetime) else str(row["published_at"]),
            }
            for row in rows[:3]
        ],
        "senior_advice": (
            "allow or slightly prefer if chart/market-quality gates agree"
            if alignment == "support" else
            "treat as warning; require stronger structure and risk/reward"
            if alignment == "conflict" else
            "do not change trade solely from sentiment"
        ),
        "reason": reason,
        "orders_allowed": False,
    }

