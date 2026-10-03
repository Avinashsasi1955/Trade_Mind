"""Unit tests for Phase C.1: 3-Regime Router & Validation Checkpoint Tracker."""
import unittest
from datetime import date
from sqlalchemy import create_engine

from backend.regime_router import (
    REGIME_TREND,
    REGIME_RANGE,
    REGIME_VOL_DEFENSE,
    RegimeRouter,
    calculate_adx,
    calculate_atr,
    calculate_ema,
    route_order_intent,
    ensure_checkpoint_schema,
    get_completed_validation_count,
    record_validation_checkpoint,
    seed_historical_checkpoints_if_empty,
)


def _generate_synthetic_bars(count: int, pattern: str = "trend_up", base: float = 24500.0):
    bars = []
    price = base
    for i in range(count):
        if pattern == "trend_up":
            # Consistent strong upward impulse
            open_p = price
            close_p = open_p + 15.0 + (i * 0.5)
            high_p = close_p + 5.0
            low_p = open_p - 2.0
            vol = 15000 + i * 200
        elif pattern == "trend_down":
            # Consistent downward impulse
            open_p = price
            close_p = open_p - 15.0 - (i * 0.5)
            high_p = open_p + 2.0
            low_p = close_p - 5.0
            vol = 15000 + i * 200
        elif pattern == "range":
            # Oscillating tight range
            wave = 6.0 if (i % 2 == 0) else -6.0
            open_p = base + wave
            close_p = base - wave
            high_p = max(open_p, close_p) + 3.0
            low_p = min(open_p, close_p) - 3.0
            vol = 8000
        elif pattern == "vol_shock":
            # Normal then sudden explosive 100 pt candle
            if i < count - 1:
                open_p = price
                close_p = open_p + 2.0
                high_p = close_p + 2.0
                low_p = open_p - 2.0
                vol = 5000
            else:
                open_p = price
                close_p = open_p + 150.0  # Massive shock
                high_p = close_p + 20.0
                low_p = open_p - 10.0
                vol = 95000
        bars.append({
            "time": f"2026-09-30T10:{i:02d}:00Z",
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": vol,
        })
        price = close_p
    return bars


class TestRegimeRouter(unittest.TestCase):
    def setUp(self):
        self.router = RegimeRouter(adx_trend_threshold=22.0, adx_range_threshold=18.0)
        self.engine = create_engine("sqlite:///:memory:")

    def test_technical_indicators(self):
        closes = [100.0 + i for i in range(30)]
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]

        ema = calculate_ema(closes, 9)
        self.assertEqual(len(ema), 30)
        self.assertAlmostEqual(ema[-1], closes[-1], delta=5.0)

        atr = calculate_atr(highs, lows, closes, 14)
        self.assertGreater(atr, 0.0)

        adx = calculate_adx(highs, lows, closes, 14)
        self.assertGreaterEqual(adx, 0.0)

    def test_classify_trend_continuation_up(self):
        bars = _generate_synthetic_bars(35, pattern="trend_up")
        result = self.router.classify(bars, symbol="NIFTY")
        self.assertEqual(result["regime"], REGIME_TREND)
        self.assertEqual(result["direction"], "BULLISH")
        self.assertIn("Bull Call Spread", result["recommended_strategies"])
        self.assertEqual(result["risk_policy"]["sizing_factor"], 1.0)
        self.assertTrue(result["risk_policy"]["allow_new_entries"])

    def test_classify_trend_continuation_down(self):
        bars = _generate_synthetic_bars(35, pattern="trend_down")
        result = self.router.classify(bars, symbol="BANKNIFTY")
        self.assertEqual(result["regime"], REGIME_TREND)
        self.assertEqual(result["direction"], "BEARISH")
        self.assertIn("Bear Put Spread", result["recommended_strategies"])

    def test_classify_range_mean_reversion(self):
        bars = _generate_synthetic_bars(35, pattern="range")
        result = self.router.classify(bars, symbol="NIFTY")
        self.assertEqual(result["regime"], REGIME_RANGE)
        self.assertIn("Iron Condor", result["recommended_strategies"])
        self.assertEqual(result["risk_policy"]["trailing_giveback_pct"], 0.20)

    def test_classify_volatility_defense_shock(self):
        bars = _generate_synthetic_bars(35, pattern="vol_shock")
        result = self.router.classify(bars, symbol="NIFTY")
        self.assertEqual(result["regime"], REGIME_VOL_DEFENSE)
        self.assertEqual(result["risk_policy"]["sizing_factor"], 0.5)
        self.assertFalse(result["risk_policy"]["allow_new_entries"])

    def test_route_order_intent(self):
        # Trending up + positive signal -> Bull Call Spread
        bars_up = _generate_synthetic_bars(35, pattern="trend_up")
        order_up = route_order_intent("NIFTY", 24500.0, signal=1, bars=bars_up, router=self.router)
        self.assertEqual(order_up["chosen_strategy"], "Bull Call Spread")
        self.assertTrue(order_up["allow_execution"])
        self.assertIsNotNone(order_up["plan"])

        # Range + neutral signal (0) -> Iron Condor
        bars_range = _generate_synthetic_bars(35, pattern="range")
        order_range = route_order_intent("NIFTY", 24500.0, signal=0, bars=bars_range, router=self.router)
        self.assertEqual(order_range["chosen_strategy"], "Iron Condor")

    def test_validation_checkpoint_database_seeding(self):
        with self.engine.begin() as conn:
            ensure_checkpoint_schema(conn)
            initial = get_completed_validation_count(conn)
            self.assertEqual(initial, 0)

            # Seed the 20 historical checkpoints
            seeded = seed_historical_checkpoints_if_empty(conn, model_version="direction-v3.0")
            self.assertEqual(seeded, 20)
            self.assertEqual(get_completed_validation_count(conn), 20)

            # Record tomorrow's checkpoint (#21)
            record_validation_checkpoint(
                connection=conn,
                checkpoint_number=21,
                checkpoint_type="SHADOW_SESSION",
                model_version="direction-v3.0",
                session_date=date(2026, 10, 1),
                trades_count=12,
                net_pnl=1850.50,
                profit_factor=1.35,
                win_rate_pct=58.3,
                max_drawdown_pct=-0.04,
                metadata={"session": "2026-10-01 Live Shadow Day 1"}
            )
            total = get_completed_validation_count(conn)
            self.assertEqual(total, 21)
            self.assertGreaterEqual(total, 21)


if __name__ == "__main__":
    unittest.main()
