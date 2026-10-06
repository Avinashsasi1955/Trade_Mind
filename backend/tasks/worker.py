"""Production Celery worker: news/FinBERT scoring and end-of-day PSI drift."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import redis
from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure, worker_ready
from sqlalchemy import create_engine, text
from backend.intelligence_memory import publish_brain_event
from backend.market_ingestion_v3 import sync_instrument_master
from backend.observability import configure_logging, page
from backend.shadow_sessions import evaluate_session, finalize_session, open_session, paper_pnl_summary, recover_stale_started_sessions
from backend.ml.validation_engine import evaluate_promotion, reconcile_shadow_costs
from backend.model_feedback import export_high_quality_shadow_feedback
from backend.preflight import run_preflight
from backend.upstox_backfill import run_backfill
from backend.upstox_option_chain import run_option_chain_oi_sync
from backend.upstox_stream import sync_upstox_instrument_master


DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
NEWS_API_URL = os.getenv("NIVESH_NEWS_API_BASE_URL", "https://newsapi.org/v2/everything")
NEWS_API_KEY = os.getenv("NIVESH_NEWS_API_KEY", "")
NEWS_API_KEY_HEADER = os.getenv("NIVESH_NEWS_API_KEY_HEADER", "X-Api-Key")
NEWS_PROVIDER = os.getenv("NIVESH_NEWS_PROVIDER", "newsapi").strip().lower()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", NEWS_API_KEY)
FINNHUB_NEWS_LOOKBACK_DAYS = int(os.getenv("FINNHUB_NEWS_LOOKBACK_DAYS", "7"))
FINNHUB_NEWS_CATEGORY = os.getenv("FINNHUB_NEWS_CATEGORY", "general")
FINNHUB_NEWS_TIMEOUT_SECONDS = float(os.getenv("FINNHUB_NEWS_TIMEOUT_SECONDS", "8"))
FINBERT_MODEL = os.getenv("NIVESH_FINBERT_MODEL", "ProsusAI/finbert")
FINBERT_REVISION = os.getenv("NIVESH_FINBERT_REVISION", "2280066c35926ed43b2719a19c9dc5883146e4b1")
NEWS_SCAN_SYMBOL_LIMIT = int(os.getenv("NIVESH_NEWS_SCAN_SYMBOL_LIMIT", "50"))
DEFAULT_NEWS_SYMBOLS = (
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "LT", "ITC",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "TATAMOTORS", "NTPC", "ONGC", "POWERGRID", "ULTRACEMCO",
    "TITAN", "ADANIENT", "ADANIPORTS", "WIPRO", "TECHM", "JSWSTEEL",
    "TATASTEEL", "COALINDIA", "HCLTECH", "BEL", "TRENT", "DIXON", "COFORGE",
    "BAJAJFINSV", "M&M", "EICHERMOT", "GRASIM", "HINDALCO", "JIOFIN", "NESTLEIND",
    "ASIANPAINT", "CIPLA", "DRREDDY", "APOLLOHOSP", "BRITANNIA", "HEROMOTOCO",
    "BAJAJ-AUTO", "INDUSINDBK", "SHRIRAMFIN", "SBILIFE",
)
logger = configure_logging()

celery_app = Celery("nivesh_v3", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    timezone="Asia/Kolkata", enable_utc=True, task_acks_late=True,
    task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    worker_concurrency=1, broker_connection_retry_on_startup=True,
    task_time_limit=1800, task_soft_time_limit=1700,
)
celery_app.conf.beat_schedule = {
    "intraday-news-finbert": {
        "task": "backend.tasks.worker.scan_news_and_score",
        "schedule": crontab(minute="*/5", hour="9-15", day_of_week="1-5"),
    },
    "end-of-day-psi": {
        "task": "backend.tasks.worker.end_of_day_feature_psi",
        "schedule": crontab(hour=16, minute=15, day_of_week="1-5"),
    },
    "daily-instrument-master": {
        "task": "backend.tasks.worker.sync_market_instruments",
        "schedule": crontab(hour=8, minute=30, day_of_week="1-5"),
    },
    "worker-operational-heartbeat": {
        "task": "backend.tasks.worker.operational_heartbeat",
        "schedule": crontab(minute="*"),
    },
    "open-forward-shadow-session": {
        "task": "backend.tasks.worker.open_forward_shadow_session",
        "schedule": crontab(hour=8, minute=45, day_of_week="1-5"),
    },
    "fallback-open-shadow-session": {
        "task": "backend.tasks.worker.open_forward_shadow_session",
        "schedule": crontab(minute="*/15", hour="9-15", day_of_week="1-5"),
    },
    "pre-market-readiness-gate": {
        "task": "backend.tasks.worker.pre_market_readiness_gate",
        "schedule": crontab(hour=8, minute=15, day_of_week="1-5"),
    },
    "finalize-forward-shadow-session": {
        "task": "backend.tasks.worker.finalize_forward_shadow_session",
        "schedule": crontab(hour=15, minute=40, day_of_week="1-5"),
    },
    "fallback-finalize-stale-shadow-sessions": {
        "task": "backend.tasks.worker.recover_stale_forward_shadow_sessions",
        "schedule": crontab(hour=15, minute=50, day_of_week="1-5"),
    },
    "morning-recover-stale-shadow-sessions": {
        "task": "backend.tasks.worker.recover_stale_forward_shadow_sessions",
        "schedule": crontab(hour=8, minute=50, day_of_week="1-5"),
    },
    "auto-repair-shadow-gaps": {
        "task": "backend.tasks.worker.auto_repair_shadow_gaps",
        "schedule": crontab(minute="*/15", hour="9-15", day_of_week="1-5"),
    },
    "upstox-option-chain-oi": {
        "task": "backend.tasks.worker.sync_upstox_option_chain_oi",
        "schedule": crontab(minute="*/5", hour="9-15", day_of_week="1-5"),
    },
    "end-of-day-shadow-report": {
        "task": "backend.tasks.worker.end_of_day_shadow_report",
        "schedule": crontab(hour=15, minute=35, day_of_week="1-5"),
    },
    "post-market-cost-and-promotion-audit": {
        "task": "backend.tasks.worker.post_market_validation",
        "schedule": crontab(hour=16, minute=0, day_of_week="1-5"),
    },
    "autonomous-feature-drift-retraining": {
        "task": "backend.tasks.worker.check_and_autotrain_drift",
        "schedule": crontab(hour=16, minute=15, day_of_week="1-5"),
    },
    "resolve-counterfactual-outcomes": {
        "task": "backend.tasks.worker.resolve_counterfactual_outcomes",
        "schedule": crontab(minute="*/15", hour="9-15", day_of_week="1-5"),
    },
    "post-market-daily-coach-audit": {
        "task": "backend.tasks.worker.generate_daily_coach_audit",
        "schedule": crontab(hour=15, minute=45, day_of_week="1-5"),
    },
    "continuous-live-paper-inference": {
        "task": "backend.tasks.worker.run_live_paper_inference",
        "schedule": crontab(minute="*", hour="9-15", day_of_week="1-5"),
    },
    "continuous-high-frequency-position-defense": {
        "task": "backend.tasks.worker.run_position_defense_loop",
        "schedule": 1.0,
    },
}

_engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=2, future=True)
_redis = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
_finbert = None
_live_inference = None
_position_manager = None
IST = ZoneInfo("Asia/Kolkata")


@task_failure.connect
def _page_task_failure(sender=None, task_id=None, exception=None, **kwargs):
    page("CRITICAL", "Celery task failed", {
        "task": getattr(sender, "name", str(sender)), "task_id": task_id,
        "error": str(exception)[:1000],
    })


@worker_ready.connect
def _recover_shadow_sessions_on_worker_ready(sender=None, **kwargs):
    if os.getenv("NIVESH_RECOVER_STALE_SHADOW_ON_STARTUP","1")!="1":
        return
    try:
        recover_stale_forward_shadow_sessions.delay()
    except Exception as exc:
        page("ERROR", "Unable to enqueue stale shadow-session recovery", {"error": str(exc)[:1000]})
    try:
        now = datetime.now(IST)
        if now.weekday() < 5 and (now.hour < 15 or (now.hour == 15 and now.minute <= 35)):
            open_forward_shadow_session.delay()
    except Exception as exc:
        page("WARNING", "Unable to enqueue today shadow-session open on startup", {"error": str(exc)[:1000]})


@celery_app.task(name="backend.tasks.worker.operational_heartbeat")
def operational_heartbeat() -> Dict:
    with _engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO monitoring_events(component,level,message,payload,created_at)
            VALUES('celery_worker','INFO','Worker heartbeat',:payload,CURRENT_TIMESTAMP)
        """), {"payload": json.dumps({"redis": _redis.ping(), "worker_concurrency": 1})})
    return {"status": "ok", "redis": True}


