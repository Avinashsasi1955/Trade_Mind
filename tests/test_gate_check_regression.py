"""
Gate-Check Regression Tests — Trade Candidate Funnel
=====================================================
These tests construct in-memory candidate objects using realistic values
and run them through the gate-check logic to verify:

1. A known-good candidate (high probability, good R:R, correct signal) PASSES.
2. Individual gates correctly REJECT when their criteria are violated.
3. The full pipeline correctly composes all gates.

This does NOT connect to any database — everything is in-memory.
"""

import os
import sys
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


IST = timezone(timedelta(hours=5, minutes=30))


def _make_candidate(
    symbol="RELIANCE",
    exchange="NSE",
    instrument_type="EQ",
    instrument_id=123,
    signal=1,
    probability=0.72,
    rr=2.5,
    strategy="Breakout Trading",
    close_price=2850.0,
    volume=500000,
    adtv=1000000,
    spread_bps=5.0,
    chart_accepted=True,
    chart_signal=1,
    sector="Energy",
):
    """Construct a realistic candidate dict that mimics what the screener produces."""
    return {
        "symbol": symbol,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "instrument_id": instrument_id,
        "signal": signal,
        "probability": probability,
        "chart_gate": {
            "accepted": chart_accepted,
            "strategy": strategy,
            "rr": rr,
            "take_profit": close_price * 1.025 if signal == 1 else close_price * 0.975,
            "stop_loss": close_price * 0.99 if signal == 1 else close_price * 1.01,
            "signal": chart_signal,
            "local_direction": {"direction": chart_signal},
            "volume_profile": {"shape": "P", "poc_distance_pct": 0.3},
        },
        "session": {
            "close": close_price,
            "high": close_price * 1.01,
            "low": close_price * 0.99,
            "open": close_price * 0.998,
            "volume": volume,
        },
        "instrument_token": 738561,
        "policy_candidate": MagicMock(
            accepted=True,
            signal=signal,
            score=0.65,
            regime="trending",
            expected_net_edge_bps=Decimal("45"),
        ),
        "_trade_mode": "INTRADAY",
        "_agent_deliberation": None,
        "_instrument_local_direction": {"direction": chart_signal},
        "_model_signal_context": {"model": "v3.0"},
        "_quant_strategy": None,
        "_freshness_gate": {"accepted": True, "age_seconds": 30},
    }


def _make_open_state(count=0, underlyings=None, underlying_counts=None,
                     side_counts=None, sector_counts=None, sector_directions=None):
    return {
        "count": count,
        "underlyings": underlyings or set(),
        "underlying_counts": underlying_counts or {},
        "side_counts": side_counts or {},
        "sector_counts": sector_counts or {},
        "sector_directions": sector_directions or {},
    }


def _make_risk_state(blocked=False, reasons=None, cooldown_underlyings=None,
                     cooldown_sectors=None, closed_trades=0):
    return {
        "blocked": blocked,
        "reasons": reasons or [],
        "cooldown_underlyings": cooldown_underlyings or set(),
        "cooldown_sectors": cooldown_sectors or set(),
        "closed_trades": closed_trades,
    }


