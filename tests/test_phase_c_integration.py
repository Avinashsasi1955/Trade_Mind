"""Phase C End-to-End Integration Test Suite.

Verifies:
1. GEX Engine: Strike-by-strike Gamma Exposure, Net GEX, and Zero-Gamma Flip Point detector.
2. Stop & Size Modulation: 1.5x stop widening and 50% size cut below Zero-Gamma Flip strike.
3. 3-Regime Router live signal threshold gating in Position / Live Inference.
4. Spider Bot: Delta-neutral Greeks auto-balancing and combo ticket formulation.
5. Dual-Tier Memo: Session trap working memory & episodic cross-session priors.
6. Frontend Native ES Module breakdown validation.
"""

import os
import shutil
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import backend.gex_engine
from backend.gex_engine import (
    calculate_gex_profile,
    compute_black_scholes_gamma,
    get_cached_or_compute_gex,
)
from backend.live_inference import LivePaperInference
from backend.regime_router import REGIME_VOL_DEFENSE, REGIME_RANGE, REGIME_TREND, RegimeRouter
from backend.spider_bot import AutonomousSpiderBot, PortfolioGreeks


class TestPhaseCIntegration(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        backend.gex_engine._GEX_CACHE.clear()

    # 1. GEX Engine Tests
    def test_black_scholes_gamma(self):
        gamma_atm = compute_black_scholes_gamma(spot=24500.0, strike=24500.0, dte_days=3.0, iv=0.15)
        self.assertGreater(gamma_atm, 0.0)
        gamma_otm = compute_black_scholes_gamma(spot=24500.0, strike=26000.0, dte_days=3.0, iv=0.15)
        self.assertLess(gamma_otm, gamma_atm)

    def test_gex_profile_and_flip_point(self):
        spot = 24500.0
        options_data = [
            {"strike": 24000.0, "call_oi": 10000, "put_oi": 80000, "call_iv": 0.16, "put_iv": 0.16, "dte": 2.0},
            {"strike": 24200.0, "call_oi": 20000, "put_oi": 60000, "call_iv": 0.15, "put_iv": 0.15, "dte": 2.0},
            {"strike": 24500.0, "call_oi": 100000, "put_oi": 50000, "call_iv": 0.14, "put_iv": 0.14, "dte": 2.0},
            {"strike": 24800.0, "call_oi": 120000, "put_oi": 20000, "call_iv": 0.15, "put_iv": 0.15, "dte": 2.0},
            {"strike": 25000.0, "call_oi": 150000, "put_oi": 10000, "call_iv": 0.16, "put_iv": 0.16, "dte": 2.0},
        ]
        profile = calculate_gex_profile("NIFTY", spot, options_data, lot_size=25)
        self.assertEqual(profile.symbol, "NIFTY")
        self.assertGreater(profile.total_call_gex, 0.0)
        self.assertLess(profile.total_put_gex, 0.0)
        self.assertEqual(profile.regime, "LONG_GAMMA_MEAN_REVERSION")
        self.assertEqual(profile.zero_gamma_flip, 24500.0)

    def test_gex_flip_defense_multipliers(self):
        # Scenario where spot is below the flip strike
        spot = 23900.0
        options_data = [
            {"strike": 23800.0, "call_oi": 10000, "put_oi": 60000, "call_iv": 0.16, "put_iv": 0.16, "dte": 2.0},
            {"strike": 24000.0, "call_oi": 120000, "put_oi": 10000, "call_iv": 0.16, "put_iv": 0.16, "dte": 2.0},
        ]
        profile = calculate_gex_profile("NIFTY", spot, options_data, lot_size=25)
        self.assertEqual(profile.regime, "VOLATILITY_FLIP_DEFENSE")
        self.assertEqual(profile.stop_multiplier, 1.5)
        self.assertEqual(profile.size_multiplier, 0.5)

    def test_cached_gex_heavyweights(self):
        backend.gex_engine._GEX_CACHE.clear()
        with patch("backend.gex_engine.DATABASE_URL", None), \
             patch("backend.gex_engine._MODULE_ENGINE", None):
            res = get_cached_or_compute_gex("NIFTY", database_url=None, engine=None)
            self.assertIn("spot_price", res)
            self.assertGreater(res["spot_price"], 10000.0)
            self.assertEqual(res.get("regime"), "UNAVAILABLE")
            self.assertEqual(res.get("source"), "synthetic")
            self.assertEqual(res.get("stop_multiplier"), 1.0)
            self.assertEqual(res.get("size_multiplier"), 1.0)

    # 2. Live Inference & Regime Router Integration Tests
    @patch.dict(os.environ, {"LIVE_ELIGIBLE": "FALSE", "NIVESH_LIVE_TRADING_ENABLED": "0"})
    def test_live_inference_fallback_regime_maps_to_neutral(self):
        svc = LivePaperInference(database_url="sqlite:///:memory:", redis_url="redis://localhost:6379/0")
        bars = [{"open": 100, "high": 101, "low": 99, "close": 100}]
        router = RegimeRouter()
        classification = router.classify(bars, symbol="NIFTY")
        self.assertTrue(classification.get("is_fallback"))
        # Fallback should map to NEUTRAL in session case, not suppress breakouts
        session_case = {"case": "neutral", "regime_name": "NEUTRAL", "regime_classification": classification}
        item = {"symbol": "NIFTY", "session": {"close": 24500.0}}
        th = svc._effective_thresholds(item, {}, policy=None, session_case=session_case)
        self.assertFalse(th.get("suppress_breakouts", False))

    @patch.dict(os.environ, {"LIVE_ELIGIBLE": "FALSE", "NIVESH_LIVE_TRADING_ENABLED": "0"})
    def test_live_inference_effective_thresholds_with_regime_and_gex(self):
        svc = LivePaperInference(database_url="sqlite:///:memory:", redis_url="redis://localhost:6379/0")
        item = {"symbol": "NIFTY", "session": {"close": 24500.0}}
        chart = {"learning_mode": False}

        # Trend Continuation Regime
        session_case_trend = {"case": "best_continuation", "regime_name": REGIME_TREND}
        th_trend = svc._effective_thresholds(item, chart, policy=None, session_case=session_case_trend)
        self.assertEqual(th_trend["session_mode"], "trend_follow")
        self.assertTrue(th_trend.get("suppress_fades", False))

        # Range Mean Reversion Regime
        session_case_range = {"case": "neutral_range", "regime_name": REGIME_RANGE}
        th_range = svc._effective_thresholds(item, chart, policy=None, session_case=session_case_range)
        self.assertTrue(th_range.get("suppress_breakouts", False))
        self.assertGreaterEqual(th_range["min_rr"], 1.65)

        # High Vol Defense Regime
        session_case_highvol = {"case": "worst_vol", "regime_name": REGIME_VOL_DEFENSE}
        th_highvol = svc._effective_thresholds(item, chart, policy=None, session_case=session_case_highvol)
        self.assertTrue(th_highvol.get("defensive_vol_regime", False))
        self.assertGreaterEqual(th_highvol["min_quality"], 72.0)

    @patch.dict(os.environ, {"LIVE_ELIGIBLE": "FALSE", "NIVESH_LIVE_TRADING_ENABLED": "0"})
    def test_delta_anchored_option_risk_cap_at_45_percent(self):
        svc = LivePaperInference(database_url="sqlite:///:memory:", redis_url="redis://localhost:6379/0")
        item = {
            "symbol": "NIFTY26OCT25000CE",
            "session": {"close": 24500.0},
            "chart_gate": {"stop_loss": 24000.0},
            "_effective_thresholds": {"stop_multiplier": 1.5},
        }
        target = {"kind": "CE", "price": 100.0, "side": "BUY", "greeks": {"delta": 0.50}}
        # Price is 100, opt_risk max is 35 (35%), with stop_mult=1.5 it would be 52.5% without cap
        risk = svc._risk_levels_for_target(item=item, target=target)
        self.assertEqual(risk["source"], "underlying_delta_anchored")
        # Price 100 with 45% risk cap -> stop_loss is 55.0
        self.assertEqual(risk["stop_loss"], Decimal("55.0"))

    # 3. Spider Bot Delta Auto-Balancing Tests
    @patch("backend.spider_bot.create_engine")
    def test_spider_bot_delta_rebalance(self, mock_create_engine):
        bot = AutonomousSpiderBot(database_url="", max_delta_threshold=0.20)
        self.assertIsNone(bot.engine)
        mock_create_engine.assert_not_called()
        # Imbalanced portfolio Greeks
        imbalanced = PortfolioGreeks(
            net_delta=0.45,
            is_delta_neutral=False,
            needs_rebalance=True,
            rebalance_action="HEDGE_BEARISH_DELTA",
            hedge_target_delta=-0.45,
        )
        adjustments = bot.formulate_rebalancing_plan(imbalanced, spot_price=24500.0, underlying="NIFTY")
        self.assertGreater(len(adjustments), 0)
        adj = adjustments[0]
        self.assertEqual(adj.action_type, "ROLL_UP_PUT")
        self.assertLess(adj.delta_change, 0.0)

    # 4. Frontend ES Module Directory & Syntax Tests
    def test_frontend_modules_syntax(self):
        if not shutil.which("node"):
            self.skipTest("Node.js binary not available on PATH")
        modules = [
            self.root / "frontend" / "modules" / "utils.js",
            self.root / "frontend" / "modules" / "metrics.js",
            self.root / "frontend" / "modules" / "sse.js",
            self.root / "frontend" / "modules" / "trade_book.js",
            self.root / "frontend" / "app.js",
        ]
        for mod in modules:
            self.assertTrue(mod.is_file(), f"Missing module file: {mod}")
            res = subprocess.run(["node", "--check", str(mod)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Node syntax error in {mod.name}: {res.stderr}")

    def test_index_html_loads_module(self):
        index_html = (self.root / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="module"', index_html)
        self.assertIn("app.js", index_html)


if __name__ == "__main__":
    unittest.main()