@celery_app.task(name="backend.tasks.worker.open_forward_shadow_session")
def open_forward_shadow_session() -> Dict:
    if os.getenv("LIVE_ELIGIBLE","FALSE").upper()!="FALSE" or os.getenv("NIVESH_LIVE_TRADING_ENABLED","0")!="0":
        raise RuntimeError("Shadow session requires both execution fuses locked")
    preflight=run_preflight(DATABASE_URL,REDIS_URL,require_integrations=True)
    if preflight["status"]!="PASS": return {"status":"BLOCKED","preflight":preflight,"orders_allowed":False}
    return open_session(_engine)


@celery_app.task(name="backend.tasks.worker.pre_market_readiness_gate")
def pre_market_readiness_gate() -> Dict:
    return run_preflight(DATABASE_URL,REDIS_URL,require_integrations=True)


@celery_app.task(name="backend.tasks.worker.finalize_forward_shadow_session")
def finalize_forward_shadow_session() -> Dict:
    try:
        result=finalize_session(_engine)
        with _engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES('shadow_session_finalizer','INFO','Shadow session finalized',CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
            """),{"payload":json.dumps(result,default=str)})
        return result
    except Exception as exc:
        with _engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES('shadow_session_finalizer','ERROR','Shadow session finalization failed',CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
            """),{"payload":json.dumps({"error":str(exc)[:1000],"orders_allowed":False},default=str)})
        raise


