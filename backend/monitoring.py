import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .database import now_iso
from .history_store import HistoryStore
from .config import DATABASE_URL, KITE_ACCESS_TOKEN, KITE_API_KEY, LIVE_TRADING_ENABLED, MARKET_DATA_PROVIDER, UPSTOX_ACCESS_TOKEN

try:
    from sqlalchemy import create_engine, text
except Exception:
    create_engine = None
    text = None


def record_event(db, component: str, level: str, message: str, user_id: Optional[int] = None, payload: Optional[Dict] = None):
    db.execute("INSERT INTO monitoring_events(user_id,component,level,message,payload,created_at) VALUES(?,?,?,?,?,?)",
               (user_id, component, level, message, json.dumps(payload or {}), now_iso()))
    db.commit()


def _fast_history_stats() -> Dict:
    if DATABASE_URL and create_engine and text:
        try:
            from .service import _get_engine
            engine = _get_engine() or create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
            with engine.connect() as connection:
                row = connection.execute(text("SELECT reltuples::bigint AS bars FROM pg_class WHERE relname = 'live_market_bars'")).mappings().one_or_none()
                if row and row["bars"] is not None:
                    return {"bars": max(0, int(row["bars"]))}
        except Exception:
            pass
    return HistoryStore().stats()


def operations_status(db, user_id: int) -> Dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    heartbeat = db.execute("SELECT created_at FROM monitoring_events WHERE component='scheduler' ORDER BY id DESC LIMIT 1").fetchone()
    recent_errors = db.execute("SELECT component,level,message,created_at FROM monitoring_events WHERE level IN ('ERROR','CRITICAL') AND created_at>=? ORDER BY id DESC LIMIT 20", ((datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(),)).fetchall()
    gateway = db.execute("SELECT COUNT(*) count, SUM(CASE WHEN status!='SUCCESS' AND status!='CACHED' THEN 1 ELSE 0 END) failures FROM ai_gateway_logs WHERE user_id=? AND created_at>=?", (user_id, (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat())).fetchone()
    store = _fast_history_stats()
    broker = db.execute("SELECT status,last_heartbeat_at FROM broker_connections WHERE user_id=?",(user_id,)).fetchone()
    switch = db.execute("SELECT active,reason FROM kill_switches WHERE user_id=?",(user_id,)).fetchone()
    scheduler_ok = bool(heartbeat and heartbeat["created_at"] >= cutoff)
    live_bar=prediction=worker=preflight=None
    try:
        live_bar=db.execute("SELECT MAX(bar_time) latest,EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-MAX(bar_time))) age_seconds FROM live_market_bars WHERE interval='5minute' AND source IN ('zerodha_kite','kite_gap_backfill','upstox_v3','upstox_rest_5m')").fetchone()
        prediction=db.execute("SELECT MAX(timestamp) latest,COUNT(*) count FROM shadow_predictions WHERE timeframe='5minute'").fetchone()
        worker=db.execute("SELECT created_at FROM monitoring_events WHERE component='celery_worker' ORDER BY id DESC LIMIT 1").fetchone()
        preflight=db.execute("SELECT status,checked_at,blockers FROM operational_preflight_runs ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        pass
    live_fresh=bool(live_bar and live_bar["latest"] and float(live_bar["age_seconds"] or 999999)<=900)
    worker_ok=bool(worker and worker["created_at"]>=cutoff)
    broker_configured = bool(UPSTOX_ACCESS_TOKEN) if MARKET_DATA_PROVIDER == "upstox" else bool(KITE_API_KEY and KITE_ACCESS_TOKEN)
    components = [
        {"name":"API server","status":"operational","detail":"Authenticated HTTP service responding"},
        {"name":"Market scheduler","status":"operational" if scheduler_ok else "waiting","detail":"60-second heartbeat" if scheduler_ok else "No recent heartbeat; outside market hours is acceptable"},
        {"name":"Historical warehouse","status":"operational" if store["bars"] else "waiting","detail":f"{store['bars']:,} candles stored"},
        {"name":"Market data feed","status":"ready" if broker_configured else "not_connected",
         "detail":f"{MARKET_DATA_PROVIDER} credentials present" if broker_configured else "Awaiting selected provider token"},
        {"name":"Completed live bars","status":"operational" if live_fresh else "waiting","detail":f"Latest completed bar {live_bar['latest']}" if live_bar and live_bar["latest"] else "No completed provider bars recorded"},
        {"name":"Live ML inference","status":"operational" if prediction and prediction["latest"] else "waiting","detail":f"{prediction['count']} idempotent predictions; latest {prediction['latest']}" if prediction and prediction["latest"] else "Awaiting a completed live bar"},
        {"name":"Celery worker","status":"operational" if worker_ok else "waiting","detail":"Recent distributed worker heartbeat" if worker_ok else "No heartbeat in the last three minutes"},
        {"name":"Pre-market gate","status":"operational" if preflight and preflight["status"]=="PASS" else "waiting",
         "detail":f"{preflight['status']} at {preflight['checked_at']}" if preflight else "No formal readiness check recorded"},
        {"name":"Broker session","status":"ready" if broker and broker["status"]=="CONNECTED" else "not_connected","detail":broker["status"] if broker else "Disconnected"},
        {"name":"Kill switch","status":"locked" if switch and switch["active"] else "operational","detail":switch["reason"] if switch and switch["active"] else "Inactive and available"},
        {"name":"Order execution","status":"ready" if LIVE_TRADING_ENABLED else "locked","detail":"Explicit deployment flag enabled" if LIVE_TRADING_ENABLED else "Server-side live flag disabled"},
    ]
    return {"overall":"paper_operational", "components":components, "recent_errors":[dict(x) for x in recent_errors],
            "metrics":{"gateway_requests_24h":gateway["count"] or 0,"gateway_failures_24h":gateway["failures"] or 0,
                       "stored_bars":store["bars"],"live_orders":0}, "updated_at":now_iso()}
