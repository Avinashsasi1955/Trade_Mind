"""
Nivesh Brain Architecture — public API.

Usage:
    from backend.brains import get_brain_pipeline, run_brain_pipeline

The pipeline is a singleton; call get_brain_pipeline() to get the wired set
of brains, then call run_brain_pipeline(context) to run a full cycle.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .bus import get_bus
from .base_brain import BaseBrain
from .market_intel_brain import MarketIntelBrain
from .screener_brain import ScreenerBrain
from .signal_brain import SignalBrain
from .sentiment_brain import SentimentBrain
from .risk_brain import RiskBrain
from .execution_brain import ExecutionBrain
from .report_brain import ReportBrain
from .trading_coach import (
    run_coach_audit,
    get_coach_proposals,
    get_latest_coach_audit,
    apply_coach_proposal,
    dismiss_coach_proposal,
    ensure_coach_schema,
)

logger = logging.getLogger("nivesh.brains")

__all__ = [
    "get_bus",
    "BaseBrain",
    "MarketIntelBrain",
    "ScreenerBrain",
    "SignalBrain",
    "SentimentBrain",
    "RiskBrain",
    "ExecutionBrain",
    "ReportBrain",
    "get_brain_pipeline",
    "run_brain_pipeline",
    "run_coach_audit",
    "get_coach_proposals",
    "get_latest_coach_audit",
    "apply_coach_proposal",
    "dismiss_coach_proposal",
    "ensure_coach_schema",
]

# ---------------------------------------------------------------------------
# Singleton pipeline
# ---------------------------------------------------------------------------

_pipeline: Optional[Dict[str, BaseBrain]] = None
_pipeline_lock = threading.Lock()


def get_brain_pipeline() -> Dict[str, BaseBrain]:
    """Return (creating if needed) the shared, wired brain pipeline."""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                bus = get_bus()
                _pipeline = {
                    "market_intel": MarketIntelBrain(bus=bus),   # Brain 1
                    "screener":     ScreenerBrain(bus=bus),       # Brain 2
                    "signal":       SignalBrain(bus=bus),          # Brain 3
                    "sentiment":    SentimentBrain(bus=bus),       # Brain 4
                    "risk":         RiskBrain(bus=bus),            # Brain 5
                    "execution":    ExecutionBrain(bus=bus),       # Brain 6
                    "report":       ReportBrain(bus=bus),          # Brain 7
                }
                logger.info("Brain pipeline initialised with %d brains", len(_pipeline))
    return _pipeline


def run_brain_pipeline(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run a full brain pipeline cycle.  This is a sequential orchestrated run:
      1. SentimentBrain  (non-blocking advisory)
      2. MarketIntelBrain → publishes MarketDataReady
      3. ScreenerBrain   → filters candidates
      4. SignalBrain     → generates BUY + SELL signals
      5. RiskBrain       → gates signals
      6. ExecutionBrain  → observes approved signals + monitors shadow ledger
      7. ReportBrain     → builds session report from shadow_execution_audits

    This pipeline is advisory.  The real paper-trade writer remains
    backend.live_inference.record_shadow_signal -> shadow_execution_audits.
    """
    pipeline = get_brain_pipeline()
    results: Dict[str, Any] = {}

    db          = context.get("db")
    user_id     = context.get("user_id", 0)
    risk_profile= context.get("risk_profile", "balanced")
    positions   = context.get("positions", {})
    execute     = context.get("execute_trade", True)
    tick        = context.get("tick", 0)

    # Step 1 — Sentiment (advisory, non-blocking)
    try:
        results["sentiment"] = pipeline["sentiment"].run({"db": db, "user_id": user_id, "tick": tick})
    except Exception as exc:
        logger.warning("SentimentBrain failed: %s", exc)
        results["sentiment"] = {}

    # Step 2 — Market data
    prior_runs = context.get("prior_runs", 1)
    market_result = pipeline["market_intel"].run({"prior_runs": prior_runs})
    results["market"] = market_result
    stocks = market_result.get("stocks", [])

    # Step 3 — Screener
    screener_result = pipeline["screener"].run({"stocks": stocks})
    results["screener"] = screener_result
    candidates = screener_result.get("candidates", stocks)  # fallback to all if screener empty

    # Step 4 — Signals (both BUY and SELL)
    signal_result = pipeline["signal"].run({
        "stocks": candidates,
        "risk_profile": risk_profile,
        "positions": positions,
        "user_id": user_id,
    })
    results["signals"] = signal_result

    # Step 5 — Risk gating
    portfolio_cash = context.get("portfolio_cash", 0)
    open_trades    = context.get("open_trades", 0)
    risk_result = pipeline["risk"].run({
        "signals": signal_result.get("signals", []),
        "risk_profile": risk_profile,
        "user_id": user_id,
        "portfolio_cash": portfolio_cash,
        "open_trades": open_trades,
        "daily_pnl_pct": context.get("daily_pnl_pct", 0.0),
    })
    results["risk"] = risk_result

    # Step 6 — Execution (only if execute_trade=True)
    if execute:
        exec_result = pipeline["execution"].run({
            "db": db,
            "user_id": user_id,
            "risk_profile": risk_profile,
            "approved_signals": risk_result.get("approved", []),
        })
        results["execution"] = exec_result
    else:
        results["execution"] = {"trade": None}

    if context.get("include_report"):
        results["report"] = pipeline["report"].run({"db": db, "user_id": user_id})

    return results
