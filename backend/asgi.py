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

    def _startup(self):
        with self._lock:
            if self._initialized:
                return
            validate_runtime_security()
            initialize()
            self._initialized = True

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
