"""Structured production logging and deduplicated webhook paging."""
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.request import Request, urlopen


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if context:
            payload["context"] = context
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("nivesh")
    logger.setLevel((level or os.getenv("NIVESH_LOG_LEVEL", "INFO")).upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


_last_page: Dict[str, float] = {}


def page(severity: str, summary: str, details: Optional[Dict] = None) -> Dict:
    """Send a PagerDuty/Slack-compatible JSON webhook with local deduplication."""
    severity = severity.upper()
    details = details or {}
    logger = configure_logging()
    logger.log(logging.CRITICAL if severity == "CRITICAL" else logging.ERROR,
               summary, extra={"context": details})
    webhook = os.getenv("NIVESH_ALERT_WEBHOOK_URL", "").strip()
    if not webhook:
        return {"sent": False, "reason": "alert webhook is not configured"}
    key = hashlib.sha256(f"{severity}:{summary}:{json.dumps(details, sort_keys=True, default=str)}".encode()).hexdigest()
    now = time.monotonic()
    cooldown = int(os.getenv("NIVESH_ALERT_COOLDOWN_SECONDS", "900"))
    if now - _last_page.get(key, -cooldown) < cooldown:
        return {"sent": False, "reason": "duplicate alert suppressed", "dedup_key": key}
    body = json.dumps({
        "service": "nivesh-ai", "severity": severity, "summary": summary,
        "details": details, "dedup_key": key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, default=str).encode()
    request = Request(webhook, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "NiveshAI/3.0",
    })
    with urlopen(request, timeout=10) as response:
        status = response.status
    if not 200 <= status < 300:
        raise RuntimeError(f"alert webhook returned HTTP {status}")
    _last_page[key] = now
    return {"sent": True, "status": status, "dedup_key": key}
