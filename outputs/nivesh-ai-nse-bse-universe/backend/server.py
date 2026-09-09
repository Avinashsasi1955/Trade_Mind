import json
import mimetypes
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import HOST, PORT, ROOT
from .database import connect, create_user, find_user_by_email, initialize
from .security import create_token, decode_token, verify_password
from .service import ai_gateway_status, backtest_report, dashboard, glossary, latest_analysis, listed_securities, market_update, model_status, reset_portfolio, run_agent, sentiment_dashboard, stock_analysis, stock_chart, update_settings
from .scheduler import start_scheduler


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "NiveshAI/1.0"

    def log_message(self, fmt, *args):
        print(f"[nivesh] {self.address_string()} {fmt % args}")

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise APIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid JSON")

    def _user_id(self) -> int:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise APIError(HTTPStatus.UNAUTHORIZED, "Authentication required")
        try:
            return int(decode_token(auth[7:])["sub"])
        except (ValueError, KeyError):
            raise APIError(HTTPStatus.UNAUTHORIZED, "Invalid or expired session")

    def _route(self, method: str):
        path = urlparse(self.path).path
        if path == "/api/health" and method == "GET":
            return self._json(200, {"status": "ok", "mode": "paper", "broker": "not_connected"})
        if path == "/api/auth/login" and method == "POST":
            data = self._body()
            with connect() as db:
                user = find_user_by_email(db, data.get("email", ""))
                if not user or not verify_password(data.get("password", ""), user["password_hash"]):
                    raise APIError(HTTPStatus.UNAUTHORIZED, "Incorrect email or password")
                return self._json(200, {"token": create_token(user["id"], user["email"]), "user": {"id": user["id"], "name": user["name"], "email": user["email"]}})
        if path == "/api/auth/signup" and method == "POST":
            data = self._body()
            if len(data.get("password", "")) < 8 or "@" not in data.get("email", "") or not data.get("name", "").strip():
                raise APIError(HTTPStatus.BAD_REQUEST, "Name, valid email and an 8-character password are required")
            try:
                with connect() as db:
                    user_id = create_user(db, data["name"], data["email"], data["password"])
                return self._json(201, {"token": create_token(user_id, data["email"]), "user": {"id": user_id, "name": data["name"], "email": data["email"]}})
            except sqlite3.IntegrityError:
                raise APIError(HTTPStatus.CONFLICT, "An account with this email already exists")
        if path == "/api/dashboard" and method == "GET":
            with connect() as db:
                return self._json(200, dashboard(db, self._user_id()))
        if path == "/api/analysis" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["RELIANCE"])[0]
            with connect() as db:
                return self._json(200, latest_analysis(db, self._user_id(), symbol))
        if path == "/api/analysis/run" and method == "POST":
            data = self._body()
            with connect() as db:
                return self._json(200, stock_analysis(db, self._user_id(), data.get("symbol", "RELIANCE")))
        if path == "/api/glossary" and method == "GET":
            self._user_id()
            return self._json(200, glossary())
        if path == "/api/models" and method == "GET":
            self._user_id()
            return self._json(200, model_status())
        if path == "/api/ai/gateway/status" and method == "GET":
            with connect() as db:
                return self._json(200, ai_gateway_status(db, self._user_id()))
        if path == "/api/sentiment" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["RELIANCE"])[0]
            with connect() as db:
                return self._json(200, sentiment_dashboard(db, self._user_id(), symbol))
        if path == "/api/market/update" and method == "GET":
            with connect() as db:
                return self._json(200, market_update(db, self._user_id(), int(time.time() // 60)))
        if path == "/api/chart" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            return self._json(200, stock_chart(query.get("symbol", ["RELIANCE"])[0], query.get("timeframe", ["5m"])[0]))
        if path == "/api/securities" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            return self._json(200, listed_securities(query.get("query", [""])[0], query.get("exchange", ["ALL"])[0], int(query.get("limit", [50])[0]), int(query.get("offset", [0])[0])))
        if path == "/api/backtest" and method == "POST":
            self._user_id()
            data = self._body()
            try:
                capital = min(100_000_000, max(10_000, float(data.get("capital", 1_000_000))))
                return self._json(200, backtest_report(data.get("symbol", "RELIANCE"), data.get("strategy", "ensemble"), capital, data.get("start", ""), data.get("end", "")))
            except ValueError as exc:
                raise APIError(HTTPStatus.BAD_REQUEST, str(exc))
        if path == "/api/agent/run" and method == "POST":
            with connect() as db:
                return self._json(200, run_agent(db, self._user_id()))
        if path == "/api/settings" and method == "PUT":
            with connect() as db:
                return self._json(200, update_settings(db, self._user_id(), self._body().get("risk_profile", "")))
        if path == "/api/portfolio/reset" and method == "POST":
            with connect() as db:
                return self._json(200, reset_portfolio(db, self._user_id()))
        if path.startswith("/api/"):
            raise APIError(HTTPStatus.NOT_FOUND, "API endpoint not found")
        return self._static(path)

    def _static(self, path: str):
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (ROOT / relative).resolve()
        if ROOT not in target.parents and target != ROOT:
            raise APIError(HTTPStatus.FORBIDDEN, "Forbidden")
        if not target.is_file() or target.parts[-2:-1] in {("backend",), ("data",)}:
            raise APIError(HTTPStatus.NOT_FOUND, "File not found")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def _handle(self, method: str):
        try:
            self._route(method)
        except APIError as exc:
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:
            print(f"[nivesh] internal error: {exc}")
            self._json(500, {"error": "Internal server error"})


def main():
    initialize()
    start_scheduler()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Nivesh AI running at http://{HOST}:{PORT}")
    print("Demo login: arjun@example.com / nivesh123")
    server.serve_forever()


if __name__ == "__main__":
    main()
