"""Approval-gated order-intent lifecycle with immutable event history."""
import json
import uuid
from typing import Dict

from .config import LIVE_ELIGIBLE, LIVE_TRADING_ENABLED
from .database import now_iso
from .risk_engine import RiskEngine
from .zerodha_adapter import ZerodhaAdapter
from .config import KITE_API_KEY
from .broker_gateway import access_token
from .instrument_router import route_ml_signal


TERMINAL={"COMPLETE","REJECTED","CANCELLED","CANCELLED_BY_KILL_SWITCH","RISK_REJECTED"}


def _event(db,intent_id:int,event_type:str,payload:Dict=None):
    db.execute("INSERT INTO order_events(order_intent_id,event_type,payload,created_at) VALUES(?,?,?,?)",(intent_id,event_type,json.dumps(payload or {}),now_iso()))


def create_intent(db,user_id:int,payload:Dict)->Dict:
    order={"symbol":payload.get("symbol","").upper().strip(),"exchange":payload.get("exchange","NSE").upper(),"transaction_type":payload.get("transaction_type","BUY").upper(),"quantity":int(payload.get("quantity",0)),"order_type":payload.get("order_type","LIMIT").upper(),"limit_price":float(payload.get("limit_price",0)),"product":payload.get("product","CNC").upper(),"strategy":payload.get("strategy","manual-supervised"),"confidence":float(payload.get("confidence",0)),"reasoning":payload.get("reasoning","")[:2000]}
    route=route_ml_signal(db,payload,order); order=route.order
    decision=RiskEngine().evaluate(db,user_id,order,payload.get("quote_timestamp"))
    if not route.allowed:
        decision.allowed=False; decision.reason=route.audit["reason"]; decision.checks.append({"name":"derivative_route","passed":False,"detail":route.audit["reason"]})
    elif route.audit.get("required"):
        decision.checks.append({"name":"derivative_route","passed":True,"detail":route.audit["reason"]})
    decision_payload={**decision.__dict__,"routing":route.audit}; status="APPROVAL_PENDING" if decision.allowed else "RISK_REJECTED"; client_id=(payload.get("client_order_id") or f"NV-{uuid.uuid4().hex[:20].upper()}")[:64]; stamp=now_iso()
    existing=db.execute("SELECT id FROM order_intents WHERE client_order_id=? AND user_id=?",(client_id,user_id)).fetchone()
    if existing: return intent_detail(db,user_id,existing["id"])
    cursor=db.execute("INSERT INTO order_intents(user_id,client_order_id,symbol,exchange,transaction_type,quantity,order_type,limit_price,product,strategy,confidence,reasoning,status,risk_payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(user_id,client_id,order["symbol"],order["exchange"],order["transaction_type"],order["quantity"],order["order_type"],order["limit_price"],order["product"],order["strategy"],order["confidence"],order["reasoning"],status,json.dumps(decision_payload),stamp,stamp))
    if route.audit.get("required"): _event(db,cursor.lastrowid,"BEARISH_ROUTE_SELECTED" if route.allowed else "BEARISH_ROUTE_REJECTED",route.audit)
    _event(db,cursor.lastrowid,"RISK_ACCEPTED" if decision.allowed else "RISK_REJECTED",decision_payload); db.commit(); return intent_detail(db,user_id,cursor.lastrowid)


def intent_detail(db,user_id:int,intent_id:int)->Dict:
    row=db.execute("SELECT * FROM order_intents WHERE id=? AND user_id=?",(intent_id,user_id)).fetchone()
    if not row: raise ValueError("Order intent not found")
    events=db.execute("SELECT event_type,payload,created_at FROM order_events WHERE order_intent_id=? ORDER BY id",(intent_id,)).fetchall()
    return {**dict(row),"risk_payload":json.loads(row["risk_payload"]),"events":[{**dict(x),"payload":json.loads(x["payload"])} for x in events]}


def list_intents(db,user_id:int,limit:int=50)->Dict:
    rows=db.execute("SELECT id,client_order_id,symbol,exchange,transaction_type,quantity,order_type,limit_price,product,strategy,confidence,status,risk_payload,broker_order_id,created_at,updated_at FROM order_intents WHERE user_id=? ORDER BY id DESC LIMIT ?",(user_id,min(200,max(1,limit)))).fetchall(); return {"items":[{**dict(x),"risk_payload":json.loads(x["risk_payload"])} for x in rows]}


def approve_intent(db,user_id:int,intent_id:int)->Dict:
    row=db.execute("SELECT * FROM order_intents WHERE id=? AND user_id=?",(intent_id,user_id)).fetchone()
    if not row or row["status"]!="APPROVAL_PENDING": raise ValueError("Intent is not awaiting approval")
    switch=db.execute("SELECT active FROM kill_switches WHERE user_id=?",(user_id,)).fetchone()
    if switch and switch["active"]: raise ValueError("Kill switch is active")
    stamp=now_iso(); db.execute("UPDATE order_intents SET status='APPROVED',approved_at=?,updated_at=? WHERE id=?",(stamp,stamp,intent_id)); _event(db,intent_id,"HUMAN_APPROVED"); db.commit(); return intent_detail(db,user_id,intent_id)


def submit_intent(db,user_id:int,intent_id:int)->Dict:
    row=db.execute("SELECT * FROM order_intents WHERE id=? AND user_id=?",(intent_id,user_id)).fetchone()
    if not row or row["status"]!="APPROVED": raise ValueError("Intent must be approved before submission")
    if not LIVE_TRADING_ENABLED: raise ValueError("Live trading deployment flag is disabled")
    if not LIVE_ELIGIBLE: raise ValueError("Model promotion fuse LIVE_ELIGIBLE is false")
    try: promotion=db.execute("SELECT live_eligible FROM system_promotion_ledger WHERE singleton=TRUE").fetchone()
    except Exception: promotion=None
    if not promotion or not promotion["live_eligible"]: raise ValueError("Database promotion ledger is not eligible")
    adapter=ZerodhaAdapter(KITE_API_KEY,access_token(user_id))
    if not adapter.configured: raise ValueError("Zerodha session is not configured")
    result=adapter.place_order(row["symbol"],row["transaction_type"],row["quantity"],row["order_type"],row["exchange"],row["product"],row["limit_price"])
    stamp=now_iso(); broker_id=str(result.get("order_id","")); db.execute("UPDATE order_intents SET status='SUBMITTED',broker_order_id=?,submitted_at=?,updated_at=? WHERE id=?",(broker_id,stamp,stamp,intent_id)); _event(db,intent_id,"BROKER_SUBMITTED",result); db.commit(); return intent_detail(db,user_id,intent_id)


def reconcile_intents(db,user_id:int)->Dict:
    if not LIVE_TRADING_ENABLED: return {"checked":0,"updated":0,"reason":"Live deployment flag disabled"}
    adapter=ZerodhaAdapter(KITE_API_KEY,access_token(user_id))
    if not adapter.configured: return {"checked":0,"updated":0,"reason":"Broker session unavailable"}
    broker_orders=adapter.orders(); by_id={str(x.get("order_id")):x for x in broker_orders}; rows=db.execute("SELECT * FROM order_intents WHERE user_id=? AND broker_order_id IS NOT NULL AND status NOT IN ('COMPLETE','REJECTED','CANCELLED')",(user_id,)).fetchall(); updated=0
    mapping={"COMPLETE":"COMPLETE","REJECTED":"REJECTED","CANCELLED":"CANCELLED","OPEN":"OPEN","TRIGGER PENDING":"TRIGGER_PENDING"}
    for row in rows:
        broker=by_id.get(str(row["broker_order_id"]));
        if not broker: continue
        status="PARTIAL" if broker.get("status")=="OPEN" and int(broker.get("filled_quantity") or 0)>0 else mapping.get(broker.get("status"),row["status"])
        if status!=row["status"]:
            db.execute("UPDATE order_intents SET status=?,updated_at=? WHERE id=?",(status,now_iso(),row["id"])); _event(db,row["id"],f"BROKER_{status}",broker); updated+=1
    broker_positions=adapter.positions().get("net",[]); local={x["symbol"]:x["quantity"] for x in db.execute("SELECT symbol,quantity FROM holdings WHERE user_id=?",(user_id,)).fetchall()}; remote={x.get("tradingsymbol"):int(x.get("quantity") or 0) for x in broker_positions if int(x.get("quantity") or 0)!=0}; discrepancies=[{"symbol":symbol,"local_quantity":local.get(symbol,0),"broker_quantity":remote.get(symbol,0)} for symbol in sorted(set(local)|set(remote)) if local.get(symbol,0)!=remote.get(symbol,0)]
    db.commit(); return {"checked":len(rows),"updated":updated,"position_discrepancies":discrepancies,"reason":"Reconciled" if not discrepancies else "Order states reconciled; position discrepancies require operator review"}


def emergency_cancel_open_orders(db,user_id:int)->Dict:
    if not LIVE_TRADING_ENABLED: return {"attempted":0,"cancelled":0,"reason":"Live deployment flag disabled; local intents were cancelled"}
    adapter=ZerodhaAdapter(KITE_API_KEY,access_token(user_id))
    if not adapter.configured: return {"attempted":0,"cancelled":0,"reason":"Broker session unavailable; local intents were cancelled"}
    candidates=[x for x in adapter.orders() if x.get("status") in {"OPEN","TRIGGER PENDING"} and x.get("tag")=="NIVESH_AI"]; cancelled=0; errors=[]
    for order in candidates:
        try: adapter.cancel_order(str(order["order_id"]),order.get("variety","regular")); cancelled+=1
        except Exception as exc: errors.append({"order_id":order.get("order_id"),"error":str(exc)[:200]})
    return {"attempted":len(candidates),"cancelled":cancelled,"errors":errors,"reason":"Emergency broker cancellation attempted"}
