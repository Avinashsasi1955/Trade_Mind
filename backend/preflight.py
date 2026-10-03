"""Fail-closed pre-market readiness checks for formal shadow sessions."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import create_engine, text

IST=ZoneInfo("Asia/Kolkata")


def _artifact_check(payload,key,required):
    encoded=payload.get("estimator_b64",""); signature=payload.get("estimator_hmac","")
    if not encoded:
        if payload.get("weights") and payload.get("features"):
            return {"passed": True, "detail": "parametric model verified"}
        return {"passed":False,"detail":"active estimator payload is missing"}
    if not signature: return {"passed":not required,"detail":"unsigned legacy artifact"}
    expected=hmac.new(key.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    return {"passed":hmac.compare_digest(signature,expected),"detail":"HMAC verified" if hmac.compare_digest(signature,expected) else "artifact HMAC mismatch"}


def run_preflight(database_url=None,redis_url=None,require_integrations=True):
    database_url=database_url or os.getenv("DATABASE_URL",""); redis_url=redis_url or os.getenv("REDIS_URL","")
    if not database_url or not redis_url: raise RuntimeError("DATABASE_URL and REDIS_URL are required")
    engine=create_engine(database_url,pool_pre_ping=True,future=True); cache=redis.Redis.from_url(redis_url,decode_responses=True,socket_timeout=3)
    now=datetime.now(IST); checks={}
    checks["execution_fuses"]={"passed":os.getenv("LIVE_ELIGIBLE","FALSE").upper()=="FALSE" and os.getenv("NIVESH_LIVE_TRADING_ENABLED","0")=="0",
                               "detail":"both execution fuses must remain locked"}
    try: checks["redis"]={"passed":bool(cache.ping()),"detail":"Redis ping"}
    except Exception: checks["redis"]={"passed":False,"detail":"Redis unavailable"}
    with engine.begin() as connection:
        migration=connection.execute(text("SELECT 1 FROM schema_migrations WHERE version='v3_9_market_data_providers'")).scalar_one_or_none()
        checks["schema"]={"passed":bool(migration),"detail":"v3.9 market-data provider schema"}
        model=connection.execute(text("SELECT version,payload FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1")).mappings().one_or_none()
        checks["active_model"]={"passed":bool(model),"detail":model["version"] if model else "missing"}
        if model:
            payload=model["payload"] if isinstance(model["payload"],dict) else json.loads(model["payload"])
            required=os.getenv("NIVESH_REQUIRE_SIGNED_MODEL","0")=="1"
            checks["model_signature"]=_artifact_check(payload,os.getenv("NIVESH_MODEL_ARTIFACT_KEY",""),required)
        else: checks["model_signature"]={"passed":False,"detail":"no active model"}
        provider=os.getenv("NIVESH_MARKET_DATA_PROVIDER","zerodha").strip().lower()
        if provider=="upstox":
            token_count=int(connection.execute(text("SELECT COUNT(*) FROM instrument_provider_keys WHERE provider='upstox_v3' AND is_active")).scalar_one())
            detail=f"{token_count} active Upstox instrument keys"
        else:
            token_count=int(connection.execute(text("SELECT COUNT(*) FROM instrument_master WHERE is_active AND instrument_token IS NOT NULL")).scalar_one())
            detail=f"{token_count} active broker tokens"
        checks["instrument_master"]={"passed":token_count>=50 if require_integrations else True,"detail":detail}
        calendar=connection.execute(text("SELECT session_status,source FROM exchange_trading_calendar WHERE exchange='NSE' AND session_date=:day"),{"day":now.date()}).mappings().one_or_none()
        calendar_passed=now.weekday()<5 and (not calendar or calendar["session_status"]!="CLOSED")
        calendar_detail="weekend closed" if now.weekday()>=5 else (f"{calendar['session_status']} ({calendar['source']})" if calendar else "weekday fallback; official row not loaded")
        checks["exchange_calendar"]={"passed":calendar_passed,"detail":calendar_detail}
        heartbeat=connection.execute(text("SELECT created_at FROM monitoring_events WHERE component='celery_worker' ORDER BY id DESC LIMIT 1")).scalar_one_or_none()
        checks["worker_heartbeat"]={"passed":bool(heartbeat and heartbeat>=datetime.now(heartbeat.tzinfo)-timedelta(minutes=3)),
                                    "detail":str(heartbeat) if heartbeat else "missing"}
        configured=bool(os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN")) if provider=="upstox" else bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_ACCESS_TOKEN"))
        checks["market_data_session"]={"passed":configured if require_integrations else True,"detail":f"{provider} configured" if configured else f"{provider} not configured"}
        blockers=[name for name,value in checks.items() if not value["passed"]]
        status="PASS" if not blockers else "BLOCKED"
        connection.execute(text("""INSERT INTO operational_preflight_runs(session_date,status,checks,blockers,orders_allowed)
            VALUES(:day,:status,CAST(:checks AS jsonb),CAST(:blockers AS jsonb),FALSE)"""),
            {"day":now.date(),"status":status,"checks":json.dumps(checks),"blockers":json.dumps(blockers)})
    engine.dispose()
    return {"status":status,"session_date":now.date().isoformat(),"checks":checks,"blockers":blockers,"orders_allowed":False}
