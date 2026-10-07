"""Cost-aware cross-sectional policy shared by research and live paper inference.

The classifier estimates direction.  This policy converts that estimate into an
auditable expected-net-edge score, applies a market-regime gate, and rejects
weak opportunities before portfolio construction.  It never enables orders.
"""
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List


POLICY_VERSION = "cost_aware_regime_rank_v1"


@dataclass(frozen=True)
class RankedCandidate:
    row: Dict
    probability: float
    raw_probability: float
    signal: int
    regime: str
    expected_move_bps: float
    expected_gross_edge_bps: float
    estimated_cost_bps: float
    expected_net_edge_bps: float
    score: float
    accepted: bool
    rejection_reason: str = ""

    def audit(self) -> Dict:
        value=asdict(self); value.pop("row",None)
        return value


def classify_regime(features: Dict) -> str:
    # 1. Daily feature check
    if "market_return_20d" in features:
        trend = float(features.get("market_return_20d", 0))
        breadth = float(features.get("market_breadth", 0))  # centred around zero
        volatility = float(features.get("market_volatility_20d", 0))
        if volatility >= .025: return "high_volatility"
        if trend >= .02 and breadth >= .05: return "bull"
        if trend <= -.02 and breadth <= -.05: return "bear"
        return "sideways"
    # 2. Intraday microstructure feature check
    slope = float(features.get("ema_9_21_slope", 0))
    adx = float(features.get("adx_14", 0))
    downside_vol = float(features.get("downside_volatility_20d", 0))
    if downside_vol >= 0.015: return "high_volatility"
    if adx >= 25.0 and slope >= 0.3: return "bull"
    if adx >= 25.0 and slope <= -0.3: return "bear"
    return "sideways"


def _minimum_confidence(regime: str, signal: int) -> float:
    # Counter-regime and high-volatility positions require stronger evidence.
    # Tightened thresholds: only accept high-conviction setups to reduce losses.
    if regime == "high_volatility": return .72
    if regime == "bull" and signal < 0: return .70
    if regime == "bear" and signal > 0: return .70
    if regime == "sideways": return .68
    return .64


def score_candidate(row: Dict, probability: float, raw_probability: float,
                    estimated_cost_bps: float, edge_buffer_bps: float = 1.0,
                    direction: int = 0) -> RankedCandidate:
    features = row["features"]
    regime = classify_regime(features)
    is_intraday = "vp_shape_code" in features or "poc_distance_bps" in features
    reason = ""

    candidate_dir = direction
    if candidate_dir == 0:
        candidate_dir = 1 if (is_intraday or probability >= 0.50) else -1

    if is_intraday and candidate_dir < 0:
        # Intraday label is long-TP only; no calibrated short-side probability exists.
        p_effective = 0.0
    else:
        p_effective = probability if candidate_dir >= 0 else (1.0 - probability)

    if is_intraday:
        expected_move_bps = max(60.0, min(500.0, float(features.get("atr_14", 0)) * 20000.0))
        # Asymmetric triple-barrier payoff: +1.0% (+100 bps) TP vs ~ -0.25% (-25 bps) avg SL/timeout
        expected_gross = max(0.0, p_effective * 100.0 - (1.0 - p_effective) * 25.0)
        expected_net = expected_gross - float(estimated_cost_bps)
        # Calibrated probability >= 0.30 represents > 2x edge over 15% base rate
        signal = candidate_dir if p_effective >= 0.30 else 0
        if signal == 0:
            reason = f"effective probability {p_effective:.4f} below intraday directional threshold (0.30)"
        elif expected_net < edge_buffer_bps:
            reason = f"expected net edge {expected_net:.2f} bps below {edge_buffer_bps:.2f} bps buffer"
        else:
            # Microstructure conflict guard: do not buy into bearish order flow or sell into bullish order flow
            imbalance = float(features.get("order_flow_imbalance", 0))
            asi_dir = float(features.get("asi_direction", 0))
            if signal > 0 and (imbalance < -0.3 and asi_dir < 0):
                reason = "bullish signal contradicted by heavy selling order flow & negative ASI"
            elif signal < 0 and (imbalance > 0.3 and asi_dir > 0):
                reason = "bearish signal contradicted by aggressive buying order flow & positive ASI"
    else:
        # Daily symmetric barrier (base rate ~50%)
        expected_move_bps = max(50.0, min(500.0, float(features.get("atr_14", 0)) * 15000.0))
        signal = candidate_dir if p_effective >= 0.55 else 0
        confidence = p_effective
        expected_gross = max(0.0, (2 * confidence - 1) * expected_move_bps)
        expected_net = expected_gross - float(estimated_cost_bps)
        minimum = _minimum_confidence(regime, signal)
        if signal == 0:
            reason = f"effective probability {p_effective:.4f} below daily directional threshold (0.55)"
        elif confidence < minimum:
            reason = f"confidence {confidence:.4f} below {minimum:.4f} for {regime} regime"
        elif expected_net < edge_buffer_bps:
            reason = f"expected net edge {expected_net:.2f} bps below {edge_buffer_bps:.2f} bps buffer"

    accepted = bool(not reason and signal != 0)
    risk_metric = float(features.get("downside_volatility_20d" if is_intraday else "volatility_20d", 0))
    risk_bps = max(25.0, risk_metric * 10000.0)
    score = expected_net / risk_bps + (raw_probability - 0.5) * 1e-4
    return RankedCandidate(row, probability, raw_probability, signal, regime, round(expected_move_bps, 4),
                           round(expected_gross, 4), round(float(estimated_cost_bps), 4), round(expected_net, 4),
                           round(score, 10), accepted, reason)


def rank_candidates(candidates: Iterable[RankedCandidate], limit: int = 10,
                    max_per_regime: int = 10) -> List[RankedCandidate]:
    accepted = [item for item in candidates if item.accepted and item.signal != 0]
    accepted.sort(key=lambda item:(item.score,item.expected_net_edge_bps,abs(item.raw_probability-.5)),reverse=True)
    selected=[]; counts={}
    for item in accepted:
        if counts.get(item.regime,0)>=max_per_regime: continue
        selected.append(item); counts[item.regime]=counts.get(item.regime,0)+1
        if len(selected)>=limit: break
    return selected


def policy_manifest() -> Dict:
    return {"version":POLICY_VERSION,"objective":"expected net edge after costs",
            "edge_buffer_bps":1.0,"maximum_positions":10,"regime_gate":True,
            "no_trade_zone":True,"orders_allowed":False}
