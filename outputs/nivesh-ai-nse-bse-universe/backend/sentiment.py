from datetime import datetime, timezone
from typing import Dict, List


POSITIVE = {"beat", "growth", "upgrade", "strong", "wins", "expands", "record", "rally", "improves", "bullish", "demand", "orders", "profit"}
NEGATIVE = {"miss", "downgrade", "weak", "falls", "risk", "probe", "decline", "bearish", "pressure", "cuts", "loss", "concern", "delay"}

HEADLINES = {
    "RELIANCE": [
        ("Energy margins improve as domestic demand remains strong", "Market desk"),
        ("Retail expansion keeps growth outlook constructive", "Business wire"),
        ("Analysts flag near-term capex pressure as a risk", "Broker note"),
    ],
    "HDFCBANK": [
        ("Deposit growth improves while asset quality remains stable", "Banking desk"),
        ("Margin pressure remains a near-term concern", "Broker note"),
        ("Digital transactions reach a record monthly level", "Industry wire"),
    ],
    "INFY": [
        ("Large deal wins support revenue growth expectations", "Technology desk"),
        ("Global discretionary spending remains weak", "Industry wire"),
        ("Analyst upgrade follows strong order pipeline", "Broker note"),
    ],
    "BEL": [
        ("Defence electronics orders expand to a record backlog", "Industry wire"),
        ("Domestic manufacturing demand remains strong", "Market desk"),
        ("Execution delay is a near-term risk", "Broker note"),
    ],
    "TATAMOTORS": [
        ("Passenger vehicle demand improves ahead of festive season", "Auto desk"),
        ("Commodity costs create margin pressure", "Industry wire"),
        ("EV unit expands charging partnership", "Business wire"),
    ],
}

DEFAULT_HEADLINES = [
    ("Sector demand remains stable with selective growth", "Market desk"),
    ("Investors monitor valuation risk after the recent rally", "Broker note"),
    ("Trading volume improves above the monthly average", "Exchange monitor"),
]


def _headline_score(text: str) -> float:
    words = {word.strip(".,:;!?()\"").lower() for word in text.split()}
    positive = len(words & POSITIVE)
    negative = len(words & NEGATIVE)
    return max(-1.0, min(1.0, (positive - negative) / max(1, positive + negative)))


def analyse_sentiment(stock: Dict, market: List[Dict]) -> Dict:
    headlines = HEADLINES.get(stock["symbol"], DEFAULT_HEADLINES)
    stories = [{"headline": text, "source": source, "score": round(_headline_score(text) * 100)} for text, source in headlines]
    news_score = sum(item["score"] for item in stories) / max(1, len(stories))
    price_score = max(-100, min(100, stock["change_pct"] / 4 * 100))
    volume_score = max(-20, min(100, (stock["volume_ratio"] - 1) * 65))
    advancing = sum(1 for item in market if item["change_pct"] > 0)
    breadth_score = (advancing / max(1, len(market)) * 2 - 1) * 100
    total = round(news_score * .42 + price_score * .28 + volume_score * .15 + breadth_score * .15, 1)
    label = "bullish" if total >= 20 else "bearish" if total <= -20 else "neutral"
    confidence = round(min(96, 58 + abs(total) * .34), 1)
    positive_count = sum(1 for item in stories if item["score"] > 0)
    negative_count = sum(1 for item in stories if item["score"] < 0)
    summary = f"{stock['symbol']} sentiment is {label} at {total:+.0f}/100. News contributes {news_score:+.0f}, price action {price_score:+.0f}, relative volume {volume_score:+.0f}, and market breadth {breadth_score:+.0f}."
    return {
        "symbol": stock["symbol"], "name": stock["name"], "price": stock["price"], "change_pct": stock["change_pct"],
        "score": total, "label": label, "confidence": confidence, "summary": summary, "headlines": stories,
        "components": {"news": round(news_score,1), "price_action": round(price_score,1), "volume": round(volume_score,1), "market_breadth": round(breadth_score,1)},
        "coverage": {"positive": positive_count, "neutral": len(stories)-positive_count-negative_count, "negative": negative_count, "articles": len(stories)},
        "data_mode": "simulated", "updated_at": datetime.now(timezone.utc).isoformat(),
    }