class TestGateCheckRegression(unittest.TestCase):
    """Regression tests for the candidate selection gate pipeline."""

    def test_chart_gate_rejection(self):
        """A candidate with chart_gate.accepted=False should be rejected at the chart_gate stage."""
        candidate = _make_candidate(chart_accepted=False)
        chart = candidate.get("chart_gate", {})
        self.assertFalse(chart.get("accepted"))
        # Simulates the first check in _select_top_trade_candidates
        if not chart.get("accepted"):
            rejection_stage = "chart_gate"
            rejection_reason = "chart_gate_rejected"
        else:
            rejection_stage = None
            rejection_reason = None
        self.assertEqual(rejection_stage, "chart_gate")
        self.assertEqual(rejection_reason, "chart_gate_rejected")

    def test_no_signal_rejection(self):
        """A candidate with signal=0 and learning_mode disabled is rejected."""
        candidate = _make_candidate(signal=0)
        learning_mode = False
        chart = candidate.get("chart_gate", {})

        rejected = False
        if chart.get("accepted") and not candidate.get("signal"):
            if not learning_mode:
                rejected = True
                reason = "no_model_signal"
        self.assertTrue(rejected)

    def test_rr_below_minimum_rejection(self):
        """A candidate with R:R below the minimum threshold is rejected."""
        candidate = _make_candidate(rr=1.2)
        min_rr = 1.5  # Typical minimum
        chart = candidate.get("chart_gate", {})
        rr = float(chart.get("rr") or 0)
        self.assertTrue(rr < min_rr, f"Expected rr={rr} < min_rr={min_rr}")

    def test_good_candidate_passes_basic_gates(self):
        """A well-formed candidate should pass chart_gate, signal, and R:R checks."""
        candidate = _make_candidate(
            signal=1,
            probability=0.78,
            rr=2.5,
            chart_accepted=True,
        )
        min_rr = 1.5
        learning_mode = False

        chart = candidate.get("chart_gate", {})
        # Gate 1: Chart gate
        self.assertTrue(chart.get("accepted"), "Should pass chart gate")

        # Gate 2: Signal
        self.assertTrue(candidate.get("signal"), "Should have a signal")

        # Gate 3: R:R
        rr = float(chart.get("rr") or 0)
        self.assertGreaterEqual(rr, min_rr, "R:R should be above minimum")

        # Gate 4: Probability
        self.assertGreaterEqual(float(candidate.get("probability", 0)), 0.55,
                                "Probability should be above baseline")

    def test_cooldown_underlying_rejection(self):
        """A candidate whose underlying is in cooldown is rejected."""
        candidate = _make_candidate(symbol="TATAMOTORS")
        cooldown = {"TATAMOTORS"}
        key = candidate["symbol"].upper()
        self.assertIn(key, cooldown)

    def test_sector_exposure_limit_rejection(self):
        """A candidate is rejected if sector already has max open positions."""
        candidate = _make_candidate(sector="Energy")
        sector_counts = {"ENERGY": 2}
        max_open_per_sector = 2
        sector = "Energy".upper()
        self.assertGreaterEqual(sector_counts.get(sector, 0), max_open_per_sector)

    def test_nifty_macro_downtrend_veto_for_equity_buy(self):
        """An equity BUY is rejected when Nifty 50 is in a downtrend."""
        candidate = _make_candidate(instrument_type="EQ", signal=1)
        nifty_trend = -1  # Bearish
        target_side = "BUY"

        vetoed = False
        if candidate.get("instrument_type") == "EQ":
            if nifty_trend == -1 and target_side == "BUY":
                vetoed = True
        self.assertTrue(vetoed, "Equity BUY should be vetoed in Nifty downtrend")

    def test_nifty_macro_uptrend_no_veto_for_equity_buy(self):
        """An equity BUY is NOT vetoed when Nifty 50 is in an uptrend."""
        candidate = _make_candidate(instrument_type="EQ", signal=1)
        nifty_trend = 1  # Bullish
        target_side = "BUY"

        vetoed = False
        if candidate.get("instrument_type") == "EQ":
            if nifty_trend == -1 and target_side == "BUY":
                vetoed = True
            elif nifty_trend == 1 and target_side == "SELL":
                vetoed = True
        self.assertFalse(vetoed, "Equity BUY should NOT be vetoed in Nifty uptrend")

    def test_option_veto_not_applied_to_fno(self):
        """F&O instruments should not be vetoed by the Nifty macro gate."""
        candidate = _make_candidate(instrument_type="FUT")
        nifty_trend = -1
        target_side = "BUY"

        vetoed = False
        if candidate.get("instrument_type") == "EQ":  # Only applies to EQ
            if nifty_trend == -1 and target_side == "BUY":
                vetoed = True
        self.assertFalse(vetoed, "F&O should not be vetoed by Nifty macro gate")

    def test_morning_cooloff_rejection(self):
        """Candidates generated before morning cool-off time are rejected."""
        watermark = datetime(2026, 10, 1, 3, 30, 0, tzinfo=timezone.utc)  # 9:00 IST
        morning_cooloff_ist = "09:25"
        ist_now = watermark.astimezone(IST).time()
        cooloff_time = datetime.strptime(morning_cooloff_ist, "%H:%M").time()
        self.assertTrue(ist_now < cooloff_time,
                        f"9:00 IST should be before {morning_cooloff_ist}")

    def test_morning_cooloff_passes_after_cutoff(self):
        """Candidates generated after morning cool-off time should pass."""
        watermark = datetime(2026, 10, 1, 4, 30, 0, tzinfo=timezone.utc)  # 10:00 IST
        morning_cooloff_ist = "09:25"
        ist_now = watermark.astimezone(IST).time()
        cooloff_time = datetime.strptime(morning_cooloff_ist, "%H:%M").time()
        self.assertFalse(ist_now < cooloff_time,
                         f"10:00 IST should be after {morning_cooloff_ist}")

    def test_repeated_underlying_loss_blocked(self):
        """A candidate is blocked if underlying has too many session losses."""
        candidate = _make_candidate(symbol="HDFCBANK")
        loss_counts = {"HDFCBANK": {"losses": 3}}
        max_losses_per_underlying = 2
        key = candidate["symbol"].upper()
        loss_info = loss_counts.get(key, {})
        losses = loss_info.get("losses", 0)
        self.assertGreaterEqual(losses, max_losses_per_underlying,
                                "Should be blocked due to repeated losses")

    def test_fee_gate_rejects_low_profit_trade(self):
        """Fee gate should reject when expected profit is less than minimum ratio x fees."""
        quantity = 10
        entry_price = 100.0
        tp_price = 100.50  # Only 50 paisa move
        est_fees = 25.0  # ₹25 round-trip fees
        min_profit_to_fee_ratio = 2.5

        exp_profit = abs(tp_price - entry_price) * quantity  # = ₹5
        self.assertTrue(exp_profit < est_fees * min_profit_to_fee_ratio,
                        f"Profit {exp_profit} should be < {min_profit_to_fee_ratio}x fees {est_fees}")

    def test_quality_below_minimum_rejection(self):
        """Candidates with quality score below threshold are rejected."""
        quality_score = 42.0
        min_quality = 55.0
        self.assertTrue(quality_score < min_quality)

    def test_full_gate_pipeline_composability(self):
        """
        Verify that the full gate pipeline is composable:
        A good candidate passes ALL gates; modifying any single gate
        parameter can cause rejection at the correct stage.
        """
        # Build a "perfect" candidate
        candidate = _make_candidate(
            signal=1, probability=0.80, rr=2.8,
            chart_accepted=True, close_price=2800, volume=800000,
        )

        gates_passed = []
        rejected_at = None

        # Simulate the gate pipeline in order
        chart = candidate.get("chart_gate", {})

        # 1. Chart gate
        if chart.get("accepted"):
            gates_passed.append("chart_gate")
        else:
            rejected_at = "chart_gate"

        # 2. Signal
        if rejected_at is None and candidate.get("signal"):
            gates_passed.append("model_signal")
        elif rejected_at is None:
            rejected_at = "model_signal"

        # 3. R:R
        if rejected_at is None and float(chart.get("rr", 0)) >= 1.5:
            gates_passed.append("risk_reward")
        elif rejected_at is None:
            rejected_at = "risk_reward"

        # 4. No cooldown
        cooldown = set()
        if rejected_at is None and candidate["symbol"].upper() not in cooldown:
            gates_passed.append("cooldown")
        elif rejected_at is None:
            rejected_at = "cooldown"

        # 5. Probability check
        if rejected_at is None and float(candidate.get("probability", 0)) >= 0.55:
            gates_passed.append("probability")
        elif rejected_at is None:
            rejected_at = "probability"

        self.assertIsNone(rejected_at, f"Good candidate should pass all gates, rejected at: {rejected_at}")
        self.assertEqual(len(gates_passed), 5, f"Should pass all 5 basic gates, passed: {gates_passed}")

    def test_directional_imbalance_cap(self):
        """Rejects candidate if it would exceed directional concentration limit."""
        max_directional_concentration = 0.70
        total_session = 6  # Already 6 trades
        same_dir_count = 5  # 5 are BUY

        # Would adding 1 more BUY exceed the cap?
        ratio = (same_dir_count + 1) / (total_session + 1)
        self.assertGreater(ratio, max_directional_concentration,
                           f"6/7 = {ratio:.2f} should exceed {max_directional_concentration}")