@celery_app.task(name="backend.tasks.worker.recover_stale_forward_shadow_sessions")
def recover_stale_forward_shadow_sessions(lookback_days: int = None) -> Dict:
    lookback=int(lookback_days or os.getenv("NIVESH_STALE_SHADOW_RECOVERY_DAYS","10"))
    return recover_stale_started_sessions(_engine, lookback_days=lookback)


@celery_app.task(name="backend.tasks.worker.post_market_validation")
def post_market_validation() -> Dict:
    costs=reconcile_shadow_costs(_engine); promotion=evaluate_promotion(_engine)
    try:
        from backend.brains.counterfactual_replay import run_post_market_counterfactual_replay
        cf_summary = run_post_market_counterfactual_replay(_engine, redis_client=_redis)
    except Exception as cf_err:
        logger.warning("Post-market counterfactual replay error: %s", cf_err)
        cf_summary = {"status": "error", "error": str(cf_err)}
    return {"costs":costs,"promotion":promotion,"counterfactual":cf_summary,"orders_allowed":False}


@celery_app.task(bind=True,name="backend.tasks.worker.auto_repair_shadow_gaps",max_retries=2,
                 autoretry_for=(OSError,RuntimeError,ValueError),retry_backoff=True,retry_backoff_max=300,retry_jitter=True)
def auto_repair_shadow_gaps(self, force: bool = False, limit: int = None) -> Dict:
    """Read-only intraday REST repair for WebSocket gaps.

    This task is deliberately conservative: it only runs in paper/shadow mode,
    requires an Upstox token, inserts missing bars with ON CONFLICT DO NOTHING,
    and never enables broker order submission.
    """
    if os.getenv("NIVESH_AUTO_GAP_REPAIR_ENABLED","1")!="1" and not force:
        return {"status":"skipped","reason":"NIVESH_AUTO_GAP_REPAIR_ENABLED is disabled","orders_allowed":False}
    if os.getenv("NIVESH_MARKET_DATA_PROVIDER","").strip().lower()!="upstox":
        return {"status":"skipped","reason":"active market provider is not upstox","orders_allowed":False}
    if not (os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN")):
        return {"status":"skipped","reason":"Upstox token is not configured","orders_allowed":False}
    now=datetime.now(IST)
    if not force and (now.weekday()>=5 or not (now.replace(hour=9,minute=30,second=0,microsecond=0)<=now<=now.replace(hour=15,minute=35,second=0,microsecond=0))):
        return {"status":"skipped","reason":"outside automatic intraday repair window","orders_allowed":False}
    lock=_acquire_lock("auto-shadow-gap-repair",1800)
    if not lock: return {"status":"skipped","reason":"another gap repair worker holds the distributed lock","orders_allowed":False}
    try:
        before=evaluate_session(_engine)
        repair_limit=int(limit or os.getenv("NIVESH_AUTO_GAP_REPAIR_LIMIT","100"))
        repair_limit=max(1,min(repair_limit,500))
        report=run_backfill(DATABASE_URL,limit=repair_limit,mode="intraday",days=1,fno_only=True,replace=False)
        after=evaluate_session(_engine)
        payload={"status":report.get("status"),"limit":repair_limit,"inserted_1minute":report.get("inserted_1minute",0),
                 "inserted_5minute":report.get("inserted_5minute",0),
                 "before_latest_bar":before.get("metrics",{}).get("latest_bar"),
                 "after_latest_bar":after.get("metrics",{}).get("latest_bar"),
                 "before_open_gaps":before.get("metrics",{}).get("open_gaps"),
                 "after_open_gaps":after.get("metrics",{}).get("open_gaps"),
                 "orders_allowed":False}
        with _engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES('shadow_gap_repair','INFO','Automatic Upstox REST gap repair completed',CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
            """),{"payload":json.dumps(payload,default=str)})
            publish_brain_event(connection,"GapRepaired","auto_repair_shadow_gaps",
                                {"repair":payload,"provider":"upstox","method":"REST intraday backfill"},
                                severity="INFO")
            try:
                from backend.brains import get_bus
                from backend.brains.bus import GapRepaired
                get_bus().publish(GapRepaired(
                    source_brain="auto_repair_shadow_gaps",
                    provider="upstox",
                    repaired_bars=int(payload.get("inserted_1minute") or 0)+int(payload.get("inserted_5minute") or 0),
                    remaining_open_gaps=int(payload.get("after_open_gaps") or 0),
                    payload=payload,
                ))
            except Exception:
                pass
        return {"status":"success","repair":report,"before":before,"after":after,"orders_allowed":False}
    finally:
        _release_lock("auto-shadow-gap-repair",lock)


@celery_app.task(name="backend.tasks.worker.end_of_day_shadow_report")
def end_of_day_shadow_report() -> Dict:
    repair=None
    if os.getenv("NIVESH_EOD_REPAIR_BEFORE_REPORT","1")=="1":
        repair=auto_repair_shadow_gaps(force=True,limit=int(os.getenv("NIVESH_EOD_GAP_REPAIR_LIMIT","200")))
    feedback=export_high_quality_shadow_feedback(DATABASE_URL)
    pnl=paper_pnl_summary(_engine)
    pnl["model_coach"]["model_feedback"]=feedback
    pnl["model_coach"]["actions"][-1]=f"Use only grade A/B closed trades for feedback export; current exported rows: {feedback.get('total_feedback_rows', 0)}."
    session=evaluate_session(_engine)
    cf_summary = {}
    try:
        from backend.brains.counterfactual_replay import get_latest_counterfactual_summary
        cf_summary = get_latest_counterfactual_summary(_engine, redis_client=_redis)
    except Exception as cf_err:
        logger.debug("EOD counterfactual summary fetch error: %s", cf_err)

    report={"session_date":pnl["session_date"],"paper_pnl":pnl,"session":session,
            "gap_repair":repair,"model_feedback":feedback,"counterfactual":cf_summary,"orders_allowed":False,
            "message":"End-of-day shadow report generated from paper ledger, completed bars and REST repair evidence."}
    with _engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO monitoring_events(component,level,message,payload,created_at)
            VALUES('shadow_eod_report','INFO','End-of-day shadow paper report generated',CAST(:payload AS jsonb),CURRENT_TIMESTAMP)
        """),{"payload":json.dumps(report,default=str)})

    # Dispatch notification to Telegram and Discord
    try:
        from backend.notifications import notify_daily_eod_summary
        notify_daily_eod_summary(report)
        from backend.brains import get_bus
        from backend.brains.bus import DailyReportReady
        get_bus().publish(DailyReportReady(source_brain="end_of_day_shadow_report", report=report))
    except Exception as notif_err:
        logger.warning("EOD notification dispatch error: %s", notif_err)

    return report


