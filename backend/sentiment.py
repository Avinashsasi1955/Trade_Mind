import json
import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from .config import (SENTIMENT_MODEL_API_KEY, SENTIMENT_MODEL_MAX_ARTICLES,
                     SENTIMENT_MODEL_NAME, SENTIMENT_MODEL_URL)


POSITIVE = {"beat": 1.1, "growth": .7, "upgrade": 1.2, "strong": .7, "wins": 1.0, "expands": .6,
            "record": .6, "rally": .8, "improves": .7, "bullish": 1.0, "orders": .5, "profit": .8,
            "surge": 1.0, "buyback": .9, "dividend": .5, "approval": .8, "outperform": 1.0}
NEGATIVE = {"miss": 1.1, "downgrade": 1.2, "weak": .7, "falls": .8, "risk": .5, "probe": 1.2,
            "decline": .8, "bearish": 1.0, "pressure": .6, "cuts": .8, "loss": .9, "concern": .5,
            "delay": .6, "fraud": 1.5, "default": 1.5, "slump": 1.0, "dilution": .9, "exit": .5}
NEGATIONS = {"not", "no", "never", "without", "hardly", "fails", "failed", "denies"}
INTENSIFIERS = {"very": 1.25, "sharply": 1.4, "significantly": 1.35, "massive": 1.5, "slightly": .7}
EVENTS = [
    ("governance_risk", ("fraud", "probe", "investigation", "default"), -1.0),
    ("earnings_beat", ("profit beat", "revenue beat", "beats estimates", "margin expansion"), .8),
    ("earnings_miss", ("profit miss", "misses estimates", "margin contraction"), -.8),
    ("analyst_upgrade", ("analyst upgrade", "rating upgrade", "raises target"), .6),
    ("analyst_downgrade", ("analyst downgrade", "rating downgrade", "cuts target"), -.6),
    ("order_win", ("order win", "wins order", "contract awarded", "new contract"), .6),
    ("capital_return", ("buyback", "special dividend", "dividend"), .4),
    ("dilution", ("rights issue", "share dilution", "qualified institutional placement"), -.35),
    ("management_change", ("ceo resigns", "cfo resigns", "management exit"), -.35),
]

HEADLINES = {
    "RELIANCE": [("Energy margins improve as domestic demand remains strong", "Offline demo"), ("Retail expansion keeps growth outlook constructive", "Offline demo"), ("Analysts flag near-term capex pressure as a risk", "Offline demo")],
    "HDFCBANK": [("Deposit growth improves while asset quality remains stable", "Offline demo"), ("Margin pressure remains a near-term concern", "Offline demo"), ("Digital transactions reach a record monthly level", "Offline demo")],
    "INFY": [("Large deal wins support revenue growth expectations", "Offline demo"), ("Global discretionary spending remains weak", "Offline demo"), ("Analyst upgrade follows strong order pipeline", "Offline demo")],
}
DEFAULT_HEADLINES = [("Sector demand remains stable with selective growth", "Offline demo"), ("Investors monitor valuation risk after the recent rally", "Offline demo"), ("Trading volume improves above the monthly average", "Offline demo")]


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z]+", text.lower())


def _event(text: str) -> Tuple[str, float]:
    lowered = text.lower()
    for name, phrases, impact in EVENTS:
        if any(phrase in lowered for phrase in phrases):
            return name, impact
    return "general", 0.0


def _lexical_score(text: str) -> float:
    words = _tokens(text)
    score = 0.0
    hits = 0
    for index, word in enumerate(words):
        polarity = POSITIVE.get(word, 0) - NEGATIVE.get(word, 0)
        if not polarity:
            continue
        window = words[max(0, index-3):index]
        if any(item in NEGATIONS for item in window):
            polarity *= -1
        multiplier = max((INTENSIFIERS.get(item, 1) for item in window), default=1)
        score += polarity * multiplier
        hits += 1
    _, event_impact = _event(text)
    score += event_impact
    return max(-1.0, min(1.0, score / max(1.0, math.sqrt(max(1, hits)) * 1.5)))


def _model_score(text: str) -> Optional[Tuple[float, float]]:
    if not SENTIMENT_MODEL_URL:
        return None
    headers = {"Content-Type": "application/json", "User-Agent": "NiveshAI/1.0"}
    if SENTIMENT_MODEL_API_KEY:
        headers["Authorization"] = f"Bearer {SENTIMENT_MODEL_API_KEY}"
    request = Request(SENTIMENT_MODEL_URL, data=json.dumps({"inputs": text[:4000], "model": SENTIMENT_MODEL_NAME}).encode(), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode())
        labels = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], list) else payload
        if isinstance(labels, dict):
            labels = labels.get("data") or labels.get("scores") or labels.get("result")
        mapping = {str(item.get("label", "")).lower(): float(item.get("score", 0)) for item in labels}
        positive = max((value for key, value in mapping.items() if "positive" in key), default=0)
        negative = max((value for key, value in mapping.items() if "negative" in key), default=0)
        return positive-negative, max(positive, negative, max(mapping.values(), default=0))
    except Exception:
        return None


