import sqlite3
import unittest

from backend.agent import TradingAgent
from backend.database import SCHEMA, create_user, seed_demo_portfolio
from backend.market import market_snapshot
from backend.security import create_token, decode_token, hash_password, verify_password
from backend.service import dashboard, run_agent, update_settings
from backend.service import stock_analysis
from backend.technical_analysis import GLOSSARY, analyse_structure
from backend.model_provider import provider_catalog
from backend.sentiment import analyse_sentiment
from backend.service import market_update, sentiment_dashboard
from backend.ai_gateway import AIGateway, gateway_status
from backend.charting import chart_data
from backend.backtesting import run_backtest
from backend.security_master import search_securities, security_master_stats


class NiveshSystemTests(unittest.TestCase):
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

    def test_manual_agent_run_persists_audit_record(self):
        result = run_agent(self.db, self.user_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM watchlist_snapshots").fetchone()[0], 4)

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
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stock_analyses").fetchone()[0], 1)

    def test_model_catalog_exposes_free_and_paid_routes(self):
        catalog = provider_catalog()
        providers = {item["id"]: item for item in catalog["providers"]}
        self.assertTrue(providers["gemini"]["free_tier"])
        self.assertTrue(providers["ollama"]["free_tier"])
        self.assertFalse(providers["anthropic"]["free_tier"])
        self.assertFalse(providers["openai"]["free_tier"])

    def test_sentiment_has_auditable_components(self):
        market = market_snapshot()
        result = analyse_sentiment(market[0], market)
        self.assertIn(result["label"], {"bullish", "neutral", "bearish"})
        self.assertEqual(set(result["components"]), {"news", "price_action", "volume", "market_breadth"})
        self.assertEqual(result["coverage"]["articles"], len(result["headlines"]))

    def test_sentiment_dashboard_and_market_updater(self):
        sentiment = sentiment_dashboard(self.db, self.user_id, "RELIANCE")
        update = market_update(self.db, self.user_id, 42)
        next_update = market_update(self.db, self.user_id, 43)
        self.assertEqual(sentiment["selected"]["symbol"], "RELIANCE")
        self.assertGreaterEqual(len(sentiment["leaders"]), 4)
        self.assertEqual(update["breadth"]["advancing"] + update["breadth"]["declining"], len(update["quotes"]))
        self.assertNotEqual(update["indices"][0]["price"], next_update["indices"][0]["price"])

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
        self.assertEqual(len(chart["candles"]), 180)
        self.assertEqual(len(chart["indicators"]["ema20"]), 180)
        self.assertEqual(len(chart["indicators"]["rsi14"]), 180)
        self.assertEqual(set(chart["indicators"]["bollinger"]), {"upper", "middle", "lower"})

    def test_backtest_uses_chronological_holdout_and_costs(self):
        report = run_backtest("RELIANCE", "ensemble", 1_000_000)
        self.assertTrue(report["methodology"]["look_ahead_protection"])
        self.assertEqual(report["methodology"]["costs_bps_per_trade"], 12)
        self.assertGreaterEqual(report["period"]["sessions"], 240)
        self.assertIn("directional_accuracy", report["out_of_sample"])
        self.assertIn(report["data_mode"], {"simulated", "historical"})


if __name__ == "__main__":
    unittest.main()
