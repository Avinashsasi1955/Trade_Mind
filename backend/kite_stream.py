"""Dormant Kite WebSocket transport with binary parsing and reconnect support.

Requires the optional `websocket-client` package plus valid session credentials.
It never starts automatically when credentials are absent.
"""
import json
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional


def _divisor(token: int) -> float:
    segment=token & 0xFF
    if segment==3: return 10_000_000.0  # CDS
    if segment==6: return 10_000.0      # BCD
    return 100.0


def split_packets(payload: bytes) -> List[bytes]:
    if len(payload)<2: return []
    count=struct.unpack(">H",payload[:2])[0]; offset=2; packets=[]
    for _ in range(count):
        if offset+2>len(payload): break
        length=struct.unpack(">H",payload[offset:offset+2])[0]; offset+=2
        if offset+length>len(payload): break
        packets.append(payload[offset:offset+length]); offset+=length
    return packets


def parse_packet(packet: bytes) -> Optional[Dict]:
    if len(packet)<8: return None
    token=struct.unpack(">I",packet[:4])[0]; divisor=_divisor(token); result={"instrument_token":token,"last_price":struct.unpack(">I",packet[4:8])[0]/divisor,"received_at":datetime.now(timezone.utc).isoformat()}
    if len(packet) in {28,32}:
        result.update({"high":struct.unpack(">I",packet[8:12])[0]/divisor,"low":struct.unpack(">I",packet[12:16])[0]/divisor,
                       "open":struct.unpack(">I",packet[16:20])[0]/divisor,"close":struct.unpack(">I",packet[20:24])[0]/divisor,
                       "change":struct.unpack(">i",packet[24:28])[0]/divisor})
        if len(packet)>=32: result["exchange_timestamp"]=struct.unpack(">I",packet[28:32])[0]
        return result
    if len(packet)>=44:
        result.update({"last_quantity":struct.unpack(">I",packet[8:12])[0],"average_price":struct.unpack(">I",packet[12:16])[0]/divisor,"volume":struct.unpack(">I",packet[16:20])[0],"buy_quantity":struct.unpack(">I",packet[20:24])[0],"sell_quantity":struct.unpack(">I",packet[24:28])[0],"open":struct.unpack(">I",packet[28:32])[0]/divisor,"high":struct.unpack(">I",packet[32:36])[0]/divisor,"low":struct.unpack(">I",packet[36:40])[0]/divisor,"close":struct.unpack(">I",packet[40:44])[0]/divisor})
    if len(packet)>=184:
        result["last_trade_time"]=struct.unpack(">I",packet[44:48])[0]; result["oi"]=struct.unpack(">I",packet[48:52])[0]; result["exchange_timestamp"]=struct.unpack(">I",packet[60:64])[0]
        depth=[]
        for index in range(10):
            start=64+index*12; quantity,price,orders=struct.unpack(">IIH",packet[start:start+10]); depth.append({"side":"buy" if index<5 else "sell","quantity":quantity,"price":price/divisor,"orders":orders})
        result["depth"]=depth
    return result


def parse_binary(payload: bytes) -> List[Dict]:
    return [parsed for parsed in (parse_packet(x) for x in split_packets(payload)) if parsed]


class KiteStream:
    def __init__(self,api_key:str,access_token:str,on_ticks:Callable[[List[Dict]],None],on_order:Optional[Callable[[Dict],None]]=None,
                 on_status:Optional[Callable[[str,Dict],None]]=None):
        self.api_key=api_key; self.access_token=access_token; self.on_ticks=on_ticks; self.on_order=on_order; self.tokens=[]; self.ws=None; self._stop=False; self.reconnect_attempts=0; self.last_tick_at=None
        self.on_status=on_status; self.mode_tokens={"ltp":[],"quote":[],"full":[]}; self.last_heartbeat_at=None

    @property
    def configured(self): return bool(self.api_key and self.access_token)

    def subscribe(self,tokens:Iterable[int],mode:str="full"):
        # Kite permits up to 3,000 instruments per WebSocket connection.
        if mode not in self.mode_tokens: raise ValueError("Unsupported Kite subscription mode")
        incoming=list(dict.fromkeys(int(x) for x in tokens)); existing=set(self.tokens)
        self.tokens=(self.tokens+[x for x in incoming if x not in existing])[:3000]
        allowed=set(self.tokens); self.mode_tokens[mode]=list(dict.fromkeys(self.mode_tokens[mode]+[x for x in incoming if x in allowed]))
        if self.ws:
            selected=[x for x in incoming if x in allowed]
            if selected:
                self.ws.send(json.dumps({"a":"subscribe","v":selected}))
                self.ws.send(json.dumps({"a":"mode","v":[mode,selected]}))

    def _run(self):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("Install websocket-client to enable Kite streaming") from exc
        if not self.configured: raise RuntimeError("Kite WebSocket credentials are not configured")
        url=f"wss://ws.kite.trade?api_key={self.api_key}&access_token={self.access_token}"
        def opened(ws):
            self.reconnect_attempts=0; self.ws=ws
            if self.tokens: ws.send(json.dumps({"a":"subscribe","v":self.tokens}))
            for mode,tokens in self.mode_tokens.items():
                if tokens: ws.send(json.dumps({"a":"mode","v":[mode,tokens]}))
            if self.on_status: self.on_status("CONNECTED",{"tokens":len(self.tokens)})
        def message(_ws,payload):
            if isinstance(payload,bytes):
                self.last_heartbeat_at=time.time(); ticks=parse_binary(payload)
                if ticks:
                    self.last_tick_at=self.last_heartbeat_at; self.on_ticks(ticks)
            else:
                event=json.loads(payload)
                if event.get("type")=="order" and self.on_order: self.on_order(event.get("data",{}))
                elif event.get("type")=="error" and self.on_status: self.on_status("ERROR",{"message":str(event.get("data","Kite error"))[:300]})
        def closed(_ws,*args):
            self.ws=None
            if self.on_status: self.on_status("DISCONNECTED",{"details":[str(x)[:200] for x in args]})
        def errored(_ws,error):
            if self.on_status: self.on_status("ERROR",{"type":type(error).__name__})
        while not self._stop:
            app=websocket.WebSocketApp(url,on_open=opened,on_message=message,on_close=closed,on_error=errored)
            app.run_forever(ping_interval=30,ping_timeout=10); self.reconnect_attempts+=1
            if not self._stop: time.sleep(min(60,2**min(5,self.reconnect_attempts)))

    def start(self):
        thread=threading.Thread(target=self._run,name="kite-market-stream",daemon=True); thread.start(); return thread

    def stop(self):
        self._stop=True
        if self.ws: self.ws.close()