def _age_weight(value: str) -> float:
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        hours = max(0, (datetime.now(timezone.utc)-published.astimezone(timezone.utc)).total_seconds()/3600)
        return max(.15, math.exp(-hours/36))
    except (ValueError, AttributeError):
        return .7


def _source_weight(source: str) -> float:
    lowered = source.lower()
    if any(name in lowered for name in ("reuters", "exchange", "nse", "bse", "company filing")):
        return 1.0
    if any(name in lowered for name in ("bloomberg", "business standard", "economic times", "moneycontrol")):
        return .9
    return .7


def _similarity(left: str, right: str) -> float:
    a, b = set(_tokens(left)), set(_tokens(right))
    return len(a & b) / max(1, len(a | b))


def _stories(stock: Dict, articles: Optional[List[Dict]]) -> List[Dict]:
    if not articles:
        articles = [{"headline": text, "source": source, "published_at": datetime.now(timezone.utc).isoformat(), "relevance": .5} for text, source in HEADLINES.get(stock["symbol"], DEFAULT_HEADLINES)]
    result, seen = [], []
    for article_index, article in enumerate(articles):
        headline = str(article.get("headline") or article.get("title") or "").strip()
        if not headline:
            continue
        novelty = .35 if any(_similarity(headline, prior) >= .72 for prior in seen) else 1.0
        seen.append(headline)
        if article.get("precomputed_sentiment_score") is not None:
            model = None
            score = max(-1.0, min(1.0, float(article.get("precomputed_sentiment_score") or 0) / 100))
            model_name = "stored-finbert"
            model_confidence = abs(float(article.get("precomputed_sentiment_score") or 0))
        else:
            lexical = _lexical_score(headline + " " + str(article.get("summary") or ""))
            model = _model_score(headline) if article_index < SENTIMENT_MODEL_MAX_ARTICLES else None
            score = lexical if not model else lexical*.3 + model[0]*.7
            model_name = SENTIMENT_MODEL_NAME if model else "finance-rules-v2"
            model_confidence = round(model[1]*100, 1) if model else None
        event, _ = _event(headline)
        weight = _age_weight(str(article.get("published_at") or "")) * _source_weight(str(article.get("source") or "")) * float(article.get("relevance", 1)) * novelty
        result.append({"headline": headline, "source": article.get("source") or "Unknown", "score": round(score*100),
                       "event": event, "published_at": article.get("published_at"), "novelty": novelty,
                       "weight": round(weight, 3), "model": model_name,
                       "model_confidence": model_confidence, "url": article.get("url", "")})
    return result


def analyse_sentiment(stock: Dict, market: List[Dict], articles: Optional[List[Dict]] = None, data_mode: str = "offline_fallback") -> Dict:
    stories = _stories(stock, articles)
    weight_sum = sum(item["weight"] for item in stories) or 1
    news_score = sum(item["score"]*item["weight"] for item in stories)/weight_sum
    price_score = max(-100, min(100, stock["change_pct"]/4*100))
    volume_score = max(-20, min(100, (stock["volume_ratio"]-1)*65))
    advancing = sum(1 for item in market if item["change_pct"] > 0)
    breadth_score = (advancing/max(1, len(market))*2-1)*100
    total = round(news_score*.42 + price_score*.28 + volume_score*.15 + breadth_score*.15, 1)
    label = "bullish" if total >= 20 else "bearish" if total <= -20 else "neutral"
    direction_agreement = abs(sum(1 if item["score"] > 10 else -1 if item["score"] < -10 else 0 for item in stories))/max(1, len(stories))
    effective_coverage = min(1, math.log2(len(stories)+1)/3)
    confidence = round(min(96, 42 + effective_coverage*24 + direction_agreement*16 + min(14, abs(total)*.2)), 1)
    positive_count = sum(1 for item in stories if item["score"] > 10)
    negative_count = sum(1 for item in stories if item["score"] < -10)
    summary = f"{stock['symbol']} sentiment is {label} at {total:+.0f}/100. News contributes {news_score:+.0f}, price action {price_score:+.0f}, relative volume {volume_score:+.0f}, and market breadth {breadth_score:+.0f}."
    return {"symbol": stock["symbol"], "name": stock["name"], "price": stock["price"], "change_pct": stock["change_pct"],
            "score": total, "label": label, "confidence": confidence, "summary": summary, "headlines": stories,
            "components": {"news": round(news_score, 1), "price_action": round(price_score, 1), "volume": round(volume_score, 1), "market_breadth": round(breadth_score, 1)},
            "coverage": {"positive": positive_count, "neutral": len(stories)-positive_count-negative_count, "negative": negative_count, "articles": len(stories)},
            "data_mode": data_mode, "updated_at": datetime.now(timezone.utc).isoformat()}
