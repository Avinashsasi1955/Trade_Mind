"""Production ASGI transport for the existing hardened HTTP application.

The local developer preview may use ``backend.server``. Public deployments must
run this module with Uvicorn behind the AWS load balancer/WAF.
"""
import asyncio
from email.message import Message
from email.parser import BytesParser
from io import BytesIO
from threading import Lock
from urllib.parse import quote_from_bytes

from .config import validate_runtime_security
from .database import initialize
from .server import Handler


MAX_BODY_BYTES = 1_000_000


class NiveshASGI:
    def __init__(self):
        self._initialized = False
        self._lock = Lock()
        try:
            self._startup()
        except Exception as exc:
            print(f"[nivesh] ASGI startup error: {exc}")

    def _startup(self):
        with self._lock:
            if self._initialized:
                return
            validate_runtime_security()
            initialize()
            try:
                from .sse_broadcaster import start_redis_listener, start_metrics_ticker, wire_bus_to_sse
                from .brains import get_bus
                start_redis_listener()
                start_metrics_ticker()
                wire_bus_to_sse(get_bus())
                print("[nivesh] ASGI SSE Broadcaster & Metrics Ticker initialized")
            except Exception as sse_err:
                print(f"[nivesh] SSE startup notice: {sse_err}")
            self._initialized = True

    async def _handle_sse(self, scope, receive, send):
        import queue
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from .security import decode_token
        from .sse_broadcaster import register_client, unregister_client, start_redis_listener, start_metrics_ticker
        import json
        IST = ZoneInfo("Asia/Kolkata")

        try:
            start_redis_listener()
            start_metrics_ticker()
        except Exception:
            pass

        token = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                v = value.decode("latin-1")
                if v.startswith("Bearer "):
                    token = v[7:]
            elif name.lower() == b"cookie":
                for part in value.decode("latin-1").split(";"):
                    p = part.strip()
                    if p.startswith("nivesh_session=") or p.startswith("__Host-nivesh_session="):
                        token = p.split("=", 1)[1].strip()

        user_id = None
        if token:
            try:
                claims = decode_token(token)
                user_id = int(claims["sub"])
            except Exception:
                user_id = None

        if not user_id:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"Authentication required for real-time streaming"}',
            })
            return

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache, no-transform"),
                (b"connection", b"keep-alive"),
                (b"x-accel-buffering", b"no"),
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-credentials", b"true"),
            ],
        })

        client_q = queue.Queue(maxsize=100)
        register_client(client_q)

        handshake = json.dumps({
            "status": "connected",
            "user_id": user_id,
            "transport": "Server-Sent Events (SSE / ASGI)",
            "server_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        })
        await send({
            "type": "http.response.body",
            "body": f"event: connected\ndata: {handshake}\n\n".encode("utf-8"),
            "more_body": True,
        })

        disconnect_event = asyncio.Event()

        async def _disconnect_listener():
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    disconnect_event.set()
                    break

        disc_task = asyncio.create_task(_disconnect_listener())

        try:
            while not disconnect_event.is_set():
                try:
                    msg = await asyncio.to_thread(client_q.get, timeout=3.0)
                    event_name = msg.get("event", "message")
                    data_str = json.dumps(msg.get("data", {}), default=str)
                    frame = f"event: {event_name}\ndata: {data_str}\n\n".encode("utf-8")
                    await send({"type": "http.response.body", "body": frame, "more_body": True})
                except queue.Empty:
                    if not disconnect_event.is_set():
                        await send({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
        except Exception:
            pass
        finally:
            unregister_client(client_q)
            disc_task.cancel()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        await asyncio.to_thread(self._startup)
                        await send({"type": "lifespan.startup.complete"})
                    except Exception as exc:
                        await send({"type": "lifespan.startup.failed", "message": str(exc)})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        if path == "/api/stream/events" and scope.get("method", "GET").upper() == "GET":
            await self._handle_sse(scope, receive, send)
            return

        body = bytearray()
        more = True
        while more:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            if event["type"] != "http.request":
                continue
            body.extend(event.get("body", b""))
            more = event.get("more_body", False)
            if len(body) > MAX_BODY_BYTES:
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b'{"error":"Request too large"}'})
                return

        status, headers, response = await asyncio.to_thread(self._dispatch, scope, bytes(body))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": response})

    @staticmethod
    def _dispatch(scope, body):
        handler = object.__new__(Handler)
        query = scope.get("query_string", b"")
        path = quote_from_bytes(scope.get("raw_path") or scope.get("path", "/").encode())
        handler.path = path + (("?" + query.decode("latin-1")) if query else "")
        handler.command = scope["method"].upper()
        handler.request_version = "HTTP/1.1"
        handler.protocol_version = "HTTP/1.1"
        handler.requestline = f"{handler.command} {path} HTTP/1.1"
        handler.close_connection = True
        client = scope.get("client") or ("unknown", 0)
        handler.client_address = (str(client[0]), int(client[1]))
        message = Message()
        for name, value in scope.get("headers", []):
            message.add_header(name.decode("latin-1"), value.decode("latin-1"))
        if "Content-Length" not in message:
            message["Content-Length"] = str(len(body))
        handler.headers = message
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler._headers_buffer = []
        handler._handle(handler.command)

        raw = handler.wfile.getvalue()
        head, separator, payload = raw.partition(b"\r\n\r\n")
        if not separator:
            raise RuntimeError("Invalid internal HTTP response")
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ", 2)[1])
        parsed = BytesParser().parsebytes(b"\r\n".join(lines[1:]) + b"\r\n\r\n")
        headers = []
        for name, value in parsed.raw_items():
            if name.lower() == "server":
                continue
            headers.append((name.lower().encode("ascii"), value.encode("latin-1")))
        return status, headers, payload


app = NiveshASGI()
