import json
import mimetypes
import sqlite3
import time
import threading
import secrets
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import (ADMIN_EMAILS, ALLOWED_ORIGINS, COOKIE_SECURE, DEMO_MODE, HOST,
                     COGNITO_AUTH_REQUIRED, COGNITO_LOGOUT_URL, IS_PRODUCTION, PORT, ROOT, SIGNUP_ENABLED, TOKEN_TTL_SECONDS,
                     validate_runtime_security)
from .database import connect, create_user, find_user_by_email, initialize
from .security import create_token, decode_token, hash_password, password_needs_rehash, token_hash, verify_password
from .service import ai_gateway_status, apply_coach_proposal_service, approve_order_intent, backtest_report, bot_chat, bot_conversation, bot_conversations, brain_memory_status, broker_disconnect, broker_login, create_order_intent, daily_executive_journal, dashboard, derivative_plan, derivative_plans, derivatives_greeks, derivatives_spreads, dismiss_coach_proposal_service, execution_status, exit_shadow_trade, get_coach_audit, get_coach_proposals_service, glossary, ingest_finnhub_webhook, latest_analysis, listed_securities, market_history_status, market_update, ml_alpha_fragility, ml_drift, ml_quant_models, ml_shadow, ml_status, ml_train, ml_validate, model_review_status, model_status, notifications_status_service, notifications_test_service, operations_copilot_status, pre_market_health_status, production_status, reconcile_orders, repair_today_missing_bars, reset_portfolio, rl_controller_status, run_agent, sentiment_dashboard, shadow_counterfactual_summary_service, shadow_session_status_api, shadow_trade_book, spider_bot_status, stock_analysis, stock_chart, submit_order_intent, trade_funnel_diagnostic, trigger_counterfactual_replay_service, update_kill_switch, update_risk_policy, update_settings, update_shadow_trade_risk
from .broker_gateway import complete_login
from .scheduler import start_scheduler
from .asi_indicator import calculate_asi, detect_nse_trap
from .volume_profile import calculate_volume_profile, evaluate_volume_profile_verdict
from .pcr_engine import get_latest_pcr


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


_rate_buckets={}
_rate_lock=threading.Lock()
SESSION_COOKIE = "__Host-nivesh_session" if COOKIE_SECURE else "nivesh_session"
CSRF_COOKIE = "__Host-nivesh_csrf" if COOKIE_SECURE else "nivesh_csrf"
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_MINUTES = 15


