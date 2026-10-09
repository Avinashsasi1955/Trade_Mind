"""Read-only projections of the latest completed Kite bars for the web API."""
from datetime import datetime, timezone
from typing import Dict, Optional
import time

from sqlalchemy import create_engine, text

_CACHE={}
_CACHE_SECONDS=2.0
LIVE_BAR_SOURCES=("zerodha_kite","kite_gap_backfill","upstox_v3","upstox_rest_5m")


def snapshot(database_url: str,limit: int = 500) -> Optional[Dict]:
    if not database_url: return None
    cache_key=(database_url,limit); cached=_CACHE.get(cache_key); now=time.monotonic()
    if cached and now-cached[0]<_CACHE_SECONDS: return cached[1]
    try:
        engine=create_engine(database_url,pool_pre_ping=True,future=True)
        with engine.connect() as connection:
            rows=connection.execute(text("""
            WITH latest_bar AS (
              SELECT DISTINCT ON (b.instrument_id) b.instrument_id,b.bar_time,b.close_price
              FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
              WHERE b.interval='5minute' AND b.source = ANY(:sources)
                AND i.exchange IN ('NSE','BSE') AND i.instrument_type='EQ' AND i.is_active
                AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15:00'
                AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time < TIME '15:30:00'
              ORDER BY b.instrument_id,b.bar_time DESC
            ), latest_feature AS (
              SELECT DISTINCT ON (instrument_id) instrument_id,features
              FROM live_feature_snapshots ORDER BY instrument_id,observed_at DESC
            )
            SELECT i.exchange,i.symbol,b.bar_time,b.close_price,
              previous.close_price previous_close,COALESCE(f.features,'{}'::jsonb) features
            FROM latest_bar b JOIN instrument_master i ON i.id=b.instrument_id
            LEFT JOIN latest_feature f ON f.instrument_id=i.id
            LEFT JOIN LATERAL(
              SELECT close_price FROM live_market_bars d WHERE d.instrument_id=i.id AND d.interval='day'
                AND (d.bar_time AT TIME ZONE 'Asia/Kolkata')::date<(b.bar_time AT TIME ZONE 'Asia/Kolkata')::date
              ORDER BY d.bar_time DESC LIMIT 1
            ) previous ON TRUE
            ORDER BY b.bar_time DESC,i.exchange,i.symbol LIMIT :limit
        """),{"limit":max(1,min(9000,limit)),"sources":list(LIVE_BAR_SOURCES)}).mappings().all()
            indices=connection.execute(text("""
            WITH latest AS (
              SELECT DISTINCT ON (b.instrument_id) b.instrument_id,b.bar_time,b.close_price
              FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
              WHERE b.interval='5minute' AND b.source = ANY(:sources) AND i.instrument_type='INDEX'
                AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15:00'
                AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::time < TIME '15:30:00'
              ORDER BY b.instrument_id,b.bar_time DESC
            )
            SELECT i.symbol,l.bar_time,l.close_price,
              COALESCE(previous_day.close_price,current_open.close_price) previous_close
            FROM latest l JOIN instrument_master i ON i.id=l.instrument_id
            LEFT JOIN LATERAL(
              SELECT close_price FROM live_market_bars d
              WHERE d.instrument_id=i.id AND d.interval='day'
                AND (d.bar_time AT TIME ZONE 'Asia/Kolkata')::date<(l.bar_time AT TIME ZONE 'Asia/Kolkata')::date
              ORDER BY d.bar_time DESC LIMIT 1
            ) previous_day ON TRUE
            LEFT JOIN LATERAL(
              SELECT close_price FROM live_market_bars intraday
              WHERE intraday.instrument_id=i.id AND intraday.interval='5minute' AND intraday.source = ANY(:sources)
                AND (intraday.bar_time AT TIME ZONE 'Asia/Kolkata')::date=(l.bar_time AT TIME ZONE 'Asia/Kolkata')::date
                AND (intraday.bar_time AT TIME ZONE 'Asia/Kolkata')::time >= TIME '09:15:00'
                AND (intraday.bar_time AT TIME ZONE 'Asia/Kolkata')::time < TIME '15:30:00'
              ORDER BY intraday.bar_time ASC LIMIT 1
            ) current_open ON TRUE
            ORDER BY CASE WHEN REPLACE(i.symbol,' ','')='INDIAVIX' THEN 4 WHEN i.symbol ILIKE '%BANK%' THEN 3
                WHEN i.symbol ILIKE '%SENSEX%' THEN 2 ELSE 1 END LIMIT 4
        """),{"sources":list(LIVE_BAR_SOURCES)}).mappings().all()
        engine.dispose()
        if not rows:
            _CACHE[cache_key]=(now,None); return None
        quotes=[]
        for row in rows:
            features=row["features"] if isinstance(row["features"],dict) else {}
            price=float(row["close_price"]); previous=float(row["previous_close"] or price)
            quotes.append({"exchange":row["exchange"],"symbol":row["symbol"],"name":row["symbol"],"price":round(price,2),
                "change_pct":round((price/previous-1)*100,2) if previous else 0.0,
                "rsi":round(float(features.get("rsi_14",0))*50+50,1),
                "volume_ratio":round(max(0,float(features.get("volume_z20",0))+1),2),
                "breakout_pct":round(float(features.get("sma20_gap",0))*100,2),"vwap_pct":0.0,
                "volatility":round(float(features.get("volatility_20d",0))*100,2),"source_timestamp":row["bar_time"].isoformat()})
        index_values=[]
        for row in indices:
            price=float(row["close_price"]); previous=float(row["previous_close"] or price)
            index_values.append({"symbol":row["symbol"],"price":round(price,2),
                "change_pct":round((price/previous-1)*100,2) if previous else 0.0,"source_timestamp":row["bar_time"].isoformat()})
        advancing=sum(item["change_pct"]>0 for item in quotes)
        latest=max(row["bar_time"] for row in rows); age=max(0,(datetime.now(timezone.utc)-latest).total_seconds())
        result={"indices":index_values,"quotes":quotes,"breadth":{"advancing":advancing,"declining":len(quotes)-advancing,
                "ratio":round(advancing/max(1,len(quotes)),2)},"updated_at":datetime.now(timezone.utc).isoformat(),
                "source_timestamp":latest.isoformat(),"data_mode":"provider_live_completed_bars" if age<=900 else "provider_stale_completed_bars",
                "is_fresh":age<=900,"age_seconds":round(age,1),"orders_allowed":False}
        _CACHE[cache_key]=(now,result)
        return result
    except Exception as exc:
        import os
        env = os.getenv("ENVIRONMENT", os.getenv("NIVESH_ENV", "development")).lower()
        if env in {"production", "staging"}:
            return {
                "status": "degraded",
                "error": f"PostgreSQL live feed unreachable: {str(exc)[:150]}",
                "indices": [],
                "quotes": [],
                "breadth": {"advancing": 0, "declining": 0, "ratio": 0.0},
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source_timestamp": None,
                "data_mode": "database_unavailable",
                "is_fresh": False,
                "age_seconds": 999999.0,
                "orders_allowed": False,
            }
        return None


def price_map(database_url: str) -> Dict[str,float]:
    result=snapshot(database_url,9000)
    if result and result.get("status") != "degraded":
        return {item["symbol"]:item["price"] for item in result.get("quotes", [])}
    return {}
