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
    trend=float(features.get("market_return_20d",0))
    breadth=float(features.get("market_breadth",0))  # centred around zero
    volatility=float(features.get("market_volatility_20d",0))
    if volatility>=.025: return "high_volatility"
    if trend>=.02 and breadth>=.05: return "bull"
    if trend<=-.02 and breadth<=-.05: return "bear"
    return "sideways"


def _minimum_confidence(regime: str,signal: int) -> float:
    # Counter-regime and high-volatility positions require stronger evidence.
    # Tightened thresholds: only accept high-conviction setups to reduce losses.
    if regime=="high_volatility": return .72
    if regime=="bull" and signal<0: return .70
    if regime=="bear" and signal>0: return .70
    if regime=="sideways": return .68
    return .64


def score_candidate(row: Dict,probability: float,raw_probability: float,
                    estimated_cost_bps: float,edge_buffer_bps: float = 10.0) -> RankedCandidate:
    features=row["features"]; signal=1 if probability>=.5 else -1
    confidence=probability if signal>0 else 1-probability
    regime=classify_regime(features)
    # Triple-barrier labels use roughly 1.5 ATR with a 50 bp floor.
    expected_move_bps=max(50.0,min(500.0,float(features.get("atr_14",0))*15000.0))
    expected_gross=max(0.0,(2*confidence-1)*expected_move_bps)
    expected_net=expected_gross-float(estimated_cost_bps)
    minimum=_minimum_confidence(regime,signal)
    reason=""
    if confidence<minimum: reason=f"confidence {confidence:.4f} below {minimum:.4f} for {regime} regime"
    elif expected_net<edge_buffer_bps: reason=f"expected net edge {expected_net:.2f} bps below {edge_buffer_bps:.2f} bps buffer"
    accepted=not reason
    risk_bps=max(25.0,float(features.get("volatility_20d",0))*10000.0)
    # Raw conviction is only a deterministic tie breaker for isotonic plateaus.
    score=expected_net/risk_bps+abs(raw_probability-.5)*1e-6
    return RankedCandidate(row,probability,raw_probability,signal,regime,round(expected_move_bps,4),
                           round(expected_gross,4),round(float(estimated_cost_bps),4),round(expected_net,4),
                           round(score,10),accepted,reason)


def rank_candidates(candidates: Iterable[RankedCandidate],limit: int = 10,
                    max_per_regime: int = 10) -> List[RankedCandidate]:
    accepted=[item for item in candidates if item.accepted]
    accepted.sort(key=lambda item:(item.score,item.expected_net_edge_bps,abs(item.raw_probability-.5)),reverse=True)
    selected=[]; counts={}
    for item in accepted:
        if counts.get(item.regime,0)>=max_per_regime: continue
        selected.append(item); counts[item.regime]=counts.get(item.regime,0)+1
        if len(selected)>=limit: break
    return selected


def policy_manifest() -> Dict:
    return {"version":POLICY_VERSION,"objective":"expected net edge after costs",
            "edge_buffer_bps":10.0,"maximum_positions":10,"regime_gate":True,
            "no_trade_zone":True,"orders_allowed":False}