class Handler(BaseHTTPRequestHandler):
    server_version = "NiveshAI"
    sys_version = ""

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "-"
        print(f'[nivesh] {self.client_address[0]} "{getattr(self,"command","-")} {urlparse(self.path).path}" {status}')

    def _json(self, status: int, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name,value in (headers or {}).items():
            for item in value if isinstance(value, (list, tuple)) else [value]:
                self.send_header(name,item)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
        if length < 0:
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
        if length > 1_000_000:
            raise APIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise APIError(HTTPStatus.BAD_REQUEST, "Invalid JSON")

    def _user_id(self) -> int:
        if IS_PRODUCTION and COGNITO_AUTH_REQUIRED:
            return self._cognito_user_id()
        auth = self.headers.get("Authorization", "")
        token=auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            token=self._cookies().get(SESSION_COOKIE,"")
        if not token:
            raise APIError(HTTPStatus.UNAUTHORIZED, "Authentication required")
        try:
            claims=decode_token(token)
            with connect() as db:
                if db.execute("SELECT 1 FROM revoked_tokens WHERE token_hash=?",(token_hash(token),)).fetchone(): raise ValueError("Token revoked")
            return int(claims["sub"])
        except (ValueError, KeyError):
            raise APIError(HTTPStatus.UNAUTHORIZED, "Invalid or expired session")

    def _cognito_user_id(self) -> int:
        from .alb_identity import verify_alb_claims
        try: claims=verify_alb_claims(self.headers.get("X-Amzn-Oidc-Data", ""))
        except ValueError: raise APIError(HTTPStatus.UNAUTHORIZED,"Managed authentication required") from None
        subject=str(claims["sub"]); email=str(claims["email"]).strip().lower()[:254]
        name=str(claims.get("name") or email.split("@",1)[0])[:120]
        with connect() as db:
            user=db.execute("SELECT * FROM users WHERE cognito_sub=?",(subject,)).fetchone()
            if not user:
                user=db.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
                if user:
                    db.execute("UPDATE users SET cognito_sub=?,email_verified=TRUE WHERE id=?",(subject,user["id"])); db.commit()
                else:
                    user_id=create_user(db,name,email,secrets.token_urlsafe(48))
                    db.execute("UPDATE users SET cognito_sub=?,email_verified=TRUE WHERE id=?",(subject,user_id)); db.commit()
                    user=db.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
            if bool(user["disabled"]): raise APIError(HTTPStatus.FORBIDDEN,"Account disabled")
            return int(user["id"])

    def _cookies(self):
        return {part.split("=",1)[0].strip():part.split("=",1)[1].strip()
                for part in self.headers.get("Cookie","").split(";") if "=" in part}

    def _require_admin(self) -> int:
        user_id = self._user_id()
        if not ADMIN_EMAILS and not IS_PRODUCTION:
            return user_id
        with connect() as db:
            row = db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or row["email"].lower() not in ADMIN_EMAILS:
            raise APIError(HTTPStatus.FORBIDDEN, "Administrator access required")
        return user_id

    @staticmethod
    def _login_is_blocked(db, email: str) -> bool:
        cutoff=(datetime.now(timezone.utc)-timedelta(minutes=LOGIN_FAILURE_WINDOW_MINUTES)).isoformat()
        row=db.execute("SELECT COUNT(*) count FROM auth_login_attempts WHERE email=? AND succeeded=0 AND created_at>=?",(email,cutoff)).fetchone()
        return int(row["count"] if row else 0) >= LOGIN_FAILURE_LIMIT

    @staticmethod
    def _record_login(db, email: str, succeeded: bool) -> None:
        now=datetime.now(timezone.utc).isoformat()
        db.execute("DELETE FROM auth_login_attempts WHERE created_at<?",((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
        if succeeded:
            db.execute("DELETE FROM auth_login_attempts WHERE email=? AND succeeded=0",(email,))
        db.execute("INSERT INTO auth_login_attempts(email,succeeded,created_at) VALUES(?,?,?)",(email,int(succeeded),now))
        db.commit()

    def _enforce_request_integrity(self, method: str):
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin:
            allowed = set(ALLOWED_ORIGINS)
            if not IS_PRODUCTION:
                allowed.update({f"http://{HOST}:{PORT}", f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"})
            if origin not in allowed:
                raise APIError(HTTPStatus.FORBIDDEN, "Untrusted request origin")
        elif IS_PRODUCTION:
            raise APIError(HTTPStatus.FORBIDDEN, "Origin header required")
        path = urlparse(self.path).path
        if path in {"/api/auth/login", "/api/auth/signup", "/api/news/finnhub/webhook"} or self.headers.get("Authorization", "").startswith("Bearer "):
            return
        cookies = self._cookies()
        supplied = self.headers.get("X-CSRF-Token", "")
        expected = cookies.get(CSRF_COOKIE, "")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            raise APIError(HTTPStatus.FORBIDDEN, "CSRF validation failed")

    def _security_headers(self):
        self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Permissions-Policy","camera=(), microphone=(), geolocation=(), payment=(), usb=()"); self.send_header("Cross-Origin-Opener-Policy","same-origin"); self.send_header("Cross-Origin-Resource-Policy","same-origin"); self.send_header("Content-Security-Policy","default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:")
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload")

    def _enforce_rate_limit(self,method:str):
        path=urlparse(self.path).path; window=300 if path.startswith("/api/auth/") else 60; limit=10 if path.startswith("/api/auth/") else 120; key=(self.client_address[0],path,method); now=time.time()
        with _rate_lock:
            if len(_rate_buckets) > 10_000:
                for stale in list(_rate_buckets)[:2_000]:
                    if not _rate_buckets[stale] or now-_rate_buckets[stale][-1] > 300:
                        _rate_buckets.pop(stale,None)
            values=[x for x in _rate_buckets.get(key,[]) if now-x<window]
            if len(values)>=limit: raise APIError(HTTPStatus.TOO_MANY_REQUESTS,"Rate limit exceeded")
            values.append(now); _rate_buckets[key]=values

    def _route(self, method: str):
        path = urlparse(self.path).path
        if path == "/api/health" and method == "GET":
            return self._json(200, {"status": "ok"})
        if path == "/api/auth/login" and method == "POST":
            if IS_PRODUCTION:
                raise APIError(HTTPStatus.NOT_FOUND,"Local login is not enabled")
            data = self._body()
            email=str(data.get("email", ""))[:254]; password=str(data.get("password", ""))[:256]
            with connect() as db:
                if self._login_is_blocked(db,email.strip().lower()):
                    raise APIError(HTTPStatus.TOO_MANY_REQUESTS,"Too many login attempts; try again later")
                user = find_user_by_email(db, email)
                password_valid=verify_password(password,user["password_hash"] if user else DUMMY_PASSWORD_HASH)
                if not user or not password_valid:
                    self._record_login(db,email.strip().lower(),False)
                    raise APIError(HTTPStatus.UNAUTHORIZED, "Incorrect email or password")
                self._record_login(db,email.strip().lower(),True)
                if password_needs_rehash(user["password_hash"]):
                    db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user["id"]))
                    db.commit()
                token=create_token(user["id"],user["email"]); csrf=secrets.token_urlsafe(32); secure="; Secure" if COOKIE_SECURE else ""
                cookies=[f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TOKEN_TTL_SECONDS}{secure}",f"{CSRF_COOKIE}={csrf}; SameSite=Strict; Path=/; Max-Age={TOKEN_TTL_SECONDS}{secure}"]
                return self._json(200, {"user": {"id": user["id"], "name": user["name"], "email": user["email"]}}, {"Set-Cookie":cookies})
        if path == "/api/auth/signup" and method == "POST":
            if not SIGNUP_ENABLED:
                raise APIError(HTTPStatus.NOT_FOUND, "Signup is not enabled")
            data = self._body()
            password=str(data.get("password", "")); email=str(data.get("email", ""))[:254]; name=str(data.get("name", ""))[:120]
            if len(password) < 12 or len(password) > 128 or "@" not in email or not name.strip():
                raise APIError(HTTPStatus.BAD_REQUEST, "Name, valid email and a 12–128 character password are required")
            try:
                with connect() as db:
                    user_id = create_user(db, name, email, password)
                token=create_token(user_id,email); csrf=secrets.token_urlsafe(32); secure="; Secure" if COOKIE_SECURE else ""
                cookies=[f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TOKEN_TTL_SECONDS}{secure}",f"{CSRF_COOKIE}={csrf}; SameSite=Strict; Path=/; Max-Age={TOKEN_TTL_SECONDS}{secure}"]
                return self._json(201, {"user": {"id": user_id, "name": name, "email": email}}, {"Set-Cookie":cookies})
            except sqlite3.IntegrityError:
                raise APIError(HTTPStatus.CONFLICT, "An account with this email already exists")
        if path == "/api/auth/logout" and method == "POST":
            auth=self.headers.get("Authorization",""); token=auth[7:] if auth.startswith("Bearer ") else ""
            if not token:
                token=self._cookies().get(SESSION_COOKIE,"")
            if token:
                try:
                    claims=decode_token(token)
                    with connect() as db: db.execute("INSERT INTO revoked_tokens(token_hash,expires_at,created_at) VALUES(?,?,?) ON CONFLICT(token_hash) DO UPDATE SET expires_at=excluded.expires_at,created_at=excluded.created_at",(token_hash(token),datetime.fromtimestamp(claims["exp"],timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat())); db.commit()
                except (ValueError, KeyError):
                    pass
            secure="; Secure" if COOKIE_SECURE else ""
            cleared=[f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure}",f"{CSRF_COOKIE}=; SameSite=Strict; Path=/; Max-Age=0{secure}"]
            if IS_PRODUCTION:
                cleared.extend([f"AWSELBAuthSessionCookie{suffix}=; HttpOnly; Secure; SameSite=None; Path=/; Max-Age=0" for suffix in ["","-0","-1","-2","-3"]])
            return self._json(200,{"status":"signed_out","logout_url":COGNITO_LOGOUT_URL if IS_PRODUCTION else ""},{"Set-Cookie":cleared})
        if path == "/api/auth/session" and method == "GET":
            user_id=self._user_id(); csrf=secrets.token_urlsafe(32); secure="; Secure" if COOKIE_SECURE else ""
            with connect() as db: user=db.execute("SELECT id,name,email FROM users WHERE id=?",(user_id,)).fetchone()
            return self._json(200,{"user":dict(user),"managed":bool(IS_PRODUCTION and COGNITO_AUTH_REQUIRED)},
                              {"Set-Cookie":f"{CSRF_COOKIE}={csrf}; SameSite=Strict; Path=/; Max-Age={TOKEN_TTL_SECONDS}{secure}"})
        if path == "/api/broker/zerodha/callback" and method == "GET":
            query=parse_qs(urlparse(self.path).query); request_token=query.get("request_token",[""])[0]; state=query.get("state",[""])[0]
            with connect() as db: return self._json(200,complete_login(db,request_token,state))
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
        if path == "/api/news/finnhub/webhook" and method == "POST":
            supplied_secret = self.headers.get("X-Finnhub-Secret") or self.headers.get("X-Nivesh-Webhook-Secret") or ""
            try:
                with connect() as db:
                    return self._json(202, ingest_finnhub_webhook(db, self._body(), supplied_secret))
            except PermissionError as exc:
                raise APIError(HTTPStatus.FORBIDDEN, str(exc))
            except ValueError as exc:
                raise APIError(HTTPStatus.BAD_REQUEST, str(exc))
        if path == "/api/market/update" and method == "GET":
            with connect() as db:
                return self._json(200, market_update(db, self._user_id(), int(time.time() // 60)))
        if path == "/api/chart" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            return self._json(200, stock_chart(query.get("symbol", ["RELIANCE"])[0], query.get("timeframe", ["5m"])[0]))
        if path == "/api/market/pcr" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            sym = query.get("symbol", ["NIFTY 50"])[0]
            from .config import DATABASE_URL
            from sqlalchemy import create_engine
            engine = create_engine(DATABASE_URL)
            with engine.connect() as conn:
                return self._json(200, get_latest_pcr(conn, sym))
        if path == "/api/indicators/asi" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            sym = query.get("symbol", ["RELIANCE"])[0]
            tf = query.get("timeframe", ["5m"])[0]
            chart = stock_chart(sym, tf)
            candles = chart.get("candles") or []
            asi = calculate_asi(candles)
            trap = detect_nse_trap(candles, asi)
            return self._json(200, {"symbol": sym, "timeframe": tf, "trap_analysis": trap, "asi": asi[-50:]})
        if path == "/api/indicators/volume_profile" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            sym = query.get("symbol", ["RELIANCE"])[0]
            tf = query.get("timeframe", ["5m"])[0]
            chart = stock_chart(sym, tf)
            candles = chart.get("candles") or []
            profile = calculate_volume_profile(candles)
            last_p = float(chart.get("price") or (candles[-1]["close"] if candles else 100))
            vp_eval = evaluate_volume_profile_verdict(last_p, 1, profile)
            return self._json(200, {"symbol": sym, "timeframe": tf, "profile": profile, "evaluation": vp_eval})
        if path == "/api/securities" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            return self._json(200, listed_securities(query.get("query", [""])[0], query.get("exchange", ["ALL"])[0], int(query.get("limit", [50])[0]), int(query.get("offset", [0])[0])))
        if path == "/api/universe/search" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            q = query.get("q", [""])[0] or query.get("query", [""])[0]
            limit = int(query.get("limit", [15])[0])
            offset = int(query.get("offset", [0])[0])
            result = listed_securities(q, "ALL", limit, offset)
            return self._json(200, result.get("items", []))
        if path == "/api/market/history/status" and method == "GET":
            self._user_id()
            return self._json(200, market_history_status())
        if path == "/api/backtest" and method == "POST":
            self._user_id()
            data = self._body()
            try:
                capital = min(100_000_000, max(10_000, float(data.get("capital", 1_000_000))))
                horizon_years = int(data.get("horizon_years", 1))
                return self._json(200, backtest_report(data.get("symbol", "RELIANCE"), data.get("strategy", "ensemble"), capital, data.get("start", ""), data.get("end", ""), horizon_years=horizon_years))
            except ValueError as exc:
                raise APIError(HTTPStatus.BAD_REQUEST, str(exc))
        if path == "/api/backtest/mining_journal" and method == "GET":
            self._user_id()
            from .backtest_miner import get_mining_journal
            journal = get_mining_journal()
            return self._json(200, {
                "count": len(journal),
                "journal": journal
            })
        if path == "/api/agent/run" and method == "POST":
            with connect() as db:
                return self._json(200, run_agent(db, self._user_id()))
        if path == "/api/brain/status" and method == "GET":
            from .brains import get_bus, get_brain_pipeline
            bus = get_bus()
            pipeline = get_brain_pipeline()
            recent_events = []
            for e in bus.recent(n=20):
                d = e.to_dict()
                d["occurred_at"] = d.get("ts")
                d["source"] = d.get("source_brain")
                d["symbol"] = getattr(e, "symbol", "") or d.get("payload", {}).get("symbol", "")
                recent_events.append(d)
            durable_memory = brain_memory_status()
            
            formatted_brains = [
                {"key": key, "label": brain.name, "status": "OK_ADVISORY", "mode": "advisory", "connected_to": "in-process bus"}
                for key, brain in pipeline.items()
            ]
            
            return self._json(200, {
                "status": "connected",
                "brains": formatted_brains,
                "durable_event_journal": {
                    "today": len(recent_events),
                    "cross_process": False,
                    "recent": recent_events
                },
                "long_term_memory": durable_memory,
                "live_inference_events_connected": True,
                "canonical_trade_writer": "live_inference",
                "canonical_ledger": "shadow_execution_audits",
                "legacy_execution_disabled": True
            })
        if path == "/api/brain/report" and method == "GET":
            from .brains import get_brain_pipeline
            pipeline = get_brain_pipeline()
            report = pipeline["report"]._shadow_ledger_report()
            return self._json(200, report)
        if path == "/api/brain/alerts" and method == "GET":
            from .brains import get_bus
            from .brains.bus import PositionAlert
            bus = get_bus()
            alerts = [e.to_dict() for e in bus.recent(n=50) if isinstance(e, PositionAlert)]
            return self._json(200, alerts)
        if path == "/api/sectors/flow" and method == "GET":
            from .sector_momentum import calculate_sector_relative_strength
            metrics = calculate_sector_relative_strength()
            return self._json(200, {
                "benchmark": "NIFTY 50",
                "sectors": list(metrics.values()),
                "leading_sectors": [s["sector"] for s in metrics.values() if s.get("is_leader")],
                "lagging_sectors": [s["sector"] for s in metrics.values() if s.get("is_laggard")]
            })
        if path == "/api/memory/golden_trades" and method == "GET":
            from .trade_memory import get_golden_trade_library
            trades = get_golden_trade_library()
            return self._json(200, {
                "count": len(trades),
                "golden_trades": trades
            })
        if path == "/api/memory/mine" and method == "POST":
            from .backtest_miner import mine_golden_patterns_from_backtests
            body = self._body()
            symbols = body.get("symbols")
            min_profit = float(body.get("min_profit_pct", 1.5))
            strategy = body.get("strategy", "breakout")
            horizon_years = int(body.get("horizon_years", 1))
            result = mine_golden_patterns_from_backtests(symbols=symbols, min_profit_pct=min_profit, strategy=strategy, horizon_years=horizon_years)
            return self._json(200, result)
        if path == "/api/memory/matches" and method == "GET":
            parsed_q = parse_qs(urlparse(self.path).query)
            symbol = parsed_q.get("symbol", ["TATAMOTORS"])[0]
            from .trade_memory import find_matching_golden_trade
            candidate = {"symbol": symbol, "confidence": 85.0, "price_change": 2.2, "volume_ratio": 2.1, "action": "BUY"}
            match = find_matching_golden_trade(candidate)
            return self._json(200, match)
        if path == "/api/brain/run" and method == "POST":
            from .brains import run_brain_pipeline
            with connect() as db:
                user_id = self._user_id()
                settings = db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
                risk_profile = settings["risk_profile"] if settings and "risk_profile" in settings.keys() else "balanced"
                position_rows = db.execute("SELECT symbol, quantity, average_price FROM holdings WHERE user_id=?", (user_id,)).fetchall()
                positions = {r["symbol"]: dict(r) for r in position_rows}
                result = run_brain_pipeline({
                    "db": db,
                    "user_id": user_id,
                    "risk_profile": risk_profile,
                    "positions": positions,
                    # Keep the agentic brain endpoint advisory. ExecutionBrain
                    # can inspect shadow_execution_audits, but live_inference
                    # remains the only writer for paper trades.
                    "execute_trade": False,
                    "include_report": True,
                })
                return self._json(200, {"status": "SUCCESS", "mode": "READ_ONLY_ADVISORY", "results": result})
        if path == "/api/derivatives/plan" and method == "POST":
            data = self._body()
            with connect() as db:
                return self._json(201, derivative_plan(db, self._user_id(), data.get("symbol","RELIANCE"), data.get("market_view","neutral"), data.get("volatility","normal"), data.get("strategy","auto")))
        if path == "/api/derivatives/plans" and method == "GET":
            with connect() as db:
                return self._json(200, derivative_plans(db, self._user_id()))
        if path == "/api/bot/conversations" and method == "GET":
            with connect() as db:
                return self._json(200, bot_conversations(db, self._user_id()))
        if path.startswith("/api/bot/conversations/") and method == "GET":
            try: conversation_id = int(path.rsplit("/",1)[-1])
            except ValueError: raise APIError(HTTPStatus.BAD_REQUEST, "Invalid conversation id")
            with connect() as db:
                return self._json(200, bot_conversation(db, self._user_id(), conversation_id))
        if path == "/api/bot/chat" and method == "POST":
            data = self._body()
            with connect() as db:
                return self._json(200, bot_chat(db, self._user_id(), data.get("message",""), data.get("conversation_id")))
        if path == "/api/operations/status" and method == "GET":
            with connect() as db:
                return self._json(200, production_status(db, self._require_admin()))
        if path == "/api/operations/copilot" and method == "GET":
            with connect() as db:
                return self._json(200, operations_copilot_status(db, self._require_admin()))
        if path == "/api/pre-market/health" and method == "GET":
            self._require_admin()
            return self._json(200, pre_market_health_status())
        if path == "/api/shadow/session/status" and method == "GET":
            self._require_admin()
            return self._json(200, shadow_session_status_api())
        if path == "/api/shadow/trades" and method == "GET":
            self._user_id()
            query = parse_qs(urlparse(self.path).query)
            return self._json(200, shadow_trade_book(int(query.get("limit", [200])[0])))
        if path.startswith("/api/shadow/trades/") and path.endswith("/exit") and method == "POST":
            self._require_admin()
            try: audit_id=int(path.split("/")[-2])
            except ValueError: raise APIError(HTTPStatus.BAD_REQUEST, "Invalid shadow trade id")
            return self._json(200, exit_shadow_trade(audit_id))
        if path.startswith("/api/shadow/trades/") and path.endswith("/risk") and method == "POST":
            self._require_admin()
            try: audit_id=int(path.split("/")[-2])
            except ValueError: raise APIError(HTTPStatus.BAD_REQUEST, "Invalid shadow trade id")
            data=self._body()
            return self._json(200, update_shadow_trade_risk(audit_id, data.get("stop_loss"), data.get("take_profit")))
        if path == "/api/shadow/repair-today" and method == "POST":
            self._require_admin()
            data=self._body()
            target_date = data.get("date") or data.get("target_date") or None
            return self._json(200, repair_today_missing_bars(int(data.get("limit",100)), target_date_str=target_date))
        if path == "/api/shadow/counterfactual/summary" and method == "GET":
            self._require_admin()
            query = parse_qs(urlparse(self.path).query)
            date_val = query.get("date", [None])[0]
            return self._json(200, shadow_counterfactual_summary_service(date_val))
        if path == "/api/shadow/counterfactual/replay" and method == "POST":
            self._require_admin()
            data = self._body()
            date_val = data.get("date") or data.get("target_date") or None
            return self._json(200, trigger_counterfactual_replay_service(date_val))
        if path == "/api/shadow/diagnostic/funnel" and method == "GET":
            self._require_admin()
            query = parse_qs(urlparse(self.path).query)
            days = int(query.get("days", [4])[0])
            return self._json(200, trade_funnel_diagnostic(days))
        if path == "/api/notifications/status" and method == "GET":
            self._require_admin()
            return self._json(200, notifications_status_service())
        if path == "/api/notifications/test" and method == "POST":
            self._require_admin()
            return self._json(200, notifications_test_service())
        if path == "/api/stream/events" and method == "GET":
            user_id = self._user_id()
            from backend.sse_broadcaster import handle_sse_connection
            handle_sse_connection(self, user_id)
            return
        if path == "/api/stream/broadcast" and method == "POST":
            self._require_admin()
            data = self._body()
            from backend.sse_broadcaster import publish_sse_event
            publish_sse_event(data.get("event", "custom_event"), data.get("data", {}))
            return self._json(200, {"status": "broadcast_queued"})
        if path == "/api/execution/status" and method == "GET":
            with connect() as db: return self._json(200,execution_status(db,self._user_id()))
        if path == "/api/execution/intent" and method == "POST":
            data=self._body()
            with connect() as db: return self._json(201,create_order_intent(db,self._user_id(),data))
        if path.startswith("/api/execution/intent/") and path.endswith("/approve") and method == "POST":
            intent_id=int(path.split("/")[-2])
            with connect() as db: return self._json(200,approve_order_intent(db,self._user_id(),intent_id))
        if path.startswith("/api/execution/intent/") and path.endswith("/submit") and method == "POST":
            intent_id=int(path.split("/")[-2])
            with connect() as db: return self._json(200,submit_order_intent(db,self._user_id(),intent_id))
        if path == "/api/execution/reconcile" and method == "POST":
            with connect() as db: return self._json(200,reconcile_orders(db,self._user_id()))
        if path == "/api/execution/kill-switch" and method == "POST":
            data=self._body()
            with connect() as db: return self._json(200,update_kill_switch(db,self._user_id(),bool(data.get("active")),data.get("reason","")))
        if path == "/api/execution/risk-policy" and method == "PUT":
            data=self._body()
            with connect() as db: return self._json(200,update_risk_policy(db,self._user_id(),data))
        if path == "/api/broker/login" and method == "POST":
            with connect() as db: return self._json(200,broker_login(db,self._user_id()))
        if path == "/api/broker/disconnect" and method == "POST":
            with connect() as db: return self._json(200,broker_disconnect(db,self._user_id()))
        if path == "/api/ml/status" and method == "GET":
            self._user_id()
            return self._json(200, ml_status())
        if path == "/api/ml/quant-models" and method == "GET":
            self._user_id()
            return self._json(200, ml_quant_models())
        if path == "/api/ml/alpha-fragility" and method == "GET":
            self._user_id()
            return self._json(200, ml_alpha_fragility())
        if path == "/api/ml/model-review" and method == "GET":
            self._user_id()
            return self._json(200, model_review_status())
        if path == "/api/ml/train" and method == "POST":
            self._require_admin()
            return self._json(200, ml_train())
        if path == "/api/ml/validate" and method == "POST":
            self._require_admin()
            return self._json(200, ml_validate())
        if path == "/api/ml/shadow" and method == "POST":
            self._require_admin()
            return self._json(200, ml_shadow())
        if path == "/api/ml/drift" and method in {"GET", "POST"}:
            self._user_id()
            return self._json(200, ml_drift())
        if path == "/api/ml/autonomous-trigger" and method == "POST":
            self._require_admin()
            from backend.tasks.worker import check_and_autotrain_drift
            task = check_and_autotrain_drift.apply_async(kwargs={"force": True})
            return self._json(202, {
                "status": "QUEUED",
                "task_id": str(task.id),
                "message": "Autonomous drift & walk-forward retraining task queued on Celery worker."
            })
        if path == "/api/derivatives/spreads" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["NIFTY"])[0]
            self._user_id()
            return self._json(200, derivatives_spreads(symbol))
        if path == "/api/derivatives/greeks" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["NIFTY"])[0]
            self._user_id()
            return self._json(200, derivatives_greeks(symbol))
        if path == "/api/derivatives/spider" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["NIFTY"])[0]
            self._user_id()
            return self._json(200, spider_bot_status(symbol))
        if path == "/api/derivatives/gex" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["NIFTY"])[0]
            self._user_id()
            from backend.gex_engine import get_cached_or_compute_gex
            return self._json(200, get_cached_or_compute_gex(symbol))
        if path == "/api/rl/status" and method == "GET":
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["NIFTY"])[0]
            self._user_id()
            return self._json(200, rl_controller_status(symbol))
        if path == "/api/reports/daily-executive-journal" and method == "GET":
            with connect() as db:
                return self._json(200, daily_executive_journal(db, self._user_id()))
        if path == "/api/coach/audit" and method == "GET":
            self._user_id()
            return self._json(200, get_coach_audit())
        if path == "/api/coach/proposals" and method == "GET":
            status = parse_qs(urlparse(self.path).query).get("status", [None])[0]
            self._user_id()
            return self._json(200, get_coach_proposals_service(status=status))
        if path == "/api/coach/proposals/apply" and method == "POST":
            self._require_admin()
            body = self._body()
            return self._json(200, apply_coach_proposal_service(body.get("proposal_id"), approved_by=body.get("approved_by", "Admin")))
        if path == "/api/coach/proposals/dismiss" and method == "POST":
            self._require_admin()
            body = self._body()
            return self._json(200, dismiss_coach_proposal_service(body.get("proposal_id")))
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
        frontend_root = (ROOT / "frontend").resolve()
        target = (frontend_root / relative).resolve()
        if frontend_root not in target.parents and target != frontend_root:
            raise APIError(HTTPStatus.FORBIDDEN, "Forbidden")
        if not target.is_file():
            raise APIError(HTTPStatus.NOT_FOUND, "File not found")
        allowed_exts = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".json", ".woff", ".woff2", ".map"}
        if target.suffix.lower() not in allowed_exts:
            raise APIError(HTTPStatus.FORBIDDEN, "Forbidden")
        body = target.read_bytes()
        self.send_response(200)
        content_type = mimetypes.guess_type(target.name)[0]
        if target.suffix.lower() == ".js":
            content_type = "application/javascript; charset=utf-8"
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
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
            self._enforce_rate_limit(method)
            self._enforce_request_integrity(method)
            self._route(method)
        except APIError as exc:
            self._json(exc.status, {"error": exc.message})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            detail = type(exc).__name__ if IS_PRODUCTION else f"{type(exc).__name__}: {exc}"
            print(f"[nivesh] internal error: {detail}")
            self._json(500, {"error": "Internal server error"})


def main():
    validate_runtime_security()
    initialize()
    start_scheduler()
    try:
        from backend.sse_broadcaster import start_redis_listener, start_metrics_ticker, wire_bus_to_sse
        from backend.brains import get_bus
        start_redis_listener()
        start_metrics_ticker()
        wire_bus_to_sse(get_bus())
        print("[nivesh] SSE Broadcaster & Metrics Ticker initialized")
    except Exception as sse_err:
        print(f"[nivesh] SSE initialization notice: {sse_err}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Nivesh AI running at http://{HOST}:{PORT}")
    if DEMO_MODE: print("Demo login: arjun@example.com / nivesh123")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nNivesh AI stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
