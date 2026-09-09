"""
Brain 2 — ScreenerBrain
Responsibility: Filter the raw universe down to a shortlist of high-quality
candidates using quality criteria (liquidity, volatility thresholds, sector
filters) before handing off to SignalBrain.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .base_brain import BaseBrain
from .bus import MarketDataReady, ScreenerResultReady

logger = logging.getLogger("nivesh.brain.screener")

# ---------------------------------------------------------------------------
# Screening thresholds (tunable)
# ---------------------------------------------------------------------------
MIN_VOLUME_RATIO = 0.6     # at least 0.6× average volume
MAX_VOLATILITY   = 4.5     # skip extremely erratic stocks
MIN_PRICE        = 10.0    # skip penny stocks
MAX_CANDIDATES   = 40      # pass at most 40 stocks to SignalBrain


class ScreenerBrain(BaseBrain):
    name = "ScreenerBrain"

    def _register_handlers(self):
        self.bus.subscribe(MarketDataReady, self._on_market_data)

    # ------------------------------------------------------------------
    # Bus handler — triggered automatically when MarketIntelBrain fires
    # ------------------------------------------------------------------
    def _on_market_data(self, event: MarketDataReady) -> None:
        self._log("info", "received MarketDataReady", stocks=len(event.stocks))
        result = self._screen(event.stocks)
        screener_event = ScreenerResultReady(
            source_brain=self.name,
            candidates=result["candidates"],
            screener_stats=result["stats"],
        )
        self.bus.publish(screener_event)

    # ------------------------------------------------------------------
    # Direct run (used by scheduler without bus)
    # ------------------------------------------------------------------
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        stocks: List[Dict] = context.get("stocks", [])
        self._log("info", "screening", total=len(stocks))
        return self._screen(stocks)

    # ------------------------------------------------------------------
    # Core screening logic
    # ------------------------------------------------------------------
    def _screen(self, stocks: List[Dict]) -> Dict[str, Any]:
        from ..sector_momentum import is_stock_in_leading_sector
        from ..mtf_confirmation import evaluate_mtf_confirmation

        candidates = []
        for s in stocks:
            if s.get("volume_ratio", 0) < MIN_VOLUME_RATIO:
                continue
            if s.get("volatility", 0) > MAX_VOLATILITY:
                continue
            if s.get("price", 0) < MIN_PRICE:
                continue

            symbol = s.get("symbol", "")
            sector_check = is_stock_in_leading_sector(symbol)
            mtf_check = evaluate_mtf_confirmation(symbol, "LONG" if s.get("price_change", 0) >= 0 else "SHORT")

            candidates.append({
                **s,
                "sector_momentum": sector_check,
                "mtf_confirmation": mtf_check,
            })

        # Rank by combined momentum and liquidity score
        candidates.sort(
            key=lambda x: (
                x.get("sector_momentum", {}).get("rs_score", 0.0) +
                (x.get("mtf_confirmation", {}).get("mtf_score", 50.0) / 20.0) +
                abs(x.get("price_change", 0))
            ),
            reverse=True
        )

        selected = candidates[:MAX_CANDIDATES]
        self._log("info", "screening complete",
                  screened=len(selected), total=len(stocks))

        return {
            "candidates": selected,
            "stats": {
                "total_screened": len(stocks),
                "passed": len(selected),
                "rejected": len(stocks) - len(selected),
                "min_volume_ratio": MIN_VOLUME_RATIO,
                "max_volatility": MAX_VOLATILITY,
                "sector_filter_active": True,
                "mtf_filter_active": True,
            },
        }
