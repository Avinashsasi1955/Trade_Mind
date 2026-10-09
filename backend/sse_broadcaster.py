"""Server-Sent Events (SSE) Push Transporter for Nivesh AI.

Enables real-time event streaming between backend multi-agent brains and the frontend UI,
replacing aggressive polling with sub-second push notifications for:
- trade_opened: Instant order execution alert & open position insertion
- trade_closed: Instant trade exit, P&L update, and history table move
- agent_thought: Live reasoning stream ticker updates from Deep Thinker
- position_alert: Risk limit, stop breach, and target hit alerts
- metrics_tick: 4s lightweight portfolio P&L and counterfactual status beacon
- counterfactual_update: Post-market replay score updates

Cross-process synchronization is powered by Redis Pub/Sub channel 'nivesh:sse:events'
with in-process queue fallback.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set
from zoneinfo import ZoneInfo

logger = logging.getLogger("nivesh.sse")
IST = ZoneInfo("Asia/Kolkata")

REDIS_CHANNEL = "nivesh:sse:events"

_clients_lock = threading.Lock()
_connected_clients: Set[queue.Queue] = set()

_redis_listener_started = False
_redis_listener_lock = threading.Lock()

_ticker_started = False
_ticker_lock = threading.Lock()


def get_connected_clients_count() -> int:
    with _clients_lock:
        return len(_connected_clients)


def register_client(client_queue: queue.Queue) -> None:
    with _clients_lock:
        _connected_clients.add(client_queue)
    logger.debug("SSE client registered. Total active: %d", len(_connected_clients))


def unregister_client(client_queue: queue.Queue) -> None:
    with _clients_lock:
        _connected_clients.discard(client_queue)
    logger.debug("SSE client unregistered. Remaining active: %d", len(_connected_clients))


def _broadcast_local(event_type: str, data: Dict[str, Any]) -> None:
    """Deliver an event to all local subscriber queues."""
    msg = {
        "event": event_type,
        "data": data,
        "timestamp": datetime.now(IST).isoformat(),
    }
    with _clients_lock:
        stale_clients = []
        for q in _connected_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                stale_clients.append(q)
            except Exception:
                stale_clients.append(q)
        for sq in stale_clients:
            _connected_clients.discard(sq)


def publish_sse_event(event_type: str, data: Dict[str, Any], redis_client: Optional[Any] = None) -> None:
    """Publish an event to all SSE streams across containers via Redis Pub/Sub."""
    payload = json.dumps({
        "event": event_type,
        "data": data,
        "timestamp": datetime.now(IST).isoformat(),
    }, default=str)

    r = redis_client
    if r is None:
        try:
            import redis
            redis_url = os.getenv("REDIS_URL", "")
            if redis_url:
                r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
        except Exception:
            r = None

    if r is not None:
        try:
            r.publish(REDIS_CHANNEL, payload)
        except Exception as exc:
            logger.debug("Redis publish notice: %s", exc)
            _broadcast_local(event_type, data)
    else:
        _broadcast_local(event_type, data)


def start_redis_listener(redis_url: Optional[str] = None) -> None:
    """Start background daemon thread listening for Redis Pub/Sub events."""
    global _redis_listener_started
    if _redis_listener_started:
        return

    with _redis_listener_lock:
        if _redis_listener_started:
            return

        url = redis_url or os.getenv("REDIS_URL", "")
        if not url:
            logger.info("No REDIS_URL configured; SSE running in single-process mode")
            _redis_listener_started = True
            return

        def _listener_loop():
            import redis
            while True:
                try:
                    r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=None, health_check_interval=30)
                    pubsub = r.pubsub()
                    pubsub.subscribe(REDIS_CHANNEL)
                    print(f"[nivesh] Subscribed to Redis SSE channel '{REDIS_CHANNEL}'", flush=True)
                    for message in pubsub.listen():
                        if message and message.get("type") == "message":
                            try:
                                parsed = json.loads(message["data"])
                                _broadcast_local(parsed.get("event", "message"), parsed.get("data", {}))
                            except Exception as parse_err:
                                print(f"[nivesh] Error parsing SSE Redis message: {parse_err}", flush=True)
                except Exception as loop_err:
                    print(f"[nivesh] Redis SSE listener reconnecting: {loop_err}", flush=True)
                    time.sleep(3.0)

        t = threading.Thread(target=_listener_loop, name="nivesh_sse_redis_listener", daemon=True)
        t.start()
        _redis_listener_started = True


def start_metrics_ticker(engine=None, redis_client=None) -> None:
    """Start background daemon thread broadcasting 4s metrics_tick to SSE clients."""
    global _ticker_started
    if _ticker_started:
        return

    with _ticker_lock:
        if _ticker_started:
            return

        def _ticker_loop():
            from sqlalchemy import text
            last_known_today = None
            while True:
                time.sleep(4.0)
                if get_connected_clients_count() == 0:
                    continue
                try:
                    eng = engine
                    if eng is None:
                        from backend.service import _get_engine
                        eng = _get_engine()
                    if not eng:
                        continue

                    with eng.connect() as conn:
                        row = conn.execute(text("""
                            SELECT 
                                COUNT(*) FILTER (WHERE realised_exit_price IS NULL) AS open_count,
                                COUNT(*) FILTER (WHERE realised_exit_price IS NOT NULL) AS closed_count,
                                COALESCE(SUM(net_pnl) FILTER (WHERE realised_exit_price IS NOT NULL), 0.0) AS realised_pnl
                            FROM shadow_execution_audits
                            WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                        """)).mappings().one()

                        # Fetch latest thought if present
                        thought_row = conn.execute(text("""
                            SELECT payload FROM brain_event_journal
                            WHERE event_type IN ('DeepThinkerDeliberation', 'CandidateRejected', 'ShadowTradeOpened')
                            ORDER BY occurred_at DESC LIMIT 1
                        """)).mappings().one_or_none()

                        thought_text = "Multi-agent Sentinel scanning live orderbook corridors."
                        if thought_row and thought_row.get("payload"):
                            p = thought_row["payload"]
                            if isinstance(p, dict):
                                thought_text = p.get("summary") or p.get("reason") or p.get("thought") or thought_text

                    r_client = redis_client
                    if not r_client:
                        try:
                            from backend.service import _get_redis
                            r_client = _get_redis()
                        except Exception:
                            pass

                    cached_today = None
                    today_str = datetime.now(IST).date().isoformat()
                    if r_client:
                        try:
                            raw = (
                                r_client.get("nivesh:cache:shadow_trade_book:100")
                                or r_client.get("nivesh:cache:shadow_trade_book:200")
                            )
                            if raw:
                                book = json.loads(raw)
                                if book.get("market_date") == today_str:
                                    cached_today = book.get("today")
                        except Exception:
                            pass

                    now_mono = time.monotonic()
                    if cached_today and isinstance(cached_today, dict):
                        last_known_today = (today_str, cached_today, now_mono)
                    elif last_known_today and last_known_today[0] == today_str:
                        # Expire cache fallback if older than 60s TTL
                        if (now_mono - last_known_today[2]) <= 60.0:
                            cached_today = last_known_today[1]
                        else:
                            last_known_today = None

                    open_count = int(row["open_count"] or 0)
                    closed_count = int(row["closed_count"] or 0)
                    realised_pnl = float(row["realised_pnl"] or 0.0)
                    unrealised_pnl = float(cached_today.get("unrealised_pnl") or 0.0) if (open_count > 0 and cached_today and isinstance(cached_today, dict)) else 0.0
                    net_marked_pnl = realised_pnl + unrealised_pnl

                    tick_data = {
                        "open_trades": open_count,
                        "closed_trades": closed_count,
                        "realised_pnl": round(realised_pnl, 2),
                        "unrealised_pnl": round(unrealised_pnl, 2),
                        "net_marked_pnl": round(net_marked_pnl, 2),
                        "total_pnl": round(net_marked_pnl, 2),
                        "latest_thought": str(thought_text)[:140],
                        "active_connections": get_connected_clients_count(),
                        "timestamp": datetime.now(IST).strftime("%H:%M:%S IST"),
                    }
                    _broadcast_local("metrics_tick", tick_data)
                except Exception as exc:
                    logger.debug("Metrics ticker error: %s", exc)

        t = threading.Thread(target=_ticker_loop, name="nivesh_sse_metrics_ticker", daemon=True)
        t.start()
        _ticker_started = True


def wire_bus_to_sse(bus: Any) -> None:
    """Wire in-process MessageBus events directly to SSE broadcaster."""
    try:
        from backend.brains.bus import (
            CandidateRejected,
            DailyReportReady,
            PositionAlert,
            ShadowTradeClosed,
            ShadowTradeOpened,
        )

        def _on_opened(event: ShadowTradeOpened):
            payload = event.payload or {}
            publish_sse_event("trade_opened", {
                "audit_id": event.audit_id,
                "symbol": event.symbol or payload.get("symbol"),
                "side": event.side or payload.get("side"),
                "quantity": event.quantity or payload.get("quantity"),
                "trade_mode": payload.get("trade_mode") or "INTRADAY",
                "entry_price": payload.get("entry_price") or payload.get("decision_price"),
                "stop_loss": payload.get("stop_loss_price"),
                "target": payload.get("target_price"),
                "strategy": payload.get("chart_strategy") or event.route,
                "kelly_factor": payload.get("kelly_factor", 1.0),
            })

        def _on_closed(event: ShadowTradeClosed):
            payload = event.payload or {}
            publish_sse_event("trade_closed", {
                "audit_id": event.audit_id,
                "symbol": event.symbol or payload.get("symbol"),
                "net_pnl": event.net_pnl,
                "pnl_pct": payload.get("pnl_pct", 0.0),
                "exit_reason": event.exit_reason or payload.get("exit_reason"),
                "trade_mode": payload.get("trade_mode") or "INTRADAY",
            })

        def _on_alert(event: PositionAlert):
            publish_sse_event("position_alert", {
                "symbol": event.symbol,
                "alert_type": event.alert_type,
                "current_price": event.current_price,
                "entry_price": event.entry_price,
                "pnl": event.pnl,
            })

        def _on_rejected(event: CandidateRejected):
            publish_sse_event("candidate_rejected", {
                "symbol": event.symbol,
                "reason": event.reason,
                "strategy": event.strategy,
                "probability": event.probability,
            })

        def _on_report(event: DailyReportReady):
            publish_sse_event("daily_report", event.report)

        bus.subscribe(ShadowTradeOpened, _on_opened)
        bus.subscribe(ShadowTradeClosed, _on_closed)
        bus.subscribe(PositionAlert, _on_alert)
        bus.subscribe(CandidateRejected, _on_rejected)
        bus.subscribe(DailyReportReady, _on_report)
        logger.info("MessageBus wired to SSE broadcaster successfully")
    except Exception as exc:
        logger.warning("Failed to wire MessageBus to SSE: %s", exc)


def handle_sse_connection(request_handler: Any, user_id: int) -> None:
    """Handle an incoming HTTP GET /api/stream/events request as an active SSE stream."""
    request_handler.send_response(200)
    request_handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    request_handler.send_header("Cache-Control", "no-cache, no-transform")
    request_handler.send_header("Connection", "keep-alive")
    origin = request_handler.headers.get("Origin") if hasattr(request_handler, "headers") and request_handler.headers else None
    if origin:
        request_handler.send_header("Access-Control-Allow-Origin", origin)
        request_handler.send_header("Access-Control-Allow-Credentials", "true")
        request_handler.send_header("Vary", "Origin")
    request_handler._security_headers()
    request_handler.end_headers()

    client_q: queue.Queue = queue.Queue(maxsize=100)
    register_client(client_q)

    # Send initial connection handshake
    handshake = json.dumps({
        "status": "connected",
        "user_id": user_id,
        "server_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "transport": "Server-Sent Events (SSE)",
        "active_clients": get_connected_clients_count(),
    })
    try:
        request_handler.wfile.write(f"event: connected\ndata: {handshake}\n\n".encode("utf-8"))
        request_handler.wfile.flush()

        while True:
            try:
                msg = client_q.get(timeout=8.0)
                event_name = msg.get("event", "message")
                data_str = json.dumps(msg.get("data", {}), default=str)
                frame = f"event: {event_name}\ndata: {data_str}\n\n".encode("utf-8")
                request_handler.wfile.write(frame)
                request_handler.wfile.flush()
            except queue.Empty:
                # Send keep-alive heartbeat ping frame
                request_handler.wfile.write(b": ping\n\n")
                request_handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        logger.debug("SSE client disconnected: user %d", user_id)
    finally:
        unregister_client(client_q)
