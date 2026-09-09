"""
Brain 1 — MarketIntelBrain
Responsibility: Fetch the raw market snapshot (quotes, volumes, technicals)
and publish MarketDataReady so downstream brains can process it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base_brain import BaseBrain
from .bus import MarketDataReady

logger = logging.getLogger("nivesh.brain.market_intel")


class MarketIntelBrain(BaseBrain):
    name = "MarketIntelBrain"

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch market snapshot and publish to bus."""
        from ..service import market_snapshot, security_master_stats

        prior_runs = context.get("prior_runs", 1)
        self._log("info", "fetching market snapshot", prior_runs=prior_runs)

        stocks = market_snapshot(prior_runs)
        universe = security_master_stats().get("counts", {})

        event = MarketDataReady(
            source_brain=self.name,
            stocks=stocks,
            universe_count=universe.get("total", 0),
        )
        self.bus.publish(event)
        self._log("info", "published MarketDataReady", stocks=len(stocks))
        return {"stocks": stocks, "universe": universe}
