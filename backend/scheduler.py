import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .database import connect
from .service import refresh_market_analyses, refresh_sentiments, run_agent
from .monitoring import record_event
from .config import ML_AUTO_RETRAIN, ML_RETRAIN_DAYS
from .ml_pipeline import build_research_dataset, detect_drift, generate_shadow_predictions, resolve_shadow_predictions, train_model, walk_forward_validate
from .execution import reconcile_intents
from .config import LIVE_TRADING_ENABLED
from .backup import backup_databases
from .brains import get_brain_pipeline, run_brain_pipeline

IST = ZoneInfo("Asia/Kolkata")

# Pre-initialise the brain pipeline at import time so brains subscribe to bus
# events before the first scheduler tick fires.
_brains = get_brain_pipeline()


def _worker():
    last_scan = {}
    last_minute_analysis = {}
    last_retrain = 0.0
    last_reconcile = {}
    last_backup = 0.0
    last_eod_report = {}   # track per-user end-of-day reports

    while True:
        now = datetime.now(IST)
        tick = int(time.time() // 60)

        # -----------------------------------------------------------------
        # Daily database backup (after 18:00 IST)
        # -----------------------------------------------------------------
        if now.hour >= 18 and time.time() - last_backup >= 86400:
            try:
                result = backup_databases()
                with connect() as db:
                    record_event(db, "backup", "INFO", "Daily database backup completed", payload=result)
            except Exception as exc:
                with connect() as db:
                    record_event(db, "backup", "ERROR", "Daily database backup failed", payload={"error": str(exc)[:300]})
            last_backup = time.time()

        with connect() as db:
            record_event(db, "scheduler", "INFO", "Scheduler heartbeat", payload={"market_time": now.isoformat()})

        market_hours = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)

        # -----------------------------------------------------------------
        # ML auto-retrain (post-market)
        # -----------------------------------------------------------------
        if ML_AUTO_RETRAIN and now.hour >= 18 and time.time() - last_retrain >= ML_RETRAIN_DAYS * 86400:
            try:
                dataset    = build_research_dataset()
                model      = train_model()
                validation = walk_forward_validate()
                resolution = resolve_shadow_predictions()
                shadow     = generate_shadow_predictions()
                drift      = detect_drift()
                with connect() as db:
                    record_event(db, "ml_pipeline", "INFO", "Scheduled research retraining completed",
                                 payload={"dataset": dataset, "model": model, "validation": validation["summary"],
                                          "shadow_resolution": resolution, "shadow": shadow, "drift": drift["overall"]})
                last_retrain = time.time()
            except Exception as exc:
                with connect() as db:
                    record_event(db, "ml_pipeline", "ERROR", "Scheduled research retraining failed", payload={"error": str(exc)[:300]})
                last_retrain = time.time()

        # -----------------------------------------------------------------
        # Market hours tasks
        # -----------------------------------------------------------------
        if market_hours:
            with connect() as db:
                users = db.execute("SELECT user_id, risk_profile FROM settings WHERE auto_scan=1").fetchall()
                for row in users:
                    user_id      = row["user_id"]
                    risk_profile = row["risk_profile"] if "risk_profile" in row.keys() else "balanced"

                    # Live broker reconciliation (every 60s)
                    if LIVE_TRADING_ENABLED and time.time() - last_reconcile.get(user_id, 0) >= 60:
                        reconcile_intents(db, user_id)
                        last_reconcile[user_id] = time.time()

                    # Market analyses + sentiment refresh (every 60s)
                    if time.time() - last_minute_analysis.get(user_id, 0) >= 60:
                        refresh_market_analyses(db, user_id)
                        refresh_sentiments(db, user_id, tick)
                        last_minute_analysis[user_id] = time.time()

                    # Brain pipeline scan (every 30 minutes) — intelligence refresh, no live execution
                    if time.time() - last_scan.get(user_id, 0) >= 1800:
                        try:
                            position_rows = db.execute(
                                "SELECT symbol, quantity, average_price FROM holdings WHERE user_id=?", (user_id,)
                            ).fetchall()
                            positions = {r["symbol"]: dict(r) for r in position_rows}
                            portfolio = db.execute(
                                "SELECT cash FROM portfolios WHERE user_id=?", (user_id,)
                            ).fetchone()
                            open_trades = db.execute(
                                "SELECT COUNT(*) cnt FROM holdings WHERE user_id=? AND quantity != 0", (user_id,)
                            ).fetchone()["cnt"]
                            prior_runs = db.execute(
                                "SELECT COUNT(*) cnt FROM agent_runs WHERE user_id=?", (user_id,)
                            ).fetchone()["cnt"]

                            brain_results = run_brain_pipeline({
                                "db": db,
                                "user_id": user_id,
                                "risk_profile": risk_profile,
                                "positions": positions,
                                "portfolio_cash": portfolio["cash"] if portfolio else 0,
                                "open_trades": open_trades,
                                "prior_runs": prior_runs + 1,
                                "tick": tick,
                                # Scheduled scans are intelligence-only; manual approval places live orders.
                                "execute_trade": False,
                            })
                            record_event(db, "brain_pipeline", "INFO",
                                         "Brain pipeline scan completed",
                                         payload={
                                             "user_id": user_id,
                                             "screener_passed": brain_results.get("screener", {}).get("stats", {}).get("passed", 0),
                                             "buy_signals": brain_results.get("signals", {}).get("buy_count", 0),
                                             "sell_signals": brain_results.get("signals", {}).get("sell_count", 0),
                                             "risk_approved": len(brain_results.get("risk", {}).get("approved", [])),
                                         })
                        except Exception as exc:
                            record_event(db, "brain_pipeline", "ERROR",
                                         "Brain pipeline scan failed",
                                         payload={"error": str(exc)[:300], "user_id": user_id})
                        last_scan[user_id] = time.time()

        # -----------------------------------------------------------------
        # End-of-day report (15:35 IST — just after market close)
        # -----------------------------------------------------------------
        eod_window = now.weekday() < 5 and now.hour == 15 and now.minute >= 35
        if eod_window:
            with connect() as db:
                users = db.execute("SELECT user_id FROM settings WHERE auto_scan=1").fetchall()
                for row in users:
                    user_id = row["user_id"]
                    today   = now.date().isoformat()
                    if last_eod_report.get(user_id) != today:
                        try:
                            report = _brains["report"].run({"db": db, "user_id": user_id})
                            record_event(db, "eod_report", "INFO", "End-of-day brain report generated",
                                         payload={"user_id": user_id, "pnl": report.get("estimated_session_pnl", 0),
                                                  "trades": report.get("total_trades", 0)})
                            last_eod_report[user_id] = today
                        except Exception as exc:
                            record_event(db, "eod_report", "ERROR", "End-of-day report failed",
                                         payload={"error": str(exc)[:300], "user_id": user_id})

        time.sleep(60)


def start_scheduler():
    thread = threading.Thread(target=_worker, name="nivesh-market-scanner", daemon=True)
    thread.start()
    return thread
