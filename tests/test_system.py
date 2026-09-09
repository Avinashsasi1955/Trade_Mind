import sqlite3
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from backend.agent import TradingAgent
from backend.database import SCHEMA, create_user, seed_demo_portfolio
from backend.market import market_snapshot
from backend.security import create_token, decode_token, hash_password, verify_password
from backend.service import dashboard, run_agent, update_settings
from backend.service import stock_analysis
from backend.technical_analysis import GLOSSARY, analyse_structure
from backend.model_provider import provider_catalog
from backend.sentiment import _event, _lexical_score, analyse_sentiment
from backend.news_gateway import articles_for_symbol
from backend.config import _load_env
from backend.service import market_update, sentiment_dashboard
from backend.ai_gateway import AIGateway, gateway_status
from backend.charting import chart_data
from backend.backtesting import run_backtest
from backend.security_master import search_securities, security_master_stats
from backend.history_store import HistoryStore
from backend.zerodha_adapter import ZerodhaAdapter, StaleBrokerTokenError
from backend.derivatives import build_derivative_plan, choose_derivative_strategy
from backend.trade_bot import chat, conversation
from backend.monitoring import operations_status, record_event
from backend.ml_pipeline import ResearchStore, adjust_corporate_actions, build_research_dataset, detect_drift, generate_shadow_predictions, train_model, walk_forward_validate
from backend.execution import approve_intent, create_intent, reconcile_intents, submit_intent
from backend.risk_engine import set_kill_switch
from backend.kite_stream import parse_binary
import struct
from decimal import Decimal
from datetime import date, timedelta
from backend.transaction_costs import estimate_zerodha_costs, reconcile_contract_note
from backend.observability import JsonFormatter, page
import logging
from urllib.error import HTTPError
from backend.shadow_sessions import open_session
from backend.remote_restore import restore as remote_restore
from backend.ml.validation_engine import classify_probability, threshold_coverage
from backend.server import Handler, CSRF_COOKIE, LOGIN_FAILURE_LIMIT
from backend import config as application_config
from backend.asgi import NiveshASGI
from backend.broker_gateway import _decrypt as decrypt_broker_session, _encrypt as encrypt_broker_session
from backend.alb_identity import verify_alb_claims


