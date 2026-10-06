"""
Nivesh Brains — Deep Thinker Agent (Adversarial Reasoning & Justification Engine).

Acts as the internal "Devil's Advocate" and risk stress-tester:
1. Challenges every candidate setup: verifies net edge under slippage & exchange fees.
2. Evaluates Multi-Timeframe coherence (1m/5m/15m/1H/1D).
3. Stress-tests Swing candidates against overnight gap risk and option theta burn.
4. Produces a structured, auditable reasoning chain persisted into shadow_execution_audits.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nivesh.deep_thinker")


class DeepThinker:
    """The analytical, skeptical reasoning agent that audits trade viability."""

    def __init__(self, min_edge_bps: float = 30.0, max_intraday_atr_pct: float = 0.035):
        self.min_edge_bps = min_edge_bps
        self.max_intraday_atr_pct = max_intraday_atr_pct

    def deliberate(
        self,
        candidate: Dict[str, Any],
        market_context: Dict[str, Any],
        proposed_mode: str = "INTRADAY"
    ) -> Dict[str, Any]:
        """Conduct an adversarial evaluation of a candidate trade setup.
        
        Returns:
            Dict containing:
                - verdict: "APPROVE", "DOWNGRADE_TO_INTRADAY", "REJECT"
                - target_mode: "INTRADAY" or "SWING"
                - confidence_score: float (0.0 to 100.0)
                - thought_chain: List[str] step-by-step reasoning statements
                - risk_assessment: Dict breakdown of gap risk, friction, and structure
        """
        thought_chain: List[str] = []
        symbol = candidate.get("symbol", "UNKNOWN")
        side = candidate.get("side", "BUY")
        prob = float(candidate.get("probability", 0.5))
        quality = candidate.get("entry_quality") or candidate.get("_entry_quality") or {}
        quality_score = float(quality.get("score") or candidate.get("quality_score") or 70.0)
        chart_gate = candidate.get("chart_gate") or {}
        strategy = chart_gate.get("strategy") or candidate.get("strategy") or "DIRECTIONAL"
        instrument_type = candidate.get("instrument_type") or "EQ"
        
        thought_chain.append(
            f"1. Setup Review: {symbol} {side} via {strategy} (prob: {prob:.3f}, quality: {quality_score:.1f})"
        )

        # 1. Friction & Expected Edge Check
        edge_bps = float(candidate.get("expected_net_edge_bps") or candidate.get("_expected_net_edge_bps") or 40.0)
        friction_pass = edge_bps >= self.min_edge_bps
        if not friction_pass:
            thought_chain.append(
                f"2. Friction Alert: Expected edge {edge_bps:.1f} bps is below required {self.min_edge_bps:.1f} bps margin."
            )
        else:
            thought_chain.append(
                f"2. Friction Margin: Expected net edge {edge_bps:.1f} bps comfortably absorbs exchange fees and slippage."
            )

        # 2. Multi-Timeframe Alignment
        mtf = candidate.get("multi_timeframe") or candidate.get("_multi_timeframe") or {}
        frames = mtf.get("frames", [])
        aligned_frames = mtf.get("aligned_frames", len(frames))
        total_ready = mtf.get("ready_frames", len(frames))
        higher_tf_bias = mtf.get("higher_tf_bias") or "neutral"
        for f in frames:
            if f.get("timeframe") in ("1H", "1D") and f.get("bias"):
                higher_tf_bias = f.get("bias", "neutral")

        thought_chain.append(
            f"3. Multi-Timeframe Structure: {aligned_frames}/{total_ready} frames aligned. Higher timeframe (1H/1D) bias: {higher_tf_bias}."
        )

        # 3. Mode Evaluation: INTRADAY vs SWING
        mode_in = candidate.get("proposed_mode") or proposed_mode or "INTRADAY"
        target_mode = mode_in
        gap_risk_warning = None
        verdict = "APPROVE"

        # Check if SWING mode is requested or justifiable
        is_swing_candidate = mode_in == "SWING" or (
            quality_score >= 76.0 and aligned_frames >= 3 and higher_tf_bias in ("call", "put")
        )

        if is_swing_candidate:
            # Swing safety invariants
            dte = float(candidate.get("dte_days") or (candidate.get("greeks") or {}).get("dte_days") or 0.0)
            is_opt = str(instrument_type or "").upper() in ("CE", "PE")
            
            # 1. Regulatory Guard: Cash equity shorts cannot be held overnight in Indian markets
            inst_upper = str(instrument_type or "EQ").upper()
            if inst_upper in ("EQ", "EQ_INTRADAY_SHORT") and side == "SELL":
                target_mode = "INTRADAY"
                thought_chain.append(
                    "4. Swing Mode Guard: Cash equity shorts are strictly intraday under exchange rules. Restricting to INTRADAY."
                )
            # 2. Options Theta Decay Guard: Short-dated naked options (<10 DTE) cannot be held overnight
            elif is_opt and not candidate.get("spread_basket_id") and dte < 10.0:
                target_mode = "INTRADAY"
                thought_chain.append(
                    f"4. Swing Mode Guard: Near-term naked option (DTE {dte:.1f}d < 10d) cannot be held overnight due to theta burn. Downgrading to INTRADAY."
                )
                gap_risk_warning = "Short-dated naked options prohibited from overnight hold without hedging leg."
            else:
                # 3. Trend Corridor Alignment
                if is_opt:
                    # BUY CE = +1, SELL CE = -1, BUY PE = -1, SELL PE = +1
                    underlying_dir = 1 if (side == "BUY" and instrument_type == "CE") or (side == "SELL" and instrument_type == "PE") else -1
                else:
                    underlying_dir = 1 if side == "BUY" else -1
                
                tf_dir = 1 if higher_tf_bias == "call" else (-1 if higher_tf_bias == "put" else 0)
                if tf_dir != 0 and underlying_dir != tf_dir:
                    target_mode = "INTRADAY"
                    thought_chain.append(
                        "4. Swing Mode Guard: Setup counters higher-timeframe 1D trend corridor. Restricting to INTRADAY scalp."
                    )
                else:
                    target_mode = "SWING"
                    thought_chain.append(
                        "4. Swing Conviction: Strong multi-timeframe alignment with high quality score justifies multi-day position."
                    )
        else:
            target_mode = "INTRADAY"
            thought_chain.append(
                "4. Intraday Focus: Setup best captured as session momentum/breakout; 15:20 IST force-flat applies."
            )

        # 4. Continuous Fractional Kelly Sizing Engine
        rr = float(candidate.get("rr") or chart_gate.get("rr") or 2.0)
        rr = max(1.0, rr)
        # Kelly criterion: f* = (p*b - q) / b
        q = 1.0 - prob
        kelly_f = (prob * rr - q) / rr
        # Conservative Half-Kelly multiplier scaled between 0.25x and 1.50x
        kelly_multiplier = round(max(0.25, min(1.50, 0.5 + (kelly_f * 0.75) + (quality_score - 70.0) * 0.01)), 2)
        thought_chain.append(
            f"5. Fractional Kelly: Edge {kelly_f:+.2f} (prob {prob:.2f}, R:R {rr:.1f}R) -> Sizing factor {kelly_multiplier:.2f}x."
        )

        # Final Verdict Decision
        if not friction_pass and quality_score < 72.0:
            verdict = "REJECT"
            thought_chain.append("6. Final Verdict: REJECT due to insufficient edge and sub-72 quality.")
        elif kelly_f < -0.10 and quality_score < 75.0:
            verdict = "REJECT"
            thought_chain.append(f"6. Final Verdict: REJECT due to negative Kelly mathematical expectation ({kelly_f:.2f}).")
        else:
            verdict = "APPROVE"
            thought_chain.append(f"6. Final Verdict: APPROVED as {target_mode} at {kelly_multiplier:.2f}x sizing.")

        confidence_score = round(min(98.0, max(45.0, (quality_score * 0.5) + (prob * 40.0) + (10.0 if aligned_frames >= 3 else 0.0))), 1)

        return {
            "verdict": verdict,
            "target_mode": target_mode,
            "confidence_score": confidence_score,
            "kelly_multiplier": kelly_multiplier,
            "thought_chain": thought_chain,
            "risk_assessment": {
                "expected_edge_bps": edge_bps,
                "friction_pass": friction_pass,
                "aligned_frames": aligned_frames,
                "higher_tf_bias": higher_tf_bias,
                "gap_risk_warning": gap_risk_warning,
                "kelly_edge": round(kelly_f, 3),
                "kelly_multiplier": kelly_multiplier
            }
        }
