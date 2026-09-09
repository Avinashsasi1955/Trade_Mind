import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import (FINNHUB_API_KEY, FINNHUB_NEWS_CATEGORY,
                     FINNHUB_NEWS_LOOKBACK_DAYS, NEWS_API_BASE_URL,
                     NEWS_API_KEY, NEWS_API_KEY_HEADER, NEWS_CACHE_SECONDS,
                     NEWS_MAX_ARTICLES, NEWS_PROVIDER)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(headline: str, source: str, published_at: str) -> str:
    canonical = "|".join((headline.strip().lower(), source.strip().lower(), published_at[:10]))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalise(item: Dict) -> Dict:
    source = item.get("source", "Unknown")
    if isinstance(source, dict):
        source = source.get("name") or "Unknown"
    published = item.get("publishedAt") or item.get("published_at") or item.get("date") or item.get("datetime")
    if isinstance(published, (int, float)):
        published = datetime.fromtimestamp(published, timezone.utc).isoformat()
    return {
        "external_id": str(item.get("id") or item.get("uuid") or item.get("external_id") or ""),
        "headline": str(item.get("title") or item.get("headline") or "").strip(),
        "summary": str(item.get("description") or item.get("summary") or item.get("content") or "").strip(),
        "source": str(source).strip() or "Unknown",
        "url": str(item.get("url") or item.get("link") or ""),
        "published_at": str(published or _now()),
    }


def cached_articles(db: sqlite3.Connection, symbol: str, limit: int = NEWS_MAX_ARTICLES) -> List[Dict]:
    rows = db.execute(
        "SELECT a.*,s.relevance FROM news_articles a JOIN news_article_symbols s ON s.article_id=a.id "
        "WHERE s.symbol=? ORDER BY a.published_at DESC LIMIT ?", (symbol.upper(), limit)
    ).fetchall()
    return [dict(row) for row in rows]


def _fresh(db: sqlite3.Connection, symbol: str) -> bool:
    row = db.execute(
        "SELECT MAX(a.received_at) received_at FROM news_articles a JOIN news_article_symbols s ON s.article_id=a.id WHERE s.symbol=?",
        (symbol.upper(),),
    ).fetchone()
    if not row or not row["received_at"]:
        return False
    try:
        received = datetime.fromisoformat(row["received_at"].replace("Z", "+00:00"))
        return received >= datetime.now(timezone.utc) - timedelta(seconds=NEWS_CACHE_SECONDS)
    except ValueError:
        return False


def _fetch_newsapi(query: str) -> List[Dict]:
    params = {"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": NEWS_MAX_ARTICLES}
    separator = "&" if "?" in NEWS_API_BASE_URL else "?"
    request = Request(f"{NEWS_API_BASE_URL}{separator}{urlencode(params)}", headers={
        "Accept": "application/json", "User-Agent": "NiveshAI/1.0", NEWS_API_KEY_HEADER: NEWS_API_KEY,
    })
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("articles") or payload.get("data") or payload.get("results") or []


def _fetch_finnhub(symbol: str, company_name: str = "") -> List[Dict]:
    """Fetch Finnhub company news first, then general market news as fallback."""
    if not FINNHUB_API_KEY:
        return []
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=max(1, FINNHUB_NEWS_LOOKBACK_DAYS))
    base = NEWS_API_BASE_URL if NEWS_PROVIDER == "finnhub" else "https://finnhub.io/api/v1"
    base_symbol = symbol.upper().split(".")[0]
    candidates = [symbol.upper()] if "." in symbol else [f"{base_symbol}.NS", f"{base_symbol}.BO", base_symbol]
    for candidate in dict.fromkeys(candidates):
        params = {"symbol": candidate, "from": start.isoformat(), "to": today.isoformat(), "token": FINNHUB_API_KEY}
        request = Request(f"{base}/company-news?{urlencode(params)}",
                          headers={"Accept": "application/json", "User-Agent": "NiveshAI/1.0"})
        try:
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list) and payload:
                return payload[:NEWS_MAX_ARTICLES]
        except Exception:
            continue
    params = {"category": FINNHUB_NEWS_CATEGORY, "token": FINNHUB_API_KEY}
    request = Request(f"{base}/news?{urlencode(params)}",
                      headers={"Accept": "application/json", "User-Agent": "NiveshAI/1.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    name = (company_name or symbol).lower()
    filtered = []
    for item in payload if isinstance(payload, list) else []:
        text = f"{item.get('headline','')} {item.get('summary','')} {item.get('related','')}".lower()
        if symbol.lower() in text or any(part and part in text for part in name.split()[:3]):
            filtered.append(item)
    return filtered[:NEWS_MAX_ARTICLES]


def _fetch(query: str, symbol: str, company_name: str = "") -> List[Dict]:
    if NEWS_PROVIDER == "finnhub":
        return _fetch_finnhub(symbol, company_name)
    return _fetch_newsapi(query)


def articles_for_symbol(db: sqlite3.Connection, symbol: str, company_name: str = "", allow_fetch: bool = True) -> Dict:
    symbol = symbol.upper().strip()
    cached = cached_articles(db, symbol)
    if not allow_fetch or not NEWS_API_KEY or not NEWS_API_BASE_URL or _fresh(db, symbol):
        return {"articles": cached, "mode": "cached_news" if cached else "offline_fallback", "provider": NEWS_PROVIDER}
    started = time.monotonic()
    query = f'("{company_name}" OR "{symbol}") AND (NSE OR BSE OR India stock)' if company_name else f'"{symbol}" AND (NSE OR BSE)'
    try:
        raw = _fetch(query, symbol, company_name)
        saved = 0
        for item in raw:
            article = _normalise(item)
            if not article["headline"]:
                continue
            digest = _content_hash(article["headline"], article["source"], article["published_at"])
            db.execute(
                "INSERT OR IGNORE INTO news_articles(provider,external_id,content_hash,headline,summary,source,url,published_at,received_at,raw_payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (NEWS_PROVIDER, article["external_id"], digest, article["headline"], article["summary"], article["source"], article["url"], article["published_at"], _now(), json.dumps(item)[:50000]),
            )
            row = db.execute("SELECT id FROM news_articles WHERE content_hash=?", (digest,)).fetchone()
            db.execute("INSERT OR IGNORE INTO news_article_symbols(article_id,exchange,symbol,relevance) VALUES(?,?,?,?)", (row["id"], "NSE", symbol, 1.0))
            saved += 1
        db.execute("INSERT INTO news_ingestion_logs(provider,query,status,articles_received,latency_ms,created_at) VALUES(?,?,?,?,?,?)",
                   (NEWS_PROVIDER, query, "ok", saved, int((time.monotonic()-started)*1000), _now()))
        db.commit()
        fresh_articles = cached_articles(db, symbol)
        return {"articles": fresh_articles, "mode": "live_news" if fresh_articles else "offline_fallback", "provider": NEWS_PROVIDER}
    except Exception as exc:
        db.execute("INSERT INTO news_ingestion_logs(provider,query,status,articles_received,latency_ms,error,created_at) VALUES(?,?,?,?,?,?,?)",
                   (NEWS_PROVIDER, query, "error", 0, int((time.monotonic()-started)*1000), str(exc)[:500], _now()))
        db.commit()
        return {"articles": cached, "mode": "cached_news" if cached else "offline_fallback", "provider": NEWS_PROVIDER, "error": "provider_unavailable"}