class NiveshSystemTests(unittest.TestCase):
    def test_production_asgi_adapter_returns_hardened_health_response(self):
        scope={"type":"http","method":"GET","path":"/api/health","raw_path":b"/api/health","query_string":b"",
               "headers":[(b"host",b"localhost")],"client":("127.0.0.1",1234)}
        status,headers,body=NiveshASGI._dispatch(scope,b"")
        header_map={name:value for name,value in headers}
        self.assertEqual(status,200)
        self.assertNotIn(b"server",header_map)
        self.assertIn(b"content-security-policy",header_map)
        self.assertEqual(__import__("json").loads(body),{"status":"ok"})

    def test_broker_session_encryption_is_user_bound(self):
        encrypted=encrypt_broker_session(7,{"access_token":"sensitive"})
        self.assertNotIn("sensitive",encrypted)
        self.assertEqual(decrypt_broker_session(7,encrypted)["access_token"],"sensitive")
        with self.assertRaises(Exception): decrypt_broker_session(8,encrypted)

    def test_distributed_login_failure_window_locks_and_clears(self):
        email="target@example.com"
        for _ in range(LOGIN_FAILURE_LIMIT): Handler._record_login(self.db,email,False)
        self.assertTrue(Handler._login_is_blocked(self.db,email))
        Handler._record_login(self.db,email,True)
        self.assertFalse(Handler._login_is_blocked(self.db,email))

    def test_alb_cognito_claims_require_expected_signer_and_verified_email(self):
        import jwt,time
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        private=ec.generate_private_key(ec.SECP256R1())
        public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        claims={"sub":"cognito-subject","email":"operator@example.com","email_verified":True,"exp":int(time.time())+60}
        headers={"alg":"ES256","kid":"test-key","signer":"arn:aws:elasticloadbalancing:ap-south-1:123:loadbalancer/app/test/1"}
        token=jwt.encode(claims,private,algorithm="ES256",headers=headers)
        with patch("backend.alb_identity.TRUSTED_ALB_ARN",headers["signer"]),patch("backend.alb_identity._public_key",return_value=public):
            self.assertEqual(verify_alb_claims(token)["sub"],"cognito-subject")
        with patch("backend.alb_identity.TRUSTED_ALB_ARN","arn:wrong"):
            with self.assertRaises(ValueError): verify_alb_claims(token)

    def test_cookie_authenticated_write_requires_double_submit_csrf(self):
        handler=object.__new__(Handler); handler.path="/api/settings"
        handler.headers={"Cookie":f"{CSRF_COOKIE}=expected"}
        with self.assertRaisesRegex(Exception,"CSRF"):
            handler._enforce_request_integrity("PUT")
        handler.headers={"Cookie":f"{CSRF_COOKIE}=expected","X-CSRF-Token":"expected"}
        handler._enforce_request_integrity("PUT")

    def test_production_configuration_fails_closed(self):
        with patch.multiple(application_config,IS_PRODUCTION=True,JWT_SECRET="weak",COOKIE_SECURE=False,
                            DEMO_MODE=True,SIGNUP_ENABLED=True,DATABASE_URL="",ALLOWED_ORIGINS=set(),ADMIN_EMAILS=set()):
            with self.assertRaisesRegex(RuntimeError,"Unsafe production configuration"):
                application_config.validate_runtime_security()

    def test_token_rejects_algorithm_or_scope_tampering(self):
        token=create_token(self.user_id,"test@example.com")
        header,payload,signature=token.split(".")
        import base64,json
        changed=base64.urlsafe_b64encode(json.dumps({"alg":"none","typ":"JWT"}).encode()).rstrip(b"=").decode()
        with self.assertRaises(ValueError): decode_token(f"{changed}.{payload}.{signature}")

    def test_access_log_redacts_query_tokens(self):
        handler=object.__new__(Handler); handler.path="/api/broker/zerodha/callback?request_token=secret"; handler.command="GET"; handler.client_address=("127.0.0.1",1234)
        with patch("builtins.print") as output:
            handler.log_message('"%s" %s %s',"GET /callback","200","-")
        rendered=output.call_args.args[0]
        self.assertNotIn("secret",rendered)
        self.assertNotIn("request_token",rendered)

    def test_static_handler_never_serves_env_file(self):
        handler=object.__new__(Handler)
        with self.assertRaisesRegex(Exception,"File not found"):
            handler._static("/.env")

    def test_confidence_floor_abstains_without_claiming_log_loss_improvement(self):
        self.assertEqual(classify_probability(.64),0)
        self.assertEqual(classify_probability(.70),1)
        self.assertEqual(classify_probability(.20),-1)
        coverage=threshold_coverage([.2,.4,.6,.7])
        self.assertEqual((coverage["bullish"],coverage["bearish"],coverage["neutral"]),(1,1,2))
    def test_shadow_ledger_refuses_historical_session_credit(self):
        with self.assertRaisesRegex(ValueError,"today's live session"):
            open_session(None,date.today()-timedelta(days=1))

    def test_remote_restore_requires_explicit_one_shot_fuse(self):
        with patch.dict(os.environ,{"ALLOW_REMOTE_RESTORE":""}):
            with self.assertRaisesRegex(RuntimeError,"ALLOW_REMOTE_RESTORE"):
                remote_restore()
    def test_structured_logging_and_alerts_fail_closed_without_webhook(self):
        record=logging.LogRecord("nivesh",logging.ERROR,"",0,"broker unavailable",(),None)
        payload=__import__("json").loads(JsonFormatter().format(record))
        self.assertEqual(payload["level"],"ERROR")
        with patch.dict(os.environ,{"NIVESH_ALERT_WEBHOOK_URL":""}):
            self.assertFalse(page("CRITICAL","test-only alert")["sent"])
    def test_decimal_transaction_costs_are_segment_and_side_aware(self):
        delivery=estimate_zerodha_costs("EQUITY_DELIVERY","BUY",Decimal("1000.125"),10)
        futures=estimate_zerodha_costs("FUTURES","SELL",Decimal("25000.50"),25)
        self.assertEqual(delivery.brokerage,Decimal("0.00"))
        self.assertGreater(futures.stt,Decimal("0"))
        reconciled=reconcile_contract_note(futures,Decimal("410.25"),Decimal("25001"),Decimal("25000.50"),25)
        self.assertEqual(reconciled["fully_reconciled"],"true")
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.user_id = create_user(self.db, "Test User", "test@example.com", "strong-pass")
        seed_demo_portfolio(self.db, self.user_id)

    def tearDown(self):
        self.db.close()

    def test_password_hash_and_signed_token(self):
        encoded = hash_password("secret-pass")
        self.assertTrue(verify_password("secret-pass", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        claims = decode_token(create_token(self.user_id, "test@example.com"))
        self.assertEqual(claims["sub"], self.user_id)

    def test_agent_produces_explainable_ranked_signals(self):
        signals = TradingAgent().analyse(market_snapshot(), "balanced")
        self.assertGreaterEqual(len(signals), 10)
        self.assertGreaterEqual(signals[0].confidence, signals[-1].confidence)
        self.assertIn("RSI", signals[0].reasoning)
        self.assertGreater(signals[0].target, signals[0].price)

    def test_strategy_is_selected_from_position_and_regime(self):
        stocks = market_snapshot()
        flat = TradingAgent().analyse(stocks, "balanced")
        held = TradingAgent().analyse(stocks, "balanced", {stocks[0]["symbol"]: {"quantity": 10}})
        self.assertIn(flat[0].strategy, {"Breakout Trading", "Intraday Momentum", "Mean Reversion", "Volatility Squeeze", "Long Put / Avoid Long", "VWAP Pullback"})
        held_signal = next(item for item in held if item.symbol == stocks[0]["symbol"])
        self.assertIn(held_signal.strategy, {"Protective Collar", "Covered Call", "Momentum Hold"})
        self.assertIn("algorithm selected", held_signal.reasoning)

    def test_exchange_security_master_is_searchable(self):
        stats = security_master_stats()
        self.assertGreater(stats["counts"]["NSE"], 2000)
        self.assertGreater(stats["counts"]["BSE"], 4000)
        result = search_securities("RELIANCE", "ALL", 20, 0)
        self.assertGreater(result["total"], 0)
        self.assertTrue(all(item["status"] == "Active" for item in result["items"]))

    def test_resumable_history_store_and_read_only_kite_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HistoryStore(Path(folder) / "history.db")
            bars = [{"timestamp": "2026-06-27T00:00:00+05:30", "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000}]
            self.assertEqual(store.save("NSE", "TEST", 1, "day", bars, "2026-06-28T00:00:00Z"), 1)
            self.assertEqual(store.stats()["bars"], 1)
        adapter = ZerodhaAdapter("", "")
        self.assertFalse(adapter.configured)
        with self.assertRaises(RuntimeError):
            adapter.place_order("TEST", "BUY", 1)

    def test_manual_agent_run_persists_audit_record(self):
        result = run_agent(self.db, self.user_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 1)
        self.assertGreaterEqual(self.db.execute("SELECT COUNT(*) FROM watchlist_snapshots").fetchone()[0], 1)

    def test_dashboard_and_risk_setting(self):
        update_settings(self.db, self.user_id, "conservative")
        data = dashboard(self.db, self.user_id)
        self.assertEqual(data["settings"]["risk_profile"], "conservative")
        self.assertEqual(len(data["holdings"]), 6)
        self.assertIn("current_value", data["summary"])

    def test_smart_money_analysis_and_trade_plan(self):
        analysis = analyse_structure("RELIANCE", "Reliance Industries", 2993.20)
        self.assertIn(analysis["bias"], {"bullish", "bearish", "neutral"})
        self.assertIn("bos", analysis["market_structure"])
        self.assertIn("bsl", analysis["liquidity"])
        self.assertGreaterEqual(analysis["trade_plan"]["risk_reward"], 1.9)
        self.assertEqual(analysis["trade_plan"]["tick_size"], .05)
        self.assertTrue({"SL", "TP", "OB", "FVG", "CHOCH", "BOS", "POI"}.issubset(GLOSSARY))

    def test_analysis_is_persisted(self):
        result = stock_analysis(self.db, self.user_id, "RELIANCE", enhance_narrative=False)
        self.assertEqual(result["symbol"], "RELIANCE")
        self.assertEqual(result["timeframes"]["ltf"], "1D")
        self.assertEqual(result["data_mode"], "kite_historical")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stock_analyses").fetchone()[0], 1)

    def test_model_catalog_exposes_free_and_paid_routes(self):
        catalog = provider_catalog()
        providers = {item["id"]: item for item in catalog["providers"]}
        self.assertTrue(providers["gemini"]["free_tier"])
        self.assertTrue(providers["ollama"]["free_tier"])
        self.assertFalse(providers["anthropic"]["free_tier"])
        self.assertFalse(providers["openai"]["free_tier"])
        self.assertFalse(providers["nvidia-nim"]["free_tier"])
        self.assertTrue(providers["nvidia-nim"]["quota_dependent"])

    def test_sentiment_has_auditable_components(self):
        market = market_snapshot()
        result = analyse_sentiment(market[0], market)
        self.assertIn(result["label"], {"bullish", "neutral", "bearish"})
        self.assertEqual(set(result["components"]), {"news", "price_action", "volume", "market_breadth"})
        self.assertEqual(result["coverage"]["articles"], len(result["headlines"]))

    def test_sentiment_understands_negation_and_financial_events(self):
        self.assertGreater(_lexical_score("profit growth remains strong"), 0)
        self.assertLess(_lexical_score("profit growth is not strong"), _lexical_score("profit growth remains strong"))
        self.assertEqual(_event("Regulator opens fraud investigation")[0], "governance_risk")

    def test_sentiment_deduplicates_similar_news_and_labels_live_mode(self):
        market = market_snapshot()
        articles = [
            {"headline": "Reliance wins a major new contract", "source": "Reuters", "published_at": "2030-01-01T00:00:00+00:00"},
            {"headline": "Reliance wins major new contract", "source": "Another wire", "published_at": "2030-01-01T00:01:00+00:00"},
        ]
        result = analyse_sentiment(market[0], market, articles, "live_news")
        self.assertEqual(result["data_mode"], "live_news")
        self.assertLess(result["headlines"][1]["novelty"], 1)

    def test_empty_news_response_never_labels_demo_headlines_live(self):
        with patch("backend.news_gateway.NEWS_API_KEY", "test-key"), patch("backend.news_gateway._fetch", return_value=[]):
            result = articles_for_symbol(self.db, "EMPTYTEST", "Empty Test Limited")
        self.assertEqual(result["mode"], "offline_fallback")
        self.assertEqual(result["articles"], [])

    def test_env_file_loader_does_not_override_process_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("AUDIT_NEW_KEY=from-file\nAUDIT_EXISTING_KEY=unsafe\n", encoding="utf-8")
            os.environ["AUDIT_EXISTING_KEY"] = "from-process"
            try:
                _load_env(path)
                self.assertEqual(os.environ["AUDIT_NEW_KEY"], "from-file")
                self.assertEqual(os.environ["AUDIT_EXISTING_KEY"], "from-process")
            finally:
                os.environ.pop("AUDIT_NEW_KEY", None)
                os.environ.pop("AUDIT_EXISTING_KEY", None)

    def test_sentiment_dashboard_and_market_updater(self):
        sentiment = sentiment_dashboard(self.db, self.user_id, "RELIANCE")
        update = market_update(self.db, self.user_id, 42)
        next_update = market_update(self.db, self.user_id, 43)
        self.assertEqual(sentiment["selected"]["symbol"], "RELIANCE")
        self.assertGreaterEqual(len(sentiment["leaders"]), 4)
        self.assertEqual(update["breadth"]["advancing"] + update["breadth"]["declining"], len(update["quotes"]))
        self.assertIsInstance(update["indices"][0]["price"], float)

    def test_ai_gateway_audits_and_caches_explanations(self):
        analysis = analyse_structure("RELIANCE", "Reliance Industries", 2993.20)
        first = AIGateway(self.db, self.user_id).explain(analysis)
        second = AIGateway(self.db, self.user_id).explain(analysis)
        self.assertEqual(first["provider"], "local")
        self.assertTrue(second["cached"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ai_gateway_logs").fetchone()[0], 2)

    def test_ai_gateway_status_denies_order_access(self):
        status = gateway_status(self.db, self.user_id)
        self.assertFalse(status["controls"]["order_execution_access"])
        self.assertEqual(status["controls"]["rate_limit_per_minute"], 10)

    def test_chart_data_contains_timeframes_and_indicators(self):
        chart = chart_data("RELIANCE", "15m")
        self.assertEqual(chart["timeframe"], "15m")
        self.assertGreaterEqual(len(chart["candles"]), 1)
        self.assertGreaterEqual(len(chart["indicators"]["ema20"]), 1)
        self.assertGreaterEqual(len(chart["indicators"]["rsi14"]), 1)
        self.assertEqual(set(chart["indicators"]["bollinger"]), {"upper", "middle", "lower"})
        daily = chart_data("RELIANCE", "1D")
        self.assertIn(daily["data_mode"], {"kite_historical", "simulated", "stored_daily_plus_live_session"})
        self.assertGreaterEqual(len(daily["candles"]), 1)

    def test_backtest_uses_chronological_holdout_and_costs(self):
        report = run_backtest("RELIANCE", "ensemble", 1_000_000)
        self.assertTrue(report["methodology"]["look_ahead_protection"])
        self.assertEqual(report["methodology"]["costs_bps_per_trade"], 12)
        self.assertGreaterEqual(report["period"]["sessions"], 240)
        self.assertIn("directional_accuracy", report["out_of_sample"])
        self.assertIn(report["data_mode"], {"simulated", "historical"})

    def test_derivative_engine_builds_guarded_multileg_ticket(self):
        plan = build_derivative_plan("RELIANCE", 3000, "bullish", "normal")
        self.assertEqual(plan["strategy"], "Bull Call Spread")
        self.assertEqual(len(plan["legs"]), 2)
        self.assertTrue(plan["execution"]["atomic_basket_required"])
        self.assertFalse(plan["execution"]["live_order_allowed"])
        self.assertEqual(choose_derivative_strategy("bearish", "normal", True), "Protective Collar")
        future = build_derivative_plan("NIFTY", 25000, "bearish", "normal", strategy="Short Future")
        self.assertEqual(future["lot_size"], 75)
        self.assertFalse(future["risk"]["defined"])

    def test_trade_bot_persists_chat_and_derivative_plan(self):
        result = chat(self.db, self.user_id, "Prepare a bullish options plan for RELIANCE")
        thread = conversation(self.db, self.user_id, result["conversation_id"])
        self.assertEqual(len(thread["messages"]), 2)
        self.assertEqual(result["message"]["metadata"]["intent"], "derivative_plan")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM derivative_plans").fetchone()[0], 1)

    def test_operations_monitoring_reports_safety_gate(self):
        record_event(self.db, "scheduler", "INFO", "Scheduler heartbeat")
        status = operations_status(self.db, self.user_id)
        order = next(item for item in status["components"] if item["name"] == "Order execution")
        self.assertEqual(order["status"], "locked")
        self.assertEqual(status["metrics"]["live_orders"], 0)

    def test_corporate_action_adjustment_is_backward_and_auditable(self):
        bars=[{"timestamp":"2026-01-01","open":100,"high":105,"low":95,"close":100,"volume":1000},{"timestamp":"2026-01-02","open":50,"high":52,"low":48,"close":50,"volume":2000}]
        actions=[{"effective_date":"2026-01-02","action_type":"split","ratio_from":1,"ratio_to":2,"cash_amount":0}]
        adjusted=adjust_corporate_actions(bars,actions)
        self.assertEqual(adjusted[0]["close"],50)
        self.assertEqual(adjusted[0]["volume"],2000)

    def test_ml_pipeline_versions_validates_shadows_and_checks_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ResearchStore(Path(folder)/"research.db")
            history=HistoryStore(Path(folder)/"history.db")
            bars=[]
            start_day=date(2024,1,1)
            for index in range(420):
                close=100+index*.08+((index%9)-4)*.35
                stamp=(start_day+timedelta(days=index)).isoformat()
                bars.append({"timestamp":f"{stamp}T00:00:00+05:30","open":close-.2,"high":close+1,"low":close-1,"close":close,"volume":100000+index*100})
            history.save("NSE","TEST",1,"day",bars,"now","test")
            dataset=build_research_dataset(history=history,store=store)
            self.assertGreaterEqual(dataset["samples"],120)
            trained=train_model(store)
            self.assertIn(trained["algorithm"],{"regularised_logistic_regression","hist_gradient_boosting_conservative","hist_gradient_boosting_flexible"})
            self.assertIn("holdout",trained["metrics"])
            self.assertEqual(trained["status"],"candidate")
            validation=walk_forward_validate(store)
            self.assertGreaterEqual(validation["summary"]["folds"],1)
            self.assertTrue(validation["candidate_promotion"]["promoted"])
            self.assertFalse(generate_shadow_predictions(store)["orders_allowed"])
            self.assertIn(detect_drift(store)["overall"],{"stable","watch","alert"})

    def test_order_intent_requires_risk_then_human_approval_and_live_gate(self):
        intent=create_intent(self.db,self.user_id,{"symbol":"RELIANCE","exchange":"NSE","transaction_type":"BUY","quantity":1,"order_type":"LIMIT","limit_price":2990,"product":"CNC","confidence":80,"reasoning":"Unit-tested supervised intent"})
        self.assertEqual(intent["status"],"APPROVAL_PENDING")
        approved=approve_intent(self.db,self.user_id,intent["id"])
        self.assertEqual(approved["status"],"APPROVED")
        with self.assertRaises(ValueError): submit_intent(self.db,self.user_id,intent["id"])
        set_kill_switch(self.db,self.user_id,True,"Test emergency")
        blocked=create_intent(self.db,self.user_id,{"symbol":"RELIANCE","exchange":"NSE","transaction_type":"BUY","quantity":1,"order_type":"LIMIT","limit_price":2990,"product":"CNC","confidence":80,"reasoning":"Must be rejected"})
        self.assertEqual(blocked["status"],"RISK_REJECTED")

    def test_bearish_ml_signal_routes_to_put_and_enforces_lot_size(self):
        self.db.execute("""CREATE TABLE instrument_master(symbol TEXT,exchange TEXT,instrument_type TEXT,underlying_symbol TEXT,
            expiry TEXT,strike REAL,lot_size INTEGER,is_active INTEGER)""")
        self.db.execute("INSERT INTO instrument_master VALUES(?,?,?,?,?,?,?,?)",("RELIANCE30JUL3000PE","NFO","PE","RELIANCE","2030-07-25",3000,250,1))
        self.db.execute("UPDATE risk_policies SET allow_derivatives=1 WHERE user_id=?",(self.user_id,)); self.db.commit()
        intent=create_intent(self.db,self.user_id,{"symbol":"RELIANCE","exchange":"NSE","transaction_type":"SELL","signal_direction":"SHORT",
            "signal_source":"ml","model_version":"test-model","quantity":10,"order_type":"LIMIT","limit_price":3000,
            "derivative_limit_price":25,"confidence":80,"reasoning":"Bearish model signal"})
        self.assertEqual(intent["status"],"APPROVAL_PENDING")
        self.assertEqual((intent["exchange"],intent["symbol"],intent["transaction_type"]),("NFO","RELIANCE30JUL3000PE","BUY"))
        self.assertEqual(intent["quantity"],250)
        self.assertEqual(intent["risk_payload"]["routing"]["route"]["instrument_type"],"PE")
        self.assertEqual(intent["events"][0]["event_type"],"BEARISH_ROUTE_SELECTED")

    def test_bearish_ml_signal_without_derivative_chain_is_rejected_and_audited(self):
        intent=create_intent(self.db,self.user_id,{"symbol":"COALINDIA","exchange":"NSE","transaction_type":"SELL","signal_direction":"SHORT",
            "signal_source":"ml","model_version":"test-model","quantity":10,"order_type":"LIMIT","limit_price":480,
            "derivative_limit_price":10,"confidence":80,"reasoning":"Bearish model signal"})
        self.assertEqual(intent["status"],"RISK_REJECTED")
        self.assertEqual(intent["risk_payload"]["routing"]["status"],"REJECTED")
        self.assertEqual(intent["events"][0]["event_type"],"BEARISH_ROUTE_REJECTED")

    def test_duplicate_order_idempotency_and_partial_fill_reconciliation(self):
        payload={"client_order_id":"IDEMPOTENT-001","symbol":"RELIANCE","exchange":"NSE","transaction_type":"BUY","quantity":10,"order_type":"LIMIT","limit_price":2990,"product":"CNC","confidence":80,"reasoning":"Failure-drill intent"}
        first=create_intent(self.db,self.user_id,payload)
        duplicate=create_intent(self.db,self.user_id,payload)
        self.assertEqual(first["id"],duplicate["id"])
        self.db.execute("UPDATE order_intents SET status='SUBMITTED',broker_order_id='KITE-1' WHERE id=?",(first["id"],))
        self.db.commit()

        class FakeBroker:
            configured=True
            def __init__(self,*args,**kwargs): pass
            def orders(self): return [{"order_id":"KITE-1","status":"OPEN","filled_quantity":4}]
            def positions(self): return {"net":[]}

        with patch("backend.execution.LIVE_TRADING_ENABLED",True), patch("backend.execution.ZerodhaAdapter",FakeBroker), patch("backend.execution.access_token",return_value="session"):
            result=reconcile_intents(self.db,self.user_id)
        self.assertEqual(result["updated"],1)
        self.assertEqual(self.db.execute("SELECT status FROM order_intents WHERE id=?",(first["id"],)).fetchone()[0],"PARTIAL")

    def test_broker_outage_does_not_mutate_approved_intent(self):
        intent=create_intent(self.db,self.user_id,{"symbol":"RELIANCE","exchange":"NSE","transaction_type":"BUY","quantity":1,"order_type":"LIMIT","limit_price":2990,"product":"CNC","confidence":80,"reasoning":"Outage drill"})
        approve_intent(self.db,self.user_id,intent["id"])
        self.db.execute("CREATE TABLE system_promotion_ledger(singleton INTEGER PRIMARY KEY,live_eligible INTEGER NOT NULL)")
        self.db.execute("INSERT INTO system_promotion_ledger VALUES(1,1)"); self.db.commit()

        class OfflineBroker:
            configured=True
            def __init__(self,*args,**kwargs): pass
            def place_order(self,*args,**kwargs): raise TimeoutError("simulated broker outage")

        with patch("backend.execution.LIVE_TRADING_ENABLED",True), patch("backend.execution.LIVE_ELIGIBLE",True), patch("backend.execution.ZerodhaAdapter",OfflineBroker), patch("backend.execution.access_token",return_value="session"):
            with self.assertRaises(TimeoutError): submit_intent(self.db,self.user_id,intent["id"])
        self.assertEqual(self.db.execute("SELECT status FROM order_intents WHERE id=?",(intent["id"],)).fetchone()[0],"APPROVED")

    def test_live_eligible_fuse_blocks_submission_independently(self):
        intent=create_intent(self.db,self.user_id,{"symbol":"RELIANCE","exchange":"NSE","transaction_type":"BUY","quantity":1,"order_type":"LIMIT","limit_price":2990,"product":"CNC","confidence":80,"reasoning":"Fuse test"})
        approve_intent(self.db,self.user_id,intent["id"])
        with patch("backend.execution.LIVE_TRADING_ENABLED",True), patch("backend.execution.LIVE_ELIGIBLE",False):
            with self.assertRaisesRegex(ValueError,"LIVE_ELIGIBLE"):
                submit_intent(self.db,self.user_id,intent["id"])

    def test_database_promotion_ledger_fails_closed(self):
        intent=create_intent(self.db,self.user_id,{"symbol":"RELIANCE","exchange":"NSE","transaction_type":"BUY","quantity":1,"order_type":"LIMIT","limit_price":2990,"product":"CNC","confidence":80,"reasoning":"Ledger test"})
        approve_intent(self.db,self.user_id,intent["id"])
        self.db.execute("CREATE TABLE system_promotion_ledger(singleton INTEGER PRIMARY KEY,live_eligible INTEGER NOT NULL)")
        self.db.execute("INSERT INTO system_promotion_ledger VALUES(1,0)"); self.db.commit()
        with patch("backend.execution.LIVE_TRADING_ENABLED",True), patch("backend.execution.LIVE_ELIGIBLE",True):
            with self.assertRaisesRegex(ValueError,"promotion ledger"):
                submit_intent(self.db,self.user_id,intent["id"])

    def test_stale_kite_token_has_explicit_operator_action(self):
        adapter=ZerodhaAdapter("api-key","expired-token")
        error=HTTPError("https://api.kite.trade/orders",403,"Forbidden",{},None)
        with patch("backend.zerodha_adapter.urlopen",side_effect=error):
            with self.assertRaisesRegex(StaleBrokerTokenError,"interactive login"):
                adapter.orders()

    def test_kite_binary_parser_decodes_ltp_packet(self):
        packet=struct.pack(">II",408065,123450)
        payload=struct.pack(">H",1)+struct.pack(">H",len(packet))+packet
        ticks=parse_binary(payload)
        self.assertEqual(ticks[0]["instrument_token"],408065)
        self.assertEqual(ticks[0]["last_price"],1234.5)


if __name__ == "__main__":
    unittest.main()
