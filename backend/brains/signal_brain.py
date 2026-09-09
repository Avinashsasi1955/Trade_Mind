"""
Brain 3 — SignalBrain
Responsibility: Run the TradingAgent ensemble on screened candidates,
produce BUY + SELL signals, and publish SignalReady.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import create_engine, text

from .base_brain import BaseBrain
from .bus import ScreenerResultReady, SentimentReady, SignalReady
from ..alpha_fragility import alpha_fragility_status
from ..config import DATABASE_URL

logger = logging.getLogger("nivesh.brain.signal")


class SignalBrain(BaseBrain):
    name = "SignalBrain"

    def __init__(self, *args, **kwargs):
        self._latest_sentiment: Dict = {}
        super().__init__(*args, **kwargs)

    def _register_handlers(self):
        self.bus.subscribe(ScreenerResultReady, self._on_screener_result)
        self.bus.subscribe(SentimentReady, self._on_sentiment)

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------
    def _on_sentiment(self, event: SentimentReady) -> None:
        """Cache latest sentiment for use in signal generation."""
        self._latest_sentiment = event.sentiment_summary

    def _on_screener_result(self, event: ScreenerResultReady) -> None:
        self._log("info", "received ScreenerResultReady", candidates=len(event.candidates))
        result = self._generate(
            stocks=event.candidates,
            risk_profile=event.payload.get("risk_profile", "balanced"),
            positions=event.payload.get("positions", {}),
            user_id=event.payload.get("user_id", 0),
        )
        self.bus.publish(SignalReady(
            source_brain=self.name,
            signals=result["signals"],
            buy_count=result["buy_count"],
            sell_count=result["sell_count"],
            user_id=event.payload.get("user_id", 0),
            risk_profile=event.payload.get("risk_profile", "balanced"),
        ))

    # ------------------------------------------------------------------
    # Direct run (used by scheduler)
    # ------------------------------------------------------------------
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        stocks: List[Dict] = context.get("stocks", [])
        risk_profile: str = context.get("risk_profile", "balanced")
        positions: Dict = context.get("positions", {})
        user_id: int = context.get("user_id", 0)
        result = self._generate(stocks, risk_profile, positions, user_id)
        live_selector = self._live_selector_context()
        result["live_selector_context"] = live_selector
        result["advisory_context"] = {
            "source": "LivePaperInference selector artifacts + TradingAgent advisory snapshot",
            "authoritative_trade_selector": "backend.live_inference.LivePaperInference",
            "required_trade_context": [
                "live_market_bars",
                "shadow_predictions",
                "Senior chart gate",
                "sentiment gate",
                "quant model snapshot",
                "alpha fragility guard",
                "risk governor",
                "active trade manager",
            ],
            "orders_allowed": False,
        }
        event = SignalReady(
            source_brain=self.name,
            signals=result["signals"],
            buy_count=result["buy_count"],
            sell_count=result["sell_count"],
            user_id=user_id,
            risk_profile=risk_profile,
        )
        self.bus.publish(event)
        return result

    # ------------------------------------------------------------------
    # Core signal generation
    # ------------------------------------------------------------------
    def _generate(self, stocks: List[Dict], risk_profile: str, positions: Dict, user_id: int) -> Dict:
        from ..agent import TradingAgent
        from ..trade_memory import find_matching_golden_trade

        agent = TradingAgent()
        signals = agent.analyse(stocks, risk_profile, positions)
        serialised = agent.serialise(signals)

        # Enrich each signal with Golden Trade Vector Similarity Match
        enriched_signals = []
        for sig in serialised:
            match_info = find_matching_golden_trade(sig)
            # Apply confidence boost if pattern matches a proven historical winner
            boost = match_info.get("confidence_boost", 0.0)
            adjusted_conf = min(98.0, round(float(sig.get("confidence", 70)) + boost, 1))
            
            enriched = {
                **sig,
                "confidence": adjusted_conf,
                "golden_trade_match": match_info,
            }
            enriched_signals.append(enriched)

        buy_sigs  = [s for s in enriched_signals if s.get("action") == "BUY"]
        sell_sigs = [s for s in enriched_signals if s.get("action") == "SELL"]

        self._log("info", "signals generated with vector memory boost",
                  buy=len(buy_sigs), sell=len(sell_sigs), total=len(enriched_signals))

        return {
            "signals": enriched_signals,
            "raw_signals": signals,
            "buy_count": len(buy_sigs),
            "sell_count": len(sell_sigs),
            "sentiment_context": self._latest_sentiment,
        }

    def _live_selector_context(self) -> Dict:
        if not DATABASE_URL:
            return {"status": "unavailable", "reason": "DATABASE_URL missing", "orders_allowed": False}
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection:
                accepted = connection.execute(text("""
                    SELECT observed_at,exchange,symbol,instrument_type,signal,probability,
                           chart_strategy,route,rr,quality_score,selector_score,details
                    FROM trade_candidate_audits
                    WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                      AND accepted
                    ORDER BY observed_at DESC,id DESC
                    LIMIT 10
                """)).mappings().all()
                rejected = connection.execute(text("""
                    SELECT COALESCE(rejection_reason,'unknown') reason, COUNT(*) count
                    FROM trade_candidate_audits
                    WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                      AND NOT accepted
                    GROUP BY COALESCE(rejection_reason,'unknown')
                    ORDER BY count DESC
                    LIMIT 8
                """)).mappings().all()
                senior = connection.execute(text("""
                    SELECT observed_at,session_block,symbol,opportunity_side,option_route,regime,
                           strategy,confidence,risk_reward,senior_decision,rejection_reason,outcome_label
                    FROM senior_market_opportunities
                    WHERE (observed_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                    ORDER BY observed_at DESC,confidence DESC,id DESC
                    LIMIT 10
                """)).mappings().all()
            return {
                "status": "connected",
                "source": "LivePaperInference persisted selector evidence",
                "accepted_candidates": [dict(row) for row in accepted],
                "rejection_reasons": [dict(row) for row in rejected],
                "senior_opportunities": [dict(row) for row in senior],
                "alpha_fragility": alpha_fragility_status(DATABASE_URL),
                "orders_allowed": False,
            }
        except Exception as exc:
            return {"status": "error", "reason": str(exc), "orders_allowed": False}
        finally:
            engine.dispose()
