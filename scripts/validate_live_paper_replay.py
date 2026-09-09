"""Disposable end-to-end validation of bars -> features -> predictions.

Creates a uniquely named local PostgreSQL database and always drops it. It
never imports or calls the broker execution module.
"""
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import psycopg2
import redis
from psycopg2.extras import Json, RealDictCursor

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from backend.live_inference import LivePaperInference
from backend.market_ingestion_v3 import PostgresBarAggregator


MIGRATIONS=[
    "v3_0_production.sql","v3_1_application.sql","v3_2_research_runtime.sql",
    "v3_3_shadow_sessions.sql","v3_4_promotion_engine.sql","v3_5_security.sql",
    "v3_6_live_paper_pipeline.sql",
    "v3_7_release_operations.sql",
    "v3_8_model_policy_evidence.sql",
]


def _database_url(base: str,name: str) -> str:
    parsed=urlparse(base.replace("postgresql+psycopg2://","postgresql://",1))
    return urlunparse(parsed._replace(path="/"+name))


def main():
    if os.getenv("NIVESH_LIVE_TRADING_ENABLED","0")!="0" or os.getenv("LIVE_ELIGIBLE","FALSE").upper()!="FALSE":
        raise RuntimeError("Replay validation requires both execution fuses locked")
    source=os.environ.get("DATABASE_URL","postgresql://127.0.0.1/nivesh_v3_staging")
    redis_url=os.environ.get("REDIS_URL","redis://127.0.0.1:6379/0")
    parsed=urlparse(source.replace("postgresql+psycopg2://","postgresql://",1)); admin=_database_url(source,"postgres")
    replay_name="nivesh_replay_"+secrets.token_hex(4); replay=_database_url(source,replay_name)
    admin_connection=psycopg2.connect(admin); admin_connection.autocommit=True
    with admin_connection.cursor() as cursor: cursor.execute(f'CREATE DATABASE "{replay_name}"')
    keys=[]
    try:
        target=psycopg2.connect(replay)
        with target:
            with target.cursor() as cursor:
                for filename in MIGRATIONS:
                    cursor.execute((ROOT/"migrations"/"postgres"/filename).read_text(encoding="utf-8"))
        source_connection=psycopg2.connect(source.replace("postgresql+psycopg2://","postgresql://",1),cursor_factory=RealDictCursor)
        with source_connection.cursor() as cursor:
            cursor.execute("SELECT * FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1"); model=cursor.fetchone()
            if not model: raise RuntimeError("Source database has no active model")
            cursor.execute("""SELECT i.id,i.exchange,i.symbol FROM instrument_master i JOIN live_market_bars b ON b.instrument_id=i.id
                WHERE i.exchange='NSE' AND i.instrument_type='EQ' AND b.interval='day'
                GROUP BY i.id,i.exchange,i.symbol HAVING COUNT(*)>=60 ORDER BY i.symbol LIMIT 8""")
            instruments=cursor.fetchall()
            if len(instruments)<2: raise RuntimeError("Need at least two replayable NSE instruments")
            histories={}
            for item in instruments:
                cursor.execute("""SELECT bar_time,open_price,high_price,low_price,close_price,volume,open_interest,source,exchange_timestamp
                    FROM live_market_bars WHERE instrument_id=%s AND interval='day' ORDER BY bar_time DESC LIMIT 70""",(item["id"],))
                histories[item["id"]]=list(reversed(cursor.fetchall()))
        with target:
            with target.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""INSERT INTO model_versions(model_name,version,feature_set,algorithm,payload,training_start,training_end,
                    training_samples,metrics,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)""",
                    (model["model_name"],model["version"],model["feature_set"],model["algorithm"],Json(model["payload"]),model["training_start"],
                     model["training_end"],model["training_samples"],Json(model["metrics"]),model["created_at"]))
                mappings=[]
                for index,item in enumerate(instruments,1):
                    token=9_000_000+index
                    cursor.execute("""INSERT INTO instrument_master(instrument_token,exchange,symbol,underlying_symbol,instrument_type,
                        lot_size,tick_size,is_fno_eligible,is_active) VALUES(%s,%s,%s,%s,'EQ',1,.05,FALSE,TRUE) RETURNING id""",
                        (token,item["exchange"],item["symbol"],item["symbol"])); target_id=cursor.fetchone()["id"]
                    mappings.append({**dict(item),"target_id":target_id,"token":token})
                    for bar in histories[item["id"]]:
                        cursor.execute("""INSERT INTO live_market_bars(instrument_id,interval,bar_time,open_price,high_price,low_price,close_price,
                            volume,open_interest,source,exchange_timestamp) VALUES(%s,'day',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (target_id,bar["bar_time"],bar["open_price"],bar["high_price"],bar["low_price"],bar["close_price"],bar["volume"],
                             bar["open_interest"],bar["source"],bar["exchange_timestamp"]))
        latest_day=max(history[-1]["bar_time"] for history in histories.values())
        day=latest_day.astimezone(ZoneInfo("Asia/Kolkata")).date()+timedelta(days=1)
        start=datetime(day.year,day.month,day.day,14,55,tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
        aggregator=PostgresBarAggregator(replay)
        cache=redis.Redis.from_url(redis_url,decode_responses=True)
        for step in range(8):
            ticks=[]
            for index,item in enumerate(mappings):
                last=histories[item["id"]][-1]; base=Decimal(last["close_price"]); price=base*(Decimal("1")+Decimal(step+index-2)/Decimal("1000"))
                ticks.append({"instrument_token":item["token"],"last_price":float(price),"volume":1_000_000+step*10_000,
                              "exchange_timestamp":int((start+timedelta(minutes=5*step)).timestamp()),
                              "depth":[{"side":"buy","price":float(price-Decimal("0.05")),"quantity":1000},
                                       {"side":"sell","price":float(price+Decimal("0.05")),"quantity":1000}]})
                key=f"nivesh:depth:{item['token']}"; keys.append(key)
                cache.setex(key,900,json.dumps({"instrument_token":item["token"],"best_bid":float(price-Decimal("0.05")),
                    "best_ask":float(price+Decimal("0.05")),"bid_quantity":1000,"ask_quantity":1000,
                    "exchange_timestamp":ticks[-1]["exchange_timestamp"]}))
            aggregator.ingest(ticks)
        watermark=start+timedelta(minutes=30)
        inference=LivePaperInference(replay,redis_url); first=inference.run(watermark); second=inference.run(watermark)
        with psycopg2.connect(replay,cursor_factory=RealDictCursor) as verify:
            with verify.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) count FROM live_feature_snapshots"); feature_count=cursor.fetchone()["count"]
                cursor.execute("SELECT COUNT(*) count FROM shadow_predictions WHERE timeframe='5minute'"); prediction_count=cursor.fetchone()["count"]
        if first["predictions_created"]<2 or feature_count!=prediction_count or second["predictions_created"]!=0:
            raise RuntimeError("Replay validation failed its prediction/idempotency assertions")
        print(json.dumps({"status":"passed","database":"disposable","model_version":first["model_version"],
            "bars_to_features":feature_count,"predictions":prediction_count,"duplicate_predictions_on_second_run":second["predictions_created"],
            "paper_audits":first["paper_audits"],"orders_allowed":False},indent=2))
    finally:
        try: redis.Redis.from_url(redis_url).delete(*set(keys)) if keys else None
        except Exception: pass
        with admin_connection.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",(replay_name,))
            cursor.execute(f'DROP DATABASE IF EXISTS "{replay_name}"')
        admin_connection.close()


if __name__=="__main__": main()
