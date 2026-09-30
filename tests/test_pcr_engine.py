"""Unit tests for the Put-Call Ratio (PCR) Engine."""
import unittest
from backend.pcr_engine import calculate_pcr


class TestPCREngine(unittest.TestCase):
    def test_pcr_calculation_and_max_pain(self):
        chain_data = [
            # Strike 24000
            {"strike": 24000, "option_type": "CE", "open_interest": 10000, "volume": 5000},
            {"strike": 24000, "option_type": "PE", "open_interest": 40000, "volume": 15000},
            # Strike 24100
            {"strike": 24100, "option_type": "CE", "open_interest": 20000, "volume": 8000},
            {"strike": 24100, "option_type": "PE", "open_interest": 30000, "volume": 12000},
            # Strike 24200 (ATM)
            {"strike": 24200, "option_type": "CE", "open_interest": 50000, "volume": 25000},
            {"strike": 24200, "option_type": "PE", "open_interest": 50000, "volume": 25000},
            # Strike 24300
            {"strike": 24300, "option_type": "CE", "open_interest": 60000, "volume": 30000},
            {"strike": 24300, "option_type": "PE", "open_interest": 20000, "volume": 8000},
            # Strike 24400
            {"strike": 24400, "option_type": "CE", "open_interest": 80000, "volume": 40000},
            {"strike": 24400, "option_type": "PE", "open_interest": 10000, "volume": 4000},
        ]

        spot = 24210.0
        result = calculate_pcr(chain_data, spot)

        self.assertIn("total_pcr", result)
        self.assertIn("atm_pcr", result)
        self.assertIn("max_pain", result)
        self.assertIn("bias", result)

        total_ce = 10000 + 20000 + 50000 + 60000 + 80000  # 220,000
        total_pe = 40000 + 30000 + 50000 + 20000 + 10000  # 150,000
        expected_total_pcr = round(150000 / 220000, 4)  # 0.6818

        self.assertEqual(result["total_pcr"], expected_total_pcr)
        self.assertEqual(result["total_call_oi"], 220000)
        self.assertEqual(result["total_put_oi"], 150000)

        # Max Pain should be calculated near the highest clustered open interest
        self.assertGreaterEqual(result["max_pain"], 24000)
        self.assertLessEqual(result["max_pain"], 24400)

    def test_extreme_bias(self):
        bull_chain = [
            {"strike": 100, "option_type": "CE", "open_interest": 1000, "volume": 100},
            {"strike": 100, "option_type": "PE", "open_interest": 2000, "volume": 200},
        ]
        res = calculate_pcr(bull_chain, 100.0)
        self.assertEqual(res["bias"], "EXTREME_BULLISH_OVERSOLD")
        self.assertEqual(res["total_pcr"], 2.0)


if __name__ == "__main__":
    unittest.main()
