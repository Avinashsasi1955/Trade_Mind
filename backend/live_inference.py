"""Causal live-paper feature generation and model inference.

This module consumes only completed PostgreSQL bars.  It writes predictions and
paper execution audits, but has no broker-order dependency or submission path.
"""
import hashlib
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import create_engine, text

from .ml.validation_engine import record_shadow_exit, record_shadow_signal, reconcile_shadow_costs
from .ml.calibration import apply_calibration
from .ml.trading_policy import rank_candidates, score_candidate
from .ml_pipeline import FEATURE_SETS, _raw_predict, estimated_trade_cost_bps, feature_rows
from .sector_map import sector_for_symbol
from .sentiment_decision import evaluate_trade_sentiment
from .transaction_costs import estimate_zerodha_costs
from .trade_quality import score_trade
from .adaptive_trade_intelligence import sync_adaptive_rewards, trap_assessment
from .intelligence_memory import publish_brain_event, refresh_intelligence_memory
from .asi_indicator import detect_nse_trap
from .pcr_engine import calculate_pcr, get_latest_pcr
from .volume_profile import calculate_volume_profile, evaluate_volume_profile_verdict
from .senior_market_intelligence import record_opportunity_scan, update_counterfactuals
from .strategies import (
    _chart_strategy_gate,
    _learning_strategy_gate,
    _instrument_local_direction,
    _structure_analysis,
    _timeframe_direction,
)


LIVE_BAR_SOURCES = ("zerodha_kite", "kite_gap_backfill", "upstox_v3", "upstox_rest_5m", "upstox_rest_intraday", "gap_repair_finalizer")
DEFAULT_TRADE_BAR_SOURCES = "zerodha_kite,upstox_v3,upstox_rest_intraday,upstox_rest_5m,upstox_rest_history,gap_repair_finalizer,official_exchange_bhavcopy,yahoo_sample,kite_gap_backfill"
TRADE_BAR_SOURCES = tuple(
    item.strip() for item in os.getenv("NIVESH_SHADOW_TRADE_BAR_SOURCES", DEFAULT_TRADE_BAR_SOURCES).split(",") if item.strip()
)
OPTION_UNDERLYING_ALIASES = {"NIFTY 50": "NIFTY", "NIFTY50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "BANK NIFTY": "BANKNIFTY", "SENSEX": "SENSEX"}
IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("nivesh.live_inference")

BREAKOUT_KEYWORDS = ("BREAKOUT", "BREAKDOWN", "ORB", "MOMENTUM_EXPANSION")
FADE_KEYWORDS = (
    "FADE", "MEAN_REVERSION", "COUNTER_TREND", "EXHAUSTION",
    "RANGE_SUPPORT", "RANGE_RESISTANCE", "FAILED_BREAKOUT",
    "LEARNING_RANGE_PUT_SELL", "LEARNING_RANGE_CALL_SELL"
)


def is_breakout_strategy(strat_name: str, setup_type: str = "") -> bool:
    if (setup_type or "").upper() == "BREAKOUT":
        return True
    s = (strat_name or "").upper()
    return any(k in s for k in BREAKOUT_KEYWORDS) and not s.startswith("FAILED_BREAKOUT")


def is_fade_strategy(strat_name: str) -> bool:
    s = (strat_name or "").upper()
    return any(k in s for k in FADE_KEYWORDS)


def _json(value):
    return value if isinstance(value, dict) else json.loads(value)


def _bar(row) -> Dict:
    return {
        "timestamp": row["bar_time"], "open": float(row["open_price"]),
        "high": float(row["high_price"]), "low": float(row["low_price"]),
        "close": float(row["close_price"]), "volume": int(row["volume"] or 0),
        "oi": int(row["open_interest"] or 0),
    }


def build_causal_feature_vector(exchange: str, symbol: str, daily_bars: List[Dict],
                                session_bar: Dict, market_context: Dict,
                                feature_set: str = "daily_v2") -> Dict:
    """Build the training-compatible vector using history through one watermark.

    ``session_bar`` must represent only the current session through a completed
    source candle. No future label is created or consumed.
    """
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"Unsupported feature set: {feature_set}")
    if not daily_bars or len(daily_bars) < 50:
        raise ValueError("At least 50 completed historical daily bars are required")
    watermark=session_bar["timestamp"]
    if any(item["timestamp"] >= watermark for item in daily_bars):
        raise ValueError("Historical bars must precede the live causal watermark")
    def serial_bar(item):
        value=dict(item); timestamp=value["timestamp"]
        value["timestamp"]=timestamp.isoformat() if isinstance(timestamp,datetime) else str(timestamp)
        return value
    watermark_key=watermark.isoformat() if isinstance(watermark,datetime) else str(watermark)
    rows=feature_rows(exchange,symbol,[*(serial_bar(item) for item in daily_bars),serial_bar(session_bar)],
                      market_context={watermark_key:market_context},feature_set=feature_set)
    if not rows:
        raise ValueError("Feature generator did not produce a live row")
    latest=rows[-1]
    if latest["timestamp"] != watermark_key:
        raise ValueError("Feature timestamp does not match the source watermark")
    missing=[name for name in FEATURE_SETS[feature_set] if name not in latest["features"]]
    if missing:
        raise ValueError("Missing model features: "+", ".join(missing))
    return latest["features"]


def _context(prepared: Iterable[Dict]) -> Dict:
    items=list(prepared); current_returns=[]; returns_by_date=defaultdict(list)
    for item in items:
        bars=item["daily"]
        previous=float(bars[-1]["close"])
        current_returns.append(float(item["session"]["close"])/previous-1)
        for before,after in zip(bars[-21:-1],bars[-20:]):
            if float(before["close"])>0:
                returns_by_date[after["timestamp"]].append(float(after["close"])/float(before["close"])-1)
    historical=[mean(values) for _,values in sorted(returns_by_date.items(),key=lambda pair:pair[0]) if values]
    market_1d=mean(current_returns) if current_returns else 0.0
    window=historical[-19:]+[market_1d]
    compounded=1.0
    for value in window: compounded*=1+value
    return {
        "market_return_1d":market_1d,
        "market_return_20d":compounded-1,
        "market_breadth":sum(value>0 for value in current_returns)/max(1,len(current_returns)),
        "market_volatility_20d":pstdev(window) if len(window)>1 else 0.0,
    }


def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha=2/(period+1)
    result=[values[0]]
    for value in values[1:]:
        result.append(value*alpha+result[-1]*(1-alpha))
    return result


