"""Tests for Institutional Index F&O Mapping and Derivatives Strategy Planner."""
import unittest
from datetime import date
from backend.derivatives import (
    INDEX_SPECS,
    LOT_SIZES,
    NSE_HOLIDAYS_2026,
    is_nse_trading_day,
    resolve_expiry,
    choose_derivative_strategy,
    build_derivative_plan,
    multi_leg_spread_catalog,
    build_index_spread_ticket,
)
from backend.service import derivatives_spreads


class TestDerivativesMapping(unittest.TestCase):
    def test_index_specifications(self):
        self.assertIn("NIFTY", INDEX_SPECS)
        self.assertIn("BANKNIFTY", INDEX_SPECS)
        self.assertIn("FINNIFTY", INDEX_SPECS)
        self.assertIn("MIDCPNIFTY", INDEX_SPECS)
        self.assertEqual(INDEX_SPECS["NIFTY"]["lot_size"], 75)
        self.assertEqual(INDEX_SPECS["BANKNIFTY"]["lot_size"], 30)
        self.assertEqual(INDEX_SPECS["FINNIFTY"]["lot_size"], 65)
        self.assertEqual(INDEX_SPECS["MIDCPNIFTY"]["lot_size"], 120)

    def test_holiday_resolution(self):
        # 2026-01-26 is Republic Day (Monday)
        self.assertIn("2026-01-26", NSE_HOLIDAYS_2026)
        self.assertFalse(is_nse_trading_day(date(2026, 1, 26)))
        # Weekend should not be trading day
        self.assertFalse(is_nse_trading_day(date(2026, 10, 3)))  # Saturday

    def test_expiry_resolution(self):
        # NIFTY weekly expiry is Thursday
        ref = date(2026, 9, 30)  # Wednesday
        exp = resolve_expiry("NIFTY", expiry_type="weekly", ref_date=ref)
        self.assertEqual(exp.weekday(), 3)  # Thursday Oct 1
        self.assertEqual(exp, date(2026, 10, 1))

        # FINNIFTY weekly expiry is Tuesday
        exp_fin = resolve_expiry("FINNIFTY", expiry_type="weekly", ref_date=ref)
        self.assertEqual(exp_fin.weekday(), 1)  # Tuesday Oct 6
        self.assertEqual(exp_fin, date(2026, 10, 6))

        # MIDCPNIFTY weekly expiry is Monday
        exp_mid = resolve_expiry("MIDCPNIFTY", expiry_type="weekly", ref_date=ref)
        self.assertEqual(exp_mid.weekday(), 0)  # Monday Oct 5
        self.assertEqual(exp_mid, date(2026, 10, 5))

    def test_multileg_catalog_nifty(self):
        catalog = multi_leg_spread_catalog("NIFTY", 24500.0, expiry_type="weekly")
        self.assertEqual(len(catalog), 9)
        strategies = {s["strategy"] for s in catalog}
        self.assertIn("Bull Call Spread", strategies)
        self.assertIn("Bull Put Spread", strategies)
        self.assertIn("Bear Put Spread", strategies)
        self.assertIn("Bear Call Spread", strategies)
        self.assertIn("Iron Condor", strategies)
        self.assertIn("Iron Butterfly", strategies)
        self.assertIn("Long Straddle", strategies)

        for spread in catalog:
            self.assertEqual(spread["lot_size"], 75)
            self.assertIn("greeks", spread)
            self.assertIn("net_delta", spread["greeks"])
            self.assertIn("fee_drag_pct", spread["risk"])
            # Index F&O fee drag should be small (<3%)
            self.assertLess(spread["risk"]["fee_drag_pct"], 3.0)

    def test_multileg_catalog_banknifty(self):
        catalog = multi_leg_spread_catalog("BANKNIFTY", 52150.0, expiry_type="weekly")
        self.assertEqual(len(catalog), 9)
        for spread in catalog:
            self.assertEqual(spread["lot_size"], 30)
            self.assertEqual(spread["strike_step"], 100)
            self.assertIn("net_delta", spread["greeks"])

    def test_build_index_spread_ticket(self):
        # Bullish signal
        ticket_bull = build_index_spread_ticket("NIFTY", 24500.0, signal=1, regime="trending_up")
        self.assertIsNotNone(ticket_bull)
        self.assertEqual(ticket_bull["strategy"], "Bull Call Spread")
        self.assertEqual(ticket_bull["signal"], 1)
        self.assertEqual(ticket_bull["status"], "PAPER_APPROVED")

        # Bearish signal
        ticket_bear = build_index_spread_ticket("BANKNIFTY", 52150.0, signal=-1, regime="trending_down")
        self.assertIsNotNone(ticket_bear)
        self.assertEqual(ticket_bear["strategy"], "Bear Put Spread")
        self.assertEqual(ticket_bear["signal"], -1)

        # Flat signal (0) returns None
        ticket_flat = build_index_spread_ticket("NIFTY", 24500.0, signal=0)
        self.assertIsNone(ticket_flat)

    def test_derivatives_spreads_service(self):
        res = derivatives_spreads("BANKNIFTY")
        self.assertEqual(res["symbol"], "BANKNIFTY")
        self.assertEqual(res["spreads_count"], 9)
        self.assertGreater(res["spot_price"], 30000.0)  # Correctly identifies BANKNIFTY not NIFTY

    def test_protective_collar_payoff_separation(self):
        plan = build_derivative_plan("NIFTY", 24500.0, "bullish", strategy="Protective Collar")
        self.assertEqual(plan["strategy"], "Protective Collar")
        # Net cashflow should reflect only option premiums (not the futures notional)
        self.assertLess(abs(plan["net_cashflow"]), 500.0)
        self.assertGreater(plan["risk"]["max_loss"], 0.0)
        self.assertLess(plan["risk"]["max_loss"], 10000.0)
        self.assertGreater(plan["risk"]["max_profit"], 0.0)
        self.assertLess(plan["risk"]["max_profit"], 10000.0)
        self.assertGreater(plan["risk"]["breakeven"], 24000.0)
        self.assertLess(plan["risk"]["breakeven"], 25000.0)



if __name__ == "__main__":
    unittest.main()
