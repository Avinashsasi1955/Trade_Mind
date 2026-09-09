"""
Brain 4 — SentimentBrain
Responsibility: Fetch & score news, social signals, and FII/DII data.
Publishes SentimentReady so SignalBrain and RiskBrain can use it as a
gating/advisory layer.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base_brain import BaseBrain
from .bus import SentimentReady

logger = logging.getLogger("nivesh.brain.sentiment")


class SentimentBrain(BaseBrain):
    name = "SentimentBrain"

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        db       = context.get("db")
        user_id  = context.get("user_id", 0)
        if not user_id:
            try:
                from ..database import connect
                with connect() as _c:
                    _row = _c.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
                    if _row:
                        user_id = int(_row["id"])
            except Exception:
                user_id = 1
        tick     = context.get("tick", 0)

        self._log("info", "refreshing sentiment", user_id=user_id)

        summary: Dict = {}
        news_items = []

        try:
            from ..database import connect
            from ..service import refresh_sentiments, get_sentiment_dashboard
            conn = connect()
            try:
                refresh_sentiments(conn, user_id, tick)
                dash = get_sentiment_dashboard(conn, user_id)
                summary = dash.get("summary") or dash
                news_items = dash.get("news", [])
            finally:
                conn.close()
        except Exception as exc:
            self._log("warning", f"sentiment fetch failed: {exc}")

        event = SentimentReady(
            source_brain=self.name,
            sentiment_summary=summary,
            news_items=news_items,
            user_id=user_id,
        )
        self.bus.publish(event)
        self._log("info", "published SentimentReady", news=len(news_items))
        return {"sentiment_summary": summary, "news_items": news_items}
