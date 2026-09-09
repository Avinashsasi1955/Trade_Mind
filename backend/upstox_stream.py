"""Read-only Upstox Market Data Feed V3 adapter.

This module deliberately exposes only market-data primitives. It contains no
order-placement code and is safe to run while the execution fuses remain locked.
"""
import gzip
import hashlib
import json
import math
import struct
import threading
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests
from sqlalchemy import create_engine, text

from .config import UPSTOX_AUTHORIZE_URL, UPSTOX_INSTRUMENTS_URL


UPSTOX_PROVIDER = "upstox_v3"
SUPPORTED_SEGMENTS = {"NSE_EQ", "BSE_EQ", "NSE_INDEX", "BSE_INDEX", "NSE_FO", "BSE_FO"}


def provider_token(instrument_key: str) -> int:
    """Stable positive token for internal aggregation maps.

    Upstox recommends ``instrument_key`` as the unique identifier. The existing
    candle pipeline indexes ticks by integer token, so we use a deterministic
    63-bit surrogate and store the original key in ``instrument_provider_keys``.
    """
    digest = hashlib.blake2b(instrument_key.encode("utf-8"), digest_size=8, person=b"niveshai").digest()
    token = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
    return token or 1


def _read_varint(payload: bytes, offset: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(payload):
        byte = payload[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 70:
            raise ValueError("Invalid protobuf varint")
    raise ValueError("Truncated protobuf varint")


def _fields(payload: bytes):
    offset = 0
    while offset < len(payload):
        key, offset = _read_varint(payload, offset)
        field = key >> 3
        wire = key & 0x07
        if wire == 0:
            value, offset = _read_varint(payload, offset)
        elif wire == 1:
            if offset + 8 > len(payload):
                raise ValueError("Truncated protobuf fixed64")
            value = struct.unpack("<d", payload[offset : offset + 8])[0]
            offset += 8
        elif wire == 2:
            length, offset = _read_varint(payload, offset)
            value = payload[offset : offset + length]
            offset += length
        elif wire == 5:
            if offset + 4 > len(payload):
                raise ValueError("Truncated protobuf fixed32")
            value = payload[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        yield field, wire, value


def _message(payload: bytes) -> Dict[int, List]:
    result: Dict[int, List] = {}
    for field, _wire, value in _fields(payload):
        result.setdefault(field, []).append(value)
    return result


def _number(value, default=0):
    if value is None:
        return default
    try:
        if isinstance(value, float) and math.isnan(value):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _double(message: Dict[int, List], field: int, default=0.0) -> float:
    values = message.get(field) or []
    return float(values[-1]) if values else default


def _int(message: Dict[int, List], field: int, default=0) -> int:
    values = message.get(field) or []
    return _number(values[-1], default) if values else default


def _nested(message: Dict[int, List], field: int) -> Optional[Dict[int, List]]:
    values = message.get(field) or []
    return _message(values[-1]) if values else None


def _ltpc(payload: bytes) -> Dict:
    message = _message(payload)
    return {
        "last_price": _double(message, 1),
        "last_trade_time": _int(message, 2),
        "last_quantity": _int(message, 3),
        "close": _double(message, 4),
    }


def _quote(payload: bytes) -> Dict:
    message = _message(payload)
    return {
        "bid_quantity": _int(message, 1),
        "bid_price": _double(message, 2),
        "ask_quantity": _int(message, 3),
        "ask_price": _double(message, 4),
    }


def _depth_from_market_level(payload: bytes) -> List[Dict]:
    depth: List[Dict] = []
    for quote_payload in _message(payload).get(1, []):
        quote = _quote(quote_payload)
        if quote["bid_price"] > 0:
            depth.append({"side": "buy", "quantity": quote["bid_quantity"], "price": quote["bid_price"]})
        if quote["ask_price"] > 0:
            depth.append({"side": "sell", "quantity": quote["ask_quantity"], "price": quote["ask_price"]})
    return depth


def _feed_tick(instrument_key: str, feed_payload: bytes, current_ts: int) -> Optional[Dict]:
    feed = _message(feed_payload)
    tick: Dict = {"instrument_key": instrument_key, "instrument_token": provider_token(instrument_key)}
    if feed.get(1):
        tick.update(_ltpc(feed[1][-1]))
    elif feed.get(2):
        full = _message(feed[2][-1])
        market_full = _nested(full, 1)
        index_full = _nested(full, 2)
        if market_full:
            if market_full.get(1):
                tick.update(_ltpc(market_full[1][-1]))
            if market_full.get(2):
                tick["depth"] = _depth_from_market_level(market_full[2][-1])
            tick["volume"] = _int(market_full, 6)
            tick["oi"] = _number(_double(market_full, 7), 0)
            tick["average_price"] = _double(market_full, 5)
            tick["implied_volatility"] = _double(market_full, 8)
        elif index_full:
            if index_full.get(1):
                tick.update(_ltpc(index_full[1][-1]))
        else:
            return None
    elif feed.get(3):
        first = _message(feed[3][-1])
        if first.get(1):
            tick.update(_ltpc(first[1][-1]))
        if first.get(2):
            quote = _quote(first[2][-1])
            tick["depth"] = [
                {"side": "buy", "quantity": quote["bid_quantity"], "price": quote["bid_price"]},
                {"side": "sell", "quantity": quote["ask_quantity"], "price": quote["ask_price"]},
            ]
        tick["volume"] = _int(first, 4)
        tick["oi"] = _number(_double(first, 5), 0)
        tick["implied_volatility"] = _double(first, 6)
    else:
        return None
    if tick.get("last_price", 0) <= 0:
        return None
    raw_time = tick.get("last_trade_time") or current_ts
    if raw_time and raw_time > 10_000_000_000:
        raw_time = raw_time / 1000
    tick["exchange_timestamp"] = raw_time or datetime.now(timezone.utc).timestamp()
    tick["received_at"] = datetime.now(timezone.utc).isoformat()
    return tick


def decode_feed_response(payload: bytes) -> List[Dict]:
    """Decode Upstox FeedResponse protobuf bytes into internal tick dictionaries."""
    response = _message(payload)
    current_ts = _int(response, 3, int(time.time() * 1000))
    ticks: List[Dict] = []
    # Upstox FeedResponse map entries have appeared in different protobuf
    # field positions across feed versions. Support both the originally
    # tested field 2 and the live v3 shape observed from the socket.
    feed_entries = list(response.get(2, [])) + list(response.get(4, []))
    for entry_payload in feed_entries:
        entry = _message(entry_payload)
        if not entry.get(1) or not entry.get(2):
            continue
        instrument_key = entry[1][-1].decode("utf-8")
        tick = _feed_tick(instrument_key, entry[2][-1], current_ts)
        if tick:
            ticks.append(tick)
    return ticks


def _tick_size(raw) -> Decimal:
    value = Decimal(str(raw or "0.05"))
    return value / Decimal("100") if value > 1 else value


def _expiry(raw) -> Optional[date]:
    if raw in (None, "", 0):
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, timezone.utc).date()
    return datetime.fromisoformat(str(raw)[:10]).date()


def _kind(row: Dict, segment: str = "") -> str:
    segment = str(segment or "").upper()
    if segment in {"NSE_INDEX", "BSE_INDEX"}:
        return "INDEX"
    value = str(row.get("instrument_type") or row.get("option_type") or "").upper()
    if value in {"CE", "PE"}:
        return value
    if value in {"FUT", "FUTSTK", "FUTIDX"}:
        return "FUT"
    if value.startswith("OPT"):
        return str(row.get("option_type") or "").upper()
    if segment in {"NSE_EQ", "BSE_EQ"}:
        return "EQ"
    return "INDEX"


def _exchange(segment: str) -> Optional[str]:
    return {"NSE_EQ": "NSE", "BSE_EQ": "BSE", "NSE_INDEX": "NSE", "BSE_INDEX": "BSE", "NSE_FO": "NFO", "BSE_FO": "BFO"}.get(segment)


def _symbol(row: Dict, kind: str) -> str:
    raw = str(row.get("trading_symbol") or row.get("short_name") or row.get("name") or "").upper().strip()
    compact = raw.replace(" ", "").replace("-", "").replace("_", "")
    if kind == "INDEX":
        aliases = {
            "NIFTY50": "NIFTY 50",
            "NIFTY": "NIFTY 50",
            "NIFTYBANK": "BANKNIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "SENSEX": "SENSEX",
            "BSESENSEX": "SENSEX",
            "INDIAVIX": "INDIA VIX",
            "VIX": "INDIA VIX",
        }
        return aliases.get(compact, raw)
    return raw


def _load_json_url(url: str) -> List[Dict]:
    response = requests.get(url, timeout=45, headers={"User-Agent": "NiveshAI/3.9 live-paper"})
    response.raise_for_status()
    data = response.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, list):
        raise ValueError("Upstox instrument file must be a JSON list")
    return parsed


def sync_upstox_instrument_master(database_url: str, instruments_url: str = UPSTOX_INSTRUMENTS_URL) -> Dict:
    rows = _load_json_url(instruments_url)
    accepted = []
    for row in rows:
        segment = str(row.get("segment") or row.get("exchange") or "").upper()
        if segment not in SUPPORTED_SEGMENTS:
            continue
        instrument_key = str(row.get("instrument_key") or "").strip()
        exchange = _exchange(segment)
        kind = _kind(row, segment)
        symbol = _symbol(row, kind)
        if not instrument_key or not exchange or not symbol or kind not in {"EQ", "INDEX", "FUT", "CE", "PE"}:
            continue
        expiry = _expiry(row.get("expiry"))
        if kind in {"FUT", "CE", "PE"} and expiry is None:
            continue
        strike = Decimal(str(row.get("strike_price") or row.get("strike") or 0)) if kind in {"CE", "PE"} else None
        underlying = str(row.get("underlying_symbol") or row.get("short_name") or symbol).upper().strip()
        accepted.append({
            "exchange": exchange,
            "symbol": symbol,
            "underlying": underlying,
            "isin": row.get("isin"),
            "kind": kind,
            "expiry": expiry,
            "strike": strike,
            "lot": int(row.get("lot_size") or row.get("minimum_lot") or 1),
            "tick": _tick_size(row.get("tick_size")),
            "fno": exchange in {"NFO", "BFO"},
            "provider_key": instrument_key,
            "provider_token": provider_token(instrument_key),
            "segment": segment,
            "mode_hint": "full" if kind in {"INDEX", "FUT", "CE", "PE"} else "ltpc",
        })
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    with engine.begin() as connection:
        for row in accepted:
            instrument_id = connection.execute(text("""
              INSERT INTO instrument_master(exchange,symbol,underlying_symbol,isin,instrument_type,expiry,strike,lot_size,tick_size,is_fno_eligible,is_active)
              VALUES(:exchange,:symbol,:underlying,:isin,:kind,:expiry,:strike,:lot,:tick,:fno,TRUE)
              ON CONFLICT(exchange,symbol) DO UPDATE SET underlying_symbol=EXCLUDED.underlying_symbol,isin=COALESCE(EXCLUDED.isin,instrument_master.isin),
              instrument_type=EXCLUDED.instrument_type,expiry=EXCLUDED.expiry,strike=EXCLUDED.strike,lot_size=EXCLUDED.lot_size,
              tick_size=EXCLUDED.tick_size,is_fno_eligible=EXCLUDED.is_fno_eligible,is_active=TRUE,system_recorded_at=CURRENT_TIMESTAMP
              RETURNING id
            """), row).scalar_one()
            connection.execute(text("""
              INSERT INTO instrument_provider_keys(instrument_id,provider,provider_key,provider_token,segment,mode_hint,is_active,last_synced_at)
              VALUES(:instrument_id,:provider,:provider_key,:provider_token,:segment,:mode_hint,TRUE,CURRENT_TIMESTAMP)
              ON CONFLICT(provider,provider_key) DO UPDATE SET instrument_id=EXCLUDED.instrument_id,provider_token=EXCLUDED.provider_token,
              segment=EXCLUDED.segment,mode_hint=EXCLUDED.mode_hint,is_active=TRUE,last_synced_at=CURRENT_TIMESTAMP
            """), {**row, "instrument_id": instrument_id, "provider": UPSTOX_PROVIDER})
        connection.execute(text("""UPDATE instrument_master equity SET is_fno_eligible=EXISTS(
            SELECT 1 FROM instrument_master derivative WHERE derivative.exchange IN ('NFO','BFO')
            AND derivative.instrument_type IN ('FUT','CE','PE') AND derivative.is_active
            AND derivative.underlying_symbol=equity.symbol)
            WHERE equity.exchange IN ('NSE','BSE') AND equity.instrument_type='EQ'"""))
    return {"received": len(rows), "accepted": len(accepted), "source": UPSTOX_PROVIDER, "updated_at": datetime.now(timezone.utc).isoformat()}


class UpstoxStream:
    def __init__(self, access_token: str, on_ticks: Callable[[List[Dict]], None],
                 on_status: Optional[Callable[[str, Dict], None]] = None, authorize_url: str = UPSTOX_AUTHORIZE_URL):
        self.access_token = access_token
        self.on_ticks = on_ticks
        self.on_status = on_status
        self.authorize_url = authorize_url
        self.mode_keys = {"ltpc": [], "full": [], "option_greeks": [], "full_d30": []}
        self.instrument_keys: List[str] = []
        self.ws = None
        self._stop = False
        self.reconnect_attempts = 0
        self.last_tick_at = None
        self.last_heartbeat_at = None
        self.message_count = 0
        self.decoded_tick_count = 0
        self.decode_error_count = 0
        self._last_message_status_at = 0.0

    @property
    def configured(self):
        return bool(self.access_token)

    def subscribe(self, instrument_keys: Iterable[str], mode: str = "ltpc"):
        if mode == "quote":
            mode = "ltpc"
        if mode not in self.mode_keys:
            raise ValueError("Unsupported Upstox subscription mode")
        incoming = list(dict.fromkeys(str(key) for key in instrument_keys if key))
        existing = set(self.instrument_keys)
        self.instrument_keys = self.instrument_keys + [key for key in incoming if key not in existing]
        self.mode_keys[mode] = list(dict.fromkeys(self.mode_keys[mode] + incoming))
        if self.ws and incoming:
            self._send_subscriptions(self.ws, {mode: incoming})

    def authorized_url(self) -> str:
        response = requests.get(
            self.authorize_url,
            timeout=20,
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        url = response.json().get("data", {}).get("authorized_redirect_uri")
        if not url or not str(url).startswith("wss://"):
            raise RuntimeError("Upstox did not return a valid authorized WebSocket URL")
        return url

    def _send_subscriptions(self, ws, mode_keys: Dict[str, List[str]]):
        try:
            import websocket
            binary_opcode = websocket.ABNF.OPCODE_BINARY
        except Exception:
            binary_opcode = None
        for mode, keys in mode_keys.items():
            if not keys:
                continue
            payload = {"guid": str(uuid.uuid4()), "method": "sub", "data": {"mode": mode, "instrumentKeys": keys}}
            # Upstox Market Data Feed V3 expects subscription commands as a
            # binary WebSocket frame containing JSON bytes. If this is sent as
            # a text frame, the socket can connect and still never deliver
            # subscribed instrument ticks.
            encoded = json.dumps(payload).encode("utf-8")
            if binary_opcode is None:
                ws.send(encoded)
            else:
                ws.send(encoded, opcode=binary_opcode)

    def _run(self):
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("Install websocket-client to enable Upstox streaming") from exc
        if not self.configured:
            raise RuntimeError("Upstox access token is not configured")

        while not self._stop:
            try:
                url = self.authorized_url()
            except Exception as exc:
                if self.on_status:
                    self.on_status("ERROR", {"type": type(exc).__name__, "stage": "authorize"})
                time.sleep(min(60, 2 ** min(5, self.reconnect_attempts)))
                self.reconnect_attempts += 1
                continue

            def opened(ws):
                self.reconnect_attempts = 0
                self.ws = ws
                self._send_subscriptions(ws, self.mode_keys)
                if self.on_status:
                    self.on_status("CONNECTED", {"keys": len(self.instrument_keys)})

            def message(_ws, payload):
                self.last_heartbeat_at = time.time()
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                self.message_count += 1
                try:
                    ticks = decode_feed_response(payload)
                except Exception as exc:
                    self.decode_error_count += 1
                    if self.on_status and (self.decode_error_count <= 3 or time.time() - self._last_message_status_at > 30):
                        self._last_message_status_at = time.time()
                        self.on_status("ERROR", {
                            "type": type(exc).__name__,
                            "stage": "decode",
                            "messages": self.message_count,
                            "bytes": len(payload),
                        })
                    return
                self.decoded_tick_count += len(ticks)
                if self.on_status and (ticks or self.message_count <= 3 or time.time() - self._last_message_status_at > 30):
                    self._last_message_status_at = time.time()
                    top_fields = []
                    if not ticks:
                        try:
                            top_fields = sorted(_message(payload).keys())
                        except Exception:
                            top_fields = []
                    self.on_status("MESSAGE", {
                        "messages": self.message_count,
                        "ticks": len(ticks),
                        "total_ticks": self.decoded_tick_count,
                        "bytes": len(payload),
                        "top_fields": top_fields,
                    })
                if ticks:
                    self.last_tick_at = self.last_heartbeat_at
                    self.on_ticks(ticks)

            def closed(_ws, *args):
                self.ws = None
                if self.on_status:
                    self.on_status("DISCONNECTED", {"details": [str(x)[:200] for x in args]})

            def errored(_ws, error):
                if self.on_status:
                    self.on_status("ERROR", {"type": type(error).__name__})

            app = websocket.WebSocketApp(url, on_open=opened, on_message=message, on_close=closed, on_error=errored)
            app.run_forever(ping_interval=30, ping_timeout=10)
            self.reconnect_attempts += 1
            if not self._stop:
                time.sleep(min(60, 2 ** min(5, self.reconnect_attempts)))

    def start(self):
        thread = threading.Thread(target=self._run, name="upstox-market-stream", daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop = True
        if self.ws:
            self.ws.close()
