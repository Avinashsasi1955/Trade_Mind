"""
Brain 6 — ExecutionBrain

Shadow-ledger advisory/monitoring brain.

This brain intentionally does not use the legacy PaperBroker/holdings tables.
The canonical paper-trading writer remains:

    backend.live_inference -> record_shadow_signal -> shadow_execution_audits

ExecutionBrain can observe approved advisory signals, inspect open shadow
positions, publish alerts, and explain what it would do. It cannot place
orders or bypass the Senior/risk/live-inference path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text

from .base_brain import BaseBrain
from .bus import PositionAlert, RiskVerdictReady, TradeExecuted
from ..config import DATABASE_URL

logger = logging.getLogger("nivesh.brain.execution")


class ExecutionBrain(BaseBrain):
    name = "ExecutionBrain"

    def _register_handlers(self):
        self.bus.subscribe(RiskVerdictReady, self._on_risk_verdict)

    def _on_risk_verdict(self, event: RiskVerdictReady) -> None:
        self._log("info", "received RiskVerdictReady",
                  approved=len(event.approved_signals), user_id=event.user_id)
        self._log("debug", "advisory only; live_inference owns shadow trade writes")

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        user_id = context.get("user_id", 0)
        approved = context.get("approved_signals", [])
        self._log("info", "shadow advisory execution check", approved=len(approved), user_id=user_id)

        ledger = self._shadow_ledger_summary()
        advisory = self._best_advisory(approved, ledger)
        alerts = self._monitor_shadow_positions(user_id)

        event = TradeExecuted(
            source_brain=self.name,
            trade=advisory,
            user_id=user_id,
            direction=advisory.get("action", "") if advisory else "",
        )
        self.bus.publish(event)

        return {
            "mode": "ADVISORY_ONLY",
            "trade": advisory,
            "shadow_ledger": ledger,
            "position_alerts": alerts,
            "writer": "backend.live_inference.record_shadow_signal",
            "orders_allowed": False,
        }

    def _best_advisory(self, approved: List[Dict], ledger: Dict) -> Optional[Dict]:
        if not approved:
            return None
        best = max(approved, key=lambda item: item.get("confidence", 0))
        return {
            "status": "ADVISORY_ONLY_NOT_EXECUTED",
            "symbol": best.get("symbol"),
            "action": best.get("action"),
            "direction": best.get("signal_direction"),
            "confidence": best.get("confidence"),
            "strategy": best.get("strategy"),
            "reason": "ExecutionBrain is connected to the real shadow ledger for observation only; live_inference is the only paper-trade writer.",
            "shadow_open_trades": ledger.get("open_trades", 0),
            "orders_allowed": False,
        }

    def _shadow_ledger_summary(self) -> Dict:
        if not DATABASE_URL:
            return {"status": "unavailable", "reason": "DATABASE_URL missing", "open_trades": 0}
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection:
                row = connection.execute(text("""
                    SELECT COUNT(*) FILTER(WHERE audit_status='RECONCILED' AND net_pnl IS NULL) open_trades,
                           COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
                           COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) realised_pnl,
                           MAX(signal_at) latest_signal
                    FROM shadow_execution_audits
                    WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                """)).mappings().one()
            return {
                "status": "connected",
                "source": "shadow_execution_audits",
                "open_trades": int(row["open_trades"] or 0),
                "closed_trades": int(row["closed_trades"] or 0),
                "realised_pnl": float(row["realised_pnl"] or 0),
                "latest_signal": row["latest_signal"].isoformat() if row["latest_signal"] else None,
            }
        except Exception as exc:
            return {"status": "error", "reason": str(exc), "open_trades": 0}
        finally:
            engine.dispose()

    def _monitor_shadow_positions(self, user_id: int) -> List[Dict]:
        if not DATABASE_URL:
            return []
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        alerts: List[Dict] = []
        try:
            with engine.connect() as connection:
                rows = connection.execute(text("""
                    SELECT a.id,i.symbol,i.instrument_type,a.side,a.decision_price,
                           a.stop_loss_price,a.take_profit_price,
                           COALESCE(latest.close_price,a.decision_price) latest_price
                    FROM shadow_execution_audits a
                    JOIN instrument_master i ON i.id=a.instrument_id
                    LEFT JOIN LATERAL (
                        SELECT close_price FROM live_market_bars
                        WHERE instrument_id=a.instrument_id AND bar_time<=CURRENT_TIMESTAMP
                        ORDER BY bar_time DESC LIMIT 1
                    ) latest ON TRUE
                    WHERE a.audit_status='RECONCILED' AND a.net_pnl IS NULL
                    ORDER BY a.signal_at DESC
                    LIMIT 25
                """)).mappings().all()
            for row in rows:
                alert = self._shadow_alert(row)
                if alert:
                    alerts.append(alert)
                    self._fire_alert(user_id, alert["symbol"], alert["alert_type"],
                                     alert["current_price"], alert["entry_price"], alert["pnl"])
        except Exception as exc:
            self._log("warning", f"shadow position monitor error: {exc}")
        finally:
            engine.dispose()
        return alerts

    def _shadow_alert(self, row) -> Optional[Dict]:
        side = str(row["side"] or "").upper()
        current = float(row["latest_price"] or 0)
        entry = float(row["decision_price"] or current or 0)
        stop = float(row["stop_loss_price"]) if row["stop_loss_price"] is not None else None
        target = float(row["take_profit_price"]) if row["take_profit_price"] is not None else None
        alert_type = None
        if side == "BUY":
            if stop is not None and current <= stop:
                alert_type = "STOP_NEAR_OR_HIT"
            elif target is not None and current >= target:
                alert_type = "TARGET_NEAR_OR_HIT"
        elif side == "SELL":
            if stop is not None and current >= stop:
                alert_type = "STOP_NEAR_OR_HIT"
            elif target is not None and current <= target:
                alert_type = "TARGET_NEAR_OR_HIT"
        if not alert_type:
            return None
        pnl = (current - entry) if side == "BUY" else (entry - current)
        return {
            "audit_id": int(row["id"]),
            "symbol": row["symbol"],
            "instrument_type": row["instrument_type"],
            "side": side,
            "alert_type": alert_type,
            "current_price": current,
            "entry_price": entry,
            "pnl": round(pnl, 2),
        }

    def _fire_alert(self, user_id, symbol, alert_type, current, entry, pnl):
        self._log("warning", f"PositionAlert: {alert_type}", symbol=symbol, pnl=round(pnl, 2))
        self.bus.publish(PositionAlert(
            source_brain=self.name,
            symbol=symbol,
            alert_type=alert_type,
            current_price=current,
            entry_price=entry,
            pnl=round(pnl, 2),
            user_id=user_id,
        ))