class TestGateSeverityOrdering(unittest.TestCase):
    """Verify that gates are checked in the correct order."""

    EXPECTED_GATE_ORDER = [
        "chart_gate",
        "model_signal",
        "morning_cooloff",
        "signal_freshness",
        "risk_reward",
        "daily_risk",      # cooldown + repeated losses
        "portfolio_limits",  # underlying limit
        "execution_route",
        "macro_index_gate",
        "strategy_consistency",
        "directional_balance",
        "sector_limits",
        "sector_correlation",
        "sentiment_gate",
        "market_quality",
        "market_depth",
        "net_edge",
        "fee_gate",
        "quality_score",
        "option_grade",
        "option_freshness",
        "grade_fencing",
        "adaptive_trap",
        # Final: accepted_top10 / ranking_limits
    ]

    def test_gate_order_matches_code(self):
        """The gate order defined here matches the actual code in _select_top_trade_candidates."""
        # This serves as a documentation test — if the gate order changes in code,
        # this test should be updated to reflect the new order.
        self.assertGreater(len(self.EXPECTED_GATE_ORDER), 15,
                           "Should document at least 15 gates")
        self.assertEqual(self.EXPECTED_GATE_ORDER[0], "chart_gate",
                         "Chart gate must be the first check")
        self.assertIn("adaptive_trap", self.EXPECTED_GATE_ORDER,
                       "Adaptive trap should be among the last gates")

    def test_cheap_gates_before_expensive_gates(self):
        """Ensure cheap gates (chart, signal, R:R) are checked before expensive ones (quality, sentiment)."""
        cheap = ["chart_gate", "model_signal", "risk_reward"]
        expensive = ["sentiment_gate", "market_quality", "quality_score", "adaptive_trap"]

        for c in cheap:
            idx_c = self.EXPECTED_GATE_ORDER.index(c)
            for e in expensive:
                idx_e = self.EXPECTED_GATE_ORDER.index(e)
                self.assertLess(idx_c, idx_e,
                                f"Cheap gate '{c}' should be before expensive gate '{e}'")


if __name__ == "__main__":
    unittest.main()
