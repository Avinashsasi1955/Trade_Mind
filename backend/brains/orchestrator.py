"""
Nivesh Brains — Agentic Supervisory Orchestrator.

Unifies the 4-specialist multi-agent system:
1. SentinelWatcher (Plumbing & Schema Guardian)
2. Quant & Chart Synthesizer (1s-1D Multi-Timeframe & 10 Quant Models)
3. DeepThinker (Adversarial Stress-Testing & Devil's Advocate)
4. SupremeExecutive (Dual-Mode Sizing, Routing, & Decision Logging)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
from .bus import get_bus, SeniorApproved, CandidateRejected
from .sentinel_watcher import SentinelWatcher
from .deep_thinker import DeepThinker

logger = logging.getLogger("nivesh.orchestrator")


class AgenticOrchestrator:
    """Master multi-agent supervisor coordinating the specialized trading brains."""

    def __init__(self, engine, redis_client=None):
        self.engine = engine
        self.redis = redis_client
        self.bus = get_bus()
        self.sentinel = SentinelWatcher(engine, redis_client)
        self.thinker = DeepThinker()

    def run_preflight_guard(self) -> Dict[str, Any]:
        """Verify plumbing and database health before inference cycles."""
        return self.sentinel.run_full_sentinel_audit()

    def evaluate_candidate(
        self,
        candidate: Dict[str, Any],
        market_context: Dict[str, Any],
        available_slots: int = 5
    ) -> Dict[str, Any]:
        """Run multi-agent deliberation on a candidate setup.
        
        Synthesizes quant context, conducts deep thinker deliberation,
        assigns sizing, routes to INTRADAY or SWING, and publishes bus events.
        """
        # 1. Deliberate via Deep Thinker
        deliberation = self.thinker.deliberate(
            candidate=candidate,
            market_context=market_context,
            proposed_mode=candidate.get("proposed_mode", "INTRADAY")
        )

        verdict = deliberation["verdict"]
        target_mode = deliberation["target_mode"]
        confidence = deliberation["confidence_score"]
        thought_chain = deliberation["thought_chain"]

        # 2. Supreme Executive Sizing & Decision Matrix
        if verdict == "APPROVE":
            # Continuous Fractional Kelly sizing factor (0.25x to 1.50x)
            sizing_factor = Decimal(str(deliberation.get("kelly_multiplier") or 1.0))

            reasoning_summary = " · ".join(thought_chain)
            reasoning_payload = {
                "supervisor": "AgenticOrchestrator",
                "verdict": verdict,
                "trade_mode": target_mode,
                "confidence": confidence,
                "sizing_factor": float(sizing_factor),
                "thought_chain": thought_chain,
                "risk_assessment": deliberation["risk_assessment"]
            }

            # Publish SeniorApproved event onto bus
            try:
                self.bus.publish(SeniorApproved(
                    source_brain="AgenticOrchestrator",
                    symbol=candidate.get("symbol", ""),
                    side=candidate.get("side", ""),
                    strategy=str(candidate.get("strategy") or ""),
                    confidence=confidence,
                    route=f"{candidate.get('side', '')} {candidate.get('instrument_type', 'EQ')} ({target_mode})",
                    payload=reasoning_payload
                ))
            except Exception as e:
                logger.warning("Failed to publish SeniorApproved event: %s", e)

            return {
                "accepted": True,
                "trade_mode": target_mode,
                "sizing_factor": sizing_factor,
                "confidence": confidence,
                "reasoning_chain": reasoning_payload,
                "summary": reasoning_summary
            }
        else:
            rejection_reason = "deep_thinker_adverse_risk"
            reasoning_payload = {
                "supervisor": "AgenticOrchestrator",
                "verdict": verdict,
                "rejection_reason": rejection_reason,
                "thought_chain": thought_chain
            }

            # Publish CandidateRejected event onto bus
            try:
                self.bus.publish(CandidateRejected(
                    source_brain="AgenticOrchestrator",
                    symbol=candidate.get("symbol", ""),
                    reason=rejection_reason,
                    strategy=str(candidate.get("strategy") or ""),
                    probability=float(candidate.get("probability", 0.5)),
                    quality_score=float(candidate.get("quality_score", 0.0))
                ))
            except Exception as e:
                logger.warning("Failed to publish CandidateRejected event: %s", e)

            return {
                "accepted": False,
                "rejection_reason": rejection_reason,
                "trade_mode": "INTRADAY",
                "reasoning_chain": reasoning_payload,
                "summary": " · ".join(thought_chain)
            }


_orchestrator_instance = None

def get_orchestrator(engine, redis_client=None) -> AgenticOrchestrator:
    """Obtain or initialize singleton AgenticOrchestrator."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = AgenticOrchestrator(engine, redis_client)
    return _orchestrator_instance
