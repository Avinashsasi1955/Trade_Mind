"""Unit tests for Volume Profile Engine, Shape Detection and Senior Trade Integration."""
from datetime import datetime, timedelta, timezone
import unittest

from backend.volume_profile import (
    calculate_volume_profile,
    detect_profile_shape,
    evaluate_volume_profile_verdict,
)


class TestVolumeProfile(unittest.TestCase):
    def test_volume_profile_poc_and_value_area(self):
        # 15 bars oscillating around 100 with massive volume at 100.0 (POC),
        # ranging from low 95.0 to high 105.0
        bars = []
        base = datetime(2026, 9, 28, 9, 15, tzinfo=timezone.utc)
        for i in range(15):
            bars.append({
                "time": (base + timedelta(minutes=i * 5)).isoformat(),
                "open": 99.0 + (i % 3) * 0.5,
                "high": 101.0 + (i % 2) * 1.5,
                "low": 97.0 - (i % 2) * 1.0,
                "close": 100.0,
                "volume": 50000 if i in (5, 6, 7) else 10000,
            })

        profile = calculate_volume_profile(bars, num_bins=30)
        self.assertTrue(profile["is_valid"])
        self.assertIsNotNone(profile["poc"])
        self.assertIsNotNone(profile["vah"])
        self.assertIsNotNone(profile["val"])
        self.assertGreaterEqual(profile["vah"], profile["poc"])
        self.assertLessEqual(profile["val"], profile["poc"])
        self.assertAlmostEqual(profile["poc"], 100.0, delta=1.5)
        # New: verify shape and confidence fields exist
        self.assertIn("shape", profile)
        self.assertIn("confidence", profile)
        self.assertIn("volume_quality_pct", profile)
        self.assertGreater(profile["confidence"], 0.0)

    def test_verdict_cautions_and_boosts(self):
        fake_profile = {
            "is_valid": True,
            "poc": 100.0,
            "vah": 103.0,
            "val": 97.0,
            "confidence": 80.0,
        }

        # Long trade at 104.5 (above VAH) -> should warn of VAH extension
        long_ext = evaluate_volume_profile_verdict(104.5, 1, fake_profile)
        self.assertEqual(long_ext["verdict"], "CAUTION_VAH_EXTENSION")
        self.assertTrue(len(long_ext["cautions"]) > 0)
        self.assertLess(long_ext["confidence_delta"], 0)

        # Long trade at 97.2 (near VAL) -> should give high conviction bounce boost
        long_bounce = evaluate_volume_profile_verdict(97.2, 1, fake_profile)
        self.assertEqual(long_bounce["verdict"], "HIGH_CONVICTION_VAL_BOUNCE")
        self.assertGreater(long_bounce["confidence_delta"], 0)

        # Short trade at 95.5 (below VAL) -> should warn of VAL extension
        short_ext = evaluate_volume_profile_verdict(95.5, -1, fake_profile)
        self.assertEqual(short_ext["verdict"], "CAUTION_VAL_EXTENSION")

    def test_verdict_includes_shape_fields(self):
        """Verify that the verdict response includes shape metadata."""
        fake_profile = {
            "is_valid": True,
            "poc": 100.0,
            "vah": 103.0,
            "val": 97.0,
            "shape": "P",
            "shape_label": "P_SHORT_COVERING",
            "shape_trade_implication": "BULLISH_DIP_BUY_AT_VAL",
            "confidence": 70.0,
        }
        result = evaluate_volume_profile_verdict(100.0, 1, fake_profile)
        self.assertEqual(result["shape"], "P")
        self.assertEqual(result["shape_label"], "P_SHORT_COVERING")
        self.assertIn("vp_confidence", result)

    def test_no_profile_data_verdict(self):
        """Ensure graceful handling of empty/invalid profiles."""
        result = evaluate_volume_profile_verdict(100.0, 1, None)
        self.assertEqual(result["verdict"], "NO_PROFILE_DATA")
        self.assertIsNone(result.get("shape"))

        result2 = evaluate_volume_profile_verdict(100.0, -1, {"is_valid": False})
        self.assertEqual(result2["verdict"], "NO_PROFILE_DATA")


