"""Unit tests for Phase C.2 Autonomous Spider Bot."""

import unittest
from backend.spider_bot import AutonomousSpiderBot, PortfolioGreeks


class TestSpiderBot(unittest.TestCase):
    def setUp(self):
        self.bot = AutonomousSpiderBot(database_url=None)

    def test_dynamic_web_geometry_low_vix(self):
        geom = self.bot.calculate_web_geometry(spot_price=24500.0, underlying_symbol="NIFTY", vix=11.5)
        self.assertEqual(geom["underlying"], "NIFTY")
        self.assertEqual(geom["atm_strike"], 24500)
        self.assertEqual(geom["regime"], "COMPRESSED_LOW_VOL")
        self.assertEqual(geom["wing_distance"], 50)
        self.assertEqual(geom["recommended_short_call"], 24550)
        self.assertEqual(geom["recommended_short_put"], 24450)

    def test_dynamic_web_geometry_elevated_vix(self):
        geom = self.bot.calculate_web_geometry(spot_price=24500.0, underlying_symbol="NIFTY", vix=19.0)
        self.assertEqual(geom["regime"], "EXPANDED_HIGH_VOL")
        self.assertEqual(geom["wing_distance"], 100)
        self.assertEqual(geom["recommended_short_call"], 24600)
        self.assertEqual(geom["recommended_short_put"], 24400)

    def test_portfolio_greeks_delta_neutral(self):
        # Balanced delta positions
        positions = [
            {"instrument_type": "CE", "side": "BUY", "quantity": 75, "greeks": {"delta": 0.30, "gamma": 0.001, "theta": -5.0, "vega": 12.0}},
            {"instrument_type": "PE", "side": "BUY", "quantity": 75, "greeks": {"delta": -0.28, "gamma": 0.001, "theta": -5.0, "vega": 12.0}},
        ]
        greeks = self.bot.evaluate_portfolio_greeks(open_audits=positions)
        self.assertTrue(greeks.is_delta_neutral)
        self.assertFalse(greeks.needs_rebalance)
        self.assertIsNone(greeks.rebalance_action)

    def test_portfolio_greeks_imbalance_triggers_rebalance(self):
        # Severe bullish delta imbalance
        positions = [
            {"instrument_type": "CE", "side": "BUY", "quantity": 150, "greeks": {"delta": 0.70, "gamma": 0.002, "theta": -10.0, "vega": 20.0}},
        ]
        greeks = self.bot.evaluate_portfolio_greeks(open_audits=positions)
        self.assertFalse(greeks.is_delta_neutral)
        self.assertTrue(greeks.needs_rebalance)
        self.assertEqual(greeks.rebalance_action, "HEDGE_BEARISH_DELTA")
        
        adjustments = self.bot.formulate_rebalancing_plan(greeks, spot_price=24500.0, underlying="NIFTY")
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0].action_type, "ROLL_UP_PUT")

    def test_build_spider_spread_iron_condor(self):
        spread = self.bot.build_spider_spread(underlying="NIFTY", spot=24500.0, strategy_name="Iron Condor", vix=14.0)
        self.assertEqual(spread["strategy"], "Iron Condor")
        self.assertEqual(len(spread["legs"]), 4)
        self.assertTrue(spread["is_sebi_defined_risk"])
        self.assertGreater(spread["margin_benefit_pct"], 50.0)
        self.assertLessEqual(spread["max_loss_rupees"], 2000.0)


if __name__ == "__main__":
    unittest.main()
