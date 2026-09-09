"""
Brain 7 — ReportBrain
Responsibility: Collect TradeExecuted + PositionAlert events throughout
the session and produce a structured daily summary report.  Stores the
report in agent_runs and publishes DailyReportReady.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from .base_brain import BaseBrain
from .bus import DailyReportReady, PositionAlert, TradeExecuted
from ..config import DATABASE_URL

logger = logging.getLogger("nivesh.brain.report")
IST = ZoneInfo("Asia/Kolkata")


class ReportBrain(BaseBrain):
    name = "ReportBrain"

    def __init__(self, *args, **kwargs):
        self._trades: List[Dict] = []
        self._alerts: List[Dict] = []
        super().__init__(*args, **kwargs)

    def _register_handlers(self):
        self.bus.subscribe(TradeExecuted, self._on_trade)
        self.bus.subscribe(PositionAlert, self._on_alert)

    # ------------------------------------------------------------------
    # Bus handlers — accumulate events during the session
    # ------------------------------------------------------------------
    def _on_trade(self, event: TradeExecuted) -> None:
        if event.trade:
            self._trades.append({**event.trade, "ts": event.ts, "user_id": event.user_id})

    def _on_alert(self, event: PositionAlert) -> None:
        self._alerts.append({
            "symbol": event.symbol,
            "alert_type": event.alert_type,
            "current_price": event.current_price,
            "entry_price": event.entry_price,
            "pnl": event.pnl,
            "ts": event.ts,
            "user_id": event.user_id,
        })

    # ------------------------------------------------------------------
    # Direct run — generate and publish report
    # ------------------------------------------------------------------
    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        db      = context.get("db")
        user_id = context.get("user_id", 0)

        self._log("info", "generating report", trades=len(self._trades), alerts=len(self._alerts))
        ledger_report = self._shadow_ledger_report()
        if ledger_report.get("status") == "connected":
            event = DailyReportReady(source_brain=self.name, report=ledger_report, user_id=user_id)
            self.bus.publish(event)
            self._log("info", "shadow-ledger report published", pnl=ledger_report.get("realised_pnl"))
            return ledger_report

        # P&L summary
        total_pnl  = sum(a["pnl"] for a in self._alerts)
        buy_trades  = [t for t in self._trades if t.get("action") == "BUY"]
        sell_trades = [t for t in self._trades if t.get("action") == "SELL"]

        # Stop/target breakdown
        stops_hit   = [a for a in self._alerts if a["alert_type"] == "STOP_HIT"]
        targets_hit = [a for a in self._alerts if a["alert_type"] == "TARGET_HIT"]

        report = {
            "status": "event_memory_fallback",
            "source": "brain_bus_events",
            "date": datetime.now(IST).date().isoformat(),
            "total_trades": len(self._trades),
            "buy_trades": len(buy_trades),
            "sell_trades": len(sell_trades),
            "total_alerts": len(self._alerts),
            "stops_hit": len(stops_hit),
            "targets_hit": len(targets_hit),
            "estimated_session_pnl": round(total_pnl, 2),
            "trades": self._trades[-20:],   # last 20 trades
            "alerts": self._alerts[-20:],
        }

        # Persist summary to agent_runs (optional, only if DB provided)
        if db:
            try:
                from ..database import now_iso
                db.execute(
                    "INSERT INTO agent_runs(user_id,status,stocks_scanned,opportunities,trades_placed,summary,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (user_id, "REPORT", 0, len(self._trades), len(self._trades), str(report), now_iso()),
                )
                db.commit()
            except Exception as exc:
                self._log("warning", f"report DB write failed: {exc}")

        event = DailyReportReady(source_brain=self.name, report=report, user_id=user_id)
        self.bus.publish(event)
        self._log("info", "report published", pnl=report["estimated_session_pnl"])

        # Reset for next session
        self._trades.clear()
        self._alerts.clear()

        return report

    def _shadow_ledger_report(self) -> Dict:
        if not DATABASE_URL:
            return {"status": "unavailable", "reason": "DATABASE_URL missing"}
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        try:
            with engine.connect() as connection:
                summary = connection.execute(text("""
                    SELECT COUNT(*) trades,
                           COUNT(*) FILTER(WHERE net_pnl IS NULL AND audit_status='RECONCILED') open_trades,
                           COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
                           COUNT(*) FILTER(WHERE side='BUY') buy_trades,
                           COUNT(*) FILTER(WHERE side='SELL') sell_trades,
                           COUNT(*) FILTER(WHERE exit_reason='STOP_LOSS') stops_hit,
                           COUNT(*) FILTER(WHERE exit_reason IN ('TARGET_HIT','PROFIT_CAPTURE','TRAILING_PROFIT_CAPTURE')) targets_hit,
                           COALESCE(SUM(net_pnl) FILTER(WHERE net_pnl IS NOT NULL),0) realised_pnl,
                           MAX(signal_at) latest_signal
                    FROM shadow_execution_audits
                    WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                """)).mappings().one()
                recent = connection.execute(text("""
                    SELECT a.id,i.symbol,i.instrument_type,a.side,a.quantity,a.decision_price,
                           a.realised_exit_price,a.net_pnl,a.exit_reason,a.signal_at,a.exit_at
                    FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
                    WHERE (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=CURRENT_DATE
                    ORDER BY a.signal_at DESC
                    LIMIT 20
                """)).mappings().all()
            return {
                "status": "connected",
                "source": "shadow_execution_audits",
                "date": datetime.now(IST).date().isoformat(),
                "total_trades": int(summary["trades"] or 0),
                "open_trades": int(summary["open_trades"] or 0),
                "closed_trades": int(summary["closed_trades"] or 0),
                "buy_trades": int(summary["buy_trades"] or 0),
                "sell_trades": int(summary["sell_trades"] or 0),
                "stops_hit": int(summary["stops_hit"] or 0),
                "targets_hit": int(summary["targets_hit"] or 0),
                "realised_pnl": float(summary["realised_pnl"] or 0),
                "latest_signal": summary["latest_signal"].isoformat() if summary["latest_signal"] else None,
                "recent_trades": [dict(row) for row in recent],
                "orders_allowed": False,
            }
        except Exception as exc:
            return {"status": "error", "source": "shadow_execution_audits", "reason": str(exc)}
        finally:
            engine.dispose()
