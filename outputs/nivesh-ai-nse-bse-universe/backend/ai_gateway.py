"""Controlled gateway between the trading engine and optional model APIs."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict

from .config import AI_GATEWAY_CACHE_SECONDS, AI_GATEWAY_CIRCUIT_SECONDS, AI_GATEWAY_RATE_LIMIT
from .database import now_iso
from .model_provider import BaseProvider, get_provider, provider_catalog


_circuit_until = {}
_failure_count = {}


def _request_hash(analysis: Dict) -> str:
    keys = ("symbol", "price", "bias", "confidence", "timeframes", "market_structure", "liquidity", "support_resistance", "order_block", "fvg", "poi", "trade_plan")
    payload = json.dumps({key: analysis[key] for key in keys}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class AIGateway:
    def __init__(self, db, user_id: int):
        self.db = db
        self.user_id = user_id

    def _recent_count(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        return self.db.execute("SELECT COUNT(*) count FROM ai_gateway_logs WHERE user_id=? AND created_at>=? AND cached=0", (self.user_id, cutoff)).fetchone()["count"]

    def _cached(self, request_hash: str):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=AI_GATEWAY_CACHE_SECONDS)).isoformat()
        return self.db.execute("SELECT * FROM ai_gateway_logs WHERE user_id=? AND request_hash=? AND status='SUCCESS' AND created_at>=? ORDER BY id DESC LIMIT 1", (self.user_id, request_hash, cutoff)).fetchone()

    def _record(self, request_hash: str, provider: str, model: str, status: str, latency_ms: int, cached: bool, response: str = None, error: str = None):
        self.db.execute("INSERT INTO ai_gateway_logs(user_id,request_hash,provider,model,status,latency_ms,cached,response_text,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (self.user_id, request_hash, provider, model, status, latency_ms, int(cached), response, error, now_iso()))
        self.db.commit()

    def explain(self, analysis: Dict) -> Dict:
        request_hash = _request_hash(analysis)
        cached = self._cached(request_hash)
        if cached:
            self._record(request_hash, cached["provider"], cached["model"], "SUCCESS", 0, True, cached["response_text"])
            return {"text": cached["response_text"], "provider": cached["provider"], "model": cached["model"], "cached": True, "fallback": False}

        provider = get_provider()
        if self._recent_count() >= AI_GATEWAY_RATE_LIMIT:
            text = analysis["narrative"] + " The external explanation limit was reached, so the local interpretation was used."
            self._record(request_hash, "local", "market-structure-ensemble", "RATE_LIMITED", 0, False, text, "Per-user gateway rate limit")
            return {"text": text, "provider": "local", "model": "market-structure-ensemble", "cached": False, "fallback": True}

        circuit_open = _circuit_until.get(provider.name, 0) > time.time()
        if not provider.available or circuit_open:
            reason = "Provider circuit open" if circuit_open else "Provider not configured"
            text = analysis["narrative"]
            self._record(request_hash, "local", "market-structure-ensemble", "FALLBACK", 0, False, text, reason)
            return {"text": text, "provider": "local", "model": "market-structure-ensemble", "cached": False, "fallback": True}

        started = time.perf_counter()
        try:
            text = provider.enhance(analysis)
            latency = int((time.perf_counter() - started) * 1000)
            _failure_count[provider.name] = 0
            self._record(request_hash, provider.name, provider.model, "SUCCESS", latency, False, text)
            return {"text": text, "provider": provider.name, "model": provider.model, "cached": False, "fallback": False}
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            _failure_count[provider.name] = _failure_count.get(provider.name, 0) + 1
            if _failure_count[provider.name] >= 3:
                _circuit_until[provider.name] = time.time() + AI_GATEWAY_CIRCUIT_SECONDS
            text = analysis["narrative"] + " The configured model provider was unavailable, so the local interpretation was used."
            self._record(request_hash, provider.name, provider.model, "ERROR", latency, False, text, str(exc)[:300])
            return {"text": text, "provider": "local", "model": "market-structure-ensemble", "cached": False, "fallback": True}


def gateway_status(db, user_id: int) -> Dict:
    catalog = provider_catalog()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    stats = db.execute("SELECT COUNT(*) requests, SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) successes, SUM(CASE WHEN cached=1 THEN 1 ELSE 0 END) cache_hits, AVG(latency_ms) avg_latency FROM ai_gateway_logs WHERE user_id=? AND created_at>=?", (user_id, since)).fetchone()
    recent = db.execute("SELECT provider,model,status,latency_ms,cached,created_at FROM ai_gateway_logs WHERE user_id=? ORDER BY id DESC LIMIT 5", (user_id,)).fetchall()
    return {
        **catalog,
        "controls": {"rate_limit_per_minute": AI_GATEWAY_RATE_LIMIT, "cache_seconds": AI_GATEWAY_CACHE_SECONDS, "circuit_breaker_seconds": AI_GATEWAY_CIRCUIT_SECONDS, "order_execution_access": False},
        "last_24h": {"requests": stats["requests"] or 0, "successes": stats["successes"] or 0, "cache_hits": stats["cache_hits"] or 0, "average_latency_ms": round(stats["avg_latency"] or 0)},
        "recent": [dict(row) for row in recent],
        "circuits": {name: "open" if until > time.time() else "closed" for name, until in _circuit_until.items()},
    }
