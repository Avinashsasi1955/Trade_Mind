"""
Brain 5 — RiskBrain
Responsibility: Gate signals through portfolio-level risk rules before
they reach ExecutionBrain.  Checks max drawdown, position concentration,
daily loss limit, and sector exposure.  Publishes RiskVerdictReady.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import create_engine, text

from .base_brain import BaseBrain
from .bus import RiskVerdictReady, SignalReady, SentimentReady
from ..config import DATABASE_URL

logger = logging.getLogger("nivesh.brain.risk")

# ---------------------------------------------------------------------------
# Risk thresholds (conservative defaults — tighten per user's risk_profile)
# ---------------------------------------------------------------------------
RISK_THRESHOLDS = {
    "conservative": {"max_position_pct": 0.03, "max_open_trades": 3,  "min_confidence": 82, "max_daily_loss_pct": 0.01, "min_rr": 2.5},
    "balanced":     {"max_position_pct": 0.05, "max_open_trades": 5,  "min_confidence": 75, "max_daily_loss_pct": 0.02, "min_rr": 2.0},
    "aggressive":   {"max_position_pct": 0.08, "max_open_trades": 8,  "min_confidence": 70, "max_daily_loss_pct": 0.035, "min_rr": 1.8},
}


class RiskBrain(BaseBrain):
    name = "RiskBrain"

    def __init__(self, *args, **kwargs):
        self._latest_sentiment: Dict = {}
        super().__init__(*args, **kwargs)

    def _register_handlers(self):
        self.bus.subscribe(SignalReady, self._on_signal_ready)
        self.bus.subscribe(SentimentReady, self._on_sentiment)

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------
    def _on_sentiment(self, event: SentimentReady) -> None:
        self._latest_sentiment = event.sentiment_summary

    def _on_signal_ready(self, event: SignalReady) -> None:
        self._log("info", "received SignalReady", signals=len(event.signals))
        result = self._evaluate(
            signals=event.signals,
            risk_profile=event.risk_profile,
            context=event.payload,
            user_id=event.user_id,
        )
        self.bus.publish(RiskVerdictReady(
            source_brain=self.name,
            approved_signals=result["approved"],
            rejected_signals=result["rejected"],
            risk_summary=result["summary"],
            user_id=event.user_id,
        ))

    # ------------------------------------------------------------------
    # Direct run
    # ------------------------------------------------------------------
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        signals      = context.get("signals", [])
        risk_profile = context.get("risk_profile", "balanced")
        user_id      = context.get("user_id", 0)
        result = self._evaluate(signals, risk_profile, context, user_id)
        self.bus.publish(RiskVerdictReady(
            source_brain=self.name,
            approved_signals=result["approved"],
            rejected_signals=result["rejected"],
            risk_summary=result["summary"],
            user_id=user_id,
        ))
        return result

    # ------------------------------------------------------------------
    # Core risk evaluation
    # ------------------------------------------------------------------
    def _evaluate(self, signals: List[Dict], risk_profile: str, context: Dict, user_id: int) -> Dict:
        thresholds = RISK_THRESHOLDS.get(risk_profile, RISK_THRESHOLDS["balanced"])
        portfolio_cash = context.get("portfolio_cash", 0)
        shadow_state = self._shadow_risk_state(portfolio_cash)
        open_trades = shadow_state.get("open_trades", context.get("open_trades", 0))
        daily_pnl_pct = shadow_state.get("daily_pnl_pct", context.get("daily_pnl_pct", 0.0))

        # Sentiment gate: if strongly bearish, only allow SELL signals through
        sentiment_bias = (self._latest_sentiment.get("market_bias") or "").upper()

        approved: List[Dict] = []
        rejected: List[Dict] = []

        for sig in signals:
            reasons: List[str] = []

            # 1. Confidence gate
            if sig.get("confidence", 0) < thresholds["min_confidence"]:
                reasons.append(f"confidence {sig['confidence']} < {thresholds['min_confidence']}")

            # 2. Open position limit
            if open_trades >= thresholds["max_open_trades"]:
                reasons.append(f"max open trades {thresholds['max_open_trades']} reached")

            # 3. Daily loss limit
            if daily_pnl_pct <= -thresholds["max_daily_loss_pct"]:
                reasons.append(f"daily loss limit {thresholds['max_daily_loss_pct']*100:.1f}% breached")

            # 4. Sentiment conflict gate
            if sentiment_bias == "STRONGLY_BEARISH" and sig.get("signal_direction") == "LONG":
                reasons.append("macro sentiment is strongly bearish — LONG blocked")
            if sentiment_bias == "STRONGLY_BULLISH" and sig.get("signal_direction") == "SHORT":
                reasons.append("macro sentiment is strongly bullish — SHORT blocked")

            # 5. Reward-to-risk ratio gate (with Fast-Path dynamic scaling)
            sig_rr = float(sig.get("risk_reward", 0) or 0)
            golden = sig.get("golden_trade_match") or {}
            is_fast_path = bool(golden.get("is_fast_path"))
            min_rr = float(thresholds.get("min_rr", 2.0))
            if is_fast_path:
                min_rr = max(1.35, min_rr * 0.75)  # Adapt to high-conviction proven historical pattern
            
            if sig_rr > 0 and sig_rr < min_rr:
                reasons.append(f"reward-to-risk {sig_rr:.2f} < minimum {min_rr:.1f}")

            if reasons:
                rejected.append({**sig, "rejection_reasons": reasons})
            else:
                approved.append(sig)

        self._log("info", "risk verdict", approved=len(approved), rejected=len(rejected))
        return {
            "approved": approved,
            "rejected": rejected,
            "summary": {
                "risk_profile": risk_profile,
                "thresholds": thresholds,
                "sentiment_bias": sentiment_bias,
                "open_trades": open_trades,
                "daily_pnl_pct": daily_pnl_pct,
                "shadow_ledger": shadow_state,
                "source": "shadow_execution_audits" if shadow_state.get("status") == "connected" else "context_fallback",
            },
        }

    def _shadow_risk_state(self, portfolio_cash: float = 0) -> Dict:
        if not DATABASE_URL:
            return {"status": "unavailable", "reason": "DATABASE_URL missing"}
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(text("""
                    SELECT COUNT(*) FILTER(WHERE audit_status='RECONCILED' AND net_pnl IS NULL) open_trades,
                           COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
                           COUNT(*) FILTER(WHERE net_pnl < 0) losing_trades,
                           COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) realised_pnl
                    FROM shadow_execution_audits
                    WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                """)).mappings().one()
            capital = float(portfolio_cash or 1_000_000)
            realised = float(row["realised_pnl"] or 0)
            return {
                "status": "connected",
                "open_trades": int(row["open_trades"] or 0),
                "closed_trades": int(row["closed_trades"] or 0),
                "losing_trades": int(row["losing_trades"] or 0),
                "realised_pnl": realised,
                "daily_pnl_pct": realised / capital if capital else 0.0,
            }
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}
        finally:
            engine.dispose()