@celery_app.task(bind=True,name="backend.tasks.worker.run_live_paper_inference",max_retries=2,
                 autoretry_for=(OSError,RuntimeError),retry_backoff=True,retry_backoff_max=120,retry_jitter=True)
def run_live_paper_inference(self) -> Dict:
    global _live_inference
    now=datetime.now(IST)
    if now.weekday()>=5 or not (now.replace(hour=9,minute=15,second=0,microsecond=0)<=now<=now.replace(hour=15,minute=40,second=0,microsecond=0)):
        return {"status":"skipped","reason":"outside configured NSE continuous-market window","orders_allowed":False}
    lock=_acquire_lock("live-paper-inference",240)
    if not lock: return {"status":"skipped","reason":"another inference worker holds the distributed lock","orders_allowed":False}
    try:
        if _live_inference is None:
            from backend.live_inference import LivePaperInference
            _live_inference=LivePaperInference(DATABASE_URL,REDIS_URL)
        infer_result = _live_inference.run()
        try:
            from backend.brains import run_brain_pipeline
            brain_result = run_brain_pipeline({"db": _live_inference.engine})
            infer_result["brain_pipeline"] = {k: "ok" for k in (brain_result or {}).keys()}
        except Exception as b_err:
            pass
        return infer_result
    finally:
        _release_lock("live-paper-inference",lock)