def _aggregate_session_bars(bars: List[Dict], minutes: int) -> List[Dict]:
    """Aggregate intraday bars into session-relative frames.

    NSE/BSE intraday sessions start at 09:15 IST, so 15m/1H context is bucketed
    from the market open instead of the wall-clock hour.  This keeps 09:15-10:15
    as the first true 1H trading structure.
    """
    if not bars or minutes <= 1:
        return list(bars or [])
    buckets = {}
    for bar in bars:
        timestamp = bar.get("timestamp")
        if not isinstance(timestamp, datetime):
            continue
        local = timestamp.astimezone(IST)
        session_start = local.replace(hour=9, minute=15, second=0, microsecond=0)
        elapsed = max(0, int((local - session_start).total_seconds() // 60))
        bucket_index = elapsed // minutes
        bucket_start = session_start + timedelta(minutes=bucket_index * minutes)
        key = bucket_start.astimezone(timezone.utc)
        current = buckets.get(key)
        if current is None:
            buckets[key] = {
                "timestamp": key,
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": int(bar.get("volume") or 0),
                "oi": int(bar.get("oi") or 0),
                "aggregated": True,
                "timeframe_minutes": minutes,
            }
        else:
            current["high"] = max(current["high"], bar["high"])
            current["low"] = min(current["low"], bar["low"])
            current["close"] = bar["close"]
            current["volume"] += int(bar.get("volume") or 0)
            current["oi"] = int(bar.get("oi") or current.get("oi") or 0)
    return [buckets[key] for key in sorted(buckets)]


class LivePaperInference:
    def __init__(self,database_url: str,redis_url: str):
        if os.getenv("LIVE_ELIGIBLE","FALSE").upper()!="FALSE" or os.getenv("NIVESH_LIVE_TRADING_ENABLED","0")!="0":
            raise RuntimeError("Live-paper inference requires both execution fuses locked")
        self.engine=create_engine(database_url,pool_pre_ping=True,future=True)
        self.redis=redis.Redis.from_url(redis_url,decode_responses=True,socket_connect_timeout=3,socket_timeout=3)
        self.max_symbols=max(1,int(os.getenv("NIVESH_LIVE_INFERENCE_SYMBOL_LIMIT","500")))
        self.paper_notional=Decimal(os.getenv("NIVESH_PAPER_NOTIONAL","50000"))
        self.exit_minutes=max(5,int(os.getenv("NIVESH_PAPER_EXIT_MINUTES","30")))
        self.equity_exit_minutes=max(15,int(os.getenv("NIVESH_EQUITY_EXIT_MINUTES","75")))
        self.probe_trades_enabled=os.getenv("NIVESH_SHADOW_PROBE_TRADES_ENABLED","0")=="1"
        self.probe_trade_limit=max(0,int(os.getenv("NIVESH_SHADOW_PROBE_TRADE_LIMIT","0")))
        self.senior_opportunity_enabled=os.getenv("NIVESH_SHADOW_SENIOR_OPPORTUNITY_ENABLED","1")=="1"
        self.senior_index_min_confidence=float(os.getenv("NIVESH_SHADOW_SENIOR_INDEX_MIN_CONFIDENCE","85"))
        self.senior_stock_min_confidence=float(os.getenv("NIVESH_SHADOW_SENIOR_STOCK_MIN_CONFIDENCE","82"))
        self.senior_stock_trade_limit=max(0,int(os.getenv("NIVESH_SHADOW_SENIOR_STOCK_TRADE_LIMIT","2")))
        self.max_open_paper_trades=max(1,int(os.getenv("NIVESH_MAX_OPEN_SHADOW_TRADES","15")))
        self.max_new_trades_per_cycle=max(1,int(os.getenv("NIVESH_SHADOW_MAX_NEW_TRADES_PER_CYCLE","3")))
        self.option_paper_enabled=os.getenv("NIVESH_SHADOW_OPTION_PAPER_ENABLED","1")=="1"
        self.intraday_short_fallback=True  # Bidirectional Long & Short coverage in paper mode
        self.learning_mode_enabled=os.getenv("NIVESH_SHADOW_LEARNING_MODE_ENABLED","1")=="1"
        self.max_index_learning_trades=max(0,int(os.getenv("NIVESH_SHADOW_MAX_INDEX_LEARNING_TRADES","3")))
        self.max_trades_per_underlying=max(1,int(os.getenv("NIVESH_SHADOW_MAX_TRADES_PER_UNDERLYING","1")))
        self.trailing_enabled=os.getenv("NIVESH_SHADOW_TRAILING_STOP_ENABLED","1")=="1"
        self.trailing_trigger_pct=Decimal(os.getenv("NIVESH_SHADOW_TRAILING_TRIGGER_PCT","0.012"))
        self.trailing_giveback_pct=Decimal(os.getenv("NIVESH_SHADOW_TRAILING_GIVEBACK_PCT","0.004"))
        self.breakeven_trigger_pct=Decimal(os.getenv("NIVESH_SHADOW_BREAKEVEN_TRIGGER_PCT","0.006"))
        self.max_daily_loss=Decimal(os.getenv("NIVESH_SHADOW_MAX_DAILY_LOSS","2000"))
        self.hard_kill_daily_loss=Decimal(os.getenv("NIVESH_SHADOW_HARD_KILL_DAILY_LOSS","5000"))
        self.daily_trade_target=max(1,int(os.getenv("NIVESH_SHADOW_DAILY_TRADE_TARGET","5")))
        self.max_daily_trades=max(self.daily_trade_target,int(os.getenv("NIVESH_SHADOW_MAX_DAILY_TRADES","8")))
        self.session_trade_limit_enabled=os.getenv("NIVESH_SHADOW_SESSION_TRADE_LIMIT_ENABLED","1")=="1"
        self.session_min_applicable_trades=max(0,int(os.getenv("NIVESH_SHADOW_SESSION_MIN_APPLICABLE_TRADES","2")))
        self.session_max_applicable_trades=max(
            self.session_min_applicable_trades,
            int(os.getenv("NIVESH_SHADOW_SESSION_MAX_APPLICABLE_TRADES","6")),
        )
        self.max_daily_losses=max(1,int(os.getenv("NIVESH_SHADOW_MAX_DAILY_LOSSES","2")))
        self.reentry_cooldown_minutes=max(0,int(os.getenv("NIVESH_SHADOW_REENTRY_COOLDOWN_MINUTES","30")))
        self.min_option_price=Decimal(os.getenv("NIVESH_SHADOW_MIN_OPTION_PRICE","20"))
        self.max_option_price=Decimal(os.getenv("NIVESH_SHADOW_MAX_OPTION_PRICE","450"))
        self.option_stop_loss_pct=Decimal(os.getenv("NIVESH_PAPER_OPTION_STOP_LOSS_PCT","0.25"))
        self.option_take_profit_pct=Decimal(os.getenv("NIVESH_PAPER_OPTION_TAKE_PROFIT_PCT","0.50"))
        self.max_option_loss_rupees=Decimal(os.getenv("NIVESH_SHADOW_MAX_OPTION_LOSS_RUPEES","2500"))
        self.option_stop_to_capture_max_ratio=Decimal(os.getenv("NIVESH_SHADOW_OPTION_STOP_TO_CAPTURE_MAX_RATIO","1.50"))
        self.active_trade_manager_enabled=os.getenv("NIVESH_SHADOW_ACTIVE_TRADE_MANAGER_ENABLED","0")=="1"
        self.inference_close_due_enabled=os.getenv("NIVESH_INFERENCE_CLOSE_DUE_ENABLED","0")=="1"
        self.manager_breakeven_buffer_pct=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_BREAKEVEN_BUFFER_PCT","0.01"))
        self.manager_atr_stop_multiple=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_ATR_STOP_MULTIPLE","1.6"))
        self.manager_profit_lock_trigger_pct=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_PROFIT_LOCK_TRIGGER_PCT","0.006"))
        self.manager_profit_lock_rupees=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_PROFIT_LOCK_RUPEES","100"))
        self.option_manager_profit_lock_rupees=Decimal(os.getenv("NIVESH_SHADOW_OPTION_MANAGER_PROFIT_LOCK_RUPEES","500"))
        self.manager_min_locked_profit_rupees=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_MIN_LOCKED_PROFIT_RUPEES","50"))
        self.option_manager_min_locked_profit_rupees=Decimal(os.getenv("NIVESH_SHADOW_OPTION_MANAGER_MIN_LOCKED_PROFIT_RUPEES","350"))
        self.manager_profit_lock_fee_buffer_rupees=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_PROFIT_LOCK_FEE_BUFFER_RUPEES","25"))
        self.option_manager_profit_lock_fee_buffer_rupees=Decimal(os.getenv("NIVESH_SHADOW_OPTION_MANAGER_PROFIT_LOCK_FEE_BUFFER_RUPEES","75"))
        self.option_manager_profit_lock_trigger_pct=Decimal(os.getenv("NIVESH_SHADOW_OPTION_MANAGER_PROFIT_LOCK_TRIGGER_PCT","0.04"))
        self.option_manager_trailing_giveback_pct=Decimal(os.getenv("NIVESH_SHADOW_OPTION_MANAGER_GIVEBACK_PCT","0.35"))
        self.manager_target_extend_trigger_pct=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_TARGET_EXTEND_TRIGGER_PCT","0.16"))
        self.manager_extended_rr=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_EXTENDED_RR","2.2"))
        self.manager_min_gap_pct=Decimal(os.getenv("NIVESH_SHADOW_MANAGER_MIN_STOP_GAP_PCT","0.004"))
        self.profit_capture_enabled=os.getenv("NIVESH_SHADOW_PROFIT_CAPTURE_ENABLED","1")=="1"
        self.profit_capture_rupees=Decimal(os.getenv("NIVESH_SHADOW_PROFIT_CAPTURE_RUPEES","100"))
        self.option_profit_capture_rupees=Decimal(os.getenv("NIVESH_SHADOW_OPTION_PROFIT_CAPTURE_RUPEES","250"))
        self.profit_capture_pct=Decimal(os.getenv("NIVESH_SHADOW_PROFIT_CAPTURE_PCT","0.004"))
        self.option_profit_capture_pct=Decimal(os.getenv("NIVESH_SHADOW_OPTION_PROFIT_CAPTURE_PCT","0.018"))
        self.top_selector_enabled=os.getenv("NIVESH_SHADOW_TOP10_SELECTOR_ENABLED","1")=="1"
        self.min_entry_quality=float(os.getenv("NIVESH_SHADOW_MIN_ENTRY_QUALITY","68"))
        self.min_option_entry_quality=float(os.getenv("NIVESH_SHADOW_MIN_OPTION_ENTRY_QUALITY","68"))
        self.min_option_grade=os.getenv("NIVESH_SHADOW_MIN_OPTION_GRADE","B").upper().strip() or "B"
        self.option_grade_a_score=float(os.getenv("NIVESH_SHADOW_OPTION_GRADE_A_SCORE","78"))
        self.option_grade_b_score=float(os.getenv("NIVESH_SHADOW_OPTION_GRADE_B_SCORE",str(self.min_option_entry_quality)))
        self.min_professional_rr=float(os.getenv("NIVESH_SHADOW_MIN_PROFESSIONAL_RR","1.90"))
        self.breakeven_trigger_r=Decimal(os.getenv("NIVESH_SHADOW_BREAKEVEN_TRIGGER_R","1.10"))
        self.profit_lock_trigger_r=Decimal(os.getenv("NIVESH_SHADOW_PROFIT_LOCK_TRIGGER_R","1.60"))
        self.profit_lock_guaranteed_r=Decimal(os.getenv("NIVESH_SHADOW_PROFIT_LOCK_GUARANTEED_R","1.00"))
        self.trailing_trigger_r=Decimal(os.getenv("NIVESH_SHADOW_TRAILING_TRIGGER_R","1.50"))
        self.trailing_giveback_r=Decimal(os.getenv("NIVESH_SHADOW_TRAILING_GIVEBACK_R","0.40"))
        self.min_expected_net_edge_bps=float(os.getenv("NIVESH_SHADOW_MIN_EXPECTED_NET_EDGE_BPS","40"))
        self.session_case_enabled=os.getenv("NIVESH_SHADOW_SESSION_CASE_ENABLED","1")=="1"
        self.learning_best_min_rr=float(os.getenv("NIVESH_SHADOW_LEARNING_BEST_MIN_RR","1.80"))
        self.learning_neutral_min_rr=float(os.getenv("NIVESH_SHADOW_LEARNING_NEUTRAL_MIN_RR","2.00"))
        self.learning_worst_min_rr=float(os.getenv("NIVESH_SHADOW_LEARNING_WORST_MIN_RR",str(self.min_professional_rr)))
        self.learning_best_min_quality=float(os.getenv("NIVESH_SHADOW_LEARNING_BEST_MIN_QUALITY","58"))
        self.learning_neutral_min_quality=float(os.getenv("NIVESH_SHADOW_LEARNING_NEUTRAL_MIN_QUALITY","64"))
        self.learning_worst_min_quality=float(os.getenv("NIVESH_SHADOW_LEARNING_WORST_MIN_QUALITY",str(self.min_entry_quality)))
        self.learning_best_min_edge_bps=float(os.getenv("NIVESH_SHADOW_LEARNING_BEST_MIN_EDGE_BPS","-25"))
        self.learning_neutral_min_edge_bps=float(os.getenv("NIVESH_SHADOW_LEARNING_NEUTRAL_MIN_EDGE_BPS","-10"))
        self.learning_worst_min_edge_bps=float(os.getenv("NIVESH_SHADOW_LEARNING_WORST_MIN_EDGE_BPS",str(self.min_expected_net_edge_bps)))
        self.require_depth_for_entries=os.getenv("NIVESH_SHADOW_REQUIRE_DEPTH_FOR_ENTRIES","0")=="1"
        self.max_same_side_open=max(1,int(os.getenv("NIVESH_SHADOW_MAX_SAME_SIDE_OPEN","5")))
        self.max_new_same_side=max(1,int(os.getenv("NIVESH_SHADOW_MAX_NEW_SAME_SIDE","3")))
        self.max_consecutive_losses=max(1,int(os.getenv("NIVESH_SHADOW_MAX_CONSECUTIVE_LOSSES","2")))
        self.max_losses_per_underlying=max(1,int(os.getenv("NIVESH_SHADOW_MAX_LOSSES_PER_UNDERLYING","1")))
        self.max_open_per_sector=max(1,int(os.getenv("NIVESH_SHADOW_MAX_OPEN_PER_SECTOR","2")))
        self.max_new_per_sector=max(1,int(os.getenv("NIVESH_SHADOW_MAX_NEW_PER_SECTOR","1")))
        self.max_spread_bps=Decimal(os.getenv("NIVESH_SHADOW_MAX_SPREAD_BPS","60"))
        self.max_option_spread_bps=Decimal(os.getenv("NIVESH_SHADOW_MAX_OPTION_SPREAD_BPS","120"))
        self.max_intraday_atr_pct=Decimal(os.getenv("NIVESH_SHADOW_MAX_INTRADAY_ATR_PCT","0.035"))
        self.min_option_volume=max(0,int(os.getenv("NIVESH_SHADOW_MIN_OPTION_VOLUME","1")))
        self.min_option_oi=max(0,int(os.getenv("NIVESH_SHADOW_MIN_OPTION_OI","0")))
        self.max_option_days_to_expiry=max(0,int(os.getenv("NIVESH_SHADOW_MAX_OPTION_DAYS_TO_EXPIRY","45")))
        self.option_coverage_sources=tuple(
            item.strip() for item in os.getenv(
                "NIVESH_SHADOW_OPTION_COVERAGE_SOURCES",
                "zerodha_kite,kite_gap_backfill,upstox_v3,upstox_rest_intraday,upstox_rest_5m",
            ).split(",") if item.strip()
        )
        self.min_option_recent_1m_coverage_pct=Decimal(os.getenv("NIVESH_SHADOW_MIN_OPTION_RECENT_1M_COVERAGE_PCT","70"))
        self.option_recent_coverage_minutes=max(5,int(os.getenv("NIVESH_SHADOW_OPTION_RECENT_COVERAGE_MINUTES","30")))
        self.min_option_session_1m_coverage_pct=Decimal(os.getenv("NIVESH_SHADOW_MIN_OPTION_SESSION_1M_COVERAGE_PCT","50"))
        self.option_stop_noise_atr_multiple=Decimal(os.getenv("NIVESH_SHADOW_OPTION_STOP_NOISE_ATR_MULTIPLE","1.15"))
        self.option_max_fee_to_reward_ratio=Decimal(os.getenv("NIVESH_SHADOW_OPTION_MAX_FEE_TO_REWARD_RATIO","0.25"))
        # Fail transparent, not silently. Until a real licensed news provider is
        # connected, sentiment is advisory only and must not block paper entries.
        self.sentiment_gate_enabled=os.getenv("NIVESH_SHADOW_SENTIMENT_GATE_ENABLED","0")=="1"
        self.require_verified_sentiment=os.getenv("NIVESH_SHADOW_REQUIRE_VERIFIED_SENTIMENT","0")=="1"
        self.sentiment_min_confidence=Decimal(os.getenv("NIVESH_SHADOW_SENTIMENT_MIN_CONFIDENCE","60"))
        self.sentiment_max_age_hours=max(1,int(os.getenv("NIVESH_SHADOW_SENTIMENT_MAX_AGE_HOURS","24")))
        self.entry_cutoff_ist=os.getenv("NIVESH_SHADOW_ENTRY_CUTOFF_IST","15:05")
        self.force_flat_ist=os.getenv("NIVESH_SHADOW_FORCE_FLAT_IST","15:15")
        self.adaptive_learning_enabled=os.getenv("NIVESH_SHADOW_ADAPTIVE_LEARNING_ENABLED","1")=="1"
        self.adaptive_min_samples=max(1,int(os.getenv("NIVESH_SHADOW_ADAPTIVE_MIN_SAMPLES","3")))
        self.adaptive_max_loss_rate=Decimal(os.getenv("NIVESH_SHADOW_ADAPTIVE_MAX_LOSS_RATE","0.62"))
        self.adaptive_min_avg_reward=Decimal(os.getenv("NIVESH_SHADOW_ADAPTIVE_MIN_AVG_REWARD","-1.0"))
        self.microstructure_gate_enabled=os.getenv("NIVESH_SHADOW_1S_MICROSTRUCTURE_GATE_ENABLED","1")=="1"
        self.microstructure_lookback_seconds=max(5,int(os.getenv("NIVESH_SHADOW_1S_LOOKBACK_SECONDS","20")))
        self.microstructure_min_bars=max(3,int(os.getenv("NIVESH_SHADOW_1S_MIN_BARS","8")))
        self.microstructure_max_adverse_bps=Decimal(os.getenv("NIVESH_SHADOW_1S_MAX_ADVERSE_BPS","8"))
        self.require_microstructure_for_entries=os.getenv("NIVESH_SHADOW_REQUIRE_1S_FOR_ENTRIES","0")=="1"
        self.multi_timeframe_gate_enabled=os.getenv("NIVESH_SHADOW_MULTI_TIMEFRAME_GATE_ENABLED","1")=="1"
        self.require_multi_timeframe_for_entries=os.getenv("NIVESH_SHADOW_REQUIRE_MULTI_TIMEFRAME_FOR_ENTRIES","1")=="1"
        self.mtf_min_required_frames=max(2,int(os.getenv("NIVESH_SHADOW_MTF_MIN_REQUIRED_FRAMES","2")))
        self.mtf_strong_conflict_threshold=float(os.getenv("NIVESH_SHADOW_MTF_STRONG_CONFLICT_THRESHOLD","58"))
        self.max_signal_age_minutes=max(1,int(os.getenv("NIVESH_SHADOW_MAX_SIGNAL_AGE_MINUTES","6")))
        self.morning_cooloff_ist=os.getenv("NIVESH_SHADOW_MORNING_COOLOFF_IST","10:15")
        self.morning_min_bars=max(12,int(os.getenv("NIVESH_SHADOW_MORNING_MIN_BARS","18")))
        self.morning_stop_widen_factor=Decimal(os.getenv("NIVESH_SHADOW_MORNING_STOP_WIDEN","1.5"))
        self.min_profit_to_fee_ratio=Decimal(os.getenv("NIVESH_SHADOW_MIN_PROFIT_FEE_RATIO","2.5"))
        self.use_weighted_ensemble=os.getenv("NIVESH_SHADOW_USE_WEIGHTED_ENSEMBLE","0")=="1"
        self.weighted_ensemble_min_score=float(os.getenv("NIVESH_SHADOW_WEIGHTED_ENSEMBLE_MIN_SCORE","75.0"))
        self.max_directional_concentration=Decimal(os.getenv("NIVESH_SHADOW_MAX_DIRECTIONAL_CONCENTRATION","0.70"))

    def _is_force_flat_time(self,watermark: datetime) -> bool:
        try:
            cutoff=datetime.strptime(self.force_flat_ist,"%H:%M").time()
            return watermark.astimezone(IST).time()>=cutoff
        except Exception:
            return False

    def _exit_bars(self,connection,instrument_id:int,signal_at:datetime,watermark:datetime) -> List[Dict]:
        """Prefer 1-second bars for exits; fall back only when not available."""
        for interval,minimum in (("1second",3),("1minute",3),("5minute",1)):
            rows=connection.execute(text("""
                SELECT bar_time,high_price,low_price,close_price,volume
                FROM live_market_bars
                WHERE instrument_id=:instrument AND interval=:interval
                  AND source = ANY(:trade_sources)
                  AND bar_time>=:signal_at AND bar_time<=:watermark
                ORDER BY bar_time
            """),{"instrument":instrument_id,"interval":interval,"signal_at":signal_at,
                 "watermark":watermark,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
            if len(rows)>=minimum:
                return [dict(row, interval=interval) for row in rows]
        return []

    def _model(self):
        with self.engine.connect() as connection:
            row=connection.execute(text("SELECT version,feature_set,payload FROM model_versions WHERE status='active' ORDER BY id DESC LIMIT 1")).mappings().one_or_none()
        if not row: raise RuntimeError("No active model is registered")
        return {**dict(row),"payload":_json(row["payload"])}

    def _watermark(self) -> Optional[datetime]:
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT MAX(bar_time) FROM live_market_bars WHERE interval='5minute' AND source = ANY(:sources)"),
                {"sources": list(TRADE_BAR_SOURCES)},
            ).scalar_one_or_none()

    def _prepare(self,watermark: datetime) -> List[Dict]:
        with self.engine.connect() as connection:
            candidates=connection.execute(text("""
                SELECT DISTINCT ON (i.id) i.id instrument_id,COALESCE(i.instrument_token,k.provider_token) instrument_token,
                    i.exchange,i.symbol,i.instrument_type,b.bar_time,live1.bar_time latest_1m_bar
                FROM live_market_bars b JOIN instrument_master i ON i.id=b.instrument_id
                JOIN LATERAL (
                    SELECT bar_time FROM live_market_bars
                    WHERE instrument_id=i.id AND interval='1minute' AND source = ANY(:trade_sources)
                      AND bar_time<=:watermark+INTERVAL '5 minutes' AND bar_time>:watermark-INTERVAL '3 minutes'
                    ORDER BY bar_time DESC LIMIT 1
                ) live1 ON TRUE
                LEFT JOIN instrument_provider_keys k ON k.instrument_id=i.id AND k.provider='upstox_v3' AND k.is_active
                WHERE b.interval='5minute' AND b.bar_time<=:watermark AND b.bar_time>:watermark-INTERVAL '10 minutes'
                  AND b.source = ANY(:trade_sources) AND i.exchange IN ('NSE','BSE') AND i.instrument_type IN ('EQ','INDEX') AND i.is_active
                ORDER BY i.id,b.bar_time DESC LIMIT :limit
            """),{"watermark":watermark,"limit":self.max_symbols,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
            prepared=[]
            for candidate in candidates:
                daily=connection.execute(text("""
                    SELECT bar_time,open_price,high_price,low_price,close_price,volume,open_interest
                    FROM live_market_bars WHERE instrument_id=:instrument AND interval='day'
                      AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date<(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                    ORDER BY bar_time DESC LIMIT 70
                """),{"instrument":candidate["instrument_id"],"watermark":watermark}).mappings().all()
                intraday=connection.execute(text("""
                    SELECT bar_time,open_price,high_price,low_price,close_price,volume,open_interest
                    FROM live_market_bars WHERE instrument_id=:instrument AND interval='5minute' AND bar_time<=:watermark
                      AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                    ORDER BY bar_time
                """),{"instrument":candidate["instrument_id"],"watermark":watermark}).mappings().all()
                one_minute=connection.execute(text("""
                    SELECT DISTINCT ON (bar_time) bar_time,open_price,high_price,low_price,close_price,volume,open_interest
                    FROM live_market_bars
                    WHERE instrument_id=:instrument AND interval='1minute'
                      AND source = ANY(:trade_sources)
                      AND bar_time<=:causal_watermark
                      AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:causal_watermark AT TIME ZONE 'Asia/Kolkata')::date
                    ORDER BY bar_time, CASE source WHEN 'upstox_v3' THEN 0 WHEN 'zerodha_kite' THEN 1 ELSE 2 END
                """),{"instrument":candidate["instrument_id"],"causal_watermark":candidate["bar_time"]+timedelta(minutes=5),
                     "trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
                seconds=connection.execute(text("""
                    SELECT bar_time,open_price,high_price,low_price,close_price,volume,NULL::BIGINT open_interest
                    FROM live_market_bars
                    WHERE instrument_id=:instrument AND interval='1second'
                      AND source = ANY(:trade_sources)
                      AND bar_time<=:watermark+INTERVAL '5 minutes'
                      AND bar_time>:watermark+INTERVAL '5 minutes' - (:lookback || ' seconds')::interval
                    ORDER BY bar_time
                """),{"instrument":candidate["instrument_id"],"watermark":watermark,
                     "trade_sources":list(TRADE_BAR_SOURCES),"lookback":self.microstructure_lookback_seconds}).mappings().all()
                daily=list(reversed([_bar(row) for row in daily])); intraday=[_bar(row) for row in intraday]
                one_minute=[_bar(row) for row in one_minute]
                if len(daily)<50 or not intraday: continue
                causal_watermark=candidate["bar_time"]+timedelta(minutes=5)
                session={"timestamp":causal_watermark,"open":intraday[0]["open"],
                         "high":max(x["high"] for x in intraday),"low":min(x["low"] for x in intraday),
                         "close":intraday[-1]["close"],"volume":sum(x["volume"] for x in intraday),
                         "oi":intraday[-1]["oi"]}
                prepared.append({**dict(candidate),"source_bar_time":candidate["bar_time"],"causal_watermark":causal_watermark,
                                 "daily":daily,"intraday":intraday,"intraday_1m":one_minute,
                                 "intraday_15m":_aggregate_session_bars(one_minute or intraday,15),
                                 "intraday_1h":_aggregate_session_bars(one_minute or intraday,60),
                                 "microstructure":[_bar(row) for row in seconds],
                                 "session":session})
        return prepared

    def _persist_prediction(self,model: Dict,item: Dict,features: Dict,probability: float,signal: int) -> bool:
        encoded=json.dumps(features,sort_keys=True,separators=(",",":")); digest=hashlib.sha256(encoded.encode()).hexdigest()
        observed=item["causal_watermark"]; price=Decimal(str(item["session"]["close"]))
        with self.engine.begin() as connection:
            inserted=connection.execute(text("""
                INSERT INTO live_feature_snapshots(model_version,instrument_id,observed_at,timeframe,feature_set,features,feature_hash,
                    source_bar_time,causal_watermark,decision_price)
                VALUES(:model,:instrument,:observed,'5minute',:feature_set,CAST(:features AS jsonb),:hash,:source_bar,:observed,:price)
                ON CONFLICT(model_version,instrument_id,timeframe,observed_at) DO NOTHING RETURNING 1
            """),{"model":model["version"],"instrument":item["instrument_id"],"observed":observed,
                    "feature_set":model["feature_set"],"features":encoded,"hash":digest,"source_bar":item["source_bar_time"],"price":price}).scalar_one_or_none()
            if not inserted: return False
            connection.execute(text("""
                INSERT INTO shadow_predictions(model_version,exchange,symbol,timestamp,probability,signal,realised_return,status,created_at,
                    instrument_id,timeframe,feature_hash,decision_price)
                VALUES(:model,:exchange,:symbol,:observed,:probability,:signal,NULL,'PENDING',CURRENT_TIMESTAMP,
                    :instrument,'5minute',:hash,:price)
                ON CONFLICT(model_version,exchange,symbol,timestamp) DO NOTHING
            """),{"model":model["version"],"exchange":item["exchange"],"symbol":item["symbol"],"observed":observed,
                    "probability":probability,"signal":signal,"instrument":item["instrument_id"],"hash":digest,"price":price})
        item.update({"probability":probability,"signal":signal,"feature_hash":digest})
        return True

    def _nearest_option_target(self,item: Dict,watermark: datetime,option_type: str) -> Optional[Dict]:
        with self.engine.connect() as connection:
            row=connection.execute(text("""
                SELECT i.id instrument_id,COALESCE(i.instrument_token,k.provider_token) instrument_token,
                    i.lot_size,i.instrument_type,i.expiry,i.strike,b.close_price
                FROM instrument_master i JOIN LATERAL(
                    SELECT close_price FROM live_market_bars WHERE instrument_id=i.id AND interval='5minute'
                      AND bar_time<=:watermark AND bar_time>:watermark-INTERVAL '10 minutes'
                      AND source = ANY(:trade_sources) ORDER BY bar_time DESC LIMIT 1
                ) b ON TRUE
                LEFT JOIN instrument_provider_keys k ON k.instrument_id=i.id AND k.provider='upstox_v3' AND k.is_active
                WHERE i.underlying_symbol=:symbol AND i.exchange IN ('NFO','BFO') AND i.instrument_type=:option_type
                  AND i.is_active AND i.expiry>=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                ORDER BY i.expiry,ABS(i.strike-:spot) LIMIT 1
            """),{"watermark":watermark,"symbol":OPTION_UNDERLYING_ALIASES.get(str(item["symbol"]).upper(),str(item["symbol"]).upper()),"spot":item["session"]["close"],
                  "option_type":option_type,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().one_or_none()
            wm_dt = (watermark.astimezone(IST) if getattr(watermark, "tzinfo", None) else watermark).date() if hasattr(watermark, "date") else watermark
            if row:
                price=Decimal(str(row["close_price"]))
                if self.min_option_price<=price<=self.max_option_price:
                    dte_days = max(1.0, float((row["expiry"] - wm_dt).days)) if row.get("expiry") else 4.0
                    strike_val = float(row["strike"]) if row.get("strike") else 0.0
                    return {"instrument_token":row["instrument_token"],"instrument_id":row["instrument_id"],"side":item.get("option_side","BUY"),
                            "price":price,"lot_size":int(row["lot_size"] or 100),"kind":row["instrument_type"],
                            "option_strike":strike_val,
                            "expiry":str(row["expiry"]) if row.get("expiry") else None,"dte_days":dte_days}
                return None
            # Real option instrument lookup without live completed bars requirement
            opt_sym = OPTION_UNDERLYING_ALIASES.get(str(item["symbol"]).upper(), str(item["symbol"]).upper())
            spot_val = float(item["session"]["close"])
            opt_row = connection.execute(text("""
                SELECT i.id instrument_id, COALESCE(k.provider_token, i.instrument_token) instrument_token,
                    i.lot_size, i.instrument_type, i.strike, i.expiry
                FROM instrument_master i
                LEFT JOIN instrument_provider_keys k ON k.instrument_id=i.id AND k.is_active
                WHERE i.underlying_symbol=:symbol AND i.exchange IN ('NFO','BFO') AND i.instrument_type=:option_type
                  AND i.is_active AND i.expiry>=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                ORDER BY i.expiry, ABS(i.strike-:spot) LIMIT 1
            """), {"watermark": watermark, "symbol": opt_sym, "spot": spot_val, "option_type": option_type}).mappings().one_or_none()
            if opt_row and opt_row.get("instrument_token"):
                try:
                    from backend.greeks_engine import get_live_or_analytical_greeks
                    strike_val = float(opt_row["strike"])
                    dte_days = max(1.0, float((opt_row["expiry"] - wm_dt).days)) if opt_row.get("expiry") else 4.0
                    greeks = get_live_or_analytical_greeks(self.engine, opt_row["instrument_id"], spot_val, strike_val, dte_days=dte_days, option_type=option_type)
                    greeks["dte_days"] = dte_days
                    price = Decimal(str(greeks["price"]))
                    if not (self.min_option_price <= price <= self.max_option_price):
                        return None
                    item["_greeks"] = greeks
                    return {
                        "instrument_token": opt_row["instrument_token"],
                        "instrument_id": opt_row["instrument_id"],
                        "side": item.get("option_side", "BUY"),
                        "price": price,
                        "lot_size": int(opt_row["lot_size"] or 100),
                        "kind": opt_row["instrument_type"],
                        "option_strike": strike_val,
                        "expiry": str(opt_row["expiry"]) if opt_row.get("expiry") else None,
                        "dte_days": dte_days,
                        "greeks": greeks
                    }
                except Exception:
                    pass
        return None

    def _option_route_direction(self, option_type: Optional[str], option_side: Optional[str]) -> Optional[int]:
        """Map option strategy route into underlying direction.

        Bullish: buy calls or sell puts.
        Bearish: buy puts or sell calls.
        """
        option_type=str(option_type or "").upper()
        option_side=str(option_side or "BUY").upper()
        if option_type=="CE" and option_side=="BUY":
            return 1
        if option_type=="PE" and option_side=="SELL":
            return 1
        if option_type=="PE" and option_side=="BUY":
            return -1
        if option_type=="CE" and option_side=="SELL":
            return -1
        return None

    def _strategy_consistency_gate(self,item: Dict,target: Dict,session_case: Optional[Dict] = None) -> Dict:
        """Final invariant check before a paper trade is allowed.

        Enforces strict 4-point geometric invariant:
        Sign(Route Direction) == Sign(Chart Structure) == Sign(Delta) == Sign(SL/TP Geometry).
        Also enforces Session-Block Regime Controller.
        """
        chart=item.get("chart_gate") or {}
        option_type=chart.get("option_type") or item.get("option_type") or target.get("fallback_from_option")
        option_side=chart.get("side") or item.get("option_side") or target.get("side")
        route_direction=self._option_route_direction(option_type,option_side)
        target_side=str(target.get("side") or "").upper()
        target_kind=str(target.get("kind") or "").upper()
        execution_direction=1 if target_side=="BUY" else -1 if target_side=="SELL" else 0
        strategy_direction=route_direction if route_direction is not None else execution_direction
        if strategy_direction and execution_direction and target_kind in {"EQ","EQ_INTRADAY_SHORT"} and strategy_direction!=execution_direction:
            return {"accepted":False,"reason":"strategy direction conflicts with equity fallback execution side",
                    "strategy_direction":strategy_direction,"execution_direction":execution_direction}
        
        # 1. Delta Direction Invariant: Sign(Delta) == Sign(Strategy Direction)
        if target_kind in {"CE","PE"}:
            target_delta = 1 if (target_kind=="CE" and target_side=="BUY") or (target_kind=="PE" and target_side=="SELL") else -1
            if strategy_direction and target_delta != strategy_direction:
                return {"accepted":False,"reason":"option delta sign conflicts with strategy direction",
                        "target_delta":target_delta,"strategy_direction":strategy_direction}
        elif target_kind in {"EQ","EQ_INTRADAY_SHORT"}:
            eq_delta = 1 if target_side=="BUY" else -1
            if strategy_direction and eq_delta != strategy_direction:
                return {"accepted":False,"reason":"equity delta sign conflicts with strategy direction",
                        "eq_delta":eq_delta,"strategy_direction":strategy_direction}

        # 2. Session-Block Regime Controller: Strategy-to-Session Block Alignment
        session_block = str((session_case or {}).get("session") or self._session_block(datetime.now(IST))).lower()
        strategy_name = str(chart.get("strategy") or item.get("chart_gate",{}).get("strategy") or "").upper()
        
        if session_block in {"opening"} and ("REVERSION" in strategy_name or "SPREAD" in strategy_name):
            return {"accepted":False,"reason":f"session block {session_block} prioritizes breakouts/momentum over reversion/spreads",
                    "strategy":strategy_name,"session_block":session_block}
        
        bias=str((session_case or {}).get("bias") or "none").lower()
        case_name=str((session_case or {}).get("case") or "neutral")
        underlying_key=str(item.get("underlying_symbol") or item.get("symbol") or "").replace(" ","").upper()
        instrument_type=str(item.get("instrument_type") or "").upper()
        is_index_context=instrument_type=="INDEX" or underlying_key in {"NIFTY50","BANKNIFTY","SENSEX","INDIAVIX"}
        if is_index_context and target_kind in {"CE","PE"} and case_name.startswith("worst_"):
            return {"accepted":False,"reason":"worst/choppy session case blocks new option entries",
                    "case":case_name,"target_kind":target_kind}
        if is_index_context and bias=="call" and strategy_direction<0:
            return {"accepted":False,"reason":"bearish strategy conflicts with bullish session case",
                    "bias":bias,"strategy_direction":strategy_direction,
                    "session_bias_scope":"index_only"}
        if is_index_context and bias=="put" and strategy_direction>0:
            return {"accepted":False,"reason":"bullish strategy conflicts with bearish session case",
                    "bias":bias,"strategy_direction":strategy_direction,
                    "session_bias_scope":"index_only"}

        # 3. Chart Structure Invariant: Sign(Route) == Sign(Structure)
        structure=chart.get("structure") or item.get("structure_gate") or {}
        structure_direction=int(structure.get("direction") or 0) if isinstance(structure,dict) else 0
        if structure_direction and strategy_direction and structure_direction!=strategy_direction:
            return {"accepted":False,"reason":"strategy route conflicts with BOS/CHoCH chart structure",
                    "structure_direction":structure_direction,"strategy_direction":strategy_direction,
                    "structure":structure}
        mtf=self._multi_timeframe_confirmation(item,strategy_direction)
        if not mtf.get("accepted",True):
            return {"accepted":False,"reason":mtf.get("reason","multi-timeframe confirmation rejected entry"),
                    "multi_timeframe":mtf,"strategy_direction":strategy_direction,
                    "execution_direction":execution_direction}

        # 4. SL/TP Geometry Invariant: Strict Monotonic Ordering
        price=Decimal(str(target.get("price") or 0))
        risk_levels=self._risk_levels_for_target(item,target)
        stop=risk_levels.get("stop_loss")
        take=risk_levels.get("take_profit")
        if price<=0:
            return {"accepted":False,"reason":"invalid execution price for strategy consistency"}
        if target_side=="BUY":
            if stop is not None and stop>=price:
                return {"accepted":False,"reason":"BUY strategy has inverted SL geometry: stop_loss >= price",
                        "price":str(price),"stop_loss":str(stop),"take_profit":str(take),
                        "target_kind":target_kind,"risk_source":risk_levels.get("source")}
            if take is not None and take<=price:
                return {"accepted":False,"reason":"BUY strategy has inverted TP geometry: take_profit <= price",
                        "price":str(price),"stop_loss":str(stop),"take_profit":str(take),
                        "target_kind":target_kind,"risk_source":risk_levels.get("source")}
        if target_side=="SELL":
            if stop is not None and stop<=price:
                return {"accepted":False,"reason":"SELL strategy has inverted SL geometry: stop_loss <= price",
                        "price":str(price),"stop_loss":str(stop),"take_profit":str(take),
                        "target_kind":target_kind,"risk_source":risk_levels.get("source")}
            if take is not None and take>=price:
                return {"accepted":False,"reason":"SELL strategy has inverted TP geometry: take_profit >= price",
                        "price":str(price),"stop_loss":str(stop),"take_profit":str(take),
                        "target_kind":target_kind,"risk_source":risk_levels.get("source")}
        micro=self._microstructure_gate(item,strategy_direction)
        if not micro.get("accepted",True):
            return {"accepted":False,"reason":micro.get("reason","1-second microstructure rejected entry"),
                    "microstructure":micro,"strategy_direction":strategy_direction,
                    "execution_direction":execution_direction}
        return {"accepted":True,"strategy_direction":strategy_direction,"execution_direction":execution_direction,
                "bias":bias,"option_type":option_type,"option_side":option_side,
                "session_bias_scope":"index_only" if is_index_context else "ignored_for_stock_specific_route",
                "multi_timeframe":mtf,
                "microstructure":micro,
                "risk_levels":{key:str(value) for key,value in risk_levels.items()}}

    def _freshness_gate(self,item: Dict,watermark: datetime,is_option: bool = False) -> Dict:
        """Reject entries whose decision candle is no longer fresh.

        Tightens options window to <= 3.5 minutes to prevent decaying premium entries.
        """
        observed=item.get("causal_watermark") or item.get("source_bar_time")
        if not observed:
            return {"accepted":False,"reason":"missing signal timestamp"}
        age=(watermark-observed).total_seconds()/60
        max_age = 5.0 if is_option else self.max_signal_age_minutes
        if age>max_age:
            return {"accepted":False,
                    "reason":f"stale signal age {age:.1f}m exceeds {max_age:.1f}m for {'option' if is_option else 'cash'}",
                    "age_minutes":round(age,2),"max_age_minutes":max_age}
        latest_1m=item.get("latest_1m_bar")
        if latest_1m:
            one_minute_lag=(watermark-latest_1m).total_seconds()/60
            max_lag = float(os.getenv("NIVESH_SHADOW_MAX_1M_LAG_MINUTES", "5.0"))
            if one_minute_lag>max_lag:
                return {"accepted":False,
                        "reason":f"latest 1m feed lag {one_minute_lag:.1f}m exceeds {max_lag:.1f}m",
                        "age_minutes":round(age,2),"one_minute_lag":round(one_minute_lag,2)}
        return {"accepted":True,"age_minutes":round(age,2)}

    def _microstructure_gate(self,item: Dict,strategy_direction: int) -> Dict:
        """Use true live 1-second bars as an entry-timing guardrail.

        The main model remains trained on completed higher-timeframe features.
        This gate only asks: are the last few seconds already moving sharply
        against the trade? If yes, skip the paper entry and wait for a cleaner
        next candle instead of guessing from a 1-minute chart alone.
        """
        if not self.microstructure_gate_enabled:
            return {"accepted":True,"enabled":False}
        bars=item.get("microstructure") or []
        if len(bars)<self.microstructure_min_bars or not strategy_direction:
            return {"accepted":not self.require_microstructure_for_entries,"enabled":True,
                    "status":"insufficient_1s_history","bars":len(bars),
                    "required":self.require_microstructure_for_entries,
                    "reason":"insufficient 1-second confirmation for entry" if self.require_microstructure_for_entries else None}
        first=Decimal(str(bars[0]["close"]))
        last=Decimal(str(bars[-1]["close"]))
        if first<=0 or last<=0:
            return {"accepted":True,"enabled":True,"status":"invalid_1s_price","bars":len(bars)}

        # < 5ms Pre-Trade Gatekeeper Checks
        best_bid = float(item.get("bid") or item.get("best_bid") or 0.0)
        best_ask = float(item.get("ask") or item.get("best_ask") or 0.0)
        ltp = float(last)
        if best_bid > 0 and best_ask > best_bid and ltp > 0:
            spread_pct = (best_ask - best_bid) / ltp
            if spread_pct > 0.0008:
                return {"accepted": False, "enabled": True, "reason": f"spread too wide ({spread_pct*100:.3f}% > 0.08%), slippage trap",
                        "spread_pct": round(spread_pct * 100, 3)}

        depth = item.get("depth") or {}
        bids = depth.get("buy") or depth.get("bids") or []
        asks = depth.get("sell") or depth.get("asks") or []
        if bids and asks:
            total_bid_qty = sum(float(b.get("quantity") or 0) for b in bids[:5])
            total_ask_qty = sum(float(a.get("quantity") or 0) for a in asks[:5])
            if strategy_direction > 0 and total_ask_qty > 0 and (total_bid_qty / total_ask_qty) < 0.85:
                return {"accepted": False, "enabled": True, "reason": "orderbook ask depth exceeds bid depth (heavy selling pressure)",
                        "bid_depth": total_bid_qty, "ask_depth": total_ask_qty}
            if strategy_direction < 0 and total_bid_qty > 0 and (total_ask_qty / total_bid_qty) < 0.85:
                return {"accepted": False, "enabled": True, "reason": "orderbook bid depth exceeds ask depth (heavy buying support)",
                        "bid_depth": total_bid_qty, "ask_depth": total_ask_qty}

        move_bps=(last/first-Decimal("1"))*Decimal("10000")
        adverse_bps=-move_bps if strategy_direction>0 else move_bps
        if adverse_bps>self.microstructure_max_adverse_bps:
            return {"accepted":False,"enabled":True,"reason":"1-second tape moved against entry timing",
                    "bars":len(bars),"move_bps":str(move_bps.quantize(Decimal("0.01"))),
                    "adverse_bps":str(adverse_bps.quantize(Decimal("0.01"))),
                    "max_adverse_bps":str(self.microstructure_max_adverse_bps)}
        return {"accepted":True,"enabled":True,"status":"1s_tape_ok","bars":len(bars),
                "move_bps":str(move_bps.quantize(Decimal("0.01"))),
                "adverse_bps":str(adverse_bps.quantize(Decimal("0.01")))}

    def _multi_timeframe_confirmation(self,item: Dict,strategy_direction: int) -> Dict:
        """Confirm Senior entries across 1m, 5m, 15m and 1H structure.

        Higher frames are permission/context; lower frames are timing.  Missing
        15m/1H data early in the session is reported, but only strong opposite
        structure blocks a paper entry.  The 1-second gate remains separate and
        is used for execution timing and exits.
        """
        if not self.multi_timeframe_gate_enabled:
            return {"accepted":True,"enabled":False}
        frames=[
            ("1m","timing",item.get("intraday_1m") or [],8),
            ("5m","trade_structure",item.get("intraday") or [],6),
            ("15m","setup_quality",item.get("intraday_15m") or [],3),
            ("1H","higher_timeframe_bias",item.get("intraday_1h") or [],2),
        ]
        analysed=[]
        aligned=0
        conflicts=[]
        ready=0
        strategy_name = str((item.get("chart_gate") or {}).get("strategy") or item.get("_quant_strategy") or "")
        is_reversion = any(k in strategy_name for k in ("REVERSION", "FAILED_BREAKOUT", "ABSORPTION", "PAIR_REVERSION")) or strategy_name.startswith("RANGE_")
        for name,role,bars,min_bars in frames:
            view=_timeframe_direction(bars,min_bars)
            direction=int(view.get("direction") or 0)
            confidence=float(view.get("confidence") or 0)
            status=view.get("status")
            if status=="ready":
                ready+=1
            if strategy_direction and direction==strategy_direction and confidence>=45:
                aligned+=1
            if name == "1m":
                conflict_thresh = max(75.0, self.mtf_strong_conflict_threshold + 15.0)
            elif name == "1H" and len(bars) < 6:
                conflict_thresh = 82.0
            else:
                conflict_thresh = self.mtf_strong_conflict_threshold

            if strategy_direction and direction and direction!=strategy_direction and confidence>=conflict_thresh:
                conflicts.append((name, confidence))
            analysed.append({
                "timeframe":name,
                "role":role,
                "status":status,
                "bars":view.get("bars",len(bars)),
                "bias":view.get("bias"),
                "direction":direction,
                "confidence":round(confidence,2),
                "reason":view.get("reason"),
                "recent_event":view.get("recent_event"),
            })

        conflict_names = [c[0] for c in conflicts]
        if is_reversion and len(conflicts) <= 1:
            has_fatal_conflict = any(c[1] >= 90.0 for c in conflicts)
            conflict_accepted = not has_fatal_conflict
        elif aligned >= 3 and len(conflicts) <= 1:
            has_fatal_conflict = any(c[1] >= 85.0 for c in conflicts)
            conflict_accepted = not has_fatal_conflict
        else:
            conflict_accepted = not conflicts

        accepted = conflict_accepted and (aligned>=self.mtf_min_required_frames or not self.require_multi_timeframe_for_entries)
        reason=(
            f"multi-timeframe conflict on {', '.join(conflict_names)}"
            if not conflict_accepted and conflicts else
            f"{aligned} timeframe(s) aligned; {ready} ready"
        )
        return {"accepted":accepted,"enabled":True,"reason":reason,
                "aligned_frames":aligned,"ready_frames":ready,
                "min_required_frames":self.mtf_min_required_frames,
                "strong_conflict_threshold":self.mtf_strong_conflict_threshold,
                "frames":analysed,
                "execution_rule":"1s confirms final timing/exit; 1m/5m/15m/1H define permission and setup"}

    def _risk_levels_for_target(self,item: Dict,target: Dict) -> Dict:
        """Return SL/TP in the same price space as the traded instrument (Senior Trader Delta-Anchored)."""
        side=str(target.get("side") or "BUY").upper()
        kind=str(target.get("kind") or "").upper()
        price=Decimal(str(target.get("price") or 0))
        chart=item.get("chart_gate") or {}
        stop_mult = Decimal(str((item.get("_effective_thresholds") or {}).get("stop_multiplier", 1.0)))
        if stop_mult <= Decimal("0"):
            stop_mult = Decimal("1.0")

        if kind in {"CE","PE"}:
            chart_sl = chart.get("stop_loss")
            spot = float(item.get("session", {}).get("close") or 0)
            greeks = target.get("greeks") or item.get("_greeks") or {}
            delta = abs(float(greeks.get("delta") or 0.50))
            if chart_sl is not None and spot > 0:
                dist = abs(spot - float(chart_sl))
                opt_risk = Decimal(str(round(dist * delta, 4)))
                min_risk = price * Decimal("0.20")
                max_risk = price * Decimal("0.35")
                actual_risk = min(price * Decimal("0.45"), max(min_risk, min(max_risk, opt_risk)) * stop_mult)
                actual_reward = actual_risk * Decimal("2.0")  # Enforce 1:2 R:R
                stop_loss = (price - actual_risk) if side == "BUY" else (price + actual_risk)
                take_profit = (price + actual_reward) if side == "BUY" else (price - actual_reward)
                chart["rr"] = 2.0
                return {"stop_loss": round(stop_loss, 4), "take_profit": round(take_profit, 4), "basis": "instrument",
                        "source": "underlying_delta_anchored", "rr": 2.0}
            sl_pct=min(Decimal("0.45"), self.option_stop_loss_pct * stop_mult)
            tp_pct=self.option_take_profit_pct
            stop_loss=(price*(Decimal("1")-sl_pct)) if side=="BUY" else (price*(Decimal("1")+sl_pct))
            take_profit=(price*(Decimal("1")+tp_pct)) if side=="BUY" else (price*(Decimal("1")-tp_pct))
            opt_rr = float(round(tp_pct / sl_pct, 2)) if sl_pct > Decimal("0") else 0.0
            chart["rr"] = opt_rr
            return {"stop_loss": round(stop_loss, 4), "take_profit": round(take_profit, 4), "basis": "instrument",
                    "source": "option_volatility_buffer", "rr": opt_rr}
        stop_loss=chart.get("stop_loss")
        take_profit=chart.get("take_profit")
        stop_loss=Decimal(str(stop_loss)) if stop_loss not in (None,"") else None
        take_profit=Decimal(str(take_profit)) if take_profit not in (None,"") else None
        rr = None
        if stop_loss is not None and stop_mult != Decimal("1.0") and price > 0:
            chart_dist = abs(price - stop_loss)
            adj_dist = chart_dist * stop_mult
            stop_loss = (price - adj_dist) if side == "BUY" else (price + adj_dist)
            if take_profit is not None and adj_dist > 0:
                reward_dist = abs(take_profit - price)
                rr = float(round(reward_dist / adj_dist, 2))
                chart["rr"] = rr
        return {"stop_loss": stop_loss, "take_profit": take_profit, "basis": "instrument",
                "source": "underlying_chart_levels", "rr": rr}

    def _execution_target(self,item: Dict,watermark: datetime) -> Optional[Dict]:
        requested_option=item.get("option_type") if item.get("option_type") in {"CE","PE"} else None
        option_direction=self._option_route_direction(requested_option,item.get("option_side"))
        is_index = item.get("instrument_type") == "INDEX" or self._underlying_key(item) in {"NIFTY", "BANKNIFTY", "SENSEX"}

        # 1. Index instruments execute via Index Options
        if is_index:
            if self.option_paper_enabled and item.get("option_type") in {"CE","PE"}:
                return self._nearest_option_target(item,watermark,item["option_type"])
            return None

        # 2. Equity stocks: route to Options or Intraday Cash Equity
        prefer_cash = item.get("prefer_cash") or item.get("trade_mode") == "INTRADAY"
        if self.option_paper_enabled and item.get("option_type") in {"CE","PE"} and not prefer_cash:
            option_target=self._nearest_option_target(item,watermark,item["option_type"])
            if option_target:
                return option_target
            if item.get("instrument_type")!="EQ":
                return None

        # Allow liquid large-cap cash equities >= ₹100 (covers TATASTEEL, BEL, ITC)
        if item.get("instrument_type") == "EQ" and Decimal(str(item["session"]["close"])) < Decimal("100"):
            return None
        direction=option_direction if option_direction is not None else item["signal"]
        if direction>0:
            return {"instrument_token":item["instrument_token"],"instrument_id":item["instrument_id"],"side":"BUY",
                    "price":Decimal(str(item["session"]["close"])),"lot_size":1,"kind":"EQ","fallback_from_option":requested_option}
        if direction<0 and self.intraday_short_fallback:
            return {"instrument_token":item["instrument_token"],"instrument_id":item["instrument_id"],"side":"SELL",
                    "price":Decimal(str(item["session"]["close"])),"lot_size":1,"kind":"EQ_INTRADAY_SHORT","fallback_from_option":requested_option}
        return None

    def _close_due(self,watermark: datetime) -> int:
        opt_cutoff=watermark-timedelta(minutes=self.exit_minutes)
        eq_cutoff=watermark-timedelta(minutes=self.equity_exit_minutes)
        closed=0
        with self.engine.connect() as connection:
            rows=connection.execute(text("""
                SELECT a.id,a.instrument_id,a.side,a.signal_at,a.stop_loss_price,a.take_profit_price,
                    a.theoretical_fill_price,a.quantity,a.estimated_fees,i.instrument_type,
                    COALESCE(a.trade_mode, 'INTRADAY') trade_mode, COALESCE(a.holding_days, 0) holding_days,
                    COALESCE(a.max_holding_days, 5) max_holding_days,
                    (SELECT b.close_price FROM live_market_bars b WHERE b.instrument_id=a.instrument_id
                     AND b.interval IN ('1second','1minute','5minute') AND b.bar_time<=:watermark
                     AND b.source = ANY(:trade_sources)
                     ORDER BY b.bar_time DESC, CASE b.interval WHEN '1second' THEN 0 WHEN '1minute' THEN 1 ELSE 2 END LIMIT 1) exit_price
                FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.audit_status='RECONCILED' AND a.net_pnl IS NULL AND a.signal_at<=:watermark
            """),{"watermark":watermark,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
        for row in rows:
            if row["exit_price"] is None:
                continue
            exit_price=Decimal(row["exit_price"]); exit_reason="TIME_EXIT"; exit_at=watermark
            stop_loss=Decimal(row["stop_loss_price"]) if row["stop_loss_price"] is not None else None
            take_profit=Decimal(row["take_profit_price"]) if row["take_profit_price"] is not None else None
            entry=Decimal(row["theoretical_fill_price"])
            quantity=Decimal(int(row["quantity"] or 0))
            fees=Decimal(row["estimated_fees"] or 0)
            is_option=row["instrument_type"] in {"CE","PE"}
            capture_rupees=self.option_profit_capture_rupees if is_option else self.profit_capture_rupees
            capture_pct=self.option_profit_capture_pct if is_option else self.profit_capture_pct
            capture_points=max(entry*capture_pct, (capture_rupees+fees)/max(Decimal("1"),quantity))
            with self.engine.connect() as connection:
                bars=self._exit_bars(connection,int(row["instrument_id"]),row["signal_at"],watermark)
            best_favourable=entry
            breakeven_armed=False
            profit_locked=False
            breakeven_price=entry
            profit_lock_price=entry
            
            # Initial risk R calculation: strictly use ATR-based risk from chart levels or ATR fallback
            fallback_risk = max(Decimal("0.50"), entry * Decimal("0.006"))
            if row["side"]=="BUY":
                r_points = (entry - stop_loss) if (stop_loss is not None and entry > stop_loss) else fallback_risk
            else:
                r_points = (stop_loss - entry) if (stop_loss is not None and stop_loss > entry) else fallback_risk

            # Fee-padded breakeven buffer (covers 2.5x estimated fees + 2 ticks spread slippage)
            tick_size = Decimal("0.05")
            fee_buffer_per_share = ((fees * Decimal("2.5")) + (Decimal("2") * tick_size * quantity)) / max(Decimal("1"), quantity)

            bars_seen = 0
            for bar in bars:
                bars_seen += 1
                high=Decimal(bar["high_price"]); low=Decimal(bar["low_price"])
                if row["side"]=="BUY":
                    best_favourable=max(best_favourable,high)
                    favorable_r = (high - entry) / max(Decimal("0.01"), r_points)

                    # Immediate Adverse Excursion Early-Cut: if setup collapses within 3 bars without expanding, cut at -0.5R
                    if bars_seen <= 3 and low <= entry - Decimal("0.5") * r_points and best_favourable <= entry + Decimal("0.1") * r_points:
                        exit_price = max(Decimal("0.05"), entry - Decimal("0.5") * r_points)
                        exit_reason = "EARLY_ADVERSE_CUT"; exit_at = bar["bar_time"]; break
                    
                    # Stage 3: +2.5R Spike Exit
                    if favorable_r >= Decimal("2.5"):
                        exit_price=entry + Decimal("2.5") * r_points; exit_reason="PROFIT_CAPTURE"; exit_at=bar["bar_time"]; break
                    # Stage 2: Calibrated Profit Lock (Guarantees +1.0R at +1.60R)
                    if favorable_r >= self.profit_lock_trigger_r:
                        profit_locked=True
                        profit_lock_price=max(profit_lock_price, entry + self.profit_lock_guaranteed_r * r_points)
                    # Stage 1: Calibrated Breakeven (+1.10R Arm)
                    if favorable_r >= self.breakeven_trigger_r:
                        breakeven_armed=True
                        breakeven_price=entry + fee_buffer_per_share

                    if self.profit_capture_enabled and high>=entry+capture_points:
                        exit_price=entry+capture_points; exit_reason="PROFIT_CAPTURE"; exit_at=bar["bar_time"]; break
                    if self.trailing_enabled and favorable_r >= self.trailing_trigger_r:
                        trailing_stop=best_favourable - self.trailing_giveback_r * r_points
                        if low<=trailing_stop:
                            exit_price=max(trailing_stop,profit_lock_price if profit_locked else (breakeven_price if breakeven_armed else trailing_stop))
                            exit_reason="TRAILING_STOP"; exit_at=bar["bar_time"]; break
                    if profit_locked and low<=profit_lock_price:
                        exit_price=profit_lock_price; exit_reason="PROFIT_LOCK_STOP"; exit_at=bar["bar_time"]; break
                    if breakeven_armed and low<=breakeven_price:
                        exit_price=breakeven_price; exit_reason="BREAKEVEN_STOP"; exit_at=bar["bar_time"]; break
                    if stop_loss is not None and low<=stop_loss:
                        exit_price=stop_loss; exit_reason="STOP_LOSS"; exit_at=bar["bar_time"]; break
                    if take_profit is not None and high>=take_profit:
                        exit_price=take_profit; exit_reason="TAKE_PROFIT"; exit_at=bar["bar_time"]; break
                else:
                    best_favourable=min(best_favourable,low)
                    favorable_r = (entry - low) / max(Decimal("0.01"), r_points)

                    # Immediate Adverse Excursion Early-Cut: if setup collapses within 3 bars without expanding, cut at -0.5R
                    if bars_seen <= 3 and high >= entry + Decimal("0.5") * r_points and best_favourable >= entry - Decimal("0.1") * r_points:
                        exit_price = entry + Decimal("0.5") * r_points
                        exit_reason = "EARLY_ADVERSE_CUT"; exit_at = bar["bar_time"]; break

                    # Stage 3: +2.5R Spike Exit
                    if favorable_r >= Decimal("2.5"):
                        exit_price=max(Decimal("0.05"), entry - Decimal("2.5") * r_points); exit_reason="PROFIT_CAPTURE"; exit_at=bar["bar_time"]; break
                    # Stage 2: Calibrated Profit Lock (Guarantees +1.0R at +1.60R)
                    if favorable_r >= self.profit_lock_trigger_r:
                        profit_locked=True
                        profit_lock_price=min(profit_lock_price if profit_lock_price!=entry else (entry - self.profit_lock_guaranteed_r * r_points), entry - self.profit_lock_guaranteed_r * r_points)
                    # Stage 1: Calibrated Breakeven (+1.10R Arm)
                    if favorable_r >= self.breakeven_trigger_r:
                        breakeven_armed=True
                        breakeven_price=entry - fee_buffer_per_share

                    if self.profit_capture_enabled and low<=entry-capture_points:
                        exit_price=max(Decimal("0.05"),entry-capture_points); exit_reason="PROFIT_CAPTURE"; exit_at=bar["bar_time"]; break
                    if self.trailing_enabled and favorable_r >= self.trailing_trigger_r:
                        trailing_stop=best_favourable + self.trailing_giveback_r * r_points
                        if high>=trailing_stop:
                            exit_price=min(trailing_stop,profit_lock_price if profit_locked else (breakeven_price if breakeven_armed else trailing_stop))
                            exit_reason="TRAILING_STOP"; exit_at=bar["bar_time"]; break
                    if profit_locked and high>=profit_lock_price:
                        exit_price=profit_lock_price; exit_reason="PROFIT_LOCK_STOP"; exit_at=bar["bar_time"]; break
                    if breakeven_armed and high>=breakeven_price:
                        exit_price=breakeven_price; exit_reason="BREAKEVEN_STOP"; exit_at=bar["bar_time"]; break
                    if stop_loss is not None and high>=stop_loss:
                        exit_price=stop_loss; exit_reason="STOP_LOSS"; exit_at=bar["bar_time"]; break
                    if take_profit is not None and low<=take_profit:
                        exit_price=take_profit; exit_reason="TAKE_PROFIT"; exit_at=bar["bar_time"]; break

            # Stagnation Guard: Force flat exit after 20 minutes without expansion to prevent capital lockup and chop loss
            duration_minutes = (watermark - row["signal_at"]).total_seconds() / 60.0
            trade_mode = str(row.get("trade_mode") or "INTRADAY").upper()
            stagnation_limit = 25.0 if is_option else 20.0
            if exit_reason == "TIME_EXIT" and duration_minutes >= stagnation_limit and trade_mode == "INTRADAY":
                is_stagnant = (best_favourable < entry + Decimal("0.25") * r_points) if row["side"]=="BUY" else (best_favourable > entry - Decimal("0.25") * r_points)
                if is_stagnant:
                    exit_price=Decimal(row["exit_price"])
                    exit_reason="STAGNATION_GUARD"
                    exit_at=watermark

            if trade_mode == "SWING":
                # Multi-day swing trade: exempt from 15:15 intraday force-flat
                # Only exit if SL or TP is hit, or max_holding_days reached
                signal_date = row["signal_at"].astimezone(IST).date()
                watermark_date = watermark.astimezone(IST).date()
                days_held = (watermark_date - signal_date).days
                if days_held >= int(row.get("max_holding_days") or 15):
                    exit_reason = "SWING_MAX_DAYS_EXPIRED"
                elif exit_reason in ("TIME_EXIT", "STAGNATION_GUARD"):
                    # Keep multi-day position open!
                    continue
            else:
                if exit_reason=="TIME_EXIT" and self._is_force_flat_time(watermark):
                    exit_reason="SESSION_FORCE_FLAT"
                target_cutoff = opt_cutoff if is_option else eq_cutoff
                if exit_reason=="TIME_EXIT" and row["signal_at"]>target_cutoff:
                    continue
            record_shadow_exit(self.engine,int(row["id"]),exit_price,exit_reason,exit_at); closed+=1
        return closed

    def _manage_open_trade_risk(self,watermark: datetime) -> Dict:
        """Actively tighten paper SL/TP using completed live candles.

        This manager is deliberately one-way on risk: it may reduce open risk,
        move a stop to breakeven/profit-lock, and extend a target when momentum
        is strong. It never widens the stop away from the current risk.
        """
        if not self.active_trade_manager_enabled:
            return {"enabled":False,"adjusted":0,"orders_allowed":False}
        adjusted=0
        details=[]
        with self.engine.begin() as connection:
            rows=connection.execute(text("""
                SELECT a.id,a.instrument_id,a.side,a.signal_at,a.theoretical_fill_price,a.quantity,
                       a.stop_loss_price,a.take_profit_price,a.estimated_fees,a.improvement_note,
                       i.symbol,i.instrument_type,COALESCE(i.underlying_symbol,i.symbol) underlying_symbol
                FROM shadow_execution_audits a
                JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.audit_status='RECONCILED' AND a.net_pnl IS NULL AND a.signal_at<=:watermark
                FOR UPDATE
            """),{"watermark":watermark}).mappings().all()
            for row in rows:
                bars=self._exit_bars(connection,int(row["instrument_id"]),row["signal_at"],watermark)
                if len(bars)<3:
                    continue
                entry=Decimal(row["theoretical_fill_price"])
                quantity=Decimal(int(row["quantity"] or 0))
                estimated_fees=Decimal(row["estimated_fees"] or 0)
                current_sl=Decimal(row["stop_loss_price"]) if row["stop_loss_price"] is not None else None
                current_tp=Decimal(row["take_profit_price"]) if row["take_profit_price"] is not None else None
                latest=Decimal(bars[-1]["close_price"])
                highs=[Decimal(item["high_price"]) for item in bars[-8:]]
                lows=[Decimal(item["low_price"]) for item in bars[-8:]]
                closes=[Decimal(item["close_price"]) for item in bars[-5:]]
                ranges=[high-low for high,low in zip(highs,lows) if high>low]
                if not ranges or entry<=0 or latest<=0:
                    continue
                atr=sum(ranges)/Decimal(len(ranges))
                min_gap=latest*self.manager_min_gap_pct
                best_high=max(Decimal(item["high_price"]) for item in bars)
                best_low=min(Decimal(item["low_price"]) for item in bars)
                momentum_up=len(closes)>=3 and closes[-1]>=closes[-2]>=closes[-3]
                momentum_down=len(closes)>=3 and closes[-1]<=closes[-2]<=closes[-3]
                proposed_sl=current_sl
                proposed_tp=current_tp
                reasons=[]
                is_option=row["instrument_type"] in {"CE","PE"}
                pct_trigger=self.option_manager_profit_lock_trigger_pct if is_option else self.manager_profit_lock_trigger_pct
                giveback_pct=self.option_manager_trailing_giveback_pct if is_option else self.trailing_giveback_pct
                lock_trigger_rupees=self.option_manager_profit_lock_rupees if is_option else self.manager_profit_lock_rupees
                min_locked_rupees=self.option_manager_min_locked_profit_rupees if is_option else self.manager_min_locked_profit_rupees
                fee_buffer=self.option_manager_profit_lock_fee_buffer_rupees if is_option else self.manager_profit_lock_fee_buffer_rupees
                required_locked_rupees=max(min_locked_rupees,estimated_fees+fee_buffer)
                locked_profit_points=(min_locked_rupees/max(Decimal("1"),quantity)) if quantity else Decimal("0")
                if is_option and quantity>0:
                    expected_capture_points=max(
                        entry*self.option_profit_capture_pct,
                        self.option_profit_capture_rupees/max(Decimal("1"),quantity),
                    )
                    hard_loss_points=self.max_option_loss_rupees/max(Decimal("1"),quantity)
                    balanced_loss_points=expected_capture_points*self.option_stop_to_capture_max_ratio
                    max_loss_points=max(Decimal("0.05"),min(hard_loss_points,balanced_loss_points,entry*Decimal("0.50")))
                    if row["side"]=="BUY":
                        cap_sl=max(Decimal("0.05"),entry-max_loss_points)
                        if current_sl is None or cap_sl>current_sl:
                            proposed_sl=cap_sl
                            reasons.append("hard_option_loss_cap")
                    else:
                        cap_sl=entry+max_loss_points
                        if current_sl is None or cap_sl<current_sl:
                            proposed_sl=cap_sl
                            reasons.append("hard_option_loss_cap")
                if row["side"]=="BUY":
                    open_return=(latest-entry)/entry
                    best_return=(best_high-entry)/entry
                    best_rupee_profit=(best_high-entry)*quantity
                    current_rupee_profit=(latest-entry)*quantity
                    if best_rupee_profit>=required_locked_rupees and (best_return>=pct_trigger or best_rupee_profit>=lock_trigger_rupees or current_rupee_profit>=lock_trigger_rupees):
                        locked_profit_points=max(locked_profit_points,required_locked_rupees/max(Decimal("1"),quantity))
                        breakeven=max(entry*(Decimal("1")+self.manager_breakeven_buffer_pct),entry+locked_profit_points)
                        atr_stop=latest-(atr*self.manager_atr_stop_multiple)
                        giveback_stop=best_high-((best_high-entry)*giveback_pct)
                        candidate_sl=max(value for value in (breakeven,atr_stop,giveback_stop,current_sl or Decimal("0")) if value is not None)
                        candidate_sl=min(candidate_sl,latest-min_gap)
                        locked_rupees=(candidate_sl-entry)*quantity
                        if locked_rupees>=required_locked_rupees and (current_sl is None or candidate_sl>current_sl):
                            proposed_sl=candidate_sl
                            reasons.append("option_rupee_profit_lock_sl" if is_option and current_rupee_profit>=lock_trigger_rupees else "option_profit_lock_sl" if is_option else "profit_lock_sl")
                    if current_tp is not None and open_return>=self.manager_target_extend_trigger_pct and momentum_up and proposed_sl is not None:
                        risk=max(entry-proposed_sl,entry*Decimal("0.01"))
                        candidate_tp=max(current_tp,entry+(risk*self.manager_extended_rr),latest+(atr*Decimal("1.2")))
                        if candidate_tp>current_tp:
                            proposed_tp=candidate_tp
                            reasons.append("momentum_target_extension")
                else:
                    open_return=(entry-latest)/entry
                    best_return=(entry-best_low)/entry
                    best_rupee_profit=(entry-best_low)*quantity
                    current_rupee_profit=(entry-latest)*quantity
                    if best_rupee_profit>=required_locked_rupees and (best_return>=pct_trigger or best_rupee_profit>=lock_trigger_rupees or current_rupee_profit>=lock_trigger_rupees):
                        locked_profit_points=max(locked_profit_points,required_locked_rupees/max(Decimal("1"),quantity))
                        breakeven=min(entry*(Decimal("1")-self.manager_breakeven_buffer_pct),entry-locked_profit_points)
                        atr_stop=latest+(atr*self.manager_atr_stop_multiple)
                        giveback_stop=best_low+((entry-best_low)*giveback_pct)
                        candidate_sl=min(value for value in (breakeven,atr_stop,giveback_stop,current_sl or entry*Decimal("10")) if value is not None)
                        candidate_sl=max(candidate_sl,latest+min_gap)
                        locked_rupees=(entry-candidate_sl)*quantity
                        if locked_rupees>=required_locked_rupees and (current_sl is None or candidate_sl<current_sl):
                            proposed_sl=candidate_sl
                            reasons.append("option_rupee_profit_lock_sl" if is_option and current_rupee_profit>=lock_trigger_rupees else "option_profit_lock_sl" if is_option else "profit_lock_sl")
                    if current_tp is not None and open_return>=self.manager_target_extend_trigger_pct and momentum_down and proposed_sl is not None:
                        risk=max(proposed_sl-entry,entry*Decimal("0.01"))
                        candidate_tp=min(current_tp,entry-(risk*self.manager_extended_rr),latest-(atr*Decimal("1.2")))
                        candidate_tp=max(candidate_tp,Decimal("0.05"))
                        if candidate_tp<current_tp:
                            proposed_tp=candidate_tp
                            reasons.append("momentum_target_extension")
                if not reasons:
                    continue
                note={}
                raw_note=row["improvement_note"]
                if raw_note:
                    try:
                        note=json.loads(raw_note) if isinstance(raw_note,str) and raw_note.strip().startswith("{") else {"previous_note":str(raw_note)}
                    except Exception:
                        note={"previous_note":str(raw_note)}
                note["risk_manager"]={"updated_at":watermark.isoformat(),"reasons":reasons,
                                      "old_sl":str(current_sl) if current_sl is not None else None,
                                      "new_sl":str(proposed_sl) if proposed_sl is not None else None,
                                      "old_tp":str(current_tp) if current_tp is not None else None,
                                      "new_tp":str(proposed_tp) if proposed_tp is not None else None,
                                      "latest":str(latest),"atr_exit_tf":str((bars[-1] or {}).get("interval","unknown")),"atr":str(atr),
                                      "estimated_fees":str(estimated_fees),
                                      "required_locked_rupees":str(required_locked_rupees),
                                      "rule":"never widen stop; protect winners; extend only with momentum"}
                connection.execute(text("""
                    UPDATE shadow_execution_audits
                    SET stop_loss_price=:stop_loss,take_profit_price=:take_profit,improvement_note=:note
                    WHERE id=:id
                """),{"id":row["id"],"stop_loss":proposed_sl,"take_profit":proposed_tp,
                     "note":json.dumps(note,default=str)[:2000]})
                adjusted+=1
                details.append({"symbol":row["symbol"],"type":row["instrument_type"],"side":row["side"],
                                "reasons":reasons,"stop_loss":str(proposed_sl),"take_profit":str(proposed_tp)})
        return {"enabled":True,"adjusted":adjusted,"details":details[:10],"orders_allowed":False}

    def _open_trade_state(self) -> Dict:
        with self.engine.connect() as connection:
            rows=connection.execute(text("""
                SELECT a.instrument_id,a.side,i.symbol,i.instrument_type,COALESCE(i.underlying_symbol,i.symbol) underlying_symbol
                FROM shadow_execution_audits a
                JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.audit_status='RECONCILED' AND a.net_pnl IS NULL
            """)).mappings().all()
        blocked=set()
        for row in rows:
            blocked.add(str(row["symbol"] or "").upper())
            blocked.add(str(row["underlying_symbol"] or "").upper())
        side_counts=defaultdict(int)
        for row in rows:
            side_counts[str(row["side"] or "").upper()]+=1
        sector_counts=defaultdict(int)
        sector_directions=defaultdict(set)
        for row in rows:
            sec=sector_for_symbol(row["underlying_symbol"] or row["symbol"], row["instrument_type"])
            sector_counts[sec]+=1
            side_val=1 if str(row["side"] or "").upper()=="BUY" else -1
            sector_directions[sec].add(side_val)
        return {"count":len(rows),
                "instrument_ids":{int(row["instrument_id"]) for row in rows},
                "underlyings":blocked,
                "underlying_counts":{key:sum(1 for row in rows if key in {str(row["symbol"] or "").upper(),str(row["underlying_symbol"] or "").upper()}) for key in blocked},
                "side_counts":dict(side_counts),
                "sector_counts":dict(sector_counts),
                "sector_directions":{k: set(v) for k, v in sector_directions.items()}}

    def _open_trade_count(self) -> int:
        return int(self._open_trade_state()["count"])

    def _daily_risk_state(self,watermark: datetime) -> Dict:
        with self.engine.connect() as connection:
            row=connection.execute(text("""
                SELECT COUNT(*) total_trades,
                       COUNT(*) FILTER(WHERE net_pnl IS NOT NULL) closed_trades,
                       COUNT(*) FILTER(WHERE net_pnl < 0) losing_trades,
                       COALESCE(SUM(net_pnl),0) realised_pnl
                FROM shadow_execution_audits
                WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
            """),{"watermark":watermark}).mappings().one()
            cooldown_rows=connection.execute(text("""
                SELECT DISTINCT COALESCE(i.underlying_symbol,i.symbol) underlying_symbol
                FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.exit_at IS NOT NULL
                  AND a.exit_at>=:watermark-(:cooldown || ' minutes')::interval
                  AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                  AND (a.net_pnl<0 OR a.exit_reason IN ('STOP_LOSS','TRAILING_STOP'))
            """),{"watermark":watermark,"cooldown":self.reentry_cooldown_minutes}).mappings().all()
            sector_loss_rows=connection.execute(text("""
                SELECT COALESCE(i.underlying_symbol,i.symbol) as symbol
                FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.exit_at IS NOT NULL
                  AND a.exit_at>=:watermark-(:cooldown || ' minutes')::interval
                  AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                  AND (a.net_pnl<0 OR a.exit_reason = 'STOP_LOSS')
            """),{"watermark":watermark,"cooldown":max(60, self.reentry_cooldown_minutes)}).mappings().all()
            sector_loss_counts=Counter(sector_for_symbol(str(r["symbol"] or "")) for r in sector_loss_rows)
            cooldown_sectors={str(sector).upper() for sector, cnt in sector_loss_counts.items() if cnt >= 2}
            recent_closed=connection.execute(text("""
                SELECT net_pnl,exit_reason
                FROM shadow_execution_audits
                WHERE net_pnl IS NOT NULL
                  AND (signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                ORDER BY exit_at DESC NULLS LAST,id DESC
                LIMIT :limit
            """),{"watermark":watermark,"limit":self.max_consecutive_losses}).mappings().all()
        realised=Decimal(row["realised_pnl"] or 0)
        reasons=[]
        consecutive_losses=0
        for item in recent_closed:
            if Decimal(item["net_pnl"] or 0)<0 or str(item["exit_reason"] or "").upper()=="STOP_LOSS":
                consecutive_losses+=1
            else:
                break
        if realised<=-self.max_daily_loss:
            reasons.append(f"daily realised paper loss {realised} breached -{self.max_daily_loss}")
        if realised<=-self.hard_kill_daily_loss:
            reasons.append(f"hard kill: daily realised paper loss {realised} breached -{self.hard_kill_daily_loss}")
        if int(row["total_trades"] or 0)>=self.max_daily_trades:
            reasons.append(f"daily trade count reached {self.max_daily_trades}")
        if int(row["losing_trades"] or 0)>=self.max_daily_losses:
            reasons.append(f"daily losing trades reached {self.max_daily_losses}")
        if consecutive_losses>=self.max_consecutive_losses:
            reasons.append(f"consecutive losing exits reached {self.max_consecutive_losses}")
        local_time=watermark.astimezone(ZoneInfo("Asia/Kolkata")).time()
        try:
            cutoff=datetime.strptime(self.entry_cutoff_ist,"%H:%M").time()
            if local_time>=cutoff:
                reasons.append(f"new entries disabled after {self.entry_cutoff_ist} IST; manager can still exit positions")
        except Exception:
            pass
        return {"blocked":bool(reasons),"reasons":reasons,
                "total_trades":int(row["total_trades"] or 0),
                "closed_trades":int(row["closed_trades"] or 0),
                "losing_trades":int(row["losing_trades"] or 0),
                "consecutive_losses":consecutive_losses,
                "realised_pnl":str(realised),
                "daily_trade_target":self.daily_trade_target,
                "max_daily_trades":self.max_daily_trades,
                "hard_kill_daily_loss":str(self.hard_kill_daily_loss),
                "cooldown_underlyings":list({str(item["underlying_symbol"] or "").upper() for item in cooldown_rows}),
                "cooldown_sectors":list(cooldown_sectors)}

    def _json_safe(self,value):
        if isinstance(value,Decimal):
            return str(value)
        if isinstance(value,(datetime,)):
            return value.isoformat()
        if isinstance(value,set):
            return sorted(str(item) for item in value)
        if isinstance(value,dict):
            return {str(key):self._json_safe(item) for key,item in value.items()}
        if isinstance(value,(list,tuple)):
            return [self._json_safe(item) for item in value]
        return value

    def _record_daily_risk_stop(self,watermark: datetime,risk_state: Dict) -> Dict:
        """Log one auditable stop event when daily paper risk limits are hit.

        This does not force-close open positions.  It blocks fresh entries while
        allowing the active trade manager, SL/TP and manual exits to keep
        protecting existing paper positions.
        """
        if not risk_state.get("blocked"):
            return {"logged":False,"reason":"risk gate not blocked"}
        market_day=watermark.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        cache_key=f"nivesh:shadow:daily_risk_stop:{market_day}"
        try:
            if not self.redis.set(cache_key,"1",nx=True,ex=36*60*60):
                return {"logged":False,"reason":"already logged","market_day":market_day}
        except Exception:
            pass
        with self.engine.begin() as connection:
            exit_rows=connection.execute(text("""
                SELECT COALESCE(exit_reason,'OPEN') reason,COUNT(*) count
                FROM shadow_execution_audits
                WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                GROUP BY COALESCE(exit_reason,'OPEN')
                ORDER BY count DESC
            """),{"watermark":watermark}).mappings().all()
            tag_rows=connection.execute(text("""
                SELECT tag,COUNT(*) count
                FROM shadow_execution_audits a,
                     LATERAL jsonb_array_elements_text(a.mistake_tags) tag
                WHERE (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                GROUP BY tag
                ORDER BY count DESC
                LIMIT 8
            """),{"watermark":watermark}).mappings().all()
            worst_rows=connection.execute(text("""
                SELECT i.symbol,i.instrument_type,a.side,a.net_pnl,a.exit_reason,
                       a.signal_probability,a.fill_source,a.improvement_note
                FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.net_pnl IS NOT NULL
                  AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                ORDER BY a.net_pnl ASC
                LIMIT 5
            """),{"watermark":watermark}).mappings().all()
            payload={
                "market_day":market_day,
                "watermark":watermark.isoformat(),
                "action":"new_entries_disabled; open positions remain under active risk manager",
                "risk_state":risk_state,
                "exit_reasons":[dict(row) for row in exit_rows],
                "mistake_tags":[dict(row) for row in tag_rows],
                "worst_trades":[dict(row) for row in worst_rows],
                "review_required":[
                    "compare stopped trades with BOS/CHoCH and session trend",
                    "check stop distance versus normal candle noise",
                    "check whether expected edge was too small after fees/slippage",
                    "reduce repeated mistake tags before raising trade frequency",
                ],
                "orders_allowed":False,
            }
            connection.execute(text("""
                INSERT INTO monitoring_events(component,level,message,payload,created_at)
                VALUES(:component,:level,:message,:payload,CURRENT_TIMESTAMP)
            """),{
                "component":"shadow_daily_risk_stop",
                "level":"WARNING",
                "message":"Daily paper risk stop engaged; new entries disabled and mistake analysis required",
                "payload":json.dumps(self._json_safe(payload),sort_keys=True),
            })
        return {"logged":True,"market_day":market_day}

    def _blocked_underlyings(self,open_state: Dict,risk_state: Dict) -> set:
        counts=open_state.get("underlying_counts",{})
        blocked={key for key,count in counts.items() if int(count)>=self.max_trades_per_underlying}
        blocked.update(risk_state.get("cooldown_underlyings",set()))
        return blocked

    def _underlying_key(self,item: Dict) -> str:
        symbol=str(item.get("symbol","")).upper().strip()
        return OPTION_UNDERLYING_ALIASES.get(symbol,symbol.replace(" ",""))

    def _limit_by_underlying(self,items: List[Dict],open_state: Dict,risk_state: Dict,limit: int) -> List[Dict]:
        counts=defaultdict(int,{str(key).upper():int(value) for key,value in open_state.get("underlying_counts",{}).items()})
        blocked={str(value).upper() for value in risk_state.get("cooldown_underlyings",set())}
        selected=[]
        for item in items:
            key=self._underlying_key(item)
            if key in blocked or counts[key]>=self.max_trades_per_underlying:
                continue
            selected.append(item)
            counts[key]+=1
            if len(selected)>=limit:
                break
        return selected

    def _target_quantity(self,target: Dict,grade: str = "A") -> int:
        multiplier = 1.0 if grade == "A" else 0.6 if grade == "B" else 0.0
        if multiplier <= 0.0:
            return 0
        if target["kind"] in {"EQ","EQ_INTRADAY_SHORT"}:
            base = int(self.paper_notional / target["price"])
            return max(1, int(base * multiplier))
        lot = int(target.get("lot_size") or 1)
        return max(1, lot)

    def _scale_quantity(self, quantity: int, target: Dict, size_multiplier: float) -> int:
        """Apply defensive or volatility sizing scaling, respecting option lot constraints."""
        if quantity <= 0:
            return 0
        is_option = str(target.get("kind", "")).upper() in {"CE", "PE"}
        lot = int(target.get("lot_size") or 1)
        if 0.0 < size_multiplier < 1.0:
            if is_option:
                scaled_lots = int((quantity / lot) * size_multiplier)
                if scaled_lots < 1:
                    return 0
                quantity = scaled_lots * lot
            else:
                quantity = max(1, int(quantity * size_multiplier))
        elif size_multiplier > 1.0:
            if is_option:
                scaled_lots = max(1, int((quantity * size_multiplier + lot - 1) // lot))
                quantity = scaled_lots * lot
            else:
                quantity = int(quantity * size_multiplier)
                price = float(target.get("price") or 0)
                if price > 0:
                    max_notional_qty = max(1, int(self.paper_notional / price))
                    quantity = min(quantity, max_notional_qty)
        if is_option and quantity > 0:
            quantity = max(lot, (quantity // lot) * lot)
        return quantity

    def _candidate_grade(self,item: Dict,target: Dict,quality: Dict,market_quality: Dict,consistency: Dict = None) -> Dict:
        """Unified candidate grading across options and cash equities.
        
        Grade A: 100% sizing, high score, strong R:R, positive volume/microstructure.
        Grade B: 60% sizing, solid score & R:R.
        Grade C: 0% sizing (Strictly gated to counterfactual observation only).
        """
        if target.get("kind") in {"CE","PE"}:
            return self._option_grade(item,target,quality,market_quality,consistency)
        score=float(quality.get("score") or 0)
        rr=float((item.get("chart_gate") or {}).get("rr") or 0)
        micro=(consistency or {}).get("microstructure") or {}
        depth=bool(market_quality.get("depth_available",True))
        if score>=self.option_grade_a_score and rr>=2.0 and depth and micro.get("accepted",True):
            grade="A"
        elif score>=self.option_grade_b_score and rr>=1.5:
            grade="B"
        else:
            grade="C"
        accepted = (grade in {"A","B"})
        reason = (
            f"equity grade {grade} accepted"
            if accepted else
            f"equity grade {grade} restricted to counterfactual observation only (0 active paper risk)"
        )
        return {"accepted":accepted,"grade":grade,"required_grade":"B",
                "score":round(score,2),"rr":round(rr,2),"depth_available":depth,
                "reason":reason}

    def _candidate_quality(self,item: Dict,target: Dict,quantity: int) -> Dict:
        risk_levels=self._risk_levels_for_target(item,target)
        note={"strategy":item.get("chart_gate",{}).get("strategy"),
              "reason":item.get("chart_gate",{}).get("reason"),
              "rr":item.get("chart_gate",{}).get("rr"),
              "route":f"{target['side']} {target['kind']}",
              "underlying":item.get("symbol"),
              "fallback_from_option":target.get("fallback_from_option"),
              "risk_price_basis":risk_levels.get("basis"),
              "risk_source":risk_levels.get("source"),
              "structure":item.get("chart_gate",{}).get("structure"),
              "volume_confirmation": "volume passed" if item.get("chart_gate",{}).get("accepted") else "pending",
              "feed_status": "fresh live feed",
              "paper_only":True}
        micro=self._microstructure_gate(item,1 if target.get("side")=="BUY" else -1)
        note["microstructure"]=micro
        note["multi_timeframe_confirmation"]=(item.get("_strategy_consistency") or {}).get("multi_timeframe") or self._multi_timeframe_confirmation(item,int(item.get("signal") or 0))
        return score_trade({"entry_price":target["price"],"latest_price":target["price"],"quantity":quantity,
                            "side":target["side"],"signal_probability":item.get("probability",0.5),
                            "stop_loss_price":risk_levels.get("stop_loss"),
                            "take_profit_price":risk_levels.get("take_profit"),
                            "estimated_fees":0,"is_open":True,
                            "fill_source":"DEPTH_SNAPSHOT",
                            "improvement_note":json.dumps(note,default=str)})

    def _option_grade(self,item: Dict,target: Dict,quality: Dict,market_quality: Dict,consistency: Dict = None) -> Dict:
        """Grade option entries before they can become paper positions.

        Grade A/B may trade in paper mode. Grade C is analysis-only: it remains
        in the candidate audit log but does not enter Positions. This is the
        practical filter that prevents weak CE/PE setups from being treated as
        real learning evidence.
        """
        if target.get("kind") not in {"CE","PE"}:
            return {"accepted":True,"grade":"N/A","reason":"not an option route"}
        score=float(quality.get("score") or 0)
        rr=float((item.get("chart_gate") or {}).get("rr") or 0)
        is_analytical = bool((target.get("greeks") or {}).get("source") == "analytical_black_scholes" or target.get("source") == "analytical_black_scholes")
        depth=bool(market_quality.get("depth_available") or market_quality.get("synthetic_depth") or is_analytical)
        spread=Decimal(str(market_quality.get("spread_bps") or ("40.0" if is_analytical else 999999)))
        micro=(consistency or {}).get("microstructure") or {}
        checks=market_quality.get("checks") or []
        hard_flags=[]
        if not depth:
            hard_flags.append("no depth snapshot")
        if not micro.get("accepted",True):
            hard_flags.append("1s tape rejected timing")
        if rr<1.5:
            hard_flags.append("weak reward/risk")
        if score>=self.option_grade_a_score and depth and rr>=2.0 and spread<=Decimal("80") and not hard_flags:
            grade="A"
        elif score>=self.option_grade_b_score and depth and rr>=1.8 and spread<=self.max_option_spread_bps and not hard_flags:
            grade="B"
        else:
            grade="C"

        # Analytical/synthetic-priced options cannot receive grade A solely from synthetic assumptions
        if (is_analytical or bool(market_quality.get("synthetic_depth"))) and grade == "A":
            grade = "B"

        min_rank={"A":3,"B":2,"C":1}.get(self.min_option_grade,2)
        grade_rank={"A":3,"B":2,"C":1}.get(grade,1)
        accepted=grade_rank>=min_rank
        reason=(
            f"option grade {grade} accepted"
            if accepted else
            f"option grade {grade} below required {self.min_option_grade}"
        )
        if hard_flags:
            reason=f"{reason}: "+", ".join(hard_flags[:3])
        return {"accepted":accepted,"grade":grade,"required_grade":self.min_option_grade,
                "score":round(score,2),"rr":round(rr,2),"depth_available":depth,
                "spread_bps":str(spread),"reason":reason,"checks":checks[:8],
                "is_synthetic":bool(is_analytical or market_quality.get("synthetic_depth") or market_quality.get("is_synthetic"))}

    def _senior_decision_report(self,item: Dict,target: Dict,quality: Dict,market_quality: Dict,
                                sentiment: Dict,adaptive: Dict,consistency: Dict,
                                option_grade: Dict = None) -> Dict:
        chart=item.get("chart_gate") or {}
        structure=chart.get("structure") or {}
        session_case=item.get("_session_case") or {}
        thresholds=item.get("_effective_thresholds") or {}
        probability=float(item.get("probability") or 0.5)
        direction="bullish" if int(item.get("signal") or 0)>0 else "bearish" if int(item.get("signal") or 0)<0 else "neutral"
        components={
            "model_edge":round(abs(probability-0.5)*100,2),
            "entry_quality":quality.get("score"),
            "rr":chart.get("rr"),
            "structure_confidence":structure.get("confidence") if isinstance(structure,dict) else None,
            "selector_score":item.get("_selector_score"),
            "sentiment_score":sentiment.get("score"),
            "sentiment_alignment":sentiment.get("alignment") or sentiment.get("action"),
        }
        cautions=[]
        blockers=[]
        vp_eval = item.get("_volume_profile")
        if not vp_eval and item.get("intraday"):
            vp_eval = evaluate_volume_profile_verdict(float(target.get("price") or 0), 1 if direction=="bullish" else -1, calculate_volume_profile(item["intraday"]))
        if vp_eval and vp_eval.get("cautions"):
            cautions.extend(vp_eval["cautions"])
        if vp_eval:
            components["volume_profile_verdict"] = vp_eval.get("verdict")
            components["poc"] = vp_eval.get("poc")
            components["vah"] = vp_eval.get("vah")
            components["val"] = vp_eval.get("val")
        if not structure.get("accepted",False):
            blockers.append("structure not strong enough for promotion-grade evidence")
        if option_grade and not option_grade.get("accepted",True):
            blockers.append(option_grade.get("reason","option grade rejected"))
        if not market_quality.get("accepted",True):
            blockers.append(market_quality.get("reason","market quality rejected"))
        if not adaptive.get("accepted",True):
            blockers.append(adaptive.get("reason","adaptive trap rejected"))
        if not consistency.get("accepted",True):
            blockers.append(consistency.get("reason","strategy consistency rejected"))
        if sentiment.get("available") and sentiment.get("alignment")=="conflict":
            cautions.append(sentiment.get("reason","news sentiment conflicts with trade direction"))
        if sentiment.get("enabled") and not sentiment.get("accepted",True):
            blockers.append(sentiment.get("reason","sentiment gate rejected"))
        action="APPROVE_PAPER" if not blockers else "REJECT_OR_REVIEW"
        if target.get("kind") in {"CE","PE"} and option_grade:
            action=f"{action}_GRADE_{option_grade.get('grade','C')}"
        return {
            "persona":"Senior Trade Decision Report",
            "experience_label":"20-year-style rulebook",
            "action":action,
            "symbol":item.get("symbol"),
            "route":f"{target.get('side')} {target.get('kind')}",
            "direction":direction,
            "strategy":chart.get("strategy"),
            "session_case":session_case,
            "structure":structure,
            "quality_grade":quality.get("grade"),
            "option_grade":option_grade or {"grade":"N/A"},
            "components":components,
            "market_quality":market_quality,
            "sentiment":sentiment,
            "adaptive":adaptive,
            "microstructure":consistency.get("microstructure") if isinstance(consistency,dict) else {},
            "multi_timeframe":consistency.get("multi_timeframe") if isinstance(consistency,dict) else {},
            "thresholds":thresholds,
            "decision_reason":chart.get("reason"),
            "blockers":blockers,
            "cautions":cautions,
            "orders_allowed":False,
        }

    def _senior_opportunity_gate(self,item: Dict,session_case: Dict = None,index_option: bool = False) -> Dict:
        """Professional paper-only opportunity scan independent of weak ML probability.

        This catches clean index-option and liquid-stock opportunities when the
        model is neutral, while still requiring chart structure, momentum, range,
        risk/reward, and session alignment. It is learning evidence, not model
        promotion evidence.
        """
        if not self.senior_opportunity_enabled:
            return {"accepted":False,"reason":"senior opportunity scanner disabled","confidence":0}
        bars=item.get("intraday") or []
        opening_range=bool(bars and bars[-1].get("forming") and (session_case or {}).get("session")=="opening")
        min_bars=6 if index_option or opening_range else 10
        if len(bars)<min_bars:
            return {"accepted":False,"reason":f"needs {min_bars} completed 5m candles","confidence":0}
        closes=[float(bar["close"]) for bar in bars]
        highs=[float(bar["high"]) for bar in bars]
        lows=[float(bar["low"]) for bar in bars]
        volumes=[max(0,int(bar.get("volume") or 0)) for bar in bars]
        last=closes[-1]
        if last<=0:
            return {"accepted":False,"reason":"invalid last price","confidence":0}
        ema_fast=_ema(closes,5)[-1]
        ema_mid=_ema(closes,13 if len(closes)>=13 else max(6,len(closes)//2))[-1]
        ema_slow=_ema(closes,21 if len(closes)>=21 else max(8,len(closes)//2+2))[-1]
        atr_window=min(10,len(bars))
        atr=sum((high-low) for high,low in zip(highs[-atr_window:],lows[-atr_window:]))/max(1,atr_window)
        if atr<=0:
            return {"accepted":False,"reason":"zero live range","confidence":0}
        prior_high=max(highs[-min(12,len(highs)):-1])
        prior_low=min(lows[-min(12,len(lows)):-1])
        recent_base=closes[-4] if len(closes)>=4 else closes[0]
        recent_move=(last/recent_base-1) if recent_base else 0
        session_open=bars[0]["open"] or closes[0]
        session_move=(last/session_open-1) if session_open else 0
        avg_volume=sum(volumes[-min(12,len(volumes)):-1])/max(1,len(volumes[-min(12,len(volumes)):-1]))
        volume_ratio=(volumes[-1]/avg_volume) if avg_volume else 1.0
        trend_up=last>=ema_fast>=ema_mid and ema_fast>=ema_slow
        trend_down=last<=ema_fast<=ema_mid and ema_fast<=ema_slow
        breakout_up=last>prior_high and volume_ratio>=0.75
        breakout_down=last<prior_low and volume_ratio>=0.75
        pullback_up=trend_up and lows[-1]<=ema_fast<=last and closes[-1]>=closes[-2]
        pullback_down=trend_down and highs[-1]>=ema_fast>=last and closes[-1]<=closes[-2]
        setup_type = "BREAKOUT" if (breakout_up or breakout_down) else "PULLBACK" if (pullback_up or pullback_down) else "MOMENTUM"
        structure=_structure_analysis(bars)
        signal=1 if (trend_up and (breakout_up or pullback_up or recent_move>0.001)) else -1 if (trend_down and (breakout_down or pullback_down or recent_move<-0.001)) else 0
        if structure.get("accepted") and structure.get("direction"):
            signal=int(structure["direction"])
        if signal==0:
            return {"accepted":False,"reason":"no clean senior trend/breakout/pullback setup","confidence":0}
        if structure.get("direction") and structure["direction"]!=signal:
            return {"accepted":False,"reason":"senior opportunity rejected: route conflicts with BOS/CHoCH structure",
                    "confidence":0,"structure":structure}
        mtf=self._multi_timeframe_confirmation(item,signal)
        if not mtf.get("accepted",True):
            return {"accepted":False,"reason":f"senior opportunity rejected: {mtf.get('reason','multi-timeframe conflict')}",
                    "confidence":0,"structure":structure,"multi_timeframe":mtf}
        case=session_case or {}
        bias=case.get("bias")
        case_name=str(case.get("case") or "neutral")
        if index_option and case_name.startswith("worst"):
            return {"accepted":False,"reason":f"session case {case_name} blocks index-option opportunity","confidence":0}
        if index_option and bias=="call" and signal<0:
            return {"accepted":False,"reason":"session call bias conflicts with put setup","confidence":0}
        if index_option and bias=="put" and signal>0:
            return {"accepted":False,"reason":"session put bias conflicts with call setup","confidence":0}
        risk=max(atr*.72,last*.0022)
        reward=max(atr*1.15,last*.004)
        rr=reward/risk if risk else 0
        components={}
        components["trend_alignment"]=20 if (trend_up and signal>0) or (trend_down and signal<0) else 8
        components["session_alignment"]=20 if index_option and ((bias=="call" and signal>0) or (bias=="put" and signal<0)) else 12 if index_option and case_name.startswith("neutral") else 10
        components["momentum"]=15 if abs(recent_move)>=0.0015 else 9 if abs(recent_move)>=0.0008 else 3
        components["breakout_or_pullback"]=15 if breakout_up or breakout_down else 11 if pullback_up or pullback_down else 6
        components["bos_choch_structure"]=18 if structure.get("accepted") else 8 if structure.get("bias")!="neutral" else 0
        components["volume"]=10 if volume_ratio>=1.2 else 7 if volume_ratio>=0.85 else 3
        components["risk_reward"]=15 if rr>=1.5 else 11 if rr>=1.25 else 4
        components["multi_timeframe"]=14 if mtf.get("aligned_frames",0)>=3 else 9 if mtf.get("aligned_frames",0)>=2 else 3
        probability=float(item.get("probability",0.5))
        ml_support=(probability>=0.5 and signal>0) or (probability<0.5 and signal<0)
        components["ml_context"]=0
        confidence=round(min(100,sum(components.values())),2)
        min_confidence=self.senior_index_min_confidence if index_option else self.senior_stock_min_confidence
        if confidence<min_confidence:
            return {"accepted":False,"reason":f"senior opportunity confidence {confidence:.1f} below {min_confidence:.1f}",
                    "confidence":confidence,"components":components,"signal":signal,"rr":round(rr,2),
                    "ml_support_observed":ml_support,
                    "session_case":case}
        route=("CE","BUY") if signal>0 else ("PE","BUY") if index_option else (None,"BUY" if signal>0 else "SELL")
        strategy=("SENIOR_INDEX_CALL_MOMENTUM" if signal>0 else "SENIOR_INDEX_PUT_MOMENTUM") if index_option else ("SENIOR_STOCK_LONG_MOMENTUM" if signal>0 else "SENIOR_STOCK_INTRADAY_SHORT_MOMENTUM")
        reason="senior opportunity scanner: high-confidence chart/session setup; paper learning evidence"
        if opening_range:
            reason+="; opening-range mode uses completed 5m bars plus current forming 5m built from live 1m bars"
        return {"accepted":True,"reason":reason,
                "strategy":strategy,"setup_type":setup_type,"option_type":route[0],"side":route[1],"rr":round(rr,2),
                "signal":signal,"confidence":confidence,"components":components,
                "ml_support_observed":ml_support,
                "session_case":case,"learning_mode":True,"senior_opportunity":True,
                "opening_range_scanner":opening_range,
                "not_promotion_evidence":opening_range,
                "structure":structure,
                "multi_timeframe":mtf,
                "stop_loss":round(last-risk,4) if signal>0 else round(last+risk,4),
                "take_profit":round(last+reward,4) if signal>0 else round(last-reward,4)}

    def _selector_score(self,item: Dict,quality: Dict,target: Dict) -> float:
        policy=item.get("policy_candidate")
        chart=item.get("chart_gate",{})
        policy_bonus=16 if getattr(policy,"accepted",False) else 0
        edge=float(getattr(policy,"expected_net_edge_bps",0) or 0)
        conviction=abs(float(item.get("probability",0.5))-0.5)*100
        rr=float(chart.get("rr") or 0)
        strict_bonus=8 if not chart.get("learning_mode") else 0
        option_bonus=4 if target.get("kind") in {"CE","PE"} else 0
        fallback_penalty=6 if target.get("fallback_from_option") else 0
        adaptive=item.get("_adaptive_gate") or {}
        adaptive_bonus=float(adaptive.get("reward_bonus") or 0)-float(adaptive.get("risk_penalty") or 0)
        sentiment=item.get("_sentiment_gate") or {}
        sentiment_bonus=float(sentiment.get("selector_adjustment") or 0)
        return round(float(quality.get("score") or 0)+policy_bonus+min(12,edge/4)+conviction+min(8,rr*2)+strict_bonus+option_bonus+adaptive_bonus+sentiment_bonus-fallback_penalty,4)

    def _adaptive_gate(self,item: Dict,target: Dict,quality: Dict) -> Dict:
        if not self.adaptive_learning_enabled:
            return {"accepted":True,"enabled":False}
        try:
            return trap_assessment(self.engine,item,target,quality,self.adaptive_min_samples,
                                   self.adaptive_max_loss_rate,self.adaptive_min_avg_reward)
        except Exception as exc:
            return {"accepted":True,"enabled":True,"reason":f"adaptive memory unavailable: {type(exc).__name__}"}

    def _daily_underlying_loss_counts(self,watermark: datetime) -> Dict[str,Dict[str,Any]]:
        with self.engine.connect() as connection:
            rows=connection.execute(text("""
                SELECT COALESCE(i.underlying_symbol,i.symbol) underlying_symbol,
                       COUNT(*) losses,
                       MAX(COALESCE(a.exit_at, a.signal_at)) latest_loss_at
                FROM shadow_execution_audits a JOIN instrument_master i ON i.id=a.instrument_id
                WHERE a.net_pnl IS NOT NULL
                  AND (a.net_pnl<0 OR a.exit_reason IN ('STOP_LOSS','TRAILING_STOP'))
                  AND (a.signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                GROUP BY COALESCE(i.underlying_symbol,i.symbol)
            """),{"watermark":watermark}).mappings().all()
        return {
            self._underlying_key({"symbol":row["underlying_symbol"]}): {
                "losses": int(row["losses"] or 0),
                "latest_loss_at": row.get("latest_loss_at"),
            }
            for row in rows
        }

    def _attach_opening_forming_5m(self,connection,instrument_id,watermark: datetime,intraday: List[Dict]) -> List[Dict]:
        """Add a paper-only forming 5m candle during the opening hour.

        Formal validation still uses completed 5m candles.  This helper is used
        only by opening-range learning/opportunity scans so 9:30-10:15 can read
        the current forming 5m candle plus the already completed bars.
        """
        if self._session_block(watermark)!="opening" or not intraday:
            return intraday
        last_completed=intraday[-1]["timestamp"]
        if watermark<=last_completed:
            return intraday
        rows=connection.execute(text("""
            SELECT bar_time,open_price,high_price,low_price,close_price,volume,open_interest
            FROM live_market_bars
            WHERE instrument_id=:instrument AND interval='1minute'
              AND source = ANY(:trade_sources)
              AND bar_time>:last_completed AND bar_time<=:watermark
              AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY bar_time
        """),{"instrument":instrument_id,"last_completed":last_completed,"watermark":watermark,
             "trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
        minute_bars=[_bar(row) for row in rows]
        if len(minute_bars)<2:
            return intraday
        forming={
            "timestamp":watermark,
            "open":minute_bars[0]["open"],
            "high":max(bar["high"] for bar in minute_bars),
            "low":min(bar["low"] for bar in minute_bars),
            "close":minute_bars[-1]["close"],
            "volume":sum(bar["volume"] for bar in minute_bars),
            "oi":minute_bars[-1]["oi"],
            "forming":True,
            "source":"opening_range_1m_forming_5m",
        }
        return [*intraday,forming]

    def _session_block(self,watermark: datetime) -> str:
        ist_time=watermark.astimezone(ZoneInfo("Asia/Kolkata")).time()
        if ist_time<datetime.strptime("09:45","%H:%M").time():
            return "opening"
        if ist_time<datetime.strptime("11:30","%H:%M").time():
            return "morning"
        if ist_time<datetime.strptime("13:30","%H:%M").time():
            return "midday"
        if ist_time<datetime.strptime("15:00","%H:%M").time():
            return "afternoon"
        return "closing"

    def _session_trade_state(self,watermark: datetime) -> Dict:
        session=self._session_block(watermark)
        if not self.session_trade_limit_enabled:
            return {
                "enabled":False,
                "session":session,
                "min_applicable_trades":self.session_min_applicable_trades,
                "max_applicable_trades":self.session_max_applicable_trades,
                "trades_taken":0,
                "remaining":999999,
                "target_remaining":0,
                "rule":"disabled",
            }
        with self.engine.connect() as connection:
            trades_taken=connection.execute(text("""
                WITH sessioned AS (
                    SELECT CASE
                        WHEN (signal_at AT TIME ZONE 'Asia/Kolkata')::time < TIME '09:45' THEN 'opening'
                        WHEN (signal_at AT TIME ZONE 'Asia/Kolkata')::time < TIME '11:30' THEN 'morning'
                        WHEN (signal_at AT TIME ZONE 'Asia/Kolkata')::time < TIME '13:30' THEN 'midday'
                        WHEN (signal_at AT TIME ZONE 'Asia/Kolkata')::time < TIME '15:00' THEN 'afternoon'
                        ELSE 'closing'
                    END session_block
                    FROM shadow_execution_audits
                    WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                )
                SELECT COUNT(*) FROM sessioned WHERE session_block=:session
            """),{"watermark":watermark,"session":session}).scalar_one()
        trades_taken=int(trades_taken or 0)
        remaining=max(0,self.session_max_applicable_trades-trades_taken)
        target_remaining=max(0,self.session_min_applicable_trades-trades_taken)
        return {
            "enabled":True,
            "session":session,
            "min_applicable_trades":self.session_min_applicable_trades,
            "max_applicable_trades":self.session_max_applicable_trades,
            "trades_taken":trades_taken,
            "remaining":remaining,
            "target_remaining":target_remaining,
            "blocked":remaining<=0,
            "rule":"Take only applicable trades; target 7 and cap 10 per session block.",
        }

    def _session_case(self,watermark: datetime) -> Dict:
        if not self.session_case_enabled:
            return {"enabled":False,"session":"disabled","case":"neutral","bias":"none",
                    "strategy_mode":"disabled","allowed_routes":[],
                    "learning_min_rr":self.min_professional_rr,
                    "learning_min_quality":self.min_entry_quality,
                    "learning_min_edge_bps":self.min_expected_net_edge_bps}
        session=self._session_block(watermark)
        session_starts={"opening":"09:15","morning":"09:45","midday":"11:30","afternoon":"13:30","closing":"15:00"}
        with self.engine.connect() as connection:
            rows=connection.execute(text("""
                WITH params AS (
                    SELECT
                        (:watermark AT TIME ZONE 'Asia/Kolkata')::date trade_date,
                        (:watermark AT TIME ZONE 'Asia/Kolkata')::timestamp watermark_ist,
                        (:session_start)::time session_start
                )
                SELECT b.bar_time,b.open_price,b.high_price,b.low_price,b.close_price,b.volume,b.open_interest
                FROM live_market_bars b
                JOIN instrument_master i ON i.id=b.instrument_id
                JOIN params p ON TRUE
                WHERE i.instrument_type='INDEX'
                  AND REPLACE(i.symbol,' ','')='NIFTY50'
                  AND b.interval='5minute'
                  AND b.source = ANY(:trade_sources)
                  AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date=p.trade_date
                  AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::timestamp>=p.trade_date+p.session_start
                  AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::timestamp<=p.watermark_ist
                ORDER BY b.bar_time
            """),{"watermark":watermark,"session_start":session_starts[session],
                 "trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
            bars=[_bar(row) for row in rows]
            if len(bars)<4:
                continuous_rows=connection.execute(text("""
                    WITH params AS (
                        SELECT
                            (:watermark AT TIME ZONE 'Asia/Kolkata')::date trade_date,
                            (:watermark AT TIME ZONE 'Asia/Kolkata')::timestamp watermark_ist
                    )
                    SELECT b.bar_time,b.open_price,b.high_price,b.low_price,b.close_price,b.volume,b.open_interest
                    FROM live_market_bars b
                    JOIN instrument_master i ON i.id=b.instrument_id
                    JOIN params p ON TRUE
                    WHERE i.instrument_type='INDEX'
                      AND REPLACE(i.symbol,' ','')='NIFTY50'
                      AND b.interval='5minute'
                      AND b.source = ANY(:trade_sources)
                      AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date=p.trade_date
                      AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::timestamp<=p.watermark_ist
                    ORDER BY b.bar_time DESC
                    LIMIT 12
                """),{"watermark":watermark,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
                if len(continuous_rows)>=4:
                    bars=list(reversed([_bar(row) for row in continuous_rows]))
                else:
                    return {"enabled":True,"session":session,"case":"neutral_insufficient_data","bias":"none",
                            "strategy_mode":"wait_for_session_structure","allowed_routes":["ANALYSIS_ONLY"],
                            "bars":len(bars),"learning_min_rr":self.learning_neutral_min_rr,
                            "learning_min_quality":self.learning_neutral_min_quality,
                            "learning_min_edge_bps":self.learning_neutral_min_edge_bps}
        closes=[bar["close"] for bar in bars]
        highs=[bar["high"] for bar in bars]
        lows=[bar["low"] for bar in bars]
        open_price=bars[0]["open"] or closes[0]
        last=closes[-1]
        move_pct=(last/open_price-1) if open_price else 0
        recent_base=closes[-4] if len(closes)>=4 else closes[0]
        recent_pct=(last/recent_base-1) if recent_base else 0
        range_pct=((max(highs)-min(lows))/last) if last else 0
        ema_fast=_ema(closes,5)[-1]
        ema_slow=_ema(closes,13)[-1]
        direction_changes=sum(
            1 for index in range(2,len(closes))
            if (closes[index]-closes[index-1])*(closes[index-1]-closes[index-2])<0
        )
        choppy=direction_changes>=max(3,len(closes)//3) and abs(move_pct)<0.002
        if range_pct>0.012 or choppy:
            case="worst_choppy"; bias="none"
            min_rr=self.learning_worst_min_rr; min_quality=self.learning_worst_min_quality; min_edge=self.learning_worst_min_edge_bps
            strategy_mode="defensive_no_new_options"
            allowed_routes=["EQ_ONLY_STRONG_STRUCTURE"]
        elif move_pct>=0.0025 and recent_pct>=0.0008 and last>=ema_fast>=ema_slow:
            case="best_call_momentum"; bias="call"
            min_rr=self.learning_best_min_rr; min_quality=self.learning_best_min_quality; min_edge=self.learning_best_min_edge_bps
            strategy_mode="bull_trend_follow"
            allowed_routes=["CE_BUY","PE_SELL","EQ_BUY"]
        elif move_pct<=-0.0025 and recent_pct<=-0.0008 and last<=ema_fast<=ema_slow:
            case="best_put_momentum"; bias="put"
            min_rr=self.learning_best_min_rr; min_quality=self.learning_best_min_quality; min_edge=self.learning_best_min_edge_bps
            strategy_mode="bear_trend_follow"
            allowed_routes=["PE_BUY","CE_SELL","EQ_INTRADAY_SHORT"]
        elif abs(move_pct)<0.0015 and range_pct<0.004:
            case="neutral_range"; bias="range"
            min_rr=self.learning_neutral_min_rr; min_quality=self.learning_neutral_min_quality; min_edge=self.learning_neutral_min_edge_bps
            strategy_mode="range_selective"
            allowed_routes=["PE_SELL","CE_SELL","EQ_ONLY_STRONG_STRUCTURE"]
        else:
            case="neutral_mixed"; bias="none"
            min_rr=self.learning_neutral_min_rr; min_quality=self.learning_neutral_min_quality; min_edge=self.learning_neutral_min_edge_bps
            strategy_mode="mixed_selective"
            allowed_routes=["CE_BUY","PE_BUY","EQ_BUY","EQ_INTRADAY_SHORT"]

        regime_classification = None
        regime_name = "NEUTRAL"
        try:
            from backend.regime_router import RegimeRouter
            router = RegimeRouter()
            regime_classification = router.classify(bars, symbol="NIFTY")
            if regime_classification and regime_classification.get("regime"):
                is_fallback = bool(
                    regime_classification.get("is_fallback")
                    or "reason" in (regime_classification.get("metrics") or {})
                    or regime_classification.get("symbol") == "UNKNOWN"
                )
                if is_fallback:
                    regime_name = "NEUTRAL"
                else:
                    regime_name = regime_classification.get("regime")
        except Exception:
            regime_classification = None
            regime_name = "NEUTRAL"

        return {"enabled":True,"session":session,"case":case,"bias":bias,"bars":len(bars),
                "move_pct":round(move_pct,5),"recent_pct":round(recent_pct,5),
                "range_pct":round(range_pct,5),"direction_changes":direction_changes,
                "strategy_mode":strategy_mode,"allowed_routes":allowed_routes,
                "learning_min_rr":min_rr,"learning_min_quality":min_quality,
                "learning_min_edge_bps":min_edge,
                "regime_classification":regime_classification,
                "regime_name":regime_name}

    def _effective_thresholds(self,item: Dict,chart: Dict,policy,session_case: Dict) -> Dict:
        learning_candidate=bool(chart.get("learning_mode") or item.get("probe_trade") or not getattr(policy,"accepted",False))
        thresholds={"learning_candidate":learning_candidate,"case":(session_case or {}).get("case","neutral"),
                    "session":(session_case or {}).get("session","unknown"),
                    "min_rr":self.min_professional_rr,
                    "min_quality":self.min_entry_quality,
                    "min_option_quality":self.min_option_entry_quality,
                    "min_edge_bps":self.min_expected_net_edge_bps}
        if learning_candidate:
            thresholds["min_rr"]=float((session_case or {}).get("learning_min_rr",self.learning_neutral_min_rr))
            thresholds["min_quality"]=float((session_case or {}).get("learning_min_quality",self.learning_neutral_min_quality))
            thresholds["min_option_quality"]=max(55.0,min(self.min_option_entry_quality,thresholds["min_quality"]))
            thresholds["min_edge_bps"]=float((session_case or {}).get("learning_min_edge_bps",self.learning_neutral_min_edge_bps))
        case=str((session_case or {}).get("case") or "neutral")
        if case.startswith("best_"):
            thresholds["session_mode"]="trend_follow"
            thresholds["min_option_quality"]=max(float(self.option_grade_b_score),float(thresholds["min_option_quality"]))
        elif case.startswith("neutral_range"):
            thresholds["session_mode"]="range_selective"
            thresholds["min_rr"]=max(float(thresholds["min_rr"]),1.65)
            thresholds["min_option_quality"]=max(float(thresholds["min_option_quality"]),float(self.option_grade_b_score))
        elif case.startswith("worst_"):
            thresholds["session_mode"]="defensive"
            thresholds["min_rr"]=max(float(thresholds["min_rr"]),2.05)
            thresholds["min_quality"]=max(float(thresholds["min_quality"]),72.0)
            thresholds["min_option_quality"]=max(float(thresholds["min_option_quality"]),74.0)
            thresholds["min_edge_bps"]=max(float(thresholds["min_edge_bps"]),float(self.min_expected_net_edge_bps))
            thresholds["option_entries_blocked"]=True
        else:
            thresholds["session_mode"]="mixed_selective"

        # Phase C: 3-Regime Router Active Strategy Gating
        regime_name = (session_case or {}).get("regime_name")
        if regime_name:
            regime_name = str(regime_name)
            thresholds["regime_name"] = regime_name
            if regime_name == "RANGE_MEAN_REVERSION":
                thresholds["suppress_breakouts"] = True
                thresholds["min_rr"] = max(float(thresholds["min_rr"]), 1.65)
            elif regime_name == "TREND_CONTINUATION":
                thresholds["suppress_fades"] = True
                thresholds["session_mode"] = "trend_follow"
            elif regime_name in ("HIGH_VOL_DEFENSE", "HIGH_VOLATILITY_DEFENSE"):
                thresholds["defensive_vol_regime"] = True
                reg_class = (session_case or {}).get("regime_classification") or {}
                risk_pol = (session_case or {}).get("risk_policy") or reg_class.get("risk_policy") or {}
                if risk_pol.get("allow_new_entries") is False or (session_case or {}).get("allow_new_entries") is False:
                    thresholds["block_new_entries"] = True
                thresholds["min_quality"] = max(float(thresholds["min_quality"]), 72.0)
                thresholds["min_rr"] = max(float(thresholds["min_rr"]), 2.0)

        # Phase C: Real-Time GEX Engine & Zero-Gamma Flip Point Modulation
        thresholds["stop_multiplier"] = 1.0
        thresholds["size_multiplier"] = 1.0
        try:
            from backend.gex_engine import TOP_HEAVYWEIGHTS, get_cached_or_compute_gex
            sym = self._underlying_key(item)
            if sym in TOP_HEAVYWEIGHTS:
                gex = get_cached_or_compute_gex(sym, engine=getattr(self, "engine", None))
                g_regime = str(gex.get("regime") or "LONG_GAMMA_MEAN_REVERSION")
                zgf = gex.get("zero_gamma_flip")
                g_flip = float(zgf) if zgf is not None else 0.0
                s_mult = float(gex.get("stop_multiplier", 1.0))
                sz_mult = float(gex.get("size_multiplier", 1.0))
                thresholds["gex_regime"] = g_regime
                thresholds["gex_flip_strike"] = g_flip
                thresholds["stop_multiplier"] = s_mult
                thresholds["size_multiplier"] = sz_mult
                if g_regime == "VOLATILITY_FLIP_DEFENSE":
                    thresholds["min_quality"] = max(float(thresholds["min_quality"]), 70.0)
        except Exception:
            thresholds["stop_multiplier"] = 1.0
            thresholds["size_multiplier"] = 1.0

        return thresholds

    def _market_quality_gate(self,item: Dict,target: Dict) -> Dict:
        bars=item.get("intraday") or []
        checks=[]
        if len(bars)>=8 and Decimal(str(item["session"]["close"]))>0:
            atr=sum(Decimal(str(bar["high"]-bar["low"])) for bar in bars[-8:])/Decimal("8")
            atr_pct=atr/Decimal(str(item["session"]["close"]))
            if atr_pct>self.max_intraday_atr_pct:
                return {"accepted":False,"reason":f"intraday ATR {atr_pct:.4f} exceeds {self.max_intraday_atr_pct}"}
            checks.append(f"ATR {atr_pct:.4f}")
        if target.get("kind") in {"CE","PE"}:
            with self.engine.connect() as connection:
                row=connection.execute(text("""
                    SELECT i.expiry,i.exchange,
                           COALESCE(SUM(b.volume),0) volume,
                           COALESCE(MAX(b.open_interest),0) open_interest,
                           COALESCE(AVG(NULLIF(b.high_price-b.low_price,0)),0) avg_5m_range,
                           COUNT(DISTINCT b.bar_time) option_5m_bars
                    FROM instrument_master i
                    LEFT JOIN live_market_bars b ON b.instrument_id=i.id
                      AND b.interval='5minute'
                      AND b.source = ANY(:trade_sources)
                      AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                    WHERE i.id=:instrument
                    GROUP BY i.expiry,i.exchange
                """),{"instrument":target["instrument_id"],"watermark":item["session"]["timestamp"],
                     "trade_sources":list(TRADE_BAR_SOURCES)}).mappings().one_or_none()
                coverage=connection.execute(text("""
                    WITH params AS (
                        SELECT
                            (:watermark AT TIME ZONE 'Asia/Kolkata')::date trade_date,
                            (:watermark AT TIME ZONE 'Asia/Kolkata')::timestamp watermark_ist,
                            ((:watermark AT TIME ZONE 'Asia/Kolkata')::timestamp - (:recent_minutes || ' minutes')::interval) recent_start_ist
                    ),
                    one_minute AS (
                        SELECT DISTINCT date_trunc('minute', b.bar_time AT TIME ZONE 'Asia/Kolkata') minute_ist
                        FROM live_market_bars b, params p
                        WHERE b.instrument_id=:instrument
                          AND b.interval='1minute'
                          AND b.source = ANY(:coverage_sources)
                          AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::date=p.trade_date
                          AND (b.bar_time AT TIME ZONE 'Asia/Kolkata')::timestamp<=p.watermark_ist
                    )
                    SELECT
                        COUNT(*) FILTER (WHERE minute_ist>=p.recent_start_ist) recent_bars,
                        COUNT(*) session_bars,
                        GREATEST(1,LEAST(CAST(:recent_minutes AS integer),
                            EXTRACT(EPOCH FROM (p.watermark_ist-GREATEST(p.recent_start_ist,p.trade_date+TIME '09:15')))/60::numeric + 1
                        )) recent_expected,
                        GREATEST(1,
                            EXTRACT(EPOCH FROM (p.watermark_ist-(p.trade_date+TIME '09:15')))/60::numeric + 1
                        ) session_expected
                    FROM params p
                    LEFT JOIN one_minute m ON TRUE
                    GROUP BY p.recent_start_ist,p.watermark_ist,p.trade_date
                """),{"instrument":target["instrument_id"],"watermark":item["session"]["timestamp"],
                     "recent_minutes":self.option_recent_coverage_minutes,
                     "coverage_sources":list(self.option_coverage_sources)}).mappings().one()
            if not row:
                return {"accepted":False,"reason":"option contract could not be verified in instrument master"}
            is_analytical = bool((target.get("greeks") or {}).get("source") == "analytical_black_scholes" or target.get("source") == "analytical_black_scholes")
            recent_pct = Decimal("0")
            session_pct = Decimal("0")
            if not is_analytical:
                recent_pct=(Decimal(int(coverage["recent_bars"] or 0))/Decimal(str(coverage["recent_expected"] or 1)))*Decimal("100")
                session_pct=(Decimal(int(coverage["session_bars"] or 0))/Decimal(str(coverage["session_expected"] or 1)))*Decimal("100")
                if recent_pct<self.min_option_recent_1m_coverage_pct:
                    return {"accepted":False,"reason":f"option recent 1m coverage {recent_pct:.2f}% below {self.min_option_recent_1m_coverage_pct}%"}
                if session_pct<self.min_option_session_1m_coverage_pct:
                    return {"accepted":False,"reason":f"option session 1m coverage {session_pct:.2f}% below {self.min_option_session_1m_coverage_pct}%"}
                checks.append(f"option recent 1m coverage {recent_pct:.2f}%")
                checks.append(f"option session 1m coverage {session_pct:.2f}%")
            else:
                checks.append("analytical Black-Scholes pricing")
            expiry=row["expiry"]
            if expiry:
                expiry_date=expiry.date() if hasattr(expiry,"date") else datetime.fromisoformat(str(expiry)).date()
                days=(expiry_date-item["session"]["timestamp"].astimezone(ZoneInfo("Asia/Kolkata")).date()).days
                if days<0:
                    return {"accepted":False,"reason":"option contract is expired"}
                if self.max_option_days_to_expiry and days>self.max_option_days_to_expiry:
                    return {"accepted":False,"reason":f"option expiry {days} days away exceeds {self.max_option_days_to_expiry}"}
                checks.append(f"expiry D+{days}")
            volume=int(row["volume"] or 0)
            oi=int(row["open_interest"] or 0)
            if volume<self.min_option_volume:
                return {"accepted":False,"reason":f"option volume {volume} below {self.min_option_volume}"}
            if self.min_option_oi and oi<self.min_option_oi:
                return {"accepted":False,"reason":f"option OI {oi} below {self.min_option_oi}"}
            checks.append(f"option volume {volume}")
            if oi:
                checks.append(f"OI {oi}")
            option_price=Decimal(str(target.get("price") or 0))
            if option_price<=0:
                return {"accepted":False,"reason":"option price is unavailable for noise/cost gate"}
            avg_range=Decimal(str(row["avg_5m_range"] or 0))
            risk_info = self._risk_levels_for_target(item, target)
            chart_sl = risk_info.get("stop_loss")
            if chart_sl and Decimal(str(chart_sl)) > 0 and Decimal(str(chart_sl)) != option_price:
                stop_distance = abs(option_price - Decimal(str(chart_sl)))
            else:
                stop_distance = option_price * self.option_stop_loss_pct

            chart_tp = risk_info.get("take_profit")
            if chart_tp and Decimal(str(chart_tp)) > 0 and Decimal(str(chart_tp)) != option_price:
                reward_distance = abs(Decimal(str(chart_tp)) - option_price)
            else:
                reward_distance = option_price * self.option_take_profit_pct

            if avg_range>0 and stop_distance<avg_range*self.option_stop_noise_atr_multiple:
                return {"accepted":False,
                        "reason":f"option stop ₹{stop_distance:.2f} is inside normal 5m noise ₹{avg_range:.2f} x {self.option_stop_noise_atr_multiple}"}
            quantity=max(1,int(target.get("lot_size") or 1))
            venue="NSE" if row["exchange"] in {"NSE","NFO"} else "BSE"
            estimated_round_trip=(
                estimate_zerodha_costs("OPTIONS",target.get("side","BUY"),option_price,quantity,venue).total
                + estimate_zerodha_costs("OPTIONS","SELL" if target.get("side","BUY")=="BUY" else "BUY",option_price,quantity,venue).total
            )
            expected_reward=reward_distance*Decimal(quantity)
            expected_capture=max(expected_reward,self.option_profit_capture_rupees)
            stop_loss_amount=stop_distance*Decimal(quantity)
            if stop_loss_amount>self.max_option_loss_rupees:
                return {"accepted":False,
                        "reason":f"option max-loss ₹{stop_loss_amount:.2f} exceeds cap ₹{self.max_option_loss_rupees:.2f}"}
            if expected_capture>0 and stop_loss_amount>expected_capture*self.option_stop_to_capture_max_ratio:
                return {"accepted":False,
                        "reason":f"option stop ₹{stop_loss_amount:.2f} exceeds {self.option_stop_to_capture_max_ratio}x expected capture ₹{expected_capture:.2f}"}
            if expected_reward>0 and estimated_round_trip>expected_reward*self.option_max_fee_to_reward_ratio:
                return {"accepted":False,
                        "reason":f"estimated fees ₹{estimated_round_trip:.2f} exceed {self.option_max_fee_to_reward_ratio:.0%} of expected reward ₹{expected_reward:.2f}"}
            checks.append(f"option stop/noise {stop_distance:.2f}/{avg_range:.2f}")
            checks.append(f"option max loss/capture {stop_loss_amount:.2f}/{expected_capture:.2f}")
            checks.append(f"fee/reward {estimated_round_trip:.2f}/{expected_reward:.2f}")
            option_checks={
                "recent_1m_coverage_pct":str(recent_pct.quantize(Decimal("0.01"))),
                "session_1m_coverage_pct":str(session_pct.quantize(Decimal("0.01"))),
                "volume":volume,
                "open_interest":oi,
                "avg_5m_range":str(avg_range),
                "stop_distance":str(stop_distance),
                "expected_reward":str(expected_reward),
                "expected_capture":str(expected_capture),
                "max_loss_amount":str(stop_loss_amount),
                "estimated_round_trip_fees":str(estimated_round_trip),
            }
        raw=self.redis.get(f"nivesh:depth:{int(target['instrument_token'])}") if target.get("instrument_token") else None
        if raw:
            try:
                depth=json.loads(raw)
            except Exception:
                depth={}
            bid=Decimal(str(depth.get("best_bid"))) if depth.get("best_bid") is not None else None
            ask=Decimal(str(depth.get("best_ask"))) if depth.get("best_ask") is not None else None
            if bid is not None and ask is not None and bid>0 and ask>=bid:
                mid=(bid+ask)/Decimal("2")
                spread_bps=(ask-bid)/mid*Decimal("10000")
                spread_limit=self.max_option_spread_bps if target.get("kind") in {"CE","PE"} else self.max_spread_bps
                if spread_bps>spread_limit:
                    return {"accepted":False,"reason":f"spread {spread_bps:.2f} bps exceeds {spread_limit} bps"}
                return {"accepted":True,"spread_bps":str(round(spread_bps,2)),"depth_available":True,
                        "option_checks":locals().get("option_checks",{}),
                        "checks":checks}
        if target.get("kind") in {"CE","PE"}:
            is_analytical = bool((target.get("greeks") or {}).get("source") == "analytical_black_scholes" or target.get("source") == "analytical_black_scholes")
            if os.getenv("NIVESH_SHADOW_ALLOW_SYNTHETIC_OPTION_DEPTH", "1") == "1":
                return {"accepted":True,"spread_bps":"40.0","depth_available":False,"synthetic_depth":True,"is_synthetic":is_analytical,
                        "option_checks":locals().get("option_checks",{}),"checks":checks}
            return {"accepted":False,"reason":"option depth missing; spread cannot be verified"}
        return {"accepted":True,"depth_available":False,"reason":"equity depth unavailable; using completed-bar fallback",
                "checks":checks}

    def _sentiment_gate(self,item: Dict,target: Dict = None) -> Dict:
        return evaluate_trade_sentiment(
            self.engine,
            item,
            target,
            max_age_hours=self.sentiment_max_age_hours,
            min_confidence=self.sentiment_min_confidence,
            gate_enabled=self.sentiment_gate_enabled,
            require_verified=self.require_verified_sentiment,
        )

    def _persist_candidate_audits(self, rows: List[Dict]) -> Dict:
        if not rows:
            return {"inserted":0}
        try:
            with self.engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO trade_candidate_audits(
                        observed_at,model_version,exchange,symbol,instrument_id,instrument_type,signal,probability,
                        decision_price,selector_stage,accepted,rejection_reason,chart_strategy,route,rr,
                        expected_net_edge_bps,quality_score,selector_score,sector,details,
                        trade_mode,agent_deliberation
                    ) VALUES (
                        :observed_at,:model_version,:exchange,:symbol,:instrument_id,:instrument_type,:signal,:probability,
                        :decision_price,:selector_stage,:accepted,:rejection_reason,:chart_strategy,:route,:rr,
                        :expected_net_edge_bps,:quality_score,:selector_score,:sector,CAST(:details AS jsonb),
                        :trade_mode,CAST(:agent_deliberation AS jsonb)
                    )
                    ON CONFLICT DO NOTHING
                """), rows[:1000])
                memory=refresh_intelligence_memory(connection)
                rejected=sum(1 for row in rows if not row.get("accepted"))
                accepted=sum(1 for row in rows if row.get("accepted"))
                top_reason=Counter(str(row.get("rejection_reason") or "accepted") for row in rows if not row.get("accepted")).most_common(1)
                if rejected:
                    publish_brain_event(connection,"CandidateRejected","LivePaperInference",
                                        {"evaluated":len(rows),"accepted":accepted,"rejected":rejected,
                                         "top_reason":top_reason[0][0] if top_reason else None,
                                         "memory":memory},
                                        severity="INFO")
                    try:
                        from .brains import get_bus
                        from .brains.bus import CandidateRejected
                        sample=next((row for row in rows if not row.get("accepted")), {})
                        reason = str(sample.get("rejection_reason") or (top_reason[0][0] if top_reason else "rejected"))
                        get_bus().publish(CandidateRejected(
                            source_brain="LivePaperInference",
                            symbol=str(sample.get("symbol") or ""),
                            reason=reason,
                            strategy=str(sample.get("chart_strategy") or ""),
                            probability=float(sample.get("probability") or 0),
                            quality_score=float(sample.get("quality_score") or 0),
                            payload={"evaluated":len(rows),"accepted":accepted,"rejected":rejected},
                        ))
                    except Exception:
                        pass
            return {"inserted":len(rows[:1000]),"truncated":max(0,len(rows)-1000)}
        except Exception as exc:
            return {"inserted":0,"error":type(exc).__name__}

    def _calculate_weighted_ensemble_score(
        self,
        item: Dict,
        target: Dict,
        chart: Dict,
        policy: Any,
        thresholds: Dict,
        consistency: Dict,
        nifty_trend: int,
        stock_alpha: bool,
    ) -> Tuple[float, Dict[str, Dict]]:
        """Calculate 10-component weighted ensemble score (0-100 pts) matching MASTER_COMPREHENSIVE_PLAN.md §6.
        
        Hard risk/safety gates (execution_route, portfolio limits, directional balance, sector caps,
        option risk ceilings, morning cooloff) are evaluated separately as mandatory vetoes.
        """
        breakdown = {}
        cg_reason = str(chart.get("reason", "") or "")
        
        # 1. Chart Gate Quality (15 pts) - Core chart pattern formation & confirmation
        cg_accepted = bool(chart.get("accepted"))
        cg_quality = float(chart.get("quality", 0) or 0)
        if cg_accepted:
            s_chart = 15.0
        elif "less than 6 completed 5m" in cg_reason or "no instrument-local" in cg_reason:
            s_chart = 0.0
        elif cg_quality > 0:
            s_chart = min(15.0, round(cg_quality * 0.15, 2))
        else:
            s_chart = 5.0
        breakdown["chart_gate_quality"] = {
            "score": s_chart, "max": 15.0, "pass": cg_accepted,
            "reason": "full confirmation" if cg_accepted else cg_reason
        }

        # 2. Risk:Reward Margin (15 pts) - Mathematical expectancy
        risk_levels = self._risk_levels_for_target(item, target)
        effective_rr = float(risk_levels.get("rr") if risk_levels.get("rr") is not None else (chart.get("rr") or 0))
        target_rr = float(thresholds.get("min_rr", 1.90))
        if effective_rr >= target_rr:
            s_rr = 15.0
        elif effective_rr >= 1.20:
            s_rr = round(15.0 * (effective_rr - 1.20) / max(0.1, target_rr - 1.20), 2)
        else:
            s_rr = 0.0
        breakdown["risk_reward_margin"] = {
            "score": s_rr, "max": 15.0, "pass": effective_rr >= target_rr,
            "value": round(effective_rr, 2), "target": round(target_rr, 2)
        }

        # 3. Expected Net Edge Margin (15 pts) - Statistical edge net of costs
        policy_edge = float(getattr(policy, "expected_net_edge_bps", 0) or 0) if policy else float(item.get("expected_net_edge_bps", 0) or 0)
        target_edge = float(thresholds.get("min_edge_bps", 25.0))
        if policy_edge >= target_edge:
            s_edge = 15.0
        elif policy_edge > 0:
            s_edge = round(15.0 * policy_edge / max(1.0, target_edge), 2)
        else:
            s_edge = 0.0
        breakdown["net_edge_margin"] = {
            "score": s_edge, "max": 15.0, "pass": policy_edge >= target_edge,
            "value": round(policy_edge, 1), "target": round(target_edge, 1)
        }

        # 4. Multi-Timeframe (MTF) Alignment (15 pts) - Higher timeframe confluence
        mtf = consistency.get("multi_timeframe") or {}
        aligned_frames = int(mtf.get("aligned_frames", 0) or 0)
        ready_frames = int(mtf.get("ready_frames", 0) or 0)
        mtf_reason = str(consistency.get("reason", "") or "")
        if "multi-timeframe" in mtf_reason or "timeframe" in mtf_reason:
            s_mtf = 5.0 if "1 timeframe" in mtf_reason else 0.0
        elif aligned_frames >= 3:
            s_mtf = 15.0
        elif aligned_frames == 2:
            s_mtf = 10.0
        elif aligned_frames == 1:
            s_mtf = 5.0
        else:
            s_mtf = 10.0 if not mtf.get("strong_conflict") else 0.0
        breakdown["mtf_alignment"] = {
            "score": s_mtf, "max": 15.0, "pass": s_mtf >= 10.0,
            "aligned_frames": aligned_frames, "ready_frames": ready_frames
        }

        # 5. Nifty Index Trend (10 pts) - Market beta alignment
        tgt_side = str(target.get("side", "")).upper()
        tgt_kind = str(target.get("kind", "")).upper()
        is_derivative = tgt_kind in {"CE", "PE", "FUT"}
        if is_derivative:
            underlying_dir = -1 if (tgt_kind == "PE" and tgt_side == "BUY") or (tgt_kind == "CE" and tgt_side == "SELL") else 1
        else:
            underlying_dir = 1 if tgt_side == "BUY" else -1
            
        if nifty_trend == underlying_dir:
            s_nifty = 10.0
        elif nifty_trend == 0:
            s_nifty = 7.0
        elif stock_alpha:
            s_nifty = 5.0
        else:
            s_nifty = 0.0
        breakdown["nifty_index_trend"] = {
            "score": s_nifty, "max": 10.0, "pass": s_nifty >= 5.0,
            "nifty_trend": nifty_trend, "trade_dir": underlying_dir, "stock_alpha": stock_alpha
        }

        # 6. VWAP Overextension Filter (10 pts) - Mean reversion risk
        is_vwap_overextended = "overextended" in cg_reason or "VWAP" in cg_reason
        s_vwap = 0.0 if is_vwap_overextended else 10.0
        breakdown["vwap_overextension"] = {
            "score": s_vwap, "max": 10.0, "pass": not is_vwap_overextended,
            "reason": cg_reason if is_vwap_overextended else "within normal volatility band"
        }

        # 7. ASI Divergence / Trap (5 pts) - Breakout vs liquidity sweep
        is_asi_trap = "ASI" in cg_reason or "trap" in cg_reason.lower()
        s_asi = 0.0 if is_asi_trap else 5.0
        breakdown["asi_divergence"] = {
            "score": s_asi, "max": 5.0, "pass": not is_asi_trap,
            "reason": cg_reason if is_asi_trap else "no sweep trap"
        }

        # 8. Wyckoff RVOL Volume Spread (5 pts) - Volume expansion
        is_wyckoff_div = "Wyckoff" in cg_reason or "RVOL" in cg_reason
        s_rvol = 0.0 if is_wyckoff_div else 5.0
        breakdown["wyckoff_rvol"] = {
            "score": s_rvol, "max": 5.0, "pass": not is_wyckoff_div,
            "reason": cg_reason if is_wyckoff_div else "volume confirms spread"
        }

        # 9. Volume Profile Value Area (5 pts) - VAH / VAL location
        is_vp_exhaustion = "Volume Profile" in cg_reason or "VAH" in cg_reason or "VAL" in cg_reason
        s_vp = 0.0 if is_vp_exhaustion else 5.0
        breakdown["volume_profile_value_area"] = {
            "score": s_vp, "max": 5.0, "pass": not is_vp_exhaustion,
            "reason": cg_reason if is_vp_exhaustion else "favourable value area location"
        }

        # 10. ADX Chop Filter (5 pts) - Trending vs consolidation
        is_choppy = "ADX" in cg_reason or "choppy" in cg_reason
        s_adx = 0.0 if is_choppy else 5.0
        breakdown["adx_chop"] = {
            "score": s_adx, "max": 5.0, "pass": not is_choppy,
            "reason": cg_reason if is_choppy else "trending market environment"
        }

        total_score = round(s_chart + s_rr + s_edge + s_mtf + s_nifty + s_vwap + s_asi + s_rvol + s_vp + s_adx, 2)
        return total_score, breakdown

    def _select_top_trade_candidates(self,created: List[Dict],open_state: Dict,risk_state: Dict,
                                     available_slots: int,watermark: datetime,model_version: str = "",
                                     session_case: Optional[Dict] = None) -> Dict:
        if available_slots<=0:
            return {"selected":[],"rejected":0,"report":{"evaluated":0,"accepted":0,"rejected":0,"reason":"no available slots"}}
        open_underlyings={str(value).upper() for value in open_state.get("underlyings",set())}
        underlying_counts=defaultdict(int,{str(key).upper():int(value) for key,value in open_state.get("underlying_counts",{}).items()})
        side_counts=defaultdict(int,{str(key).upper():int(value) for key,value in open_state.get("side_counts",{}).items()})
        sector_counts=defaultdict(int,{str(key).upper():int(value) for key,value in open_state.get("sector_counts",{}).items()})
        cooldown={str(value).upper() for value in risk_state.get("cooldown_underlyings",set())}
        loss_counts=self._daily_underlying_loss_counts(watermark)

        # Macro Market Trend Gate (Nifty 50 5m Alignment)
        nifty_trend = 0  # 1 = bullish, -1 = bearish, 0 = neutral
        try:
            with self.engine.connect() as conn:
                n_bars = conn.execute(text("""
                    SELECT b.close_price FROM live_market_bars b
                    JOIN instrument_master i ON i.id = b.instrument_id
                    WHERE i.symbol = 'NIFTY 50' AND b.interval = '5minute' AND b.bar_time <= :wm
                    ORDER BY b.bar_time DESC LIMIT 25
                """), {"wm": watermark}).fetchall()
                if len(n_bars) >= 21:
                    n_closes = [float(r[0]) for r in reversed(n_bars)]
                    n_ema9 = _ema(n_closes, 9)[-1]
                    n_ema21 = _ema(n_closes, 21)[-1]
                    if n_ema9 > n_ema21 and n_closes[-1] >= n_ema9:
                        nifty_trend = 1
                    elif n_ema9 < n_ema21 and n_closes[-1] <= n_ema9:
                        nifty_trend = -1
        except Exception:
            nifty_trend = 0

        eligible=[]; rejected=0; reject_reasons=defaultdict(int); audit_rows=[]
        def audit(item: Dict, stage: str, accepted: bool = False, reason: str = "", target: Dict = None,
                  quality: Dict = None, market_quality: Dict = None, sentiment: Dict = None,
                  adaptive: Dict = None, selector_score=None, sector: str = None, thresholds: Dict = None,
                  consistency: Dict = None):
            chart=item.get("chart_gate") or {}
            policy=item.get("policy_candidate")
            audit_rows.append({
                "observed_at":watermark,
                "model_version":model_version or "unknown",
                "exchange":item.get("exchange"),
                "symbol":item.get("symbol"),
                "instrument_id":item.get("instrument_id"),
                "instrument_type":item.get("instrument_type"),
                "signal":int(item.get("signal") or 0),
                "probability":Decimal(str(item.get("probability") or 0)),
                "decision_price":Decimal(str((item.get("session") or {}).get("close") or 0)),
                "selector_stage":stage,
                "accepted":bool(accepted),
                "rejection_reason":reason or None,
                "chart_strategy":chart.get("strategy"),
                "route":f"{(target or {}).get('side','')} {(target or {}).get('kind','')}".strip() or None,
                "rr":Decimal(str(chart.get("rr") or 0)),
                "expected_net_edge_bps":Decimal(str(getattr(policy,"expected_net_edge_bps",0) or 0)),
                "quality_score":Decimal(str((quality or {}).get("score") or 0)),
                "selector_score":Decimal(str(selector_score or 0)),
                "sector":sector or sector_for_symbol(item.get("symbol"), item.get("instrument_type")),
                "trade_mode":str(item.get("_trade_mode") or "INTRADAY"),
                "agent_deliberation":json.dumps(item.get("_agent_deliberation"), default=str) if item.get("_agent_deliberation") else None,
                "details":json.dumps({
                    "chart_gate":chart,
                    "instrument_local_direction":item.get("_instrument_local_direction") or chart.get("local_direction") or {},
                    "model_signal_context":item.get("_model_signal_context"),
                    "policy_candidate":{
                        "accepted":bool(getattr(policy,"accepted",False)) if policy else False,
                        "signal":int(getattr(policy,"signal",0) or 0) if policy else 0,
                        "score":float(getattr(policy,"score",0) or 0) if policy else 0,
                        "regime":getattr(policy,"regime",None) if policy else None,
                    },
                    "target":target or {},
                    "quality":quality or {},
                    "market_quality":market_quality or {},
                    "sentiment":sentiment or {},
                    "adaptive":adaptive or {},
                    "strategy_consistency":consistency or item.get("_strategy_consistency") or {},
                    "multi_timeframe_confirmation":(consistency or item.get("_strategy_consistency") or {}).get("multi_timeframe") or {},
                    "freshness":item.get("_freshness_gate") or {},
                    "session_case":session_case or {},
                    "effective_thresholds":thresholds or {},
                    "weighted_ensemble":item.get("_weighted_ensemble") or {},
                    "open_state":{"open_count":open_state.get("count",0)},
                    "risk_state":{"blocked":risk_state.get("blocked"),"reasons":risk_state.get("reasons",[])},
                },default=str),
            })
        for item in created:
            chart=item.get("chart_gate") or {}
            if not self.use_weighted_ensemble and not chart.get("accepted"):
                rejected+=1; reject_reasons["chart_gate_rejected"]+=1; audit(item,"chart_gate",False,"chart_gate_rejected"); continue
            elif self.use_weighted_ensemble and not chart.get("accepted"):
                cg_r = str(chart.get("reason", "") or "")
                if "less than 6 completed 5m" in cg_r or "no instrument-local" in cg_r:
                    rejected+=1; reject_reasons["chart_gate_rejected"]+=1; audit(item,"chart_gate",False,"insufficient_live_candles"); continue
            if not item.get("signal"):
                if not self.learning_mode_enabled:
                    rejected+=1; reject_reasons["no_model_signal"]+=1; audit(item,"model_signal",False,"no_model_signal"); continue
                chart_signal=int((chart.get("signal") or (chart.get("local_direction") or {}).get("direction") or item.get("paper_probe_signal") or 0))
                if not chart_signal:
                    rejected+=1; reject_reasons["no_chart_direction"]+=1; audit(item,"chart_direction",False,"no_chart_direction"); continue
                item["signal"]=chart_signal
                item["probe_trade"]=True
            policy=item.get("policy_candidate")
            thresholds=self._effective_thresholds(item,chart,policy,session_case or {})
            item["_session_case"]=session_case or {}
            item["_effective_thresholds"]=thresholds
            
            if item.get("_quant_strategy"):
                chart["strategy"] = str(item["_quant_strategy"])
            effective_strategy = str((chart.get("strategy") or item.get("strategy") or "")).upper()
            if effective_strategy:
                item["_effective_strategy"] = effective_strategy

            # Autonomous AI Fast-Path Vector Memory Check
            golden_match = None
            try:
                from .trade_memory import find_matching_golden_trade
                golden_match = find_matching_golden_trade(item)
                item["_golden_match"] = golden_match
            except Exception:
                pass

            is_fast_path = bool(golden_match and golden_match.get("is_fast_path"))
            is_golden_match = bool(golden_match and golden_match.get("is_golden_match"))
            if is_fast_path or is_golden_match:
                item["_fast_path_active"] = True
                p_name = str(golden_match.get("matched_pattern", "VECTOR_RESONANCE")).upper().replace(" ", "_").replace("-", "_")
                chart["strategy"] = f"GOLDEN_{p_name}"
                item["probability"] = min(0.95, float(item.get("probability") or 0.70) + (0.15 if is_fast_path else 0.08))
                thresholds["min_rr"] = max(1.35, float(thresholds.get("min_rr", 1.90)) * 0.75)

            ist_now = watermark.astimezone(IST).time()
            try:
                cooloff_time = datetime.strptime(self.morning_cooloff_ist, "%H:%M").time()
                if ist_now < cooloff_time:
                    rejected += 1; reject_reasons["morning_cooloff_active"] += 1
                    audit(item, "morning_cooloff", False, f"morning cool-off active before {self.morning_cooloff_ist} IST", thresholds=thresholds)
                    continue
            except Exception:
                pass

            if thresholds.get("block_new_entries"):
                rejected += 1
                reject_reasons["regime_high_vol_defense_block"] += 1
                audit(item, "regime_filter", False, "high volatility defense: new entries prohibited by risk policy", thresholds=thresholds)
                continue

            eval_strat = str(item.get("_effective_strategy") or chart.get("strategy") or item.get("strategy") or "").upper()
            setup_type = str(chart.get("setup_type") or item.get("setup_type") or "")
            if thresholds.get("suppress_breakouts") and is_breakout_strategy(eval_strat, setup_type=setup_type):
                rejected += 1
                reject_reasons["breakout_suppressed_in_range"] += 1
                audit(item, "regime_filter", False, f"breakout strategy {eval_strat} suppressed in range regime", thresholds=thresholds)
                continue
            if thresholds.get("suppress_fades") and is_fade_strategy(eval_strat):
                rejected += 1
                reject_reasons["fade_suppressed_in_trend"] += 1
                audit(item, "regime_filter", False, f"fade strategy {eval_strat} suppressed in trend regime", thresholds=thresholds)
                continue

            freshness=self._freshness_gate(item,watermark)
            if not freshness.get("accepted"):
                rejected+=1; reject_reasons["stale_signal_rejected"]+=1
                audit(item,"signal_freshness",False,freshness.get("reason","stale_signal_rejected"),thresholds=thresholds)
                continue
            item["_freshness_gate"]=freshness
            key=self._underlying_key(item)
            if key in cooldown:
                rejected+=1; reject_reasons["cooldown_underlying"]+=1; audit(item,"daily_risk",False,"cooldown_underlying"); continue
            loss_info = loss_counts.get(key, {})
            losses = loss_info.get("losses", 0) if isinstance(loss_info, dict) else int(loss_info or 0)
            if losses >= self.max_losses_per_underlying:
                rejected += 1; reject_reasons["repeated_underlying_loss_blocked"] += 1
                audit(item, "daily_risk", False, f"underlying {key} already has {losses} session losses (max={self.max_losses_per_underlying})")
                continue
            if key in open_underlyings and underlying_counts[key]>=self.max_trades_per_underlying:
                rejected+=1; reject_reasons["underlying_limit"]+=1; audit(item,"portfolio_limits",False,"underlying_limit"); continue
            target=self._execution_target(item,watermark)
            if not target:
                rejected+=1; reject_reasons["not_executable"]+=1; audit(item,"execution_route",False,"not_executable"); continue

            risk_levels = self._risk_levels_for_target(item, target)
            effective_rr = float(risk_levels.get("rr") if risk_levels.get("rr") is not None else (chart.get("rr") or 0))
            if risk_levels.get("rr") is not None and "rr" in chart:
                chart["rr"] = risk_levels["rr"]

            tgt_side = str(target.get("side", "")).upper()
            tgt_kind = str(target.get("kind", "")).upper()
            is_derivative = tgt_kind in {"CE", "PE", "FUT"}
            if is_derivative:
                underlying_dir = -1 if (tgt_kind == "PE" and tgt_side == "BUY") or (tgt_kind == "CE" and tgt_side == "SELL") else 1
            else:
                underlying_dir = 1 if tgt_side == "BUY" else -1

            stock_alpha = False
            if item.get("instrument_type") == "EQ" and not is_derivative:
                pre_quality = self._candidate_quality(item, target, self._target_quantity(target, "A"))
                stock_alpha = bool(
                    item.get("_stock_alpha_override") or
                    (item.get("policy_candidate") and getattr(item.get("policy_candidate"), "score", 0) >= 0.75) or
                    (float((pre_quality or {}).get("score") or 0.0) >= 78.0)
                )

            consistency = self._strategy_consistency_gate(item, target, session_case)
            item["_strategy_consistency"] = consistency

            # HARD VETO: Directional Balance Cap
            total_session = int(risk_state.get("closed_trades", 0)) + int(open_state.get("count", 0))
            if total_session >= 5:
                strat_dir = int(consistency.get("strategy_direction", 0) or 0)
                dir_label = "BUY" if strat_dir > 0 else "SELL"
                same_dir_count = side_counts.get(dir_label, 0) + sum(
                    1 for c in eligible if int((c.get("_strategy_consistency") or {}).get("strategy_direction", 0) or 0) == strat_dir
                )
                if (same_dir_count + 1) / (total_session + 1) > float(self.max_directional_concentration):
                    rejected += 1; reject_reasons["directional_imbalance_cap"] += 1
                    audit(item, "directional_balance", False, f"{dir_label} concentration would exceed {self.max_directional_concentration}", target=target)
                    continue

            # HARD VETO: Sector limits and cooldowns
            sector = sector_for_symbol(item.get("symbol"), item.get("instrument_type"))
            if sector_counts[sector] >= self.max_open_per_sector:
                rejected += 1; reject_reasons["sector_exposure_limit"] += 1; audit(item, "sector_limits", False, "sector_exposure_limit", target=target, sector=sector); continue
            open_sector_dirs = open_state.get("sector_directions", {})
            if sector in open_sector_dirs:
                strat_dir = int(consistency.get("strategy_direction", 0) or 0)
                if strat_dir and -strat_dir in open_sector_dirs[sector]:
                    rejected += 1; reject_reasons["sector_direction_conflict"] += 1
                    audit(item, "sector_correlation", False, f"sector {sector} already has opposite direction open", target=target, sector=sector)
                    continue
            if sector in risk_state.get("cooldown_sectors", set()):
                rejected += 1; reject_reasons["sector_cooldown_active"] += 1
                audit(item, "sector_limits", False, f"sector {sector} in cooldown after repeated intraday losses", target=target, sector=sector)
                continue

            # HARD VETO: Physically invalid execution price or inverted SL/TP geometry
            if not consistency.get("accepted"):
                c_reason = str(consistency.get("reason", ""))
                if "inverted" in c_reason or "invalid execution price" in c_reason:
                    rejected += 1; reject_reasons["strategy_consistency_rejected"] += 1
                    audit(item, "strategy_consistency", False, c_reason, target=target, thresholds=thresholds, consistency=consistency)
                    continue

            if self.use_weighted_ensemble:
                # HARD VETO: Market quality must be valid for execution
                market_quality = self._market_quality_gate(item, target)
                if not market_quality.get("accepted") and not is_fast_path:
                    rejected += 1; reject_reasons["market_quality_rejected"] += 1
                    audit(item, "market_quality", False, market_quality.get("reason", "market_quality_rejected"), target=target, market_quality=market_quality, thresholds=thresholds)
                    continue

                # Weighted Ensemble Evaluation (>= 75.0 / 100)
                score, breakdown = self._calculate_weighted_ensemble_score(
                    item=item,
                    target=target,
                    chart=chart,
                    policy=policy,
                    thresholds=thresholds,
                    consistency=consistency,
                    nifty_trend=nifty_trend,
                    stock_alpha=stock_alpha,
                )
                passed_ensemble = score >= self.weighted_ensemble_min_score
                item["_weighted_ensemble"] = {
                    "score": score,
                    "min_score": self.weighted_ensemble_min_score,
                    "breakdown": breakdown,
                }
                for g_name, g_info in breakdown.items():
                    audit(item, f"gate_{g_name}", g_info.get("pass", False), str(g_info.get("reason", "evaluated")),
                          target=target, thresholds=thresholds)
                audit(item, "weighted_ensemble", passed_ensemble,
                      f"ensemble score {score:.1f}/{100.0} (min {self.weighted_ensemble_min_score:.1f})",
                      target=target, thresholds=thresholds, selector_score=score, consistency=consistency)
                if not passed_ensemble:
                    rejected += 1; reject_reasons["weighted_ensemble_score_low"] += 1; continue

                sentiment = self._sentiment_gate(item, target)
                candidate_grade = {"grade": "B", "accepted": True, "score": score}
                assigned_grade = "B"
                quantity = self._target_quantity(target, assigned_grade)
                size_multiplier = float((thresholds or {}).get("size_multiplier", 1.0))
                quantity = self._scale_quantity(quantity, target, size_multiplier)
                if quantity <= 0:
                    rejected += 1; reject_reasons["grade_c_zero_sizing"] += 1; continue

                # HARD VETO: Option stop loss ceiling
                if str(target.get("kind")) in {"CE", "PE"}:
                    rl = self._risk_levels_for_target(item, target)
                    p = Decimal(str(target.get("price") or 0))
                    sl = Decimal(str(rl.get("stop_loss") or 0))
                    if p > Decimal("0") and sl > Decimal("0"):
                        stop_dist = abs(p - sl)
                        if (stop_dist * Decimal(quantity)) > self.max_option_loss_rupees:
                            rejected += 1
                            reject_reasons["max_option_loss_exceeded"] += 1
                            audit(item, "risk_limits", False, f"scaled option stop loss ₹{stop_dist * Decimal(quantity):.2f} exceeds cap ₹{self.max_option_loss_rupees:.2f}", target=target)
                            continue

                quality = self._candidate_quality(item, target, quantity)
                adaptive = self._adaptive_gate(item, target, quality)
                item["_execution_target"] = target
                item["_execution_quantity"] = quantity
                item["_entry_quality"] = quality
                item["_option_grade"] = candidate_grade
                item["_candidate_grade"] = candidate_grade
                item["_market_quality"] = market_quality
                item["_sentiment_gate"] = sentiment
                item["_adaptive_gate"] = adaptive
                item["_sector"] = sector
                item["_selector_score"] = score
                item["_senior_decision_report"] = self._senior_decision_report(
                    item, target, quality, market_quality, sentiment, adaptive, consistency, candidate_grade
                )
                eligible.append(item)
            else:
                # Legacy Sequential AND-Cascade
                if effective_rr < float(thresholds["min_rr"]):
                    rejected += 1; reject_reasons["rr_below_effective_minimum"] += 1; audit(item, "risk_reward", False, "rr_below_effective_minimum", thresholds=thresholds); continue

                if item.get("instrument_type") == "EQ" and not is_derivative and not stock_alpha:
                    if nifty_trend == -1 and underlying_dir == 1:
                        rejected += 1; reject_reasons["nifty_macro_downtrend_veto"] += 1
                        audit(item, "macro_index_gate", False, "stock BUY rejected: Nifty 50 5m is in downtrend (EMA9 < EMA21)", target=target, thresholds=thresholds)
                        continue
                    elif nifty_trend == 1 and underlying_dir == -1:
                        rejected += 1; reject_reasons["nifty_macro_uptrend_veto"] += 1
                        audit(item, "macro_index_gate", False, "stock SHORT rejected: Nifty 50 5m is in uptrend (EMA9 > EMA21)", target=target, thresholds=thresholds)
                        continue

                if not consistency.get("accepted"):
                    rejected += 1; reject_reasons["strategy_consistency_rejected"] += 1
                    audit(item, "strategy_consistency", False, consistency.get("reason", "strategy_consistency_rejected"), target=target, thresholds=thresholds, consistency=consistency)
                    continue

                sentiment = self._sentiment_gate(item, target)
                if not sentiment.get("accepted") and not is_fast_path:
                    rejected += 1; reject_reasons["sentiment_gate_rejected"] += 1; audit(item, "sentiment_gate", False, "sentiment_gate_rejected", target=target, sentiment=sentiment, sector=sector); continue
                market_quality = self._market_quality_gate(item, target)
                if not market_quality.get("accepted") and not is_fast_path:
                    rejected += 1; reject_reasons["market_quality_rejected"] += 1; audit(item, "market_quality", False, "market_quality_rejected", target=target, market_quality=market_quality, sentiment=sentiment, sector=sector); continue
                if self.require_depth_for_entries and not market_quality.get("depth_available") and not is_fast_path:
                    rejected += 1; reject_reasons["depth_required_no_fallback_fill"] += 1; audit(item, "market_depth", False, "depth_required_no_fallback_fill", target=target, market_quality=market_quality, sentiment=sentiment, sector=sector); continue

                policy_edge = float(getattr(policy, "expected_net_edge_bps", 0) or 0)
                if policy and policy_edge < float(thresholds["min_edge_bps"]):
                    rejected += 1; reject_reasons["net_edge_below_effective_minimum"] += 1
                    audit(item, "net_edge", False, "net_edge_below_effective_minimum", target=target, market_quality=market_quality, sentiment=sentiment, sector=sector, thresholds=thresholds)
                    continue
                quantity = self._target_quantity(target, "A")
                tgt_price = float(target.get("price") or 0)
                tp_price = float(self._risk_levels_for_target(item, target).get("take_profit") or 0)
                if tgt_price > 0 and tp_price > 0 and quantity > 0:
                    try:
                        seg = "OPTIONS" if str(target.get("kind")) in {"CE", "PE"} else "EQUITY_INTRADAY"
                        side_str = str(target.get("side", "BUY")).upper()
                        opp_side = "SELL" if side_str == "BUY" else "BUY"
                        c_entry = estimate_zerodha_costs(seg, side_str, Decimal(str(tgt_price)), quantity).total
                        c_exit = estimate_zerodha_costs(seg, opp_side, Decimal(str(tp_price)), quantity).total
                        est_fees = float(c_entry + c_exit)
                        exp_profit = abs(tp_price - tgt_price) * quantity
                        net_profit = exp_profit - est_fees
                        if est_fees > 0 and (exp_profit < est_fees * float(self.min_profit_to_fee_ratio) or net_profit < 10.0):
                            rejected += 1; reject_reasons["fee_ratio_too_low"] += 1
                            audit(item, "fee_gate", False, f"expected net profit {net_profit:.1f} < ₹10 min or profit {exp_profit:.1f} < {self.min_profit_to_fee_ratio}x fees {est_fees:.1f}", target=target, sector=sector)
                            continue
                    except Exception:
                        pass
                quality = self._candidate_quality(item, target, quantity)
                if float(quality.get("score") or 0) < float(thresholds["min_quality"]):
                    rejected += 1; reject_reasons["quality_below_effective_minimum"] += 1; audit(item, "quality_score", False, "quality_below_effective_minimum", target=target, quality=quality, market_quality=market_quality, sentiment=sentiment, sector=sector, thresholds=thresholds); continue
                if target.get("kind") in {"CE", "PE"}:
                    if float(quality.get("score") or 0) < float(thresholds["min_option_quality"]):
                        rejected += 1; reject_reasons["option_quality_below_effective_minimum"] += 1; audit(item, "option_grade", False, "option_quality_below_effective_minimum", target=target, quality=quality, market_quality=market_quality, sentiment=sentiment, sector=sector, thresholds=thresholds); continue
                    opt_fresh = self._freshness_gate(item, watermark, is_option=True)
                    if not opt_fresh.get("accepted"):
                        rejected += 1; reject_reasons["option_stale_rejected"] += 1
                        audit(item, "option_freshness", False, opt_fresh.get("reason", "option stale"), target=target, quality=quality, thresholds=thresholds)
                        continue
                candidate_grade = self._candidate_grade(item, target, quality, market_quality, item.get("_strategy_consistency"))
                if not candidate_grade.get("accepted", True):
                    rejected += 1; reject_reasons["grade_c_counterfactual_only"] += 1
                    audit(item, "grade_fencing", False, candidate_grade.get("reason", "grade C counterfactual only"), target=target, quality=quality, market_quality=market_quality, sentiment=sentiment, sector=sector, thresholds={**thresholds, "candidate_grade": candidate_grade, "option_grade": candidate_grade})
                    continue
                assigned_grade = candidate_grade.get("grade", "B")
                quantity = self._target_quantity(target, assigned_grade)
                size_multiplier = float((thresholds or {}).get("size_multiplier", 1.0))
                quantity = self._scale_quantity(quantity, target, size_multiplier)
                if quantity <= 0:
                    rejected += 1; reject_reasons["grade_c_zero_sizing"] += 1; continue
                if str(target.get("kind")) in {"CE", "PE"}:
                    rl = self._risk_levels_for_target(item, target)
                    p = Decimal(str(target.get("price") or 0))
                    sl = Decimal(str(rl.get("stop_loss") or 0))
                    if p > Decimal("0") and sl > Decimal("0"):
                        stop_dist = abs(p - sl)
                        if (stop_dist * Decimal(quantity)) > self.max_option_loss_rupees:
                            rejected += 1
                            reject_reasons["max_option_loss_exceeded"] += 1
                            audit(item, "risk_limits", False, f"scaled option stop loss ₹{stop_dist * Decimal(quantity):.2f} exceeds cap ₹{self.max_option_loss_rupees:.2f}", target=target)
                            continue
                adaptive = self._adaptive_gate(item, target, quality)
                if not adaptive.get("accepted"):
                    rejected += 1; reject_reasons["adaptive_trap_rejected"] += 1; audit(item, "adaptive_trap", False, "adaptive_trap_rejected", target=target, quality=quality, market_quality=market_quality, sentiment=sentiment, adaptive=adaptive, sector=sector, thresholds=thresholds); continue
                item["_execution_target"] = target
                item["_execution_quantity"] = quantity
                item["_entry_quality"] = quality
                item["_option_grade"] = candidate_grade
                item["_candidate_grade"] = candidate_grade
                item["_market_quality"] = market_quality
                item["_sentiment_gate"] = sentiment
                item["_adaptive_gate"] = adaptive
                item["_sector"] = sector
                item["_selector_score"] = self._selector_score(item, quality, target)
                item["_senior_decision_report"] = self._senior_decision_report(item, target, quality, market_quality, sentiment, adaptive, item.get("_strategy_consistency") or {}, candidate_grade)
                eligible.append(item)
        eligible.sort(key=lambda item:(item["_selector_score"],
                                       1 if item.get("policy_candidate") and item["policy_candidate"].accepted else 0,
                                       float(item.get("_entry_quality",{}).get("score") or 0),
                                       abs(float(item.get("probability",.5))-.5)),reverse=True)
        selected=[]; new_side_counts=defaultdict(int); new_sector_counts=defaultdict(int)
        for item in eligible:
            target=item["_execution_target"]
            sector=item.get("_sector") or sector_for_symbol(item.get("symbol"), item.get("instrument_type"))
            side=str(target["side"]).upper()
            kind=str(target.get("kind", "")).upper()
            item_dir = (-1 if (kind == "PE" and side == "BUY") or (kind == "CE" and side == "SELL") else 1) if kind in {"CE", "PE"} else (-1 if side == "SELL" else 1)
            key=self._underlying_key(item)
            # Prevent simultaneous opposing bets in the same minute cycle (exempting paired spreads / multi-leg)
            strat_name = str((item.get("chart_gate") or {}).get("strategy") or item.get("strategy") or "").upper()
            is_spread_leg = bool(item.get("spread_basket_id") or "SPREAD" in strat_name or "PAIR" in strat_name or item.get("trade_mode") == "MULTI_LEG")
            if selected:
                first_target = selected[0]["_execution_target"]
                f_side = str(first_target["side"]).upper()
                f_kind = str(first_target.get("kind", "")).upper()
                primary_cycle_dir = (-1 if (f_kind == "PE" and f_side == "BUY") or (f_kind == "CE" and f_side == "SELL") else 1) if f_kind in {"CE", "PE"} else (-1 if f_side == "SELL" else 1)
            else:
                primary_cycle_dir = None

            if primary_cycle_dir is not None and item_dir != primary_cycle_dir and not is_spread_leg:
                rejected += 1
                reject_reasons["cycle_directional_conflict"] = reject_reasons.get("cycle_directional_conflict", 0) + 1
                dir_label = "BEARISH" if primary_cycle_dir < 0 else "BULLISH"
                audit(item, "cycle_directional_consensus", False, f"conflicts with cycle primary direction {dir_label}", target=target)
                continue

            if underlying_counts[key]>=self.max_trades_per_underlying:
                rejected+=1; reject_reasons["underlying_limit_after_ranking"]+=1; audit(item,"ranking_limits",False,"underlying_limit_after_ranking",target=target,quality=item.get("_entry_quality"),market_quality=item.get("_market_quality"),sentiment=item.get("_sentiment_gate"),adaptive=item.get("_adaptive_gate"),selector_score=item.get("_selector_score"),sector=sector,thresholds=item.get("_effective_thresholds")); continue
            if sector_counts[sector]>=self.max_open_per_sector or new_sector_counts[sector]>=self.max_new_per_sector:
                rejected+=1; reject_reasons["sector_concentration_after_ranking"]+=1; audit(item,"ranking_limits",False,"sector_concentration_after_ranking",target=target,quality=item.get("_entry_quality"),market_quality=item.get("_market_quality"),sentiment=item.get("_sentiment_gate"),adaptive=item.get("_adaptive_gate"),selector_score=item.get("_selector_score"),sector=sector,thresholds=item.get("_effective_thresholds")); continue
            if side_counts[side]>=self.max_same_side_open or new_side_counts[side]>=self.max_new_same_side:
                rejected+=1; reject_reasons["same_side_concentration"]+=1; audit(item,"ranking_limits",False,"same_side_concentration",target=target,quality=item.get("_entry_quality"),market_quality=item.get("_market_quality"),sentiment=item.get("_sentiment_gate"),adaptive=item.get("_adaptive_gate"),selector_score=item.get("_selector_score"),sector=sector,thresholds=item.get("_effective_thresholds")); continue
            selected.append(item)
            audit(item,"accepted_top10",True,"",target=target,quality=item.get("_entry_quality"),
                  market_quality=item.get("_market_quality"),sentiment=item.get("_sentiment_gate"),
                  adaptive=item.get("_adaptive_gate"),selector_score=item.get("_selector_score"),sector=sector,
                  consistency=item.get("_strategy_consistency"),
                  thresholds={**(item.get("_effective_thresholds") or {}),"option_grade":item.get("_option_grade"),
                              "senior_decision_report":item.get("_senior_decision_report")})
            underlying_counts[key]+=1
            side_counts[side]+=1
            sector_counts[sector]+=1
            new_side_counts[side]+=1
            new_sector_counts[sector]+=1
            if len(selected)>=min(10,available_slots):
                break
        audit_report=self._persist_candidate_audits(audit_rows)
        return {"selected":selected,"rejected":rejected,
                "report":{"enabled":self.top_selector_enabled,"evaluated":len(created),"eligible":len(eligible),
                          "accepted":len(selected),"rejected":rejected,
                          "min_quality":self.min_entry_quality,"min_rr":self.min_professional_rr,
                          "min_option_quality":self.min_option_entry_quality,
                          "min_option_grade":self.min_option_grade,
                          "max_spread_bps":str(self.max_spread_bps),"max_option_spread_bps":str(self.max_option_spread_bps),
                          "min_expected_net_edge_bps":self.min_expected_net_edge_bps,
                          "require_depth_for_entries":self.require_depth_for_entries,
                          "max_intraday_atr_pct":str(self.max_intraday_atr_pct),
                          "min_option_recent_1m_coverage_pct":str(self.min_option_recent_1m_coverage_pct),
                          "min_option_session_1m_coverage_pct":str(self.min_option_session_1m_coverage_pct),
                          "option_stop_noise_atr_multiple":str(self.option_stop_noise_atr_multiple),
                          "max_option_loss_rupees":str(self.max_option_loss_rupees),
                          "option_stop_to_capture_max_ratio":str(self.option_stop_to_capture_max_ratio),
                          "option_max_fee_to_reward_ratio":str(self.option_max_fee_to_reward_ratio),
                          "max_open_per_sector":self.max_open_per_sector,"max_new_per_sector":self.max_new_per_sector,
                          "daily_trade_target":self.daily_trade_target,
                          "session_case":session_case or {},
                          "adaptive_learning_enabled":self.adaptive_learning_enabled,
                          "adaptive_min_samples":self.adaptive_min_samples,
                          "candidate_audit":audit_report,
                          "reject_reasons":dict(reject_reasons),
                          "top": [{"symbol":item.get("symbol"),"side":item["_execution_target"]["side"],
                                   "kind":item["_execution_target"]["kind"],"score":item["_selector_score"],
                                  "quality":item["_entry_quality"]["score"],"rr":item.get("chart_gate",{}).get("rr"),
                                   "option_grade":item.get("_option_grade",{}).get("grade"),
                                   "senior_decision":item.get("_senior_decision_report",{}).get("action"),
                                   "effective_thresholds":item.get("_effective_thresholds"),
                                   "sector":item.get("_sector"),"sentiment":item.get("_sentiment_gate"),
                                   "adaptive":item.get("_adaptive_gate"),
                                   "multi_timeframe":(item.get("_strategy_consistency") or {}).get("multi_timeframe"),
                                   "strategy":item.get("chart_gate",{}).get("strategy")}
                                  for item in selected]}}

    def _index_option_learning_items(self,watermark: datetime,open_underlyings=None,session_case: Dict = None) -> List[Dict]:
        if not self.option_paper_enabled:
            return []
        blocked={str(value).upper() for value in (open_underlyings or set())}
        with self.engine.connect() as connection:
            indexes=connection.execute(text("""
                SELECT i.id instrument_id,COALESCE(i.instrument_token,k.provider_token) instrument_token,
                    i.exchange,i.symbol
                FROM instrument_master i
                LEFT JOIN instrument_provider_keys k ON k.instrument_id=i.id AND k.provider='upstox_v3' AND k.is_active
                WHERE i.instrument_type='INDEX' AND i.is_active
                  AND REPLACE(i.symbol,' ','') IN ('NIFTY50','BANKNIFTY','SENSEX')
                  AND EXISTS (
                    SELECT 1 FROM live_market_bars b WHERE b.instrument_id=i.id AND b.interval='5minute'
                      AND b.source = ANY(:trade_sources) AND b.bar_time<=:watermark
                      AND b.bar_time>:watermark-INTERVAL '10 minutes')
                ORDER BY CASE REPLACE(i.symbol,' ','') WHEN 'NIFTY50' THEN 0 WHEN 'BANKNIFTY' THEN 1 ELSE 2 END
            """),{"watermark":watermark,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
            items=[]
            for index in indexes:
                symbol=str(index["symbol"]).upper()
                option_underlying=OPTION_UNDERLYING_ALIASES.get(symbol,symbol.replace(" ",""))
                if symbol in blocked or option_underlying in blocked:
                    continue
                intraday=connection.execute(text("""
                    SELECT bar_time,open_price,high_price,low_price,close_price,volume,open_interest
                    FROM live_market_bars WHERE instrument_id=:instrument AND interval='5minute'
                      AND source = ANY(:trade_sources) AND bar_time<=:watermark
                      AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                    ORDER BY bar_time
                """),{"instrument":index["instrument_id"],"watermark":watermark,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
                one_minute=connection.execute(text("""
                    SELECT DISTINCT ON (bar_time) bar_time,open_price,high_price,low_price,close_price,volume,open_interest
                    FROM live_market_bars
                    WHERE instrument_id=:instrument AND interval='1minute'
                      AND source = ANY(:trade_sources) AND bar_time<=:watermark+INTERVAL '5 minutes'
                      AND (bar_time AT TIME ZONE 'Asia/Kolkata')::date=(:watermark AT TIME ZONE 'Asia/Kolkata')::date
                    ORDER BY bar_time, CASE source WHEN 'upstox_v3' THEN 0 WHEN 'zerodha_kite' THEN 1 ELSE 2 END
                """),{"instrument":index["instrument_id"],"watermark":watermark,"trade_sources":list(TRADE_BAR_SOURCES)}).mappings().all()
                intraday=[_bar(row) for row in intraday]
                one_minute=[_bar(row) for row in one_minute]
                intraday=self._attach_opening_forming_5m(connection,index["instrument_id"],watermark,intraday)
                if len(intraday)<6:
                    continue
                closes=[bar["close"] for bar in intraday]
                signal=1 if closes[-1]>=_ema(closes,5)[-1] else -1
                session={"timestamp":watermark+timedelta(minutes=5),"open":intraday[0]["open"],
                         "high":max(x["high"] for x in intraday),"low":min(x["low"] for x in intraday),
                         "close":intraday[-1]["close"],"volume":sum(x["volume"] for x in intraday),
                         "oi":intraday[-1]["oi"]}
                item={**dict(index),"intraday":intraday,"intraday_1m":one_minute,
                      "intraday_15m":_aggregate_session_bars(one_minute or intraday,15),
                      "intraday_1h":_aggregate_session_bars(one_minute or intraday,60),
                      "session":session,"signal":signal,
                      "probability":0.56 if signal>0 else 0.44,"paper_probe_signal":signal}
                gate=self._senior_opportunity_gate(item,session_case=session_case,index_option=True)
                if not gate.get("accepted"):
                    gate=_learning_strategy_gate(item,signal)
                if gate.get("signal"):
                    item["signal"]=int(gate["signal"])
                    item["paper_probe_signal"]=int(gate["signal"])
                    item["probability"]=0.60 if item["signal"]>0 else 0.40
                if gate.get("accepted"):
                    if intraday[-1].get("forming"):
                        gate["opening_range_scanner"]=True
                        gate["reason"]=gate.get("reason","") + "; opening-range scanner used current forming 5m from live 1m bars"
                        gate["not_promotion_evidence"]=True
                    item["chart_gate"]=gate
                    item["option_type"]=gate.get("option_type")
                    item["option_side"]=gate.get("side","BUY")
                    item["senior_opportunity"]=bool(gate.get("senior_opportunity"))
                    try:
                        from backend.derivatives import build_index_spread_ticket
                        spot_val = float(session["close"])
                        regime_name = str((session_case or {}).get("regime_name") or (session_case or {}).get("case", "neutral"))
                        is_vol = any(k in regime_name.lower() for k in ("volatile", "crash", "shock", "expanding", "defense", "breakdown")) or float((session_case or {}).get("range_pct") or 0.0) >= 0.015
                        item["derivative_ticket"] = build_index_spread_ticket(
                            symbol=option_underlying,
                            spot=spot_val,
                            signal=int(item["signal"]),
                            regime=regime_name,
                            volatility="extreme" if is_vol else "neutral",
                        )
                    except Exception:
                        pass
                    item["trade_mode"] = "INTRADAY"
                    items.append(item)
            return items

    def _senior_stock_opportunity_items(self,created: List[Dict],remaining_slots: int,open_underlyings=None,
                                        session_case: Dict = None) -> List[Dict]:
        if not self.learning_mode_enabled or not self.senior_opportunity_enabled or remaining_slots<=0:
            return []
        blocked={str(value).upper() for value in (open_underlyings or set())}
        selected=[]
        for item in created:
            if item.get("instrument_type")!="EQ":
                continue
            key=self._underlying_key(item)
            if key in blocked or str(item.get("symbol","")).upper() in blocked:
                continue
            gate=self._senior_opportunity_gate(item,session_case=session_case,index_option=False)
            if not gate.get("accepted"):
                continue
            signal=int(gate.get("signal") or 0)
            if signal<0 and not self.intraday_short_fallback:
                continue
            probe=dict(item)
            probe["signal"]=signal
            probe["paper_probe_signal"]=signal
            probe["probability"]=0.60 if signal>0 else 0.40
            probe["chart_gate"]=gate
            # Preserve the Senior scanner's CE/PE route for stock setups too.
            # Without this, a visible stock PUT/CALL opportunity could be logged
            # in senior_market_opportunities but never reach the position layer
            # as an option paper trade.  If a valid option contract is not
            # available, _execution_target() safely falls back to EQ long/short.
            probe["option_type"]=gate.get("option_type")
            probe["option_side"]=gate.get("side","BUY")
            probe["probe_trade"]=True
            probe["senior_opportunity"]=True
            probe_thresholds=self._effective_thresholds(probe,gate,probe.get("policy_candidate"),session_case or {})
            if probe_thresholds.get("block_new_entries"):
                continue
            probe_strat = str(probe.get("_effective_strategy") or (gate.get("strategy") or probe.get("strategy") or "")).upper()
            setup_type = str(gate.get("setup_type") or probe.get("setup_type") or "")
            if probe_thresholds.get("suppress_breakouts") and is_breakout_strategy(probe_strat, setup_type=setup_type):
                continue
            if probe_thresholds.get("suppress_fades") and is_fade_strategy(probe_strat):
                continue
            probe_stop_mult=float((probe_thresholds or {}).get("stop_multiplier", 1.0))
            probe_size_mult=float((probe_thresholds or {}).get("size_multiplier", 1.0))
            probe_min_rr=float((probe_thresholds or {}).get("min_rr", self.min_professional_rr))
            probe_min_quality=max(float((probe_thresholds or {}).get("min_quality", self.learning_best_min_quality)), float(self.learning_best_min_quality))
            probe["_effective_thresholds"]={
                "scanner":"senior_stock_opportunity",
                "min_confidence":self.senior_stock_min_confidence,
                "min_quality":probe_min_quality,
                "min_rr":probe_min_rr,
                "stop_multiplier":probe_stop_mult,
                "size_multiplier":probe_size_mult,
                "block_new_entries":bool(probe_thresholds.get("block_new_entries")),
            }
            target=self._execution_target(probe,probe["session"]["timestamp"])
            if not target:
                continue
            risk_levels=self._risk_levels_for_target(probe,target)
            target_rr=float(risk_levels.get("rr") if risk_levels.get("rr") is not None else (gate.get("rr") or 0))
            if target_rr<probe_min_rr:
                continue
            consistency=self._strategy_consistency_gate(probe,target,session_case)
            if not consistency.get("accepted"):
                continue
            quantity=self._target_quantity(target,"A")
            market_quality=self._market_quality_gate(probe,target)
            if not market_quality.get("accepted"):
                continue
            sentiment=self._sentiment_gate(probe,target)
            if not sentiment.get("accepted",True):
                continue
            quality=self._candidate_quality(probe,target,quantity)
            if float(quality.get("score") or 0)<probe_min_quality:
                continue
            candidate_grade=self._candidate_grade(probe,target,quality,market_quality,consistency)
            if not candidate_grade.get("accepted",True):
                continue
            assigned_grade=candidate_grade.get("grade","B")
            quantity=self._target_quantity(target,assigned_grade)
            quantity=self._scale_quantity(quantity, target, probe_size_mult)
            if quantity<=0:
                continue
            if str(target.get("kind")) in {"CE", "PE"}:
                p = Decimal(str(target.get("price") or 0))
                sl = Decimal(str(risk_levels.get("stop_loss") or 0))
                if p > Decimal("0") and sl > Decimal("0") and (abs(p - sl) * Decimal(quantity)) > self.max_option_loss_rupees:
                    continue
            adaptive=self._adaptive_gate(probe,target,quality)
            if not adaptive.get("accepted"):
                continue
            probe["_execution_target"]=target
            probe["_execution_quantity"]=quantity
            probe["_entry_quality"]=quality
            probe["_candidate_grade"]=candidate_grade
            probe["_option_grade"]=candidate_grade
            probe["_market_quality"]=market_quality
            probe["_sentiment_gate"]=sentiment
            probe["_adaptive_gate"]=adaptive
            probe["_strategy_consistency"]=consistency
            probe["_sector"]=sector_for_symbol(probe.get("symbol"), probe.get("instrument_type"))
            probe["_selector_score"]=float(gate.get("confidence") or 0)+float(sentiment.get("selector_adjustment") or 0)
            probe["_session_case"]=session_case or {}
            selected.append(probe)
        selected.sort(key=lambda item:(float(item.get("_selector_score") or 0),float(item.get("_entry_quality",{}).get("score") or 0)),reverse=True)
        return selected[:min(remaining_slots,self.senior_stock_trade_limit,self.max_new_trades_per_cycle)]

    def _probe_candidates(self,created: List[Dict],remaining_slots: int,open_underlyings=None) -> List[Dict]:
        """Select a tiny paper-only exploratory basket when strict policy says no-trade.

        These rows are for validating the live-shadow accounting chain only:
        Positions, Trade History, SL/TP exits, costs, and P&L. They do not relax
        the model-promotion gate and never enable broker execution.
        """
        if not self.probe_trades_enabled or self.probe_trade_limit<=0 or remaining_slots<=0:
            return []
        open_underlyings={str(value).upper() for value in (open_underlyings or set())}
        candidates=[item for item in created if item.get("policy_candidate") and item.get("chart_gate",{}).get("accepted")
                    and str(item.get("symbol","")).upper() not in open_underlyings]
        def candidate_score(item):
            bars=item.get("intraday") or []
            closes=[float(bar["close"]) for bar in bars[-6:]]
            chart_momentum=(closes[-1]/closes[0]-1) if len(closes)>=2 and closes[0] else 0
            volume_score=sum(int(bar.get("volume") or 0) for bar in bars[-3:]) if bars else 0
            return (
            item["policy_candidate"].score,
            item["policy_candidate"].expected_net_edge_bps,
            abs(item["policy_candidate"].probability-.5),
                abs(chart_momentum),
                volume_score,
            )
        bullish=sorted([item for item in candidates if int(item.get("paper_probe_signal") or (item.get("chart_gate") or {}).get("signal") or 0)>0],
                       key=candidate_score,reverse=True)
        bearish=sorted([item for item in candidates if int(item.get("paper_probe_signal") or (item.get("chart_gate") or {}).get("signal") or 0)<0],
                       key=candidate_score,reverse=True)
        mixed=[]
        while bullish or bearish:
            if bullish: mixed.append(bullish.pop(0))
            if bearish: mixed.append(bearish.pop(0))
        selected=[]
        seen=set()
        bullish_count=bearish_count=0
        for item in mixed:
            if item["instrument_id"] in seen:
                continue
            item["signal"]=int(item.get("paper_probe_signal") or (item.get("chart_gate") or {}).get("signal") or 0)
            if not item["signal"]:
                continue
            if item["signal"]>0: bullish_count+=1
            else: bearish_count+=1
            item["option_type"]=item["chart_gate"].get("option_type")
            item["option_side"]=item["chart_gate"].get("side","BUY")
            item["probe_trade"]=True
            selected.append(item)
            seen.add(item["instrument_id"])
            if len(selected)>=min(self.probe_trade_limit,self.max_new_trades_per_cycle,remaining_slots):
                break
        return selected

    def run(self,watermark: Optional[datetime] = None) -> Dict:
        source_watermark=watermark or self._watermark()
        if not source_watermark: return {"status":"skipped","reason":"no completed five-minute Kite bars","orders_allowed":False}
        model=self._model(); prepared=self._prepare(source_watermark)
        if not prepared: return {"status":"skipped","reason":"no instruments have both live bars and 50 prior daily bars","orders_allowed":False}
        causal_watermark=max(item["causal_watermark"] for item in prepared)
        provisional_intraday=model["feature_set"].startswith("daily_") and causal_watermark.astimezone(ZoneInfo("Asia/Kolkata")).time()<datetime.strptime("15:30","%H:%M").time()
        allow_shadow_intraday=os.getenv("NIVESH_SHADOW_PAPER_INTRADAY_ENABLED","1")=="1"
        if provisional_intraday and os.getenv("NIVESH_ALLOW_PROVISIONAL_INTRADAY_MODEL","0")!="1" and not allow_shadow_intraday:
            return {"status":"skipped","reason":"active model is daily; intraday train/serve mismatch is forbidden",
                    "next_eligible_after":"15:30 Asia/Kolkata","orders_allowed":False}
        context=_context(prepared); created=[]; failures=[]; predictions_inserted=0
        threshold=float(os.getenv("NIVESH_ML_DECISION_THRESHOLD", model["payload"].get("decision_threshold",.70))); lower=1-threshold

        # Quant Models Strategy Ingestion Bridge
        quant_opportunities = {}
        try:
            from backend.quant_models import run_quant_snapshot
            db_url = os.getenv("DATABASE_URL")
            if db_url:
                q_snap = run_quant_snapshot(db_url, limit=40)
                mo = q_snap.get("model_outputs", {})
                for p in mo.get("stat_arb_pairs", {}).get("pairs", []):
                    z = float(p.get("spread_zscore") or 0)
                    pair_syms = [s.strip() for s in p.get("pair", "").split("/") if s.strip()]
                    if abs(z) >= 1.35 and len(pair_syms) == 2:
                        s1, s2 = pair_syms[0], pair_syms[1]
                        if z > 1.35:
                            quant_opportunities[s1] = {"strategy": f"QUANT_STAT_ARB_{s1}_{s2}_PAIR_REVERSION_PUT", "signal": -1, "option_type": "PE", "side": "BUY"}
                            quant_opportunities[s2] = {"strategy": f"QUANT_STAT_ARB_{s2}_{s1}_PAIR_REVERSION_CALL", "signal": 1, "option_type": "CE", "side": "BUY"}
                        elif z < -1.35:
                            quant_opportunities[s1] = {"strategy": f"QUANT_STAT_ARB_{s1}_{s2}_PAIR_REVERSION_CALL", "signal": 1, "option_type": "CE", "side": "BUY"}
                            quant_opportunities[s2] = {"strategy": f"QUANT_STAT_ARB_{s2}_{s1}_PAIR_REVERSION_PUT", "signal": -1, "option_type": "PE", "side": "BUY"}
                for f_item in mo.get("factor_investing_model", {}).get("top_factor_long_bias", [])[:4]:
                    sym = f_item.get("symbol")
                    if sym and sym not in quant_opportunities:
                        quant_opportunities[sym] = {"strategy": "QUANT_FACTOR_MOMENTUM_CALL_BUY", "signal": 1, "option_type": "CE", "side": "BUY"}
                for f_item in mo.get("factor_investing_model", {}).get("top_factor_short_bias", [])[:4]:
                    sym = f_item.get("symbol")
                    if sym and sym not in quant_opportunities:
                        quant_opportunities[sym] = {"strategy": "QUANT_FACTOR_MOMENTUM_PUT_BUY", "signal": -1, "option_type": "PE", "side": "BUY"}
                for l_item in mo.get("var_lead_lag", {}).get("top_lead_lag_correlations", [])[:3]:
                    sym = l_item.get("symbol")
                    corr = float(l_item.get("lag1_correlation") or 0)
                    if sym and abs(corr) >= 0.55 and sym not in quant_opportunities:
                        quant_opportunities[sym] = {
                            "strategy": "QUANT_LEAD_LAG_MOMENTUM_CALL_BUY" if corr > 0 else "QUANT_LEAD_LAG_BREAKDOWN_PUT_BUY",
                            "signal": 1 if corr > 0 else -1,
                            "option_type": "CE" if corr > 0 else "PE",
                            "side": "BUY"
                        }
        except Exception:
            quant_opportunities = {}

        for item in prepared:
            try:
                features=build_causal_feature_vector(item["exchange"],item["symbol"],item["daily"],item["session"],context,model["feature_set"])
                raw_probability=float(_raw_predict(model["payload"],features))
                probability=float(apply_calibration(raw_probability,model["payload"].get("calibration",{})))
                price=float(item["session"]["close"]); quantity=max(1,int(self.paper_notional/Decimal(str(price))))
                local_direction=_instrument_local_direction(item)
                chart_signal=int(local_direction.get("direction") or 0)
                is_idx = item.get("instrument_type") == "INDEX" or str(item.get("symbol")).upper() in {"NIFTY 50", "BANKNIFTY", "SENSEX"}
                cost_bps=estimated_trade_cost_bps(price,quantity,features.get("average_daily_value_20d", 0), is_index=is_idx)["total_bps"]
                candidate=score_candidate({"features":features},probability,raw_probability,cost_bps,
                                          edge_buffer_bps=self.min_expected_net_edge_bps,
                                          direction=chart_signal)
                model_signal=(candidate.signal if candidate.accepted else 0) if model["payload"].get("trading_policy") else (1 if probability>=threshold else -1 if probability<=lower else 0)
                if not chart_signal and model_signal:
                    chart_signal = model_signal
                item["_instrument_local_direction"]=local_direction
                item["_model_signal_context"]=model_signal
                signal=chart_signal
                sym_upper = str(item.get("symbol", "")).upper()
                try:
                    pcr_info = get_latest_pcr(self.engine, sym_upper, float(price))
                    item["atm_pcr"] = pcr_info.get("atm_pcr", 1.0)
                    item["pcr_info"] = pcr_info
                except Exception:
                    item["atm_pcr"] = 1.0

                chart_gate=_chart_strategy_gate(item,chart_signal)
                if not chart_gate["accepted"] and self.learning_mode_enabled:
                    chart_gate=_learning_strategy_gate(item,chart_signal)
                if sym_upper in quant_opportunities:
                    q_setup = quant_opportunities[sym_upper]
                    item["_quant_strategy"] = q_setup["strategy"]
                    if not chart_gate["accepted"]:
                        chart_gate = {
                            "accepted": True,
                            "strategy": q_setup["strategy"],
                            "option_type": q_setup["option_type"],
                            "side": q_setup["side"],
                            "signal": q_setup["signal"],
                            "rr": 2.4,
                            "stop_loss": round(price * 0.985 if q_setup["signal"] > 0 else price * 1.015, 4),
                            "take_profit": round(price * 1.035 if q_setup["signal"] > 0 else price * 0.965, 4),
                            "reason": f"Quant Engine Conviction: {q_setup['strategy']}"
                        }
                        signal = q_setup["signal"]
                if not chart_gate["accepted"]:
                    signal=0
                if chart_gate["accepted"]:
                    item["option_type"]=chart_gate.get("option_type")
                    item["option_side"]=chart_gate.get("side","BUY")
                    if chart_gate.get("signal"):
                        item["paper_probe_signal"]=int(chart_gate["signal"])
                    else:
                        item["paper_probe_signal"]=signal
                item["policy_candidate"]=candidate
                item["chart_gate"]=chart_gate
                if self._persist_prediction(model,item,features,probability,signal):
                    predictions_inserted+=1
                else:
                    item.update({"probability":probability,"signal":signal,
                                 "feature_hash":hashlib.sha256(json.dumps(features,sort_keys=True,separators=(",",":")).encode()).hexdigest()})
                created.append(item)
            except Exception as exc:
                failures.append({"exchange":item["exchange"],"symbol":item["symbol"],"error":type(exc).__name__})
        reconciled=reconcile_shadow_costs(self.engine)
        risk_adjustments=self._manage_open_trade_risk(causal_watermark) if self.active_trade_manager_enabled else {"status":"delegated_to_position_manager"}
        closed=self._close_due(causal_watermark) if self.inference_close_due_enabled else 0
        adaptive_rewards=sync_adaptive_rewards(self.engine,score_trade) if self.adaptive_learning_enabled else {"status":"disabled"}
        audits=0; rejected=0
        policy_enabled=bool(model["payload"].get("trading_policy"))
        open_state=self._open_trade_state()
        open_count=int(open_state["count"])
        risk_state=self._daily_risk_state(causal_watermark)
        daily_risk_stop=self._record_daily_risk_stop(causal_watermark,risk_state) if risk_state.get("blocked") else {"logged":False}
        daily_remaining=max(0,self.max_daily_trades-risk_state["total_trades"])
        target_remaining=max(0,self.daily_trade_target-risk_state["total_trades"])
        cycle_limit=self.max_new_trades_per_cycle if target_remaining else min(self.max_new_trades_per_cycle,daily_remaining)
        
        # Dynamic Alpha Fragility Portfolio Throttle
        try:
            from .alpha_fragility import alpha_fragility_status
            frag_status = alpha_fragility_status(self.engine)
            frag_robust = float(frag_status.get("robust_score") or 100.0)
            if frag_robust < 40.0:
                cycle_limit = min(cycle_limit, 1)
        except Exception:
            pass

        session_case=self._session_case(causal_watermark)
        session_trade_state=self._session_trade_state(causal_watermark)
        session_remaining=int(session_trade_state.get("remaining",daily_remaining))
        available_slots=0 if risk_state["blocked"] else max(0,min(self.max_open_paper_trades-open_count,cycle_limit,daily_remaining,session_remaining))
        selection=self._select_top_trade_candidates(created,open_state,risk_state,available_slots,causal_watermark,model["version"],session_case=session_case) if self.top_selector_enabled else {"selected":[],"rejected":0,"report":{"enabled":False,"session_case":session_case}}
        ranked=list(selection["selected"])
        senior_stock_additions=[]
        if self.learning_mode_enabled and self.senior_opportunity_enabled and len(ranked)<available_slots:
            open_state_after_selection=self._open_trade_state()
            blocked_for_senior=self._blocked_underlyings(open_state_after_selection,risk_state)
            senior_stock_additions=self._senior_stock_opportunity_items(created,available_slots-len(ranked),blocked_for_senior,session_case=session_case)
            ranked.extend(senior_stock_additions)
            selection.setdefault("report",{})["senior_stock_opportunity_scanner"]={
                "enabled":True,
                "added":len(senior_stock_additions),
                "limit":self.senior_stock_trade_limit,
                "min_confidence":self.senior_stock_min_confidence,
                "paper_only":True,
                "counts_for_promotion":False,
            }
        probe_additions=[]
        if self.learning_mode_enabled and self.probe_trades_enabled and len(ranked)<available_slots:
            open_state_after_selection=self._open_trade_state()
            blocked_for_probe=self._blocked_underlyings(open_state_after_selection,risk_state)
            probe_additions=self._probe_candidates(created,available_slots-len(ranked),blocked_for_probe)
            ranked.extend(probe_additions)
            selection.setdefault("report",{})["probe_learning_lane"]={
                "enabled":True,
                "added":len(probe_additions),
                "limit":self.probe_trade_limit,
                "paper_only":True,
                "counts_for_promotion":False,
            }
        probe_ranked=[item for item in ranked if item.get("probe_trade")]
        for item in ranked:
            thresholds = item.get("_effective_thresholds") or self._effective_thresholds(
                item, item.get("chart_gate") or {}, item.get("policy_candidate"), item.get("_session_case") or session_case
            )
            item["_effective_thresholds"] = thresholds
            if thresholds.get("block_new_entries"):
                rejected += 1
                continue
            item_strat = str(item.get("_effective_strategy") or (item.get("chart_gate", {}).get("strategy") or item.get("strategy") or "")).upper()
            setup_type = str((item.get("chart_gate") or {}).get("setup_type") or item.get("setup_type") or "")
            if thresholds.get("suppress_breakouts") and is_breakout_strategy(item_strat, setup_type=setup_type):
                rejected += 1
                continue
            if thresholds.get("suppress_fades") and is_fade_strategy(item_strat):
                rejected += 1
                continue
            target=item.get("_execution_target") or self._execution_target(item,causal_watermark)
            if not target:
                rejected+=1; continue
            consistency=item.get("_strategy_consistency") or self._strategy_consistency_gate(item,target,item.get("_session_case") or session_case)
            if not consistency.get("accepted"):
                rejected+=1; continue
            assigned_grade = (item.get("_candidate_grade") or item.get("_option_grade") or {}).get("grade", "B")
            quantity=int(item.get("_execution_quantity") or self._target_quantity(target, assigned_grade))
            if not item.get("_execution_quantity"):
                size_mult = float(thresholds.get("size_multiplier", 1.0))
                quantity = self._scale_quantity(quantity, target, size_mult)
            if quantity <= 0:
                rejected+=1; continue
            risk_levels=self._risk_levels_for_target(item,target)
            strategy_name=item.get("chart_gate",{}).get("strategy")
            if target.get("fallback_from_option"):
                strategy_name=f"{strategy_name}_EQ_FALLBACK"
            
            # Spread basket identification for multi-leg derivative tracking
            sym_tag = str(item.get("symbol") or "")
            basket_seed = f"{strategy_name}:{sym_tag}:{causal_watermark.isoformat()}".encode()
            spread_basket_id = item.get("spread_basket_id") or (f"spread_{hashlib.sha256(basket_seed).hexdigest()[:12]}" if "SPREAD" in str(strategy_name).upper() else None)

            # Agentic Multi-Agent Deliberation & Dual-Mode Routing
            dte_val = float(target.get("dte_days") or (target.get("greeks") or {}).get("dte_days") or (item.get("_greeks") or {}).get("dte_days") or 0.0)
            is_monthly_opt = str(target.get("kind") or "").upper() in ("CE", "PE") and dte_val >= float(os.getenv("NIVESH_SHADOW_SWING_OPTION_MIN_DTE", "10"))
            default_mode = "SWING" if is_monthly_opt else "INTRADAY"
            agent_eval = {"accepted": True, "trade_mode": default_mode, "sizing_factor": Decimal("1.0"), "reasoning_chain": None}
            try:
                from .brains.orchestrator import get_orchestrator
                orch = get_orchestrator(self.engine, self.redis)
                is_equity_long = str(target.get("kind") or "EQ").upper() == "EQ" and str(target.get("side") or "BUY").upper() == "BUY"
                quality_score = float((item.get("_entry_quality") or {}).get("score") or 0.0)
                proposed_mode = "SWING" if (is_monthly_opt or (is_equity_long and quality_score >= 70.0)) else "INTRADAY"
                candidate_ctx = {
                    "symbol": item.get("symbol"),
                    "side": target["side"],
                    "probability": item.get("probability"),
                    "entry_quality": item.get("_entry_quality"),
                    "quality_score": quality_score,
                    "strategy": strategy_name,
                    "instrument_type": target.get("kind"),
                    "dte_days": dte_val,
                    "expected_net_edge_bps": float(getattr(item.get("policy_candidate"), "expected_net_edge_bps", 40.0) or 40.0),
                    "multi_timeframe": consistency.get("multi_timeframe"),
                    "spread_basket_id": spread_basket_id,
                    "proposed_mode": proposed_mode
                }
                agent_eval = orch.evaluate_candidate(candidate_ctx, {"session_case": session_case})
                if not agent_eval.get("accepted"):
                    rejected += 1
                    continue
                quantity = max(1, int(Decimal(str(quantity)) * Decimal(str(agent_eval.get("sizing_factor", 1.0)))))
            except Exception as orch_exc:
                logger.warning(f"Agentic orchestrator deliberation bypassed due to error: {orch_exc}")

            underlying_invalidation_level = None
            if agent_eval.get("trade_mode") == "SWING":
                try:
                    from .swing_risk import compute_swing_risk_parameters
                    with self.engine.connect() as d_conn:
                        d_rows = d_conn.execute(
                            text("""
                                SELECT bar_time, open_price, high_price, low_price, close_price, volume
                                FROM live_market_bars
                                WHERE instrument_id = :inst_id
                                  AND interval = 'day'
                                  AND bar_time <= :wm
                                ORDER BY bar_time ASC
                            """),
                            {"inst_id": int(item.get("instrument_id") or 0), "wm": causal_watermark}
                        ).mappings().all()
                        daily_candles = [dict(r) for r in d_rows]
                        
                        und_daily_candles = None
                        if str(target.get("kind") or "").upper() in ("CE", "PE"):
                            und_sym = item.get("symbol")
                            und_rows = d_conn.execute(
                                text("""
                                    SELECT b.bar_time, b.open_price, b.high_price, b.low_price, b.close_price, b.volume
                                    FROM live_market_bars b
                                    JOIN instrument_master i ON i.id = b.instrument_id
                                    WHERE (i.symbol = :sym OR i.underlying_symbol = :sym)
                                      AND i.instrument_type IN ('EQ', 'INDEX')
                                      AND b.interval = 'day'
                                      AND b.bar_time <= :wm
                                    ORDER BY b.bar_time ASC
                                """),
                                {"sym": und_sym, "wm": causal_watermark}
                            ).mappings().all()
                            und_daily_candles = [dict(r) for r in und_rows]

                    greeks = target.get("greeks") or {}
                    opt_delta = greeks.get("delta")
                    lot_sz = int(target.get("lot_size") or 1)
                    swing_res, swing_reason = compute_swing_risk_parameters(
                        symbol=str(item.get("symbol") or ""),
                        side=target["side"],
                        entry_price=target["price"],
                        daily_candles=daily_candles,
                        is_option=bool(str(target.get("kind") or "").upper() in ("CE", "PE")),
                        underlying_daily_candles=und_daily_candles,
                        option_type=target.get("kind"),
                        delta=opt_delta,
                        lot_size=lot_sz,
                        return_reason=True,
                    )
                    if swing_res:
                        risk_levels["stop_loss"] = Decimal(str(swing_res["stop_loss_price"]))
                        risk_levels["take_profit"] = Decimal(str(swing_res["take_profit_price"]))
                        quantity = max(1, swing_res["quantity"])
                        underlying_invalidation_level = swing_res.get("underlying_invalidation_level")
                    else:
                        reject_code = swing_reason or "SWING_REJECTED_UNKNOWN"
                        logger.warning(f"Swing setup rejected for {item.get('symbol')} ({reject_code}); skipping candidate")
                        rejected += 1
                        try:
                            with self.engine.begin() as connection:
                                updated = connection.execute(text("""
                                    UPDATE trade_candidate_audits
                                    SET accepted = FALSE,
                                        rejection_reason = :reason,
                                        selector_stage = 'swing_risk'
                                    WHERE symbol = :symbol 
                                      AND observed_at >= :watermark - INTERVAL '2 minutes'
                                      AND observed_at <= :watermark + INTERVAL '2 minutes'
                                      AND accepted = TRUE
                                      AND (instrument_id = :instrument_id OR instrument_id IS NULL)
                                      AND (model_version = :model_version OR model_version IS NULL)
                                """), {
                                    "symbol": item.get("symbol"),
                                    "instrument_id": int(item.get("instrument_id") or 0) or None,
                                    "model_version": model.get("version"),
                                    "watermark": item["session"]["timestamp"],
                                    "reason": reject_code
                                }).rowcount
                                if not updated:
                                    connection.execute(text("""
                                        INSERT INTO trade_candidate_audits (
                                            observed_at, model_version, exchange, symbol, instrument_id,
                                            signal, probability, decision_price, selector_stage, accepted,
                                            rejection_reason, trade_mode
                                        ) VALUES (
                                            :observed_at, :model_version, :exchange, :symbol, :instrument_id,
                                            :signal, :probability, :decision_price, 'swing_risk', FALSE,
                                            :reason, 'SWING'
                                        )
                                    """), {
                                        "observed_at": item["session"]["timestamp"],
                                        "model_version": model.get("version"),
                                        "exchange": item.get("exchange", "NSE"),
                                        "symbol": item.get("symbol"),
                                        "instrument_id": int(item.get("instrument_id") or 0) or None,
                                        "signal": target.get("side", "BUY"),
                                        "probability": float(item.get("probability") or 0.0),
                                        "decision_price": float(target.get("price") or 0.0),
                                        "reason": reject_code,
                                    })
                        except Exception as audit_err:
                            logger.debug(f"Failed to record swing rejection in candidate audit log: {audit_err}")
                        continue
                except Exception as sw_exc:
                    logger.warning(f"Swing risk computation failed for {item.get('symbol')}: {sw_exc}; skipping candidate")
                    rejected += 1
                    try:
                        with self.engine.begin() as connection:
                            connection.execute(text("""
                                UPDATE trade_candidate_audits
                                SET accepted = FALSE,
                                    rejection_reason = 'SWING_REJECTED_ERROR',
                                    selector_stage = 'swing_risk'
                                WHERE symbol = :symbol 
                                  AND observed_at >= :watermark - INTERVAL '2 minutes'
                                  AND observed_at <= :watermark + INTERVAL '2 minutes'
                                  AND accepted = TRUE
                                  AND (instrument_id = :instrument_id OR instrument_id IS NULL)
                                  AND (model_version = :model_version OR model_version IS NULL)
                            """), {
                                "symbol": item.get("symbol"),
                                "instrument_id": int(item.get("instrument_id") or 0) or None,
                                "model_version": model.get("version"),
                                "watermark": item["session"]["timestamp"],
                            })
                    except Exception:
                        pass
                    continue

            if str(target.get("kind", "")).upper() in {"CE", "PE"}:
                lot = int(target.get("lot_size") or 1)
                quantity = (quantity // lot) * lot
                if quantity < lot:
                    rejected += 1
                    continue

            result=record_shadow_signal(self.engine,self.redis,model["version"],int(target["instrument_token"]),target["side"],quantity,
                                        item["probability"],target["price"],item["session"]["timestamp"],
                                        strategy_note=json.dumps({"strategy":strategy_name,
                                                                  "synthetic_entry":bool((target.get("greeks") or {}).get("source") == "analytical_black_scholes" or target.get("source") == "analytical_black_scholes"),
                                                                  "directional_intent":item.get("chart_gate",{}).get("directional_intent"),
                                                                  "trade_mode":agent_eval.get("trade_mode","INTRADAY"),
                                                                  "reasoning_chain":agent_eval.get("reasoning_chain"),
                                                                  "underlying_invalidation_level":underlying_invalidation_level,
                                                                  "spread_basket_id":spread_basket_id,
                                                                  "candidate_grade":assigned_grade,
                                                                  "reason":item.get("chart_gate",{}).get("reason"),
                                                                  "rr":item.get("chart_gate",{}).get("rr"),
                                                                  "strategy_stop_loss":risk_levels.get("stop_loss"),
                                                                  "strategy_take_profit":risk_levels.get("take_profit"),
                                                                  "structure":item.get("chart_gate",{}).get("structure"),
                                                                  "risk_price_basis":risk_levels.get("basis"),
                                                                  "risk_source":risk_levels.get("source"),
                                                                  "route":f"{target['side']} {target['kind']}",
                                                                  "underlying":item.get("symbol"),
                                                                  "fallback_from_option":target.get("fallback_from_option"),
                                                                  "selector_score":item.get("_selector_score"),
                                                                  "entry_quality":item.get("_entry_quality"),
                                                                  "market_quality":item.get("_market_quality"),
                                                                  "sentiment_gate":item.get("_sentiment_gate"),
                                                                  "adaptive_gate":item.get("_adaptive_gate"),
                                                                  "option_grade":item.get("_option_grade"),
                                                                  "senior_decision_report":item.get("_senior_decision_report"),
                                                                  "strategy_consistency":consistency,
                                                                  "multi_timeframe_confirmation":consistency.get("multi_timeframe"),
                                                                  "sector":item.get("_sector"),
                                                                  "session_case":item.get("_session_case") or session_case,
                                                                  "effective_thresholds":item.get("_effective_thresholds"),
                                                                  "probe_trade":bool(item.get("probe_trade")),
                                                                  "learning_only":bool(item.get("probe_trade")),
                                                                  "paper_only":True},default=str))
            audits+=int(result["recorded"])
            if result.get("recorded"):
                try:
                    payload={"audit_id":result.get("audit_id"),"symbol":item.get("symbol"),"side":target.get("side"),
                             "kind":target.get("kind"),"strategy":strategy_name,
                             "confidence":(item.get("_senior_decision_report") or {}).get("confidence_score")
                                          or (item.get("_entry_quality") or {}).get("score")
                                          or item.get("probability"),
                             "route":f"{target.get('side')} {target.get('kind')}",
                             "session_case":item.get("_session_case") or session_case}
                    with self.engine.begin() as connection:
                        publish_brain_event(connection,"SeniorApproved","LivePaperInference",payload,
                                            symbol=item.get("symbol"),severity="INFO")
                        connection.execute(text("""
                            UPDATE trade_candidate_audits
                            SET trade_mode = :trade_mode,
                                agent_deliberation = COALESCE(CAST(:deliberation AS jsonb), agent_deliberation)
                            WHERE symbol = :symbol 
                              AND observed_at >= :watermark - INTERVAL '1 minute'
                              AND observed_at <= :watermark + INTERVAL '1 minute'
                              AND accepted = TRUE
                              AND (instrument_id = :instrument_id OR instrument_id IS NULL)
                              AND (model_version = :model_version OR model_version IS NULL)
                        """), {
                            "symbol": item.get("symbol"),
                            "instrument_id": int(item.get("instrument_id") or 0) or None,
                            "model_version": model.get("version"),
                            "watermark": item["session"]["timestamp"],
                            "trade_mode": agent_eval.get("trade_mode", "INTRADAY"),
                            "deliberation": json.dumps(agent_eval.get("reasoning_chain"), default=str) if agent_eval.get("reasoning_chain") else None
                        })
                    from .brains import get_bus
                    from .brains.bus import SeniorApproved
                    get_bus().publish(SeniorApproved(source_brain="LivePaperInference",
                                                     symbol=str(item.get("symbol") or ""),
                                                     side=str(target.get("side") or ""),
                                                     strategy=str(strategy_name or ""),
                                                     confidence=float(payload.get("confidence") or 0),
                                                     route=str(payload.get("route") or ""),
                                                     payload=payload))
                except Exception:
                    pass
            else:
                # Execution layer rejected or dropped trade - synchronize candidate audit to prevent bleed
                try:
                    rejection_reason = result.get("rejection_reason") or "execution_layer_dropped"
                    with self.engine.begin() as connection:
                        connection.execute(text("""
                            UPDATE trade_candidate_audits
                            SET accepted = FALSE,
                                rejection_reason = :reason
                            WHERE symbol = :symbol 
                              AND observed_at >= :watermark - INTERVAL '1 minute'
                              AND observed_at <= :watermark + INTERVAL '1 minute'
                              AND accepted = TRUE
                              AND (instrument_id = :instrument_id OR instrument_id IS NULL)
                              AND (model_version = :model_version OR model_version IS NULL)
                        """), {
                            "symbol": item.get("symbol"),
                            "instrument_id": int(item.get("instrument_id") or 0) or None,
                            "model_version": model.get("version"),
                            "watermark": item["session"]["timestamp"],
                            "reason": rejection_reason
                        })
                except Exception:
                    pass
        if audits<available_slots and self.learning_mode_enabled:
            open_state_after=self._open_trade_state()
            fresh_blocked=self._blocked_underlyings(open_state_after,risk_state)
            loss_counts=self._daily_underlying_loss_counts(source_watermark)
            index_audits=0
            senior_index_opportunity_audits=0
            for item in self._index_option_learning_items(source_watermark,fresh_blocked,session_case=session_case):
                if audits>=available_slots:
                    break
                if index_audits>=self.max_index_learning_trades:
                    break
                key=self._underlying_key(item)
                loss_info=loss_counts.get(key, {})
                losses = loss_info.get("losses", 0) if isinstance(loss_info, dict) else int(loss_info or 0)
                if losses>=self.max_losses_per_underlying:
                    rejected+=1
                    continue
                thresholds=self._effective_thresholds(item,item.get("chart_gate",{}),item.get("policy_candidate"),session_case)
                item["_session_case"]=session_case
                item["_effective_thresholds"]=thresholds
                if thresholds.get("block_new_entries"):
                    rejected+=1
                    continue
                item_strat = str(item.get("_effective_strategy") or (item.get("chart_gate", {}).get("strategy") or item.get("strategy") or "")).upper()
                setup_type = str((item.get("chart_gate") or {}).get("setup_type") or item.get("setup_type") or "")
                if thresholds.get("suppress_breakouts") and is_breakout_strategy(item_strat, setup_type=setup_type):
                    rejected += 1
                    continue
                if thresholds.get("suppress_fades") and is_fade_strategy(item_strat):
                    rejected += 1
                    continue
                target=self._execution_target(item,source_watermark)
                if not target:
                    rejected+=1
                    continue
                risk_levels=self._risk_levels_for_target(item,target)
                target_rr=float(risk_levels.get("rr") if risk_levels.get("rr") is not None else (item.get("chart_gate",{}).get("rr") or 0))
                if target_rr<float(thresholds["min_rr"]):
                    rejected+=1
                    continue
                consistency=self._strategy_consistency_gate(item,target,session_case)
                if not consistency.get("accepted"):
                    rejected+=1
                    continue
                market_quality=self._market_quality_gate(item,target)
                if not market_quality.get("accepted"):
                    rejected+=1
                    continue
                if self.require_depth_for_entries and not market_quality.get("depth_available"):
                    rejected+=1
                    continue
                base_qty=target["lot_size"]
                size_mult=float(thresholds.get("size_multiplier", 1.0))
                quantity=self._scale_quantity(base_qty, target, size_mult)
                if quantity<=0:
                    rejected+=1
                    continue
                quality=self._candidate_quality(item,target,quantity)
                if float(quality.get("score") or 0)<float(thresholds["min_option_quality"]):
                    rejected+=1
                    continue
                option_grade=self._option_grade(item,target,quality,market_quality,consistency)
                if not option_grade.get("accepted",True):
                    rejected+=1
                    continue
                adaptive=self._adaptive_gate(item,target,quality)
                if not adaptive.get("accepted"):
                    rejected+=1
                    continue
                sentiment=self._sentiment_gate(item,target)
                if not sentiment.get("accepted",True):
                    rejected+=1
                    continue
                senior_decision=self._senior_decision_report(
                    item,target,quality,market_quality,
                    sentiment,adaptive,consistency,option_grade
                )
                risk_levels=self._risk_levels_for_target(item,target)
                result=record_shadow_signal(self.engine,self.redis,model["version"],int(target["instrument_token"]),target["side"],quantity,
                                            item["probability"],target["price"],item["session"]["timestamp"],
                                            strategy_note=json.dumps({"strategy":item.get("chart_gate",{}).get("strategy"),
                                                                      "synthetic_entry":bool((target.get("greeks") or {}).get("source") == "analytical_black_scholes" or target.get("source") == "analytical_black_scholes"),
                                                                      "directional_intent":item.get("chart_gate",{}).get("directional_intent"),
                                                                      "trade_mode":item.get("trade_mode","INTRADAY"),
                                                                      "reason":item.get("chart_gate",{}).get("reason"),
                                                                      "rr":item.get("chart_gate",{}).get("rr"),
                                                                      "strategy_stop_loss":risk_levels.get("stop_loss"),
                                                                      "strategy_take_profit":risk_levels.get("take_profit"),
                                                                      "structure":item.get("chart_gate",{}).get("structure"),
                                                                      "risk_price_basis":risk_levels.get("basis"),
                                                                      "risk_source":risk_levels.get("source"),
                                                                      "route":f"{target['side']} {target['kind']}",
                                                                      "underlying":item.get("symbol"),
                                                                      "engine":"senior_index_opportunity" if item.get("senior_opportunity") else "index_option_learning",
                                                                      "entry_quality":quality,
                                                                      "market_quality":market_quality,
                                                                      "sentiment_gate":sentiment,
                                                                      "adaptive_gate":adaptive,
                                                                      "option_grade":option_grade,
                                                                      "senior_decision_report":senior_decision,
                                                                      "strategy_consistency":consistency,
                                                                      "multi_timeframe_confirmation":consistency.get("multi_timeframe"),
                                                                      "session_case":session_case,
                                                                      "effective_thresholds":thresholds,
                                                                      "derivative_ticket":item.get("derivative_ticket"),
                                                                      "paper_only":True},default=str))
                audits+=int(result["recorded"])
                if result.get("recorded"):
                    # Record paired hedge leg if derivative ticket is multi-leg (e.g. Bull Call Spread, Bear Put Spread, Protective Collar)
                    dticket = item.get("derivative_ticket")
                    plan = ((dticket or {}).get("plan") or {}) if (dticket or {}).get("status") == "PAPER_APPROVED" else {}
                    plan_legs = plan.get("legs") or []
                    plan_exp_raw = target.get("expiry") or plan.get("expiry")
                    plan_expiry = str(plan_exp_raw).split("T")[0] if plan_exp_raw else None
                    target_strike = float(target.get("option_strike") or target.get("strike") or 0.0)
                    target_side = str(target.get("side")).upper()
                    target_kind = str(target.get("kind")).upper() if target.get("kind") in ("CE", "PE", "FUT") else None
                    primary_leg_idx = None
                    for idx, lg in enumerate(plan_legs):
                        lg_side = str(lg.get("side")).upper()
                        lg_otype = str(lg.get("option_type") or "FUT").upper()
                        lg_strike = float(lg.get("strike") or 0.0)
                        if lg_side == target_side and (target_kind is None or lg_otype == target_kind):
                            if target_strike > 0 and lg_strike > 0:
                                if abs(lg_strike - target_strike) < 0.01:
                                    primary_leg_idx = idx
                                    break
                            elif target_strike == 0 and lg_strike == 0:
                                primary_leg_idx = idx
                                break
                    if primary_leg_idx is not None and len(plan_legs) > 1:
                        hedge_legs = [lg for idx, lg in enumerate(plan_legs) if idx != primary_leg_idx]
                    else:
                        if len(plan_legs) > 1:
                            logger.warning("Could not map target to strike-matching primary leg in multi-leg plan %s; target=%s; skipping hedge recording", plan_legs, target)
                        hedge_legs = []
                    if hedge_legs and audits < available_slots:
                        primary_audit_id = result.get("audit_id")
                        spread_basket_ref = f"spread_{primary_audit_id}"
                        for other_leg in hedge_legs:
                            if audits >= available_slots:
                                break
                            sym_clean = self._underlying_key(item)
                            try:
                                with self.engine.connect() as conn:
                                    other_otype = other_leg.get("option_type")
                                    is_fut = not other_otype or other_otype == "FUT"
                                    if is_fut:
                                        other_inst = conn.execute(
                                            text("""
                                                SELECT i.id, i.lot_size, COALESCE(i.instrument_token, k.provider_token) AS instrument_token
                                                FROM instrument_master i
                                                LEFT JOIN instrument_provider_keys k ON k.instrument_id = i.id AND k.is_active
                                                WHERE i.underlying_symbol = :sym
                                                  AND i.instrument_type IN ('FUT', 'FUTIDX', 'FUTSTK')
                                                  AND i.is_active = TRUE
                                                  AND i.exchange IN ('NFO', 'BFO')
                                                  AND (:plan_exp IS NULL OR i.expiry = CAST(:plan_exp AS date))
                                                  AND i.expiry >= (:wm AT TIME ZONE 'Asia/Kolkata')::date
                                                ORDER BY i.expiry ASC LIMIT 1
                                            """),
                                            {
                                                "sym": sym_clean,
                                                "plan_exp": plan_expiry,
                                                "wm": source_watermark,
                                            }
                                        ).mappings().one_or_none()
                                    else:
                                        other_inst = conn.execute(
                                            text("""
                                                SELECT i.id, i.lot_size, COALESCE(i.instrument_token, k.provider_token) AS instrument_token
                                                FROM instrument_master i
                                                LEFT JOIN instrument_provider_keys k ON k.instrument_id = i.id AND k.is_active
                                                WHERE i.underlying_symbol = :sym
                                                  AND i.instrument_type = :otype
                                                  AND i.strike = :strike
                                                  AND i.is_active = TRUE
                                                  AND i.exchange IN ('NFO', 'BFO')
                                                  AND (:plan_exp IS NULL OR i.expiry = CAST(:plan_exp AS date))
                                                  AND i.expiry >= (:wm AT TIME ZONE 'Asia/Kolkata')::date
                                                ORDER BY i.expiry ASC LIMIT 1
                                            """),
                                            {
                                                "sym": sym_clean,
                                                "otype": other_otype,
                                                "strike": other_leg.get("strike"),
                                                "plan_exp": plan_expiry,
                                                "wm": source_watermark,
                                            }
                                        ).mappings().one_or_none()

                                    if other_inst and other_inst.get("instrument_token"):
                                        mark = conn.execute(text("""
                                            SELECT close_price FROM live_market_bars
                                            WHERE instrument_id = :id AND interval IN ('1minute', '5minute')
                                              AND source = ANY(:src) AND bar_time <= :wm
                                              AND bar_time > :wm - INTERVAL '15 minutes'
                                            ORDER BY bar_time DESC LIMIT 1
                                        """), {"id": other_inst["id"], "src": list(TRADE_BAR_SOURCES), "wm": source_watermark}).scalar_one_or_none()
                                        if mark is None or float(mark) <= 0:
                                            logger.info("No live mark for hedge leg %s %s strike=%s; skipping", sym_clean, other_otype, other_leg.get("strike"))
                                            continue
                                        other_price = Decimal(str(mark))
                                        other_qty = int(other_inst["lot_size"] or target["lot_size"])
                                        rec = record_shadow_signal(
                                            self.engine, self.redis, model["version"],
                                            int(other_inst["instrument_token"]), other_leg["side"], other_qty,
                                            item["probability"], other_price, item["session"]["timestamp"],
                                            strategy_note=json.dumps({
                                                "strategy": dticket.get("strategy"),
                                                "synthetic_entry": False,
                                                "trade_mode": item.get("trade_mode", "INTRADAY"),
                                                "spread_basket_id": spread_basket_ref,
                                                "paired_primary_audit_id": primary_audit_id,
                                                "paired_leg": f"{other_leg['side']} {other_leg.get('option_type') or 'FUT'}",
                                                "engine": "multi_leg_hedge",
                                                "underlying": item.get("symbol"),
                                                "reasoning_chain": {
                                                    "strategy_group": f"MULTI_LEG:{primary_audit_id}",
                                                    "strategy": dticket.get("strategy"),
                                                    "role": "HEDGE_FINANCING_LEG",
                                                }
                                            })
                                        )
                                        audits += int(rec.get("recorded", 0))
                                        index_audits += int(rec.get("recorded", 0))
                                    else:
                                        logger.info("Could not resolve hedge contract in master for %s %s strike=%s", sym_clean, other_otype, other_leg.get("strike"))
                            except Exception as hedge_err:
                                logger.warning("Failed to record paired hedge leg for %s: %s", sym_clean, hedge_err)

                    try:
                        strategy_name=item.get("chart_gate",{}).get("strategy")
                        payload={"audit_id":result.get("audit_id"),"symbol":item.get("symbol"),"side":target.get("side"),
                                 "kind":target.get("kind"),"strategy":strategy_name,
                                 "confidence":senior_decision.get("confidence_score") or quality.get("score") or item.get("probability"),
                                 "route":f"{target.get('side')} {target.get('kind')}",
                                 "session_case":session_case,
                                 "engine":"senior_index_opportunity" if item.get("senior_opportunity") else "index_option_learning"}
                        with self.engine.begin() as connection:
                            publish_brain_event(connection,"SeniorApproved","LivePaperInference",payload,
                                                symbol=item.get("symbol"),severity="INFO")
                        from .brains import get_bus
                        from .brains.bus import SeniorApproved
                        get_bus().publish(SeniorApproved(source_brain="LivePaperInference",
                                                         symbol=str(item.get("symbol") or ""),
                                                         side=str(target.get("side") or ""),
                                                         strategy=str(strategy_name or ""),
                                                         confidence=float(payload.get("confidence") or 0),
                                                         route=str(payload.get("route") or ""),
                                                         payload=payload))
                    except Exception:
                        pass
                index_audits+=int(result["recorded"])
                if item.get("senior_opportunity"):
                    senior_index_opportunity_audits+=int(result["recorded"])
        else:
            senior_index_opportunity_audits=0
        try:
            opportunity_candidates=list(created)
            if self.learning_mode_enabled and self.senior_opportunity_enabled:
                opportunity_candidates.extend(self._index_option_learning_items(source_watermark,set(),session_case=session_case))
            with self.engine.begin() as connection:
                market_intelligence=record_opportunity_scan(connection,causal_watermark,opportunity_candidates,ranked,limit=5)
                market_intelligence["counterfactuals"]=update_counterfactuals(connection,causal_watermark)
                market_intelligence["durable_memory"]=refresh_intelligence_memory(connection)
                publish_brain_event(connection,"SeniorOpportunityScan","LivePaperInference",{
                    "recorded":market_intelligence.get("recorded",0),
                    "session_block":market_intelligence.get("session_block"),
                    "counterfactuals":market_intelligence.get("counterfactuals",{}),
                    "durable_memory":market_intelligence.get("durable_memory",{}),
                },severity="INFO")
                market_intelligence["purpose"]="records top visible/missed setups so no-trade days still teach the Senior layer"
                market_intelligence["paper_only"]=True
        except Exception as exc:
            market_intelligence={"status":"error","error":type(exc).__name__,"paper_only":True}
        if audits: reconciled=reconcile_shadow_costs(self.engine)
        return {"status":"success","model_version":model["version"],"watermark":causal_watermark.isoformat(),"candidates":len(prepared),
                "predictions_created":predictions_inserted,"candidates_evaluated":len(created),"signals":len(ranked),"policy_enabled":policy_enabled,
                "no_trade_rejections":sum(not x["policy_candidate"].accepted for x in created) if policy_enabled else 0,
                "regimes":{name:sum(x["policy_candidate"].regime==name for x in created) for name in {x["policy_candidate"].regime for x in created}},
                "paper_audits":audits,"bearish_routes_rejected":rejected,
                "exploratory_probe_trades":len(probe_ranked),"open_paper_trades_before":open_count,
                "senior_stock_opportunity_trades":len(senior_stock_additions),
                "senior_index_opportunity_trades":senior_index_opportunity_audits,
                "senior_market_intelligence":market_intelligence,
                "top10_selector":selection.get("report",{}),
                "max_open_paper_trades":self.max_open_paper_trades,"max_new_trades_per_cycle":self.max_new_trades_per_cycle,
                "daily_trade_target":self.daily_trade_target,
                "session_case":session_case,
                "session_trade_state":session_trade_state,
                "max_trades_per_underlying":self.max_trades_per_underlying,"max_index_learning_trades":self.max_index_learning_trades,
                "max_open_per_sector":self.max_open_per_sector,"max_new_per_sector":self.max_new_per_sector,
                "daily_risk_state":risk_state,
                "daily_risk_stop":daily_risk_stop,
                "active_trade_manager":risk_adjustments,
                "chart_gate_rejections":sum(not x.get("chart_gate",{}).get("accepted") for x in created),
                "trade_bar_sources":list(TRADE_BAR_SOURCES),
                "positions_closed":closed,"adaptive_rewards":adaptive_rewards,
                "cost_reconciliation":reconciled,"feature_failures":failures[:20],
                "provisional_intraday_model":provisional_intraday,
                "evidence_quality":"exploratory paper evidence" if provisional_intraday else "formal completed-bar evidence",
                "orders_allowed":False}
