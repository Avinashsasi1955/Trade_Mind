"""Read-only Kite stream runner for deterministic PostgreSQL aggregation."""
import json
import os
import signal
import threading
import time
from datetime import date, datetime, time as clock
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
import redis

from .config import MARKET_DATA_PROVIDER, UPSTOX_ACCESS_TOKEN
from .intelligence_memory import publish_brain_event
from .kite_stream import KiteStream
from .market_ingestion_v3 import PostgresBarAggregator
from .observability import configure_logging, page
from .postgres_repositories import PostgresHistoryStore
from .upstox_stream import UPSTOX_PROVIDER, UpstoxStream
from .zerodha_adapter import ZerodhaAdapter


IST=ZoneInfo("Asia/Kolkata"); logger=configure_logging()


class LiveStreamService:
    def __init__(self):
        self.database_url=os.environ["DATABASE_URL"]; self.provider=os.getenv("NIVESH_MARKET_DATA_PROVIDER",MARKET_DATA_PROVIDER).strip().lower()
        self.source=UPSTOX_PROVIDER if self.provider=="upstox" else "zerodha_kite"
        self.api_key=os.environ.get("KITE_API_KEY",""); self.access_token=os.environ.get("KITE_ACCESS_TOKEN","")
        self.upstox_token=os.environ.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_ANALYTICS_TOKEN") or UPSTOX_ACCESS_TOKEN
        if os.getenv("LIVE_ELIGIBLE","FALSE").upper()!="FALSE" or os.getenv("NIVESH_LIVE_TRADING_ENABLED","0")!="0":
            raise RuntimeError("Read-only stream requires both execution fuses locked")
        if self.provider=="upstox":
            if not self.upstox_token:
                raise RuntimeError("Read-only Upstox stream requires UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN")
        elif not self.api_key or not self.access_token:
            raise RuntimeError("Read-only Kite stream requires an API key and today's supported access token")
        elif self.provider!="zerodha":
            raise RuntimeError("NIVESH_MARKET_DATA_PROVIDER must be 'zerodha' or 'upstox'")
        self.engine=create_engine(self.database_url,pool_pre_ping=True,future=True); self.aggregator=PostgresBarAggregator(self.database_url,source=self.source)
        self.redis=redis.Redis.from_url(os.environ["REDIS_URL"],decode_responses=True,socket_timeout=5)
        self.lock=threading.Lock(); self.stop_event=threading.Event(); self.streams=[]; self.last_stale=(); self.open_gaps={}
        self.backfill_lock=threading.Lock(); self.full_limit=max(100,int(os.getenv("NIVESH_KITE_FULL_MODE_LIMIT","2500")))
        self.total_limit=min(9000,max(self.full_limit,int(os.getenv("NIVESH_KITE_TOTAL_TOKEN_LIMIT","9000"))))
        if self.provider=="upstox":
            self.full_limit=max(0,min(1500,int(os.getenv("NIVESH_UPSTOX_FULL_MODE_LIMIT","500"))))
            self.total_limit=max(self.full_limit,min(2000,int(os.getenv("NIVESH_UPSTOX_TOTAL_KEY_LIMIT","2000"))))

    def subscriptions(self):
        if self.provider=="upstox":
            with self.engine.connect() as connection:
                rows=connection.execute(text("""WITH latest_index AS (
                    SELECT DISTINCT ON (CASE
                        WHEN REPLACE(i.symbol,' ','') IN ('NIFTY50','NIFTY') THEN 'NIFTY'
                        WHEN REPLACE(i.symbol,' ','') IN ('BANKNIFTY','NIFTYBANK') THEN 'BANKNIFTY'
                        WHEN REPLACE(i.symbol,' ','') IN ('SENSEX','BSESENSEX') THEN 'SENSEX'
                        ELSE REPLACE(i.symbol,' ','') END)
                        CASE
                            WHEN REPLACE(i.symbol,' ','') IN ('NIFTY50','NIFTY') THEN 'NIFTY'
                            WHEN REPLACE(i.symbol,' ','') IN ('BANKNIFTY','NIFTYBANK') THEN 'BANKNIFTY'
                            WHEN REPLACE(i.symbol,' ','') IN ('SENSEX','BSESENSEX') THEN 'SENSEX'
                            ELSE REPLACE(i.symbol,' ','') END underlying_key,
                        b.close_price
                    FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
                    WHERE i.instrument_type='INDEX' AND b.interval='1minute' AND b.source=:source
                    ORDER BY underlying_key,b.bar_time DESC
                )
                SELECT k.provider_key,k.mode_hint,i.exchange,i.symbol,i.instrument_type,i.is_fno_eligible,i.expiry,i.strike,
                    li.close_price underlying_spot
                    FROM instrument_provider_keys k JOIN instrument_master i ON i.id=k.instrument_id
                    LEFT JOIN latest_index li ON li.underlying_key=i.underlying_symbol
                    WHERE i.is_active AND k.is_active AND k.provider='upstox_v3'
                    AND (i.instrument_type='INDEX' OR i.exchange IN ('NSE','BSE','NFO','BFO'))
                    AND (i.instrument_type NOT IN ('FUT','CE','PE') OR i.expiry>=CURRENT_DATE)
                    ORDER BY CASE
                        WHEN REPLACE(i.symbol,' ','') IN ('NIFTY','NIFTY50','NIFTYBANK','BANKNIFTY','SENSEX','BSESENSEX','INDIAVIX','VIX') THEN 0
                        WHEN i.exchange='NSE' AND i.instrument_type='EQ' AND i.symbol IN (
                            'RELIANCE','TCS','HDFCBANK','ICICIBANK','INFY','SBIN','LT','ITC','BHARTIARTL','AXISBANK',
                            'KOTAKBANK','HINDUNILVR','BAJFINANCE','MARUTI','SUNPHARMA','TRENT','NTPC','ONGC',
                            'POWERGRID','ULTRACEMCO','TITAN','ADANIENT','ADANIPORTS','WIPRO','TECHM','JSWSTEEL',
                            'TATASTEEL','COALINDIA','HCLTECH','BEL') THEN 1
                        WHEN i.instrument_type IN ('CE','PE') AND i.underlying_symbol IN ('NIFTY','BANKNIFTY','SENSEX')
                            AND i.expiry<=CURRENT_DATE+INTERVAL '14 days' THEN 2
                        WHEN i.exchange='NSE' AND i.instrument_type='EQ' AND i.is_fno_eligible THEN 3
                        WHEN i.instrument_type='FUT' AND i.underlying_symbol IN ('NIFTY','BANKNIFTY','SENSEX')
                            AND i.expiry<=CURRENT_DATE+INTERVAL '45 days' THEN 4
                        WHEN i.exchange='NSE' AND i.instrument_type='EQ' THEN 5
                        ELSE 6 END,
                        i.expiry NULLS LAST,
                        CASE WHEN i.instrument_type IN ('CE','PE') THEN ABS(i.strike-COALESCE(li.close_price,i.strike)) ELSE 0 END,
                        i.exchange,i.symbol"""),{"source":self.source}).mappings().all()
            selected=rows[:self.total_limit]; subscriptions=[]
            for index,row in enumerate(selected):
                priority=row["instrument_type"]=="INDEX" or row["instrument_type"] in {"FUT","CE","PE"} or row["is_fno_eligible"]
                mode="full" if priority and index<self.full_limit else "ltpc"
                subscriptions.append((row["provider_key"],mode))
            if len(rows)>self.total_limit:
                self.record("WARNING","Instrument universe exceeds Upstox WebSocket capacity",{"active":len(rows),"subscribed":len(selected),"limit":self.total_limit})
            return subscriptions
        with self.engine.connect() as connection:
            rows=connection.execute(text("""SELECT instrument_token,exchange,symbol,instrument_type,is_fno_eligible,expiry FROM instrument_master
                WHERE is_active AND instrument_token IS NOT NULL AND (instrument_type='INDEX' OR exchange IN ('NSE','BSE','NFO','BFO'))
                ORDER BY CASE WHEN REPLACE(symbol,' ','') IN ('INDIAVIX','VIX') THEN 0 WHEN instrument_type='INDEX' THEN 1
                    WHEN instrument_type IN ('FUT','CE','PE') AND expiry<=CURRENT_DATE+INTERVAL '45 days' THEN 2
                    WHEN exchange IN ('NSE','BSE') AND instrument_type='EQ' AND is_fno_eligible THEN 3 ELSE 4 END,
                    expiry NULLS LAST,exchange,symbol""")).mappings().all()
        selected=rows[:self.total_limit]; subscriptions=[]
        for index,row in enumerate(selected):
            priority=row["instrument_type"]=="INDEX" or row["instrument_type"] in {"FUT","CE","PE"} or row["is_fno_eligible"]
            mode="full" if priority and index<self.full_limit else "quote"
            subscriptions.append((int(row["instrument_token"]),mode))
        if len(rows)>self.total_limit:
            self.record("WARNING","Instrument universe exceeds Kite WebSocket capacity",{"active":len(rows),"subscribed":len(selected),"limit":self.total_limit})
        return subscriptions

    def tokens(self): return [token for token,_ in self.subscriptions()]

    def stream_status(self,index,status,payload):
        if status in {"CONNECTED", "MESSAGE"}:
            level = "INFO"
        elif status == "STALE":
            level = "WARNING"
        else:
            level = "ERROR"
        self.record(level,f"{self.source} stream {status.lower()}",{"stream":index,**payload})
        if status in {"DISCONNECTED","ERROR"} and not self.market_open():
            if index in self.open_gaps:
                with self.engine.begin() as connection:
                    connection.execute(text("UPDATE market_data_gaps SET status='RECOVERED',recovered_at=CURRENT_TIMESTAMP WHERE id=:id"),
                                       {"id":self.open_gaps.pop(index)})
            return
        if status in {"DISCONNECTED","ERROR"} and index not in self.open_gaps:
            with self.engine.begin() as connection:
                gap=connection.execute(text("""INSERT INTO market_data_gaps(stream_id,last_tick_at,affected_tokens,status,details,provider)
                    VALUES(:stream,:last_tick,:tokens,'OPEN',CAST(:details AS jsonb),:provider) RETURNING id"""),
                    {"stream":f"{self.source}-{index}","provider":self.source,
                     "last_tick":datetime.fromtimestamp(self.streams[index].last_tick_at,ZoneInfo("UTC")) if index<len(self.streams) and self.streams[index].last_tick_at else None,
                     "tokens":len(getattr(self.streams[index],"tokens",getattr(self.streams[index],"instrument_keys",[]))) if index<len(self.streams) else 0,
                     "details":json.dumps(payload)}).scalar_one()
                publish_brain_event(connection,"GapDetected","LiveStreamService",
                                    {"gap_id":int(gap),"stream":f"{self.source}-{index}","provider":self.source,
                                     "status":status,"details":payload},
                                    severity="WARNING")
                try:
                    from .brains import get_bus
                    from .brains.bus import GapDetected
                    get_bus().publish(GapDetected(source_brain="LiveStreamService",provider=self.source,
                                                  stream_id=f"{self.source}-{index}",
                                                  affected_tokens=len(getattr(self.streams[index],"tokens",getattr(self.streams[index],"instrument_keys",[]))) if index<len(self.streams) else 0,
                                                  payload={"gap_id":int(gap),"status":status}))
                except Exception:
                    pass
            self.open_gaps[index]=int(gap)
        elif status=="CONNECTED" and index in self.open_gaps:
            with self.engine.begin() as connection:
                gap_id=self.open_gaps.pop(index)
                connection.execute(text("UPDATE market_data_gaps SET status='RECOVERED',recovered_at=CURRENT_TIMESTAMP WHERE id=:id"),{"id":gap_id})
                publish_brain_event(connection,"GapRepaired","LiveStreamService",
                                    {"gap_id":int(gap_id),"stream":f"{self.source}-{index}","provider":self.source,
                                     "method":"stream_reconnected"},
                                    severity="INFO")
                try:
                    from .brains import get_bus
                    from .brains.bus import GapRepaired
                    get_bus().publish(GapRepaired(source_brain="LiveStreamService",provider=self.source,
                                                  repaired_bars=0,remaining_open_gaps=len(self.open_gaps),
                                                  payload={"gap_id":int(gap_id),"method":"stream_reconnected"}))
                except Exception:
                    pass
            if self.provider=="zerodha":
                threading.Thread(target=self.backfill_priority,name="kite-gap-backfill",daemon=True).start()

    def backfill_priority(self):
        if self.provider!="zerodha": return
        if not self.backfill_lock.acquire(blocking=False): return
        try:
            limit=max(1,min(50,int(os.getenv("NIVESH_GAP_BACKFILL_LIMIT","20"))))
            with self.engine.connect() as connection:
                rows=connection.execute(text("""SELECT instrument_token,exchange,symbol FROM instrument_master WHERE is_active
                    AND instrument_token IS NOT NULL AND (instrument_type='INDEX' OR is_fno_eligible) ORDER BY instrument_type='INDEX' DESC,symbol LIMIT :limit"""),{"limit":limit}).mappings().all()
            adapter=ZerodhaAdapter(self.api_key,self.access_token); store=PostgresHistoryStore(); restored=0
            for row in rows:
                candles=adapter.historical(int(row["instrument_token"]),"5minute",date.today(),date.today())
                if candles: restored+=store.save(row["exchange"],row["symbol"],int(row["instrument_token"]),"5minute",candles,datetime.now(ZoneInfo("UTC")).isoformat(),"kite_gap_backfill")
            self.record("INFO","Priority candle backfill completed",{"bars":restored,"instruments":len(rows)})
        except Exception as exc:
            self.record("ERROR","Priority candle backfill failed",{"error_type":type(exc).__name__})
        finally: self.backfill_lock.release()

    def on_ticks(self,ticks):
        pipeline=self.redis.pipeline(transaction=False)
        for tick in ticks:
            depth=tick.get("depth") or []; buys=[x for x in depth if x.get("side")=="buy" and x.get("price")]; sells=[x for x in depth if x.get("side")=="sell" and x.get("price")]
            if buys and sells:
                bid=max(buys,key=lambda x:x["price"]); ask=min(sells,key=lambda x:x["price"])
                snapshot={"instrument_token":tick["instrument_token"],"best_bid":bid["price"],"best_ask":ask["price"],
                          "bid_quantity":bid.get("quantity",0),"ask_quantity":ask.get("quantity",0),
                          "exchange_timestamp":tick.get("exchange_timestamp"),"received_at":tick.get("received_at")}
                pipeline.setex(f"nivesh:depth:{int(tick['instrument_token'])}",900,json.dumps(snapshot))
        pipeline.execute()
        with self.lock:
            completed=self.aggregator.ingest(ticks)
            partials=self.aggregator.partial_snapshots()
        partial_pipeline=self.redis.pipeline(transaction=False)
        for partial in partials:
            partial_pipeline.setex(f"nivesh:partial_bar:{partial['instrument_id']}:{partial['interval']}",120,json.dumps(partial))
        partial_pipeline.execute()
        if completed: logger.info("market bars completed",extra={"context":{"bars":completed,"ticks":len(ticks)}})

    def market_open(self):
        now=datetime.now(IST)
        if now.weekday()>=5: return False
        with self.engine.connect() as connection:
            session=connection.execute(text("SELECT session_status,opens_at,closes_at FROM exchange_trading_calendar WHERE exchange='NSE' AND session_date=:day"),{"day":now.date()}).mappings().one_or_none()
        if session and session["session_status"]=="CLOSED": return False
        opens=session["opens_at"] if session and session["opens_at"] else clock(9,15)
        closes=session["closes_at"] if session and session["closes_at"] else clock(15,30)
        return opens<=now.time().replace(tzinfo=None)<=closes

    def record(self,level,message,payload=None):
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO monitoring_events(component,level,message,payload,created_at) VALUES(:component,:level,:message,:payload,CURRENT_TIMESTAMP)"),
                {"component":f"{self.source}_stream","level":level,"message":message,"payload":json.dumps(payload or {})})

    def purge_post_close_bars(self):
        """Remove accidental post-close provider candles for the current IST session.

        The tick aggregator rejects candles outside 09:15-15:30 IST, but this
        cleanup protects charts/ML if an older stream container or delayed
        reconnect ever wrote rows after the continuous session.
        """
        market_date=datetime.now(IST).date()
        with self.engine.begin() as connection:
            deleted=connection.execute(text("""
                DELETE FROM live_market_bars
                WHERE source=:source
                  AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date=:market_date
                  AND (bar_time AT TIME ZONE 'Asia/Kolkata')::time > TIME '15:30:00'
            """),{"source":self.source,"market_date":market_date}).rowcount
        if deleted:
            self.record("WARNING","Post-close market bars purged",{"deleted":deleted,"market_date":market_date.isoformat()})
        return int(deleted or 0)

    def purge_expired_second_bars(self):
        retention_days=max(0,int(os.getenv("NIVESH_1S_RETENTION_DAYS","30")))
        if retention_days<=0:
            return 0
        with self.engine.begin() as connection:
            deleted=connection.execute(text("""
                DELETE FROM live_market_bars
                WHERE interval='1second'
                  AND bar_time < CURRENT_TIMESTAMP - (:days || ' days')::interval
            """),{"days":retention_days}).rowcount
        if deleted:
            self.record("INFO","Expired 1-second market bars purged",{"deleted":deleted,"retention_days":retention_days})
        return int(deleted or 0)

    def run(self):
        self.purge_post_close_bars()
        self.purge_expired_second_bars()
        subscriptions=self.subscriptions()
        if not subscriptions: raise RuntimeError("No synchronized instrument tokens are available")
        chunk_size=2000 if self.provider=="upstox" else 3000
        for start in range(0,len(subscriptions),chunk_size):
            index=len(self.streams); chunk=subscriptions[start:start+chunk_size]
            if self.provider=="upstox":
                stream=UpstoxStream(self.upstox_token,self.on_ticks,on_status=lambda status,payload,index=index:self.stream_status(index,status,payload))
                for mode in ("full","ltpc","option_greeks","full_d30"): stream.subscribe([key for key,item_mode in chunk if item_mode==mode],mode)
            else:
                stream=KiteStream(self.api_key,self.access_token,self.on_ticks,on_status=lambda status,payload,index=index:self.stream_status(index,status,payload))
                for mode in ("full","quote","ltp"): stream.subscribe([token for token,item_mode in chunk if item_mode==mode],mode)
            self.streams.append(stream); stream.start()
        self.record("INFO",f"Read-only {self.source} streams started",{"connections":len(self.streams),"instruments":len(subscriptions),
            "full_mode":sum(mode=="full" for _,mode in subscriptions),"orders_allowed":False})
        while not self.stop_event.wait(30):
            with self.lock: clock_flushed=self.aggregator.flush_closed(datetime.now(ZoneInfo("UTC")))
            if clock_flushed: logger.info("clock-closed market bars persisted",extra={"context":{"bars":clock_flushed}})
            if self.market_open():
                stale=[index for index,stream in enumerate(self.streams) if not stream.last_tick_at or time.time()-stream.last_tick_at>60]
                state=tuple(stale)
                if state and state!=self.last_stale:
                    self.record("ERROR",f"{self.source} stream stale",{"connections":stale})
                    page("CRITICAL",f"{self.source} stream stale during market hours",{"connections":stale})
                elif not state and self.last_stale:
                    self.record("INFO",f"{self.source} stream recovered",{"connections":len(self.streams)})
                self.last_stale=state
        for stream in self.streams: stream.stop()
        with self.lock: discarded=self.aggregator.discard_partial()
        purged=self.purge_post_close_bars()
        self.record("INFO",f"Read-only {self.source} streams stopped",{"discarded_partial_bars":discarded})
        if purged:
            logger.warning("post-close market bars purged",extra={"context":{"bars":purged,"source":self.source}})

    def stop(self,*_args): self.stop_event.set()


def main():
    service=LiveStreamService(); signal.signal(signal.SIGTERM,service.stop); signal.signal(signal.SIGINT,service.stop); service.run()


if __name__=="__main__": main()