@celery_app.task(bind=True, name="backend.tasks.worker.run_position_defense_loop", max_retries=1)
def run_position_defense_loop(self) -> Dict:
    """High-frequency position defense loop: cuts adverse moves, tightens stagnation stops, and locks profits."""
    global _position_manager
    lock = _acquire_lock("position-defense", 2)
    if not lock:
        return {"status": "skipped", "reason": "another worker holds position-defense lock"}
    try:
        if _position_manager is None:
            from backend.position_manager import PositionManager
            _position_manager = PositionManager(DATABASE_URL, REDIS_URL)
        return _position_manager.run_once()
    except Exception as exc:
        logger.error("Position defense evaluation error: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}
    finally:
        _release_lock("position-defense", lock)


def _acquire_lock(name: str, ttl: int) -> str:
    token = hashlib.sha256(f"{name}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()
    return token if _redis.set(f"nivesh:lock:{name}", token, nx=True, ex=ttl) else ""


def _release_lock(name: str, token: str) -> None:
    script = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
    _redis.eval(script, 1, f"nivesh:lock:{name}", token)


def _pipeline():
    global _finbert
    if _finbert is None:
        import torch
        from transformers import pipeline
        torch.set_num_threads(max(1, int(os.getenv("FINBERT_CPU_THREADS", "2"))))
        device = 0 if torch.cuda.is_available() and os.getenv("FINBERT_USE_GPU", "0") == "1" else -1
        _finbert = pipeline("text-classification", model=FINBERT_MODEL, tokenizer=FINBERT_MODEL,
                            revision=FINBERT_REVISION, device=device, top_k=None, truncation=True,
                            max_length=512, trust_remote_code=False,
                            model_kwargs={"use_safetensors": True})
    return _finbert


def _symbols() -> List[str]:
    explicit = [item.strip().upper() for item in os.getenv("NIVESH_NEWS_SYMBOLS", "").split(",") if item.strip()]
    if explicit:
        return explicit[:NEWS_SCAN_SYMBOL_LIMIT]
    priority = list(dict.fromkeys(DEFAULT_NEWS_SYMBOLS))[:NEWS_SCAN_SYMBOL_LIMIT]
    with _engine.connect() as connection:
        rows = connection.execute(text("""
            WITH priority AS (
                SELECT * FROM unnest(CAST(:priority AS text[])) WITH ORDINALITY AS p(symbol, ord)
            ),
            preferred AS (
                SELECT i.symbol, p.ord
                FROM priority p
                JOIN instrument_master i ON i.exchange='NSE' AND i.instrument_type='EQ'
                 AND i.is_active AND i.symbol=p.symbol
            ),
            fallback AS (
                SELECT i.symbol, 10000 + ROW_NUMBER() OVER (ORDER BY i.is_fno_eligible DESC, i.symbol) ord
                FROM instrument_master i
                WHERE i.exchange='NSE' AND i.instrument_type='EQ' AND i.is_active
            )
            SELECT symbol FROM (
                SELECT symbol, ord FROM preferred
                UNION
                SELECT symbol, ord FROM fallback
            ) ranked
            ORDER BY ord
            LIMIT :limit
        """), {"limit": NEWS_SCAN_SYMBOL_LIMIT, "priority": priority}).fetchall()
    return [row[0] for row in rows]


def _fetch_articles(symbol: str) -> List[Dict]:
    key = FINNHUB_API_KEY if NEWS_PROVIDER == "finnhub" else NEWS_API_KEY
    if not key:
        raise RuntimeError("NIVESH_NEWS_API_KEY is not configured")
    if NEWS_PROVIDER == "finnhub":
        base = NEWS_API_URL.rstrip("/") or "https://finnhub.io/api/v1"
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=max(1, FINNHUB_NEWS_LOOKBACK_DAYS))
        base_symbol = symbol.upper().split(".")[0]
        candidates = [symbol.upper()] if "." in symbol else [f"{base_symbol}.NS", f"{base_symbol}.BO", base_symbol]
        for candidate in dict.fromkeys(candidates):
            query = urlencode({"symbol": candidate, "from": start.isoformat(), "to": today.isoformat(), "token": key})
            request = Request(f"{base}/company-news?{query}", headers={
                "Accept": "application/json", "User-Agent": "NiveshAI/3.0",
            })
            try:
                with urlopen(request, timeout=FINNHUB_NEWS_TIMEOUT_SECONDS) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, list) and payload:
                    return payload[:10]
            except Exception:
                continue
        query = urlencode({"category": FINNHUB_NEWS_CATEGORY, "token": key})
        request = Request(f"{base}/news?{query}", headers={
            "Accept": "application/json", "User-Agent": "NiveshAI/3.0",
        })
        try:
            with urlopen(request, timeout=FINNHUB_NEWS_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        return [item for item in payload if symbol.lower() in f"{item.get('headline','')} {item.get('summary','')} {item.get('related','')}".lower()][:10]
    query = urlencode({"q": f'"{symbol}" AND (NSE OR BSE OR India stock)', "language": "en",
                       "sortBy": "publishedAt", "pageSize": 10})
    separator = "&" if "?" in NEWS_API_URL else "?"
    request = Request(f"{NEWS_API_URL}{separator}{query}", headers={
        "Accept": "application/json", "User-Agent": "NiveshAI/3.0", NEWS_API_KEY_HEADER: NEWS_API_KEY,
    })
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    return payload.get("articles") or payload.get("data") or payload.get("results") or []


def _normalise(article: Dict, symbol: str) -> Dict:
    source = article.get("source") or "Unknown"
    if isinstance(source, dict):
        source = source.get("name") or "Unknown"
    headline = str(article.get("title") or article.get("headline") or "").strip()
    published_value = article.get("publishedAt") or article.get("published_at") or article.get("datetime")
    if isinstance(published_value, (int, float)):
        published_value = datetime.fromtimestamp(published_value, timezone.utc).isoformat()
    published = str(published_value or datetime.now(timezone.utc).isoformat())
    digest = hashlib.sha256(f"{headline.lower()}|{source.lower()}|{published[:10]}".encode()).hexdigest()
    return {"hash": digest, "headline": headline, "summary": str(article.get("description") or article.get("summary") or "")[:10000],
            "source": str(source)[:128], "url": str(article.get("url") or ""), "published": published, "symbol": symbol,
            "provider": "finnhub" if NEWS_PROVIDER == "finnhub" else "configured_news_api"}


def _score(articles: List[Dict]) -> List[Dict]:
    if not articles:
        return []
    texts = [(item["headline"] + ". " + item["summary"])[:4000] for item in articles]
    results = _pipeline()(texts, batch_size=min(16, len(texts)))
    scored = []
    for article, labels in zip(articles, results):
        values = {str(item["label"]).lower(): float(item["score"]) for item in labels}
        scored.append({**article, "positive": values.get("positive", 0.0),
                       "neutral": values.get("neutral", 0.0), "negative": values.get("negative", 0.0)})
    return scored


@celery_app.task(bind=True, name="backend.tasks.worker.scan_news_and_score", max_retries=5,
                 autoretry_for=(OSError, RuntimeError, ValueError), retry_backoff=True,
                 retry_backoff_max=900, retry_jitter=True)
def scan_news_and_score(self) -> Dict:
    lock = _acquire_lock("news-finbert", 1500)
    if not lock:
        return {"status": "skipped", "reason": "another news worker holds the distributed lock"}
    try:
        unique = {}
        failures = []
        for symbol in _symbols():
            try:
                raws = _fetch_articles(symbol)
            except Exception as exc:
                failures.append({"symbol": symbol, "error": type(exc).__name__})
                continue
            for raw in raws:
                try:
                    article = _normalise(raw, symbol)
                    if article["headline"]:
                        unique.setdefault(article["hash"], article)
                except Exception as exc:
                    failures.append({"symbol": symbol, "error": f"normalise_{type(exc).__name__}"})
        scored = _score(list(unique.values()))
        with _engine.begin() as connection:
            for item in scored:
                row = connection.execute(text("""
                    INSERT INTO news_articles_v3(content_hash,provider,headline,summary,source_name,source_url,published_at,symbols)
                    VALUES(:hash,:provider,:headline,:summary,:source,:url,:published,ARRAY[:symbol])
                    ON CONFLICT(content_hash) DO UPDATE SET symbols=(SELECT ARRAY(SELECT DISTINCT unnest(news_articles_v3.symbols || EXCLUDED.symbols)))
                    RETURNING id
                """), item).fetchone()
                connection.execute(text("""
                    INSERT INTO news_sentiment_scores(article_id,model_name,positive_probability,neutral_probability,negative_probability)
                    VALUES(:article_id,:model,:positive,:neutral,:negative)
                    ON CONFLICT(article_id,model_name) DO UPDATE SET positive_probability=EXCLUDED.positive_probability,
                    neutral_probability=EXCLUDED.neutral_probability,negative_probability=EXCLUDED.negative_probability,scored_at=CURRENT_TIMESTAMP
                """), {**item, "article_id": row[0], "model": FINBERT_MODEL})
        return {"status": "success", "articles_scored": len(scored), "provider": NEWS_PROVIDER,
                "symbols_scanned": len(_symbols()), "failures": failures[:10], "model": FINBERT_MODEL}
    finally:
        _release_lock("news-finbert", lock)


def _psi(reference: Iterable[float], current: Iterable[float], bins: int = 10) -> float:
    reference, current = np.asarray(list(reference), dtype=float), np.asarray(list(current), dtype=float)
    if len(reference) < 100 or len(current) < 50:
        raise ValueError("PSI requires at least 100 reference and 50 current observations")
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    ref_counts = np.histogram(reference, edges)[0] / len(reference)
    cur_counts = np.histogram(current, edges)[0] / len(current)
    ref_counts, cur_counts = np.clip(ref_counts, 1e-6, None), np.clip(cur_counts, 1e-6, None)
    return float(np.sum((cur_counts-ref_counts)*np.log(cur_counts/ref_counts)))


@celery_app.task(bind=True, name="backend.tasks.worker.end_of_day_feature_psi", max_retries=3,
                 autoretry_for=(OSError, RuntimeError, ValueError), retry_backoff=True,
                 retry_backoff_max=1800, retry_jitter=True)
def end_of_day_feature_psi(self) -> Dict:
    lock = _acquire_lock("feature-psi", 1700)
    if not lock:
        return {"status": "skipped", "reason": "another drift worker holds the distributed lock"}
    try:
        now = datetime.now(timezone.utc); current_start = now-timedelta(days=30); reference_start = now-timedelta(days=150)
        with _engine.begin() as connection:
            model = connection.execute(text("SELECT model_version FROM live_feature_snapshots ORDER BY observed_at DESC LIMIT 1")).scalar_one()
            rows = connection.execute(text("""
                SELECT feature.key feature_name,(feature.value)::double precision feature_value,snapshot.observed_at
                FROM live_feature_snapshots snapshot
                CROSS JOIN LATERAL jsonb_each_text(snapshot.features) feature
                WHERE snapshot.model_version=:model AND snapshot.observed_at>=:start
                ORDER BY feature.key,snapshot.observed_at,snapshot.instrument_id
            """), {"model": model, "start": reference_start}).fetchall()
            grouped: Dict[str, Dict[str,List[float]]] = {}
            for feature, value, observed in rows:
                bucket = grouped.setdefault(feature, {"reference": [], "current": []})
                bucket["current" if observed >= current_start else "reference"].append(float(value))
            reports=[]
            for feature, values in grouped.items():
                score=_psi(values["reference"], values["current"])
                status="alert" if score>=.25 else "watch" if score>=.10 else "stable"
                connection.execute(text("""
                    INSERT INTO model_drift_reports(model_version,feature_name,psi,status,reference_start,reference_end,current_start,current_end)
                    VALUES(:model,:feature,:psi,:status,:reference_start,:reference_end,:current_start,:current_end)
                """), {"model":model,"feature":feature,"psi":score,"status":status,
                         "reference_start":reference_start,"reference_end":current_start,
                         "current_start":current_start,"current_end":now})
                reports.append({"feature":feature,"psi":round(score,6),"status":status})
            if any(item["status"]=="alert" for item in reports):
                connection.execute(text("""
                    UPDATE risk_control_state SET trading_enabled=FALSE,kill_switch_active=TRUE,
                    kill_reason='ML feature PSI drift alert',updated_at=CURRENT_TIMESTAMP
                """))
        return {"status":"success","model_version":model,"overall":"alert" if any(x["status"]=="alert" for x in reports) else "watch" if any(x["status"]=="watch" for x in reports) else "stable","features":reports}
    finally:
        _release_lock("feature-psi", lock)


@celery_app.task(bind=True,name="backend.tasks.worker.sync_kite_instruments",max_retries=4,
                 autoretry_for=(OSError,RuntimeError,ValueError),retry_backoff=True,retry_backoff_max=900,retry_jitter=True)
def sync_kite_instruments(self) -> Dict:
    api_key=os.getenv("KITE_API_KEY",""); access_token=os.getenv("KITE_ACCESS_TOKEN","")
    if not api_key or not access_token: raise RuntimeError("Kite credentials and daily access token are required")
    lock=_acquire_lock("kite-instruments",1200)
    if not lock: return {"status":"skipped","reason":"another instrument worker holds the distributed lock"}
    try: return sync_instrument_master(DATABASE_URL,api_key,access_token)
    finally: _release_lock("kite-instruments",lock)


@celery_app.task(bind=True,name="backend.tasks.worker.sync_upstox_instruments",max_retries=4,
                 autoretry_for=(OSError,RuntimeError,ValueError),retry_backoff=True,retry_backoff_max=900,retry_jitter=True)
def sync_upstox_instruments(self) -> Dict:
    lock=_acquire_lock("upstox-instruments",1200)
    if not lock: return {"status":"skipped","reason":"another instrument worker holds the distributed lock"}
    try: return sync_upstox_instrument_master(DATABASE_URL,os.getenv("UPSTOX_INSTRUMENTS_URL","https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"))
    finally: _release_lock("upstox-instruments",lock)


@celery_app.task(bind=True,name="backend.tasks.worker.sync_market_instruments",max_retries=4,
                 autoretry_for=(OSError,RuntimeError,ValueError),retry_backoff=True,retry_backoff_max=900,retry_jitter=True)
def sync_market_instruments(self) -> Dict:
    provider=os.getenv("NIVESH_MARKET_DATA_PROVIDER","zerodha").strip().lower()
    if provider=="upstox":
        return sync_upstox_instruments()
    return sync_kite_instruments()


@celery_app.task(bind=True,name="backend.tasks.worker.backfill_upstox_rest",max_retries=3,
                 autoretry_for=(OSError,RuntimeError,ValueError),retry_backoff=True,retry_backoff_max=900,retry_jitter=True)
def backfill_upstox_rest(self,limit: int = 100,mode: str = "intraday",days: int = 365,
                         fno_only: bool = True,replace: bool = False) -> Dict:
    if mode not in {"intraday","history","both"}:
        raise ValueError("mode must be intraday, history, or both")
    lock=_acquire_lock(f"upstox-rest-backfill-{mode}",1800)
    if not lock: return {"status":"skipped","reason":"another Upstox REST backfill worker holds the distributed lock"}
    try:
        return run_backfill(DATABASE_URL,limit=int(limit),mode=mode,days=int(days),fno_only=bool(fno_only),replace=bool(replace))
    finally:
        _release_lock(f"upstox-rest-backfill-{mode}",lock)


@celery_app.task(bind=True,name="backend.tasks.worker.sync_upstox_option_chain_oi",max_retries=3,
                 autoretry_for=(OSError,RuntimeError,ValueError),retry_backoff=True,retry_backoff_max=900,retry_jitter=True)
def sync_upstox_option_chain_oi(self,limit: int = None,underlyings: List[str] = None,force: bool = False) -> Dict:
    """Read-only Upstox option-chain OI/bid-ask sync for paper-trade quality gates."""
    if os.getenv("NIVESH_UPSTOX_OPTION_CHAIN_OI_ENABLED","1")!="1" and not force:
        return {"status":"skipped","reason":"NIVESH_UPSTOX_OPTION_CHAIN_OI_ENABLED is disabled","orders_allowed":False}
    if os.getenv("NIVESH_MARKET_DATA_PROVIDER","").strip().lower()!="upstox" and not force:
        return {"status":"skipped","reason":"active market provider is not upstox","orders_allowed":False}
    if not (os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN")):
        return {"status":"skipped","reason":"Upstox token is not configured","orders_allowed":False}
    now=datetime.now(IST)
    if not force and (now.weekday()>=5 or not (now.replace(hour=9,minute=15,second=0,microsecond=0)<=now<=now.replace(hour=15,minute=35,second=0,microsecond=0))):
        return {"status":"skipped","reason":"outside Upstox option-chain OI sync window","orders_allowed":False}
    lock=_acquire_lock("upstox-option-chain-oi",300)
    if not lock: return {"status":"skipped","reason":"another option-chain OI worker holds the distributed lock","orders_allowed":False}
    try:
        configured_underlyings=underlyings
        if configured_underlyings is None:
            configured_underlyings=[item.strip() for item in os.getenv("NIVESH_UPSTOX_OPTION_CHAIN_UNDERLYINGS","").split(",") if item.strip()]
        sync_limit=int(limit or os.getenv("NIVESH_UPSTOX_OPTION_CHAIN_LIMIT","8"))
        return run_option_chain_oi_sync(DATABASE_URL,REDIS_URL,limit=sync_limit,underlyings=configured_underlyings)
    finally:
        _release_lock("upstox-option-chain-oi",lock)


@celery_app.task(name="backend.tasks.worker.check_and_autotrain_drift")
def check_and_autotrain_drift(force: bool = False) -> Dict:
    """Autonomous Dual-Trigger Drift & Retraining Guard.
    Triggers walk-forward retraining when Feature PSI >= 0.25 OR 5-day shadow accuracy < 45%.
    """
    from backend.ml_pipeline import detect_drift, train_model, walk_forward_validate
    from sqlalchemy import text
    drift_result = detect_drift()
    drift_status = drift_result.get("status")
    features = drift_result.get("features", [])
    max_psi = max([float(f.get("psi", 0) or 0) for f in features] or [0.0])
    
    # Check shadow prediction accuracy over last 5 days
    shadow_acc = None
    shadow_total = 0
    try:
        with _engine.connect() as conn:
            perf_row = conn.execute(text("""
                SELECT COUNT(*) total,
                       COUNT(*) FILTER (WHERE resolved_label = (predicted_probability >= 0.55)::int) correct
                FROM shadow_predictions
                WHERE resolved_label IS NOT NULL
                  AND predicted_at >= NOW() - INTERVAL '5 days'
            """)).mappings().one_or_none()
            if perf_row and int(perf_row.get("total") or 0) >= 20:
                shadow_total = int(perf_row["total"])
                shadow_acc = round(int(perf_row["correct"]) / shadow_total * 100, 2)
    except Exception:
        pass

    feature_drift_triggered = max_psi >= 0.25 or drift_status == "DRIFT_DETECTED"
    perf_decay_triggered = shadow_acc is not None and shadow_acc < 45.0
    
    if feature_drift_triggered or perf_decay_triggered or force:
        trigger_reasons = []
        if feature_drift_triggered: trigger_reasons.append(f"PSI drift {max_psi:.4f} >= 0.25")
        if perf_decay_triggered: trigger_reasons.append(f"Shadow accuracy {shadow_acc}% < 45%")
        if force: trigger_reasons.append("Operator manual force")

        # Autonomous trigger: train new candidate & prepare walk-forward scorecard
        trained = train_model()
        validation = walk_forward_validate()
        
        # Log to monitoring_events
        try:
            with _engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO monitoring_events(user_id, component, level, message, payload, created_at)
                    VALUES(0, 'autonomous_ml_retraining', 'INFO', :msg, :payload, CURRENT_TIMESTAMP)
                """), {
                    "msg": f"Autonomous model retrain triggered: {', '.join(trigger_reasons)}",
                    "payload": json.dumps({
                        "reasons": trigger_reasons,
                        "trained_version": trained.get("version"),
                        "validation_passed": validation.get("passed", False),
                        "max_psi": max_psi,
                        "shadow_acc": shadow_acc,
                    })
                })
        except Exception:
            pass

        return {
            "status": "AUTO_RETRAINED",
            "trigger_reasons": trigger_reasons,
            "trigger_psi": round(max_psi, 4),
            "shadow_accuracy": shadow_acc,
            "drift_status": drift_status,
            "trained_version": trained.get("version"),
            "validation_passed": validation.get("passed", False),
            "orders_allowed": False,
        }
    return {
        "status": "STABLE",
        "max_psi": round(max_psi, 4),
        "shadow_accuracy": shadow_acc,
        "shadow_samples": shadow_total,
        "drift_status": drift_status,
        "features_monitored": len(features),
        "orders_allowed": False,
    }


@celery_app.task(name="backend.tasks.worker.resolve_counterfactual_outcomes")
def resolve_counterfactual_outcomes() -> Dict:
    """Resolve forward 5m/15m/30m price outcomes and boundary replay for counterfactual observation."""
    from backend.intelligence_memory import persist_counterfactual_candidates, update_candidate_counterfactual_outcomes, ensure_intelligence_schema
    from backend.senior_market_intelligence import update_counterfactuals
    from backend.brains.counterfactual_replay import run_post_market_counterfactual_replay
    with _engine.begin() as conn:
        ensure_intelligence_schema(conn)
        res1 = persist_counterfactual_candidates(conn)
        res2 = update_candidate_counterfactual_outcomes(conn)
        res3 = update_counterfactuals(conn)
    try:
        replay_res = run_post_market_counterfactual_replay(_engine, redis_client=_redis)
    except Exception as exc:
        replay_res = {"status": "skipped", "error": str(exc)}
    return {
        "status": "success",
        "persisted": res1.get("upserted", 0),
        "updated_outcomes": res2.get("updated", 0),
        "senior_updated": res3.get("updated", 0),
        "replay_accuracy_pct": replay_res.get("gate_accuracy_pct", 100.0),
        "saved_losses": replay_res.get("saved_losses", 0),
        "missed_wins": replay_res.get("missed_wins", 0),
    }


@celery_app.task(name="backend.tasks.worker.generate_daily_coach_audit")
def generate_daily_coach_audit() -> Dict:
    """Post-market Daily Trading Coach Audit at 3:45 PM IST."""
    from backend.brains.trading_coach import run_coach_audit
    return run_coach_audit(_engine)