class TestProfileShapeDetection(unittest.TestCase):
    """Tests for detect_profile_shape() and shape-aware VP calculation."""

    def _make_bars(self, count, price_center=100.0, price_range=10.0,
                   volume_fn=None):
        """Helper to create synthetic bars with configurable volume profiles."""
        bars = []
        base = datetime(2026, 9, 28, 9, 15, tzinfo=timezone.utc)
        step = price_range / max(1, count)
        for i in range(count):
            price = price_center - price_range / 2 + i * step
            vol = volume_fn(i, count) if volume_fn else 10000
            bars.append({
                "time": (base + timedelta(minutes=i * 5)).isoformat(),
                "open": price - 0.5,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.3,
                "volume": max(0, int(vol)),
            })
        return bars

    def test_d_shape_balanced(self):
        """Bell-curve volume distribution should produce D-shape."""
        import math
        bars = self._make_bars(
            60, price_center=100.0, price_range=10.0,
            volume_fn=lambda i, n: int(50000 * math.exp(-0.5 * ((i - n/2) / (n/6))**2))
        )
        profile = calculate_volume_profile(bars, num_bins=30)
        self.assertTrue(profile["is_valid"])
        self.assertEqual(profile["shape"], "D")
        self.assertEqual(profile["shape_label"], "D_BALANCED")
        self.assertIn("RANGE_BOUND", profile["shape_trade_implication"])

    def test_p_shape_short_covering(self):
        """Volume concentrated at higher prices should produce P-shape."""
        bars = self._make_bars(
            60, price_center=100.0, price_range=10.0,
            volume_fn=lambda i, n: int(5000 + 50000 * (i / n) ** 2)
        )
        profile = calculate_volume_profile(bars, num_bins=30)
        self.assertTrue(profile["is_valid"])
        self.assertEqual(profile["shape"], "P")
        self.assertEqual(profile["shape_label"], "P_SHORT_COVERING")
        self.assertGreater(profile["skewness"], 0.0)

    def test_b_shape_long_liquidation(self):
        """Volume concentrated at lower prices should produce b-shape."""
        bars = self._make_bars(
            60, price_center=100.0, price_range=10.0,
            volume_fn=lambda i, n: int(5000 + 50000 * ((n - i) / n) ** 2)
        )
        profile = calculate_volume_profile(bars, num_bins=30)
        self.assertTrue(profile["is_valid"])
        self.assertEqual(profile["shape"], "b")
        self.assertEqual(profile["shape_label"], "b_LONG_LIQUIDATION")
        self.assertLess(profile["skewness"], 0.0)

    def test_developing_profile(self):
        """Less than 15 bars should return DEVELOPING shape."""
        bars = self._make_bars(10, price_center=100.0, price_range=5.0)
        profile = calculate_volume_profile(bars, num_bins=20)
        self.assertTrue(profile["is_valid"])
        self.assertEqual(profile["shape"], "DEVELOPING")
        self.assertEqual(profile["shape_label"], "DEVELOPING_PROFILE")

    def test_insufficient_bars(self):
        """Less than 3 bars should return invalid profile."""
        profile = calculate_volume_profile([{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}])
        self.assertFalse(profile["is_valid"])
        self.assertEqual(profile["shape_label"], "INSUFFICIENT_DATA")

    def test_zero_volume_bars_reduce_confidence(self):
        """Bars with zero volume should reduce VP confidence."""
        bars = self._make_bars(
            60, price_center=100.0, price_range=10.0,
            volume_fn=lambda i, n: 10000 if i % 2 == 0 else 0
        )
        profile = calculate_volume_profile(bars, num_bins=30)
        self.assertTrue(profile["is_valid"])
        # 50% of bars have zero volume → volume quality should be ~50%
        self.assertLess(profile["volume_quality_pct"], 55.0)
        self.assertGreater(profile["volume_quality_pct"], 45.0)

    def test_shape_boost_p_shape_bullish(self):
        """P-shape should boost bullish trades and penalise bearish trades."""
        fake_profile = {
            "is_valid": True,
            "poc": 104.0,
            "vah": 106.0,
            "val": 95.0,
            "shape": "P",
            "shape_label": "P_SHORT_COVERING",
            "shape_trade_implication": "BULLISH_DIP_BUY_AT_VAL",
            "confidence": 75.0,
        }
        # Bullish trade in value area below POC → should get shape boost
        bullish = evaluate_volume_profile_verdict(98.0, 1, fake_profile)
        self.assertGreater(bullish["confidence_delta"], 0)

        # Bearish trade → P-shape should penalise
        bearish = evaluate_volume_profile_verdict(98.0, -1, fake_profile)
        # b/D shape doesn't apply, so base is VALUE_AREA_DISCOUNT_SHORT → 0
        # shape penalty should make delta negative
        has_shape_caution = any("P (short covering) conflicts" in c for c in bearish["cautions"])
        self.assertTrue(has_shape_caution)

    def test_shape_boost_b_shape_bearish(self):
        """b-shape should boost bearish trades and penalise bullish trades."""
        fake_profile = {
            "is_valid": True,
            "poc": 96.0,
            "vah": 105.0,
            "val": 94.0,
            "shape": "b",
            "shape_label": "b_LONG_LIQUIDATION",
            "shape_trade_implication": "BEARISH_SELL_RALLY_AT_VAH",
            "confidence": 75.0,
        }
        bearish = evaluate_volume_profile_verdict(102.0, -1, fake_profile)
        has_support = any("b (long liquidation) supports" in c for c in bearish["cautions"])
        self.assertTrue(has_support)

    def test_low_confidence_never_boosts(self):
        """VP confidence below 30% should clamp boost to zero or below."""
        fake_profile = {
            "is_valid": True,
            "poc": 100.0,
            "vah": 103.0,
            "val": 97.0,
            "shape": "P",
            "shape_label": "P_SHORT_COVERING",
            "shape_trade_implication": "BULLISH_DIP_BUY_AT_VAL",
            "confidence": 20.0,  # Very low
        }
        result = evaluate_volume_profile_verdict(97.2, 1, fake_profile)
        self.assertLessEqual(result["confidence_delta"], 0.0)
        has_low_conf = any("Low Volume Profile confidence" in c for c in result["cautions"])
        self.assertTrue(has_low_conf)


if __name__ == "__main__":
    unittest.main()

