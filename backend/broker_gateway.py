"""Interactive broker-session handshake. Access tokens remain in process memory."""
import base64
import json
import secrets
import hashlib
from datetime import datetime,timedelta,timezone
from typing import Dict,Optional
from urllib.parse import quote, urlencode

from .config import BROKER_SESSION_KEY,KITE_API_KEY,KITE_API_SECRET,KITE_REDIRECT_URL,LIVE_TRADING_ENABLED,REDIS_URL
from .database import now_iso
from .zerodha_adapter import ZerodhaAdapter


_sessions: Dict[int,Dict]={}


def _redis():
    if not REDIS_URL: return None
    import redis
    return redis.Redis.from_url(REDIS_URL,decode_responses=True,socket_connect_timeout=3,socket_timeout=3)


def _session_name(user_id:int)->str: return f"nivesh:broker-session:{user_id}"


def _encrypt(user_id:int,payload:Dict)->str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key=hashlib.sha256(BROKER_SESSION_KEY.encode()).digest(); nonce=secrets.token_bytes(12); aad=_session_name(user_id).encode()
    ciphertext=AESGCM(key).encrypt(nonce,json.dumps(payload,separators=(",",":")).encode(),aad)
    return base64.urlsafe_b64encode(nonce+ciphertext).decode()


def _decrypt(user_id:int,value:str)->Dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw=base64.urlsafe_b64decode(value); key=hashlib.sha256(BROKER_SESSION_KEY.encode()).digest(); aad=_session_name(user_id).encode()
    return json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],aad))


def _save_session(user_id:int,payload:Dict)->None:
    client=_redis()
    if client is None: _sessions[user_id]=payload; return
    client.setex(_session_name(user_id),20*60*60,_encrypt(user_id,payload))


def _remove_session(user_id:int)->None:
    _sessions.pop(user_id,None); client=_redis()
    if client is not None: client.delete(_session_name(user_id))


def begin_login(db,user_id:int)->Dict:
    if not KITE_API_KEY or not KITE_API_SECRET: raise ValueError("KITE_API_KEY and KITE_API_SECRET are not configured")
    state=secrets.token_urlsafe(32); expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    db.execute("INSERT INTO broker_auth_states(state,user_id,expires_at,created_at) VALUES(?,?,?,?)",(state,user_id,expires,now_iso())); db.commit()
    url=f"https://kite.zerodha.com/connect/login?v=3&api_key={KITE_API_KEY}&redirect_params={quote(urlencode({'state':state}))}"
    return {"login_url":url,"redirect_url":KITE_REDIRECT_URL,"expires_at":expires}


def complete_login(db,request_token:str,state:str)->Dict:
    row=db.execute("SELECT * FROM broker_auth_states WHERE state=?",(state,)).fetchone()
    if not row or row["used"] or row["expires_at"]<now_iso(): raise ValueError("Broker login state is invalid or expired")
    session=ZerodhaAdapter(KITE_API_KEY,api_secret=KITE_API_SECRET).create_session(request_token); user_id=row["user_id"]
    _save_session(user_id,{"access_token":session["access_token"],"broker_user_id":session.get("user_id"),"created_at":now_iso()})
    db.execute("UPDATE broker_auth_states SET used=1 WHERE state=?",(state,)); db.execute("UPDATE broker_connections SET status='CONNECTED',broker_user_id=?,last_heartbeat_at=?,updated_at=? WHERE user_id=?",(session.get("user_id"),now_iso(),now_iso(),user_id)); db.commit()
    return {"user_id":user_id,"broker_user_id":session.get("user_id"),"status":"CONNECTED"}


def access_token(user_id:int)->str:
    client=_redis()
    if client is None: return _sessions.get(user_id,{}).get("access_token","")
    value=client.get(_session_name(user_id))
    if not value: return ""
    try: return _decrypt(user_id,value).get("access_token","")
    except Exception:
        client.delete(_session_name(user_id)); return ""


def disconnect(db,user_id:int)->Dict:
    _remove_session(user_id); db.execute("UPDATE broker_connections SET status='DISCONNECTED',broker_user_id=NULL,session_expires_at=NULL,last_heartbeat_at=NULL,updated_at=? WHERE user_id=?",(now_iso(),user_id)); db.commit(); return connection_status(db,user_id)


def connection_status(db,user_id:int)->Dict:
    row=db.execute("SELECT * FROM broker_connections WHERE user_id=?",(user_id,)).fetchone(); result=dict(row)
    result.update({"api_credentials_configured":bool(KITE_API_KEY and KITE_API_SECRET),"session_in_memory":bool(access_token(user_id)),"live_deployment_enabled":LIVE_TRADING_ENABLED,"redirect_url":KITE_REDIRECT_URL,"secrets_persisted_in_database":False})
    return result
