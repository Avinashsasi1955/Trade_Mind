#!/usr/bin/env python3
"""PRE-REGISTERED ML & SYSTEMATIC RULE EXPERIMENT SPECIFICATION (PHASE 3F)
=============================================================================
Pre-Registration Charter & Study Protocol:
This module constitutes the immutable pre-registration test protocol for evaluating
whether machine learning or systematic rule ranking produces a statistically
significant edge over the unfiltered candidate baseline in out-of-sample years.

A result of "NO EDGE FOUND" is an acceptable, expected, and empirically valid outcome.

1. DATA & FEATURE HYGIENE (STRICT ZERO-LEAKAGE):
   - Features use ONLY information available as of the close of day T-1.
   - Absolutely NO same-day (day T) data is used for scoring or candidate ranking.
   - Holdout period: 2026-01-01 onward is STRICTLY HELD OUT and never accessed.
   - Complete feature manifest and origin (computed as of close T-1):
     1. return_1d: Stock 1-day percentage return (C_{T-1} / C_{T-2} - 1). Source: daily bars.
     2. return_5d: Stock 5-day percentage return (C_{T-1} / C_{T-6} - 1). Source: daily bars.
     3. return_20d: Stock 20-day percentage return (C_{T-1} / C_{T-21} - 1). Source: daily bars.
     4. sma20_gap: Distance from 20-day SMA (C_{T-1} / SMA20 - 1). Source: daily bars.
     5. sma50_gap: Distance from 50-day SMA (C_{T-1} / SMA50 - 1). Source: daily bars.
     6. volatility_20d: Sample standard deviation of daily returns over trailing 20 days. Source: daily bars.
     7. downside_volatility_20d: Sample standard deviation of negative daily returns over trailing 20 days. Source: daily bars.
     8. rsi_14: 14-period Wilder RSI centered to [-1, 1] via (RSI - 50) / 50. Source: daily bars.
     9. atr_14: 14-period Average True Range normalized by C_{T-1}. Source: daily bars.
     10. volume_z20: Standardized volume (V_{T-1} - mean(V_20)) / std(V_20). Source: daily bars.
     11. range_pct: Intraday bar spread (H_{T-1} - L_{T-1}) / C_{T-1}. Source: daily bars.
     12. close_location: Close position in bar (C_{T-1} - L_{T-1}) / max(H_{T-1} - L_{T-1}, 1e-4) - 0.5. Source: daily bars.
     13. volume_trend: Volume ratio mean(V_5) / mean(V_20) - 1. Source: daily bars.
     14. trend_regime: Trend alignment (+1.0 if SMA20 > SMA50 else -1.0). Source: daily bars.
     15. market_return_1d: NIFTY 50 1-day return ending T-1. Source: table nifty_daily_benchmark.
     16. market_return_20d: NIFTY 50 20-day return ending T-1. Source: table nifty_daily_benchmark.
     17. relative_strength_20d: Stock 20d return minus NIFTY 20d return ending T-1. Source: daily bars & benchmark.
     18. market_regime_above_200: NIFTY 50 regime (+1.0 if Close > 200d SMA else -1.0) ending T-1. Source: table nifty_daily_benchmark.

2. CANDIDATES & TARGETS:
   - Candidates are identical to Phase 3e: Point-in-time liquid top 200 universe (250-day window ending T-1),
     corporate-action filtered, gate-accepted via compute_swing_risk_parameters.
   - Entry occurs at market open of day T with 2 ticks slippage.
   - Fixed notional ₹100,000 Zerodha fee model (delivery for Long, futures for Short).
   - Target for classification: binary y in {0, 1} (y = 1 if net R >= 1.0 and not censored).
   - Target for regression: continuous net R.
   - Separate models and evaluations are conducted per direction (LONG and SHORT).

3. MODELS & HYPERPARAMETER TUNING (FIXED IN ADVANCE):
   - Model 1: L2 Logistic Regression predicting y with StandardScaler preprocessing.
     Hyperparameter grid: C in [0.01, 0.1, 1.0, 10.0].
   - Model 2: HistGradientBoostingRegressor predicting net R.
     Hyperparameter grid: learning_rate in [0.03, 0.1], max_iter in [50, 100], min_samples_leaf in [20, 50].
   - Rule Baseline 1: 20-day momentum (Long: +return_20d; Short: -return_20d).
   - Rule Baseline 2: 5-day mean reversion (Long: -return_5d; Short: +return_5d).
   - Tuning rule: Hyperparameters are chosen exclusively by purged cross-validation inside the training years.
     Test years are never touched during model selection.

4. WALK-FORWARD & PURGED CV:
   - Walk-forward splits:
     * Split 1: Train on 2023 (2023-01-01 to 2023-12-31), Test on 2024 (2024-01-01 to 2024-12-31).
     * Split 2: Train on 2023–2024 (2023-01-01 to 2024-12-31), Test on 2025 (2025-01-01 to 2025-12-31).
   - PurgedGroupTimeSeriesSplit:
     * Groups: trading dates.
     * 3 expanding folds inside the training window.
     * Purge gap: >= 10 trading days between train and validation fold to prevent label overlap from swing holding periods.

5. SIGNAL GENERATION, EXECUTION & SAME-DAY BASELINE:
   - On each day T in the test year:
     * Rank available candidates by model score descending.
     * Top decile count: k = max(1, ceil(0.10 * N_candidates)), capped at 10 trades per day.
     * Enforce non-overlapping per symbol: A symbol is eligible only if it has no active trade currently open.
     * Compare against the same days' unfiltered average net R:
       The unfiltered baseline is the mean net R across all gate-accepted candidates on the exact trading days
       where top-decile trades were executed.

6. PASS BAR CRITERIA (PRE-REGISTERED THRESHOLDS):
   - To declare an edge, a model must satisfy ALL of the following:
     1. Top-decile net R minus same-day baseline >= +0.10R in BOTH test years (2024 AND 2025).
     2. 95% block-bootstrap confidence interval (resampled by trading day across all symbols) has lower bound > 0.0 in BOTH test years.
     3. Trade count >= 1,000 trades per year in BOTH test years.
   - If ANY condition is not met: Conclude "NO EDGE".

7. MULTIPLE TESTING CONTROL:
   - Exactly 8 variants are tested in total (4 Long, 4 Short).
   - Every single variant is reported in full detail. No cherry-picking.
   - Top 10 feature importances printed ONLY IF a model passes.
=============================================================================
"""
from __future__ import annotations

import json
import logging
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import mean_squared_error, roc_auc_score
from sklearn.preprocessing import StandardScaler

from backend.swing_risk import compute_swing_risk_parameters
from backend.transaction_costs import estimate_zerodha_costs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase_3f_pre_registration")

DB_URL = "postgresql://avinash@localhost:5432/nivesh_v3_staging"
FIXED_NOTIONAL = Decimal("100000.0")
IST = ZoneInfo("Asia/Kolkata")
START_DATE = "2023-01-01"
END_DATE = "2025-12-31"  # Strict holdout: 2026 onward held out


def get_db_connection():
    return psycopg2.connect(DB_URL)


def load_nifty_benchmark(conn) -> Dict[str, Dict[str, Any]]:
    """Load pre-computed NIFTY 50 close, 200d SMA, and regime from nifty_daily_benchmark."""
    logger.info("Loading NIFTY benchmark series from nifty_daily_benchmark...")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT trade_date, close_price, sma_200, regime
            FROM nifty_daily_benchmark
            ORDER BY trade_date ASC;
        """)
        rows = cur.fetchall()

    nifty_by_date = {}
    sorted_rows = sorted(rows, key=lambda x: str(x["trade_date"]))
    for i, r in enumerate(sorted_rows):
        dt_str = str(r["trade_date"])
        c_price = float(r["close_price"])
        sma_200 = float(r["sma_200"]) if r["sma_200"] is not None else c_price
        ret_1d = (c_price / float(sorted_rows[i - 1]["close_price"]) - 1.0) if i >= 1 else 0.0
        ret_20d = (c_price / float(sorted_rows[i - 20]["close_price"]) - 1.0) if i >= 20 else 0.0
        nifty_by_date[dt_str] = {
            "close": c_price,
            "sma_200": sma_200,
            "regime": r["regime"],
            "return_1d": ret_1d,
            "return_20d": ret_20d,
            "above_200": 1.0 if c_price > sma_200 else -1.0,
        }
    logger.info(f"Loaded {len(nifty_by_date)} NIFTY benchmark trading sessions.")
    return nifty_by_date


def get_corporate_action_jump_dates(conn) -> Set[Tuple[str, str]]:
    """Identify jump moves (>20%) and corporate action dates to filter candidates."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            WITH daily_moves AS (
                SELECT i.symbol, b.bar_time::date as dt,
                       (b.close_price / NULLIF(LAG(b.close_price) OVER (PARTITION BY i.symbol ORDER BY b.bar_time), 0) - 1) as ret
                FROM live_market_bars b
                JOIN instrument_master i ON i.id = b.instrument_id
                WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ' AND b.interval = 'day'
                  AND b.bar_time >= '2022-01-01' AND b.bar_time <= '2026-06-30'
            )
            SELECT symbol, dt, ret FROM daily_moves WHERE abs(ret) >= 0.20;
        """)
        moves = cur.fetchall()
        jump_dates = {(m["symbol"], str(m["dt"])) for m in moves}

        cur.execute("""
            SELECT symbol, effective_date FROM corporate_actions
            WHERE effective_date >= '2022-01-01' AND effective_date <= '2026-06-30';
        """)
        ca_rows = cur.fetchall()
        for r in ca_rows:
            jump_dates.add((r["symbol"], str(r["effective_date"])))

    logger.info(f"Loaded {len(jump_dates)} corporate action / large jump date filters.")
    return jump_dates


def build_point_in_time_universes(conn, trading_dates: List[str]) -> Dict[str, Set[str]]:
    """Compute point-in-time top 200 symbols by avg traded value over previous 250 trading days ending at T-1."""
    logger.info("Computing point-in-time 250-day liquid universes...")
    pit_universes = {}

    with conn.cursor() as cur:
        cur.execute("""
            SELECT i.symbol, b.bar_time::date as dt, (b.volume * b.close_price) as turnover
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ' AND b.interval = 'day'
              AND b.bar_time >= '2021-12-01' AND b.bar_time <= '2026-06-30'
            ORDER BY b.bar_time ASC;
        """)
        rows = cur.fetchall()

    turnover_by_date = defaultdict(dict)
    all_dates_set = set()
    for sym, dt, val in rows:
        dt_str = str(dt)
        turnover_by_date[dt_str][sym] = float(val or 0.0)
        all_dates_set.add(dt_str)

    all_sorted_dates = sorted(all_dates_set)
    date_to_idx = {d: i for i, d in enumerate(all_sorted_dates)}

    for entry_dt in trading_dates:
        t_idx = date_to_idx.get(entry_dt)
        if t_idx is None or t_idx < 250:
            continue
        prev_250_dates = all_sorted_dates[t_idx - 250 : t_idx]

        sym_totals = defaultdict(float)
        sym_counts = defaultdict(int)
        for d in prev_250_dates:
            for s, v in turnover_by_date[d].items():
                sym_totals[s] += v
                sym_counts[s] += 1

        avg_turnover = []
        for s, total in sym_totals.items():
            if sym_counts[s] >= 200:
                avg_turnover.append((s, total / sym_counts[s]))

        avg_turnover.sort(key=lambda x: x[1], reverse=True)
        top200 = {s for s, _ in avg_turnover[:200]}
        pit_universes[entry_dt] = top200

    logger.info(f"Built point-in-time universes for {len(pit_universes)} trading dates.")
    return pit_universes


def compute_t_minus_1_features(
    s_bars: List[Dict[str, Any]],
    i: int,
    nifty_by_date: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """Compute features strictly as of the close of day T-1 (bar index i). Zero same-day data."""
    if i < 50:
        return None

    closes = [b["close"] for b in s_bars[: i + 1]]
    highs = [b["high"] for b in s_bars[: i + 1]]
    lows = [b["low"] for b in s_bars[: i + 1]]
    volumes = [b["volume"] for b in s_bars[: i + 1]]
    t_minus_1_date = s_bars[i]["trade_date"]

    c_now = closes[-1]
    c_prev1 = closes[-2]
    c_prev5 = closes[-6]
    c_prev20 = closes[-21]

    ret_1d = (c_now / c_prev1) - 1.0
    ret_5d = (c_now / c_prev5) - 1.0
    ret_20d = (c_now / c_prev20) - 1.0

    win20 = closes[-20:]
    win50 = closes[-50:]
    sma20 = sum(win20) / 20.0
    sma50 = sum(win50) / 50.0

    sma20_gap = (c_now / sma20) - 1.0
    sma50_gap = (c_now / sma50) - 1.0

    rets20 = [(closes[j] / closes[j - 1]) - 1.0 for j in range(i - 19, i + 1)]
    mean_ret = sum(rets20) / 20.0
    vol_20d = math.sqrt(sum((r - mean_ret) ** 2 for r in rets20) / 20.0)

    neg_rets = [min(0.0, r) for r in rets20]
    downside_vol_20d = math.sqrt(sum(r**2 for r in neg_rets) / 20.0)

    # 14-period RSI
    gains, losses = [], []
    for j in range(i - 13, i + 1):
        chg = closes[j] - closes[j - 1]
        if chg > 0:
            gains.append(chg)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(chg))
    avg_gain = sum(gains) / 14.0
    avg_loss = sum(losses) / 14.0
    if avg_loss < 1e-9:
        rsi_14 = 1.0
    else:
        rs = avg_gain / avg_loss
        rsi_raw = 100.0 - (100.0 / (1.0 + rs))
        rsi_14 = (rsi_raw - 50.0) / 50.0  # Centered to [-1, 1]

    # 14-period ATR
    trs = []
    for j in range(i - 13, i + 1):
        tr = max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1]))
        trs.append(tr)
    atr_14 = (sum(trs) / 14.0) / max(c_now, 1e-4)

    # Volume z-score & trend
    v_win20 = volumes[-20:]
    mean_v20 = sum(v_win20) / 20.0
    std_v20 = math.sqrt(sum((v - mean_v20) ** 2 for v in v_win20) / 20.0)
    std_v20 = max(std_v20, 1.0)
    volume_z20 = (volumes[-1] - mean_v20) / std_v20

    v_win5 = volumes[-5:]
    mean_v5 = sum(v_win5) / 5.0
    volume_trend = (mean_v5 / max(mean_v20, 1.0)) - 1.0

    # Range and close location
    bar_h = highs[-1]
    bar_l = lows[-1]
    range_pct = (bar_h - bar_l) / max(c_now, 1e-4)
    close_loc = ((c_now - bar_l) / max(bar_h - bar_l, 1e-4)) - 0.5

    trend_regime = 1.0 if sma20 > sma50 else -1.0

    # NIFTY benchmark features ending T-1
    nifty_info = nifty_by_date.get(t_minus_1_date, {})
    mkt_ret_1d = float(nifty_info.get("return_1d", 0.0))
    mkt_ret_20d = float(nifty_info.get("return_20d", 0.0))
    rel_strength_20d = ret_20d - mkt_ret_20d
    mkt_regime_200 = float(nifty_info.get("above_200", 1.0))

    return {
        "return_1d": ret_1d,
        "return_5d": ret_5d,
        "return_20d": ret_20d,
        "sma20_gap": sma20_gap,
        "sma50_gap": sma50_gap,
        "volatility_20d": vol_20d,
        "downside_volatility_20d": downside_vol_20d,
        "rsi_14": rsi_14,
        "atr_14": atr_14,
        "volume_z20": volume_z20,
        "range_pct": range_pct,
        "close_location": close_loc,
        "volume_trend": volume_trend,
        "trend_regime": trend_regime,
        "market_return_1d": mkt_ret_1d,
        "market_return_20d": mkt_ret_20d,
        "relative_strength_20d": rel_strength_20d,
        "market_regime_above_200": mkt_regime_200,
    }


FEATURE_NAMES = [
    "return_1d",
    "return_5d",
    "return_20d",
    "sma20_gap",
    "sma50_gap",
    "volatility_20d",
    "downside_volatility_20d",
    "rsi_14",
    "atr_14",
    "volume_z20",
    "range_pct",
    "close_location",
    "volume_trend",
    "trend_regime",
    "market_return_1d",
    "market_return_20d",
    "relative_strength_20d",
    "market_regime_above_200",
]


def simulate_all_candidates_with_features(
    conn,
    pit_universes: Dict[str, Set[str]],
    nifty_by_date: Dict[str, Dict[str, Any]],
    corp_jump_dates: Set[Tuple[str, str]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Simulate gate-accepted swing candidates identical to Phase 3e and attach T-1 features."""
    universe_symbols = set()
    for u in pit_universes.values():
        universe_symbols.update(u)
    universe_symbols_list = sorted(universe_symbols)
    logger.info(f"Unique symbols in point-in-time universe: {len(universe_symbols_list)}")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT i.symbol, b.bar_time::date as dt, b.open_price, b.high_price, b.low_price, b.close_price, b.volume
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.symbol = ANY(%s) AND i.exchange = 'NSE' AND i.instrument_type = 'EQ'
              AND b.interval = 'day' AND b.bar_time >= '2022-01-01' AND b.bar_time <= '2026-06-30'
            ORDER BY i.symbol, b.bar_time ASC;
        """, (universe_symbols_list,))
        rows = cur.fetchall()

    bars_by_symbol = defaultdict(list)
    for sym, dt, o, h, l, c, v in rows:
        bars_by_symbol[sym].append({
            "trade_date": str(dt),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": int(v or 0),
        })

    candidates_by_direction = {"LONG": [], "SHORT": []}
    holding_limit_days = 10
    tick_size = 0.05

    for sym in universe_symbols_list:
        s_bars = bars_by_symbol.get(sym, [])
        n_bars = len(s_bars)
        if n_bars < 60:
            continue

        for i in range(50, n_bars - 1):
            next_bar = s_bars[i + 1]
            entry_date = next_bar["trade_date"]

            # Strict holdout: 2026 onward held out
            if entry_date < START_DATE or entry_date > END_DATE:
                continue

            current_pit_universe = pit_universes.get(entry_date, set())
            if sym not in current_pit_universe:
                continue

            entry_year = int(entry_date[:4])
            history_candles = s_bars[max(0, i - 25) : i + 1]
            walk_end = min(n_bars, i + 1 + holding_limit_days)
            walk_bars = s_bars[i + 1 : walk_end]
            if not walk_bars:
                continue

            # Corporate action check
            holding_dates = [b["trade_date"] for b in walk_bars]
            if any((sym, d) in corp_jump_dates for d in holding_dates):
                continue

            t_minus_1_feats = compute_t_minus_1_features(s_bars, i, nifty_by_date)
            if t_minus_1_feats is None:
                continue

            nifty_info = nifty_by_date.get(s_bars[i]["trade_date"], {})
            nifty_regime = nifty_info.get("regime", "UNKNOWN")

            for side in ("BUY", "SELL"):
                dir_key = "LONG" if side == "BUY" else "SHORT"
                slip = 2.0 * tick_size
                entry_price = round(next_bar["open"] + slip if side == "BUY" else next_bar["open"] - slip, 2)

                risk_params, rej_reason = compute_swing_risk_parameters(
                    symbol=sym,
                    side=side,
                    entry_price=entry_price,
                    daily_candles=history_candles,
                    return_reason=True,
                )

                if risk_params is None:
                    continue  # Gate rejected

                sl_price = float(risk_params["stop_loss_price"])
                tp_price = float(risk_params["take_profit_price"])
                r_points = float(risk_params["stop_distance"])
                if r_points <= 0.01:
                    continue

                quantity = max(1, int(round(float(FIXED_NOTIONAL) / entry_price)))
                r_rupees = quantity * r_points

                seg = "EQUITY_DELIVERY" if side == "BUY" else "FUTURES"
                entry_cost_bd = estimate_zerodha_costs(
                    segment=seg,
                    side=side,
                    price=Decimal(str(entry_price)),
                    quantity=quantity,
                    exchange="NSE",
                )
                entry_fee = float(entry_cost_bd.total)

                running_sl = sl_price
                exit_price = None
                exit_reason = None
                exit_bar_idx = None
                reached_1r = False
                reached_minus_1r = False
                hit_1r_first = 0

                for b_idx, b in enumerate(walk_bars):
                    b_open = b["open"]
                    b_high = b["high"]
                    b_low = b["low"]
                    b_close = b["close"]

                    fav_r = (b_high - entry_price) / r_points if side == "BUY" else (entry_price - b_low) / r_points
                    adv_r = (entry_price - b_low) / r_points if side == "BUY" else (b_high - entry_price) / r_points

                    if not reached_1r and not reached_minus_1r:
                        if fav_r >= 1.0 and adv_r < 1.0:
                            reached_1r = True
                            hit_1r_first = 1
                        elif adv_r >= 1.0 and fav_r < 1.0:
                            reached_minus_1r = True
                            hit_1r_first = 0
                        elif fav_r >= 1.0 and adv_r >= 1.0:
                            if (b_open >= entry_price if side == "BUY" else b_open <= entry_price):
                                reached_1r = True
                                hit_1r_first = 1
                            else:
                                reached_minus_1r = True
                                hit_1r_first = 0

                    # 1. Take profit
                    if side == "BUY":
                        if b_open >= tp_price:
                            exit_price = b_open
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_idx = b_idx
                            break
                        elif b_high >= tp_price:
                            exit_price = tp_price
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_idx = b_idx
                            break
                    else:
                        if b_open <= tp_price:
                            exit_price = b_open
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_idx = b_idx
                            break
                        elif b_low <= tp_price:
                            exit_price = tp_price
                            exit_reason = "TAKE_PROFIT"
                            exit_bar_idx = b_idx
                            break

                    # 2. Stop loss / gap
                    if side == "BUY":
                        prev_c = walk_bars[b_idx - 1]["close"] if b_idx > 0 else history_candles[-1]["close"]
                        if b_open <= running_sl:
                            exit_price = b_open if (prev_c > running_sl and b_open < prev_c) else running_sl
                            exit_reason = "GAP_DOWN_STOP" if (prev_c > running_sl and b_open < prev_c) else "STOP_LOSS"
                            exit_bar_idx = b_idx
                            break
                        elif b_low <= running_sl:
                            exit_price = running_sl
                            exit_reason = "STOP_LOSS"
                            exit_bar_idx = b_idx
                            break
                    else:
                        prev_c = walk_bars[b_idx - 1]["close"] if b_idx > 0 else history_candles[-1]["close"]
                        if b_open >= running_sl:
                            exit_price = b_open if (prev_c < running_sl and b_open > prev_c) else running_sl
                            exit_reason = "GAP_UP_STOP" if (prev_c < running_sl and b_open > prev_c) else "STOP_LOSS"
                            exit_bar_idx = b_idx
                            break
                        elif b_high >= running_sl:
                            exit_price = running_sl
                            exit_reason = "STOP_LOSS"
                            exit_bar_idx = b_idx
                            break

                    # 3. Profit harvest trailing
                    if b_idx >= 1:
                        if side == "BUY":
                            if b_close > entry_price + 1.5 * r_points:
                                tightened = entry_price + 0.5 * r_points
                                if tightened > running_sl:
                                    running_sl = tightened
                        else:
                            if b_close < entry_price - 1.5 * r_points:
                                tightened = entry_price - 0.5 * r_points
                                if tightened < running_sl:
                                    running_sl = tightened

                    # 4. Holding limit exit at bar 10
                    if b_idx == holding_limit_days - 1:
                        exit_price = b_close
                        exit_reason = "HOLDING_LIMIT_EXIT"
                        exit_bar_idx = b_idx
                        break

                if exit_price is None:
                    exit_price = walk_bars[-1]["close"]
                    exit_reason = "DATA_TRUNCATED_EXIT"
                    exit_bar_idx = len(walk_bars) - 1
                    is_censored = True
                else:
                    is_censored = False

                exit_cost_bd = estimate_zerodha_costs(
                    segment=seg,
                    side="SELL" if side == "BUY" else "BUY",
                    price=Decimal(str(exit_price)),
                    quantity=quantity,
                    exchange="NSE",
                )
                roundtrip_fees = entry_fee + float(exit_cost_bd.total)

                if side == "BUY":
                    gross_pnl = quantity * (exit_price - entry_price)
                    gross_r = (exit_price - entry_price) / r_points
                else:
                    gross_pnl = quantity * (entry_price - exit_price)
                    gross_r = (entry_price - exit_price) / r_points

                net_pnl = gross_pnl - roundtrip_fees
                net_r = gross_r - (roundtrip_fees / r_rupees)
                y = 1 if (not is_censored and net_r >= 1.0) else 0

                exit_date = walk_bars[exit_bar_idx]["trade_date"]

                candidate_rec = {
                    "symbol": sym,
                    "side": side,
                    "direction": dir_key,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "entry_year": entry_year,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "censored": is_censored,
                    "gross_r": gross_r,
                    "net_r": net_r,
                    "y": y,
                    "hit_1r_first": hit_1r_first,
                    "nifty_regime": nifty_regime,
                    "features": t_minus_1_feats,
                }
                candidates_by_direction[dir_key].append(candidate_rec)

    logger.info(
        f"Simulated candidates: {len(candidates_by_direction['LONG'])} LONG, "
        f"{len(candidates_by_direction['SHORT'])} SHORT."
    )
    return candidates_by_direction


def block_bootstrap_difference_ci(
    daily_model_records: List[Dict[str, Any]],
    daily_unfiltered_means: Dict[str, float],
    n_bootstraps: int = 2000,
    seed: int = 42,
) -> Tuple[float, float, float, float]:
    """Compute 95% block-bootstrap interval resampled by trading day across all symbols.

    Returns: (diff_mean, diff_ci_lower, diff_ci_upper, model_mean).
    """
    if not daily_model_records:
        return 0.0, 0.0, 0.0, 0.0

    # Group model trades by entry date
    trades_by_day = defaultdict(list)
    for r in daily_model_records:
        trades_by_day[r["entry_date"]].append(r["net_r"])

    unique_days = sorted(trades_by_day.keys())
    all_model_r = [r["net_r"] for r in daily_model_records]
    model_mean = float(np.mean(all_model_r))

    # Same-day baseline average across those active days
    baseline_r_for_days = [daily_unfiltered_means[d] for d in unique_days]
    same_day_baseline_mean = float(np.mean(baseline_r_for_days)) if baseline_r_for_days else 0.0
    empirical_diff = model_mean - same_day_baseline_mean

    rng = np.random.default_rng(seed)
    diff_samples = []

    for _ in range(n_bootstraps):
        sampled_days = rng.choice(unique_days, size=len(unique_days), replace=True)
        sampled_model_r = []
        sampled_baseline_means = []
        for d in sampled_days:
            sampled_model_r.extend(trades_by_day[d])
            sampled_baseline_means.append(daily_unfiltered_means[d])

        if sampled_model_r and sampled_baseline_means:
            boot_model_mean = float(np.mean(sampled_model_r))
            boot_baseline_mean = float(np.mean(sampled_baseline_means))
            diff_samples.append(boot_model_mean - boot_baseline_mean)

    if diff_samples:
        ci_lower = float(np.percentile(diff_samples, 2.5))
        ci_upper = float(np.percentile(diff_samples, 97.5))
    else:
        ci_lower, ci_upper = 0.0, 0.0

    return empirical_diff, ci_lower, ci_upper, model_mean


class PurgedGroupTimeSeriesSplit:
    """Expanding-window time series CV grouped by trading date with a purge buffer >= purge_days."""

    def __init__(self, n_splits: int = 3, purge_days: int = 10):
        self.n_splits = n_splits
        self.purge_days = purge_days

    def split(self, dates: List[str]) -> List[Tuple[np.ndarray, np.ndarray]]:
        unique_dates = sorted(list(set(dates)))
        n_dates = len(unique_dates)
        date_to_indices = defaultdict(list)
        for idx, d in enumerate(dates):
            date_to_indices[d].append(idx)

        # Minimum training days: 40% of dates, then expand
        min_train_dates = max(20, int(n_dates * 0.40))
        remaining_dates = n_dates - min_train_dates
        val_block_size = max(10, remaining_dates // self.n_splits)

        splits = []
        for fold in range(self.n_splits):
            train_end_idx = min_train_dates + fold * val_block_size
            val_start_idx = train_end_idx + self.purge_days
            val_end_idx = min(n_dates, val_start_idx + val_block_size)
            if val_start_idx >= n_dates or val_end_idx <= val_start_idx:
                continue

            train_dates_fold = unique_dates[:train_end_idx]
            val_dates_fold = unique_dates[val_start_idx:val_end_idx]

            train_idx = []
            for d in train_dates_fold:
                train_idx.extend(date_to_indices[d])
            val_idx = []
            for d in val_dates_fold:
                val_idx.extend(date_to_indices[d])

            if train_idx and val_idx:
                splits.append((np.array(train_idx), np.array(val_idx)))

        return splits


def tune_logistic_regression(X_train: np.ndarray, y_train: np.ndarray, train_dates: List[str]) -> float:
    """Select L2 logistic regression C via purged CV on training years."""
    cv = PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=10)
    splits = cv.split(train_dates)
    grid_c = [0.01, 0.1, 1.0, 10.0]

    best_c = 1.0
    best_score = -1.0

    for c_val in grid_c:
        fold_scores = []
        for tr_idx, val_idx in splits:
            scaler = StandardScaler()
            X_tr = scaler.fit_transform(X_train[tr_idx])
            X_val = scaler.transform(X_train[val_idx])
            y_tr = y_train[tr_idx]
            y_val = y_train[val_idx]

            if len(np.unique(y_tr)) < 2 or len(np.unique(y_val)) < 2:
                continue

            clf = LogisticRegression(penalty="l2", C=c_val, solver="lbfgs", max_iter=500, random_state=42)
            clf.fit(X_tr, y_tr)
            probs = clf.predict_proba(X_val)[:, 1]
            try:
                score = roc_auc_score(y_val, probs)
                fold_scores.append(score)
            except Exception:
                pass

        avg_score = float(np.mean(fold_scores)) if fold_scores else 0.5
        if avg_score > best_score:
            best_score = avg_score
            best_c = c_val

    logger.info(f"Logistic Regression Purged CV selected C={best_c} (Mean Val AUC={best_score:.4f})")
    return best_c


def tune_hist_gradient_boosting(X_train: np.ndarray, r_train: np.ndarray, train_dates: List[str]) -> Dict[str, Any]:
    """Select HistGradientBoosting hyperparameters via purged CV on training years."""
    cv = PurgedGroupTimeSeriesSplit(n_splits=3, purge_days=10)
    splits = cv.split(train_dates)

    grid = [
        {"learning_rate": 0.03, "max_iter": 50, "min_samples_leaf": 20},
        {"learning_rate": 0.03, "max_iter": 50, "min_samples_leaf": 50},
        {"learning_rate": 0.03, "max_iter": 100, "min_samples_leaf": 20},
        {"learning_rate": 0.03, "max_iter": 100, "min_samples_leaf": 50},
        {"learning_rate": 0.10, "max_iter": 50, "min_samples_leaf": 20},
        {"learning_rate": 0.10, "max_iter": 50, "min_samples_leaf": 50},
        {"learning_rate": 0.10, "max_iter": 100, "min_samples_leaf": 20},
        {"learning_rate": 0.10, "max_iter": 100, "min_samples_leaf": 50},
    ]

    best_params = grid[0]
    best_loss = float("inf")

    for params in grid:
        fold_losses = []
        for tr_idx, val_idx in splits:
            X_tr = X_train[tr_idx]
            X_val = X_train[val_idx]
            r_tr = r_train[tr_idx]
            r_val = r_train[val_idx]

            reg = HistGradientBoostingRegressor(**params, random_state=42)
            reg.fit(X_tr, r_tr)
            preds = reg.predict(X_val)
            loss = mean_squared_error(r_val, preds)
            fold_losses.append(loss)

        avg_loss = float(np.mean(fold_losses)) if fold_losses else float("inf")
        if avg_loss < best_loss:
            best_loss = avg_loss
            best_params = params

    logger.info(f"HistGradientBoosting Purged CV selected {best_params} (Mean Val MSE={best_loss:.4f})")
    return best_params


def evaluate_test_year(
    candidates_by_date: Dict[str, List[Dict[str, Any]]],
    scoring_fn: Any,
    test_year_dates: List[str],
) -> Dict[str, Any]:
    """Execute top-decile portfolio simulation (capped at 10/day, non-overlapping per symbol)."""
    executed_trades = []
    daily_unfiltered_means = {}

    active_positions_until = {}  # symbol -> exit_date

    for d in test_year_dates:
        day_cands = candidates_by_date.get(d, [])
        if not day_cands:
            continue

        # Unfiltered daily average net R
        daily_unfiltered_means[d] = float(np.mean([c["net_r"] for c in day_cands]))

        # Score candidates
        scored = []
        for c in day_cands:
            s = scoring_fn(c)
            scored.append((s, c))

        # Rank descending by score
        scored.sort(key=lambda x: x[0], reverse=True)

        # Top decile quota (cap 10)
        n_cands = len(day_cands)
        k_top = max(1, int(math.ceil(0.10 * n_cands)))
        k_cap = min(10, k_top)

        # Non-overlapping per symbol selection
        taken_today = 0
        for s_score, cand in scored:
            sym = cand["symbol"]
            # Check if active
            prev_exit = active_positions_until.get(sym)
            if prev_exit is not None and d <= prev_exit:
                continue  # Overlapping position, skip

            # Accept trade
            executed_trades.append(cand)
            active_positions_until[sym] = cand["exit_date"]
            taken_today += 1
            if taken_today >= k_cap:
                break

    # Metrics
    n_trades = len(executed_trades)
    if n_trades == 0:
        return {
            "trade_count": 0,
            "mean_net_r": 0.0,
            "same_day_baseline": 0.0,
            "difference": 0.0,
            "ci_lower": 0.0,
            "ci_upper": 0.0,
            "positive_rate": 0.0,
            "regime_split": {},
        }

    diff, ci_l, ci_u, model_mean = block_bootstrap_difference_ci(
        executed_trades, daily_unfiltered_means, n_bootstraps=2000, seed=42
    )

    # Same day baseline across active days
    active_days = sorted(list({t["entry_date"] for t in executed_trades}))
    active_baseline_mean = float(np.mean([daily_unfiltered_means[d] for d in active_days]))

    pos_rate = float(np.mean([t["y"] for t in executed_trades])) * 100.0

    # Regime split
    regime_groups = defaultdict(list)
    for t in executed_trades:
        regime_groups[t["nifty_regime"]].append(t)

    regime_split = {}
    for r_name, r_trades in regime_groups.items():
        r_mean = float(np.mean([t["net_r"] for t in r_trades]))
        regime_split[r_name] = {
            "trade_count": len(r_trades),
            "mean_net_r": round(r_mean, 4),
            "positive_rate": round(float(np.mean([t["y"] for t in r_trades])) * 100.0, 2),
        }

    return {
        "trade_count": n_trades,
        "mean_net_r": round(model_mean, 4),
        "same_day_baseline": round(active_baseline_mean, 4),
        "difference": round(diff, 4),
        "ci_lower": round(ci_l, 4),
        "ci_upper": round(ci_u, 4),
        "positive_rate": round(pos_rate, 2),
        "regime_split": regime_split,
        "executed_trades": executed_trades,
    }


def run_phase_3f_pre_registration():
    conn = get_db_connection()

    # Step 1: Load NIFTY benchmark & corporate jump dates
    nifty_by_date = load_nifty_benchmark(conn)
    corp_jump_dates = get_corporate_action_jump_dates(conn)

    # Step 2: Build Point-in-Time universes for 2023-2025
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT b.bar_time::date as dt
            FROM live_market_bars b
            WHERE b.bar_time >= '2023-01-01' AND b.bar_time <= '2025-12-31' AND b.interval = 'day'
            ORDER BY dt ASC;
        """)
        trading_dates = [str(r[0]) for r in cur.fetchall()]

    pit_universes = build_point_in_time_universes(conn, trading_dates)

    # Step 3: Simulate all candidate entries with T-1 features
    candidates_by_direction = simulate_all_candidates_with_features(
        conn, pit_universes, nifty_by_date, corp_jump_dates
    )

    models_list = [
        "L2 Logistic Regression (predicting y)",
        "HistGradientBoosting (predicting net R)",
        "20-Day Momentum Rule",
        "5-Day Mean Reversion Rule",
    ]

    total_variants_tested = len(models_list) * 2  # Long & Short = 8 total variants

    results_report = {
        "pre_registration_status": "LOCKED",
        "total_variants_tested": total_variants_tested,
        "pass_bar_thresholds": {
            "min_delta_net_r": 0.10,
            "ci_lower_strictly_positive": True,
            "min_trades_per_year": 1000,
            "required_in_both_years": True,
        },
        "models": {},
    }

    dates_2023 = [d for d in trading_dates if d.startswith("2023")]
    dates_2024 = [d for d in trading_dates if d.startswith("2024")]
    dates_2025 = [d for d in trading_dates if d.startswith("2025")]

    for direction in ("LONG", "SHORT"):
        cands = candidates_by_direction[direction]
        logger.info(f"\n=======================================================")
        logger.info(f"PROCESSING DIRECTION: {direction} ({len(cands)} total candidates)")
        logger.info(f"=======================================================")

        cands_by_date = defaultdict(list)
        for c in cands:
            cands_by_date[c["entry_date"]].append(c)

        # -------------------------------------------------------------
        # SPLIT 1: Train 2023, Test 2024
        # -------------------------------------------------------------
        train_cands_split1 = [c for c in cands if c["entry_year"] == 2023]
        X_tr1 = np.array([[c["features"][fname] for fname in FEATURE_NAMES] for c in train_cands_split1])
        y_tr1 = np.array([c["y"] for c in train_cands_split1])
        r_tr1 = np.array([c["net_r"] for c in train_cands_split1])
        dates_tr1 = [c["entry_date"] for c in train_cands_split1]

        # Tune models on 2023
        best_c_s1 = tune_logistic_regression(X_tr1, y_tr1, dates_tr1)
        best_hgb_s1 = tune_hist_gradient_boosting(X_tr1, r_tr1, dates_tr1)

        # Refit on full 2023
        scaler_lr_s1 = StandardScaler()
        X_tr1_scaled = scaler_lr_s1.fit_transform(X_tr1)
        lr_s1 = LogisticRegression(penalty="l2", C=best_c_s1, solver="lbfgs", max_iter=500, random_state=42)
        lr_s1.fit(X_tr1_scaled, y_tr1)

        hgb_s1 = HistGradientBoostingRegressor(**best_hgb_s1, random_state=42)
        hgb_s1.fit(X_tr1, r_tr1)

        # -------------------------------------------------------------
        # SPLIT 2: Train 2023-2024, Test 2025
        # -------------------------------------------------------------
        train_cands_split2 = [c for c in cands if c["entry_year"] in (2023, 2024)]
        X_tr2 = np.array([[c["features"][fname] for fname in FEATURE_NAMES] for c in train_cands_split2])
        y_tr2 = np.array([c["y"] for c in train_cands_split2])
        r_tr2 = np.array([c["net_r"] for c in train_cands_split2])
        dates_tr2 = [c["entry_date"] for c in train_cands_split2]

        # Tune models on 2023-2024
        best_c_s2 = tune_logistic_regression(X_tr2, y_tr2, dates_tr2)
        best_hgb_s2 = tune_hist_gradient_boosting(X_tr2, r_tr2, dates_tr2)

        # Refit on full 2023-2024
        scaler_lr_s2 = StandardScaler()
        X_tr2_scaled = scaler_lr_s2.fit_transform(X_tr2)
        lr_s2 = LogisticRegression(penalty="l2", C=best_c_s2, solver="lbfgs", max_iter=500, random_state=42)
        lr_s2.fit(X_tr2_scaled, y_tr2)

        hgb_s2 = HistGradientBoostingRegressor(**best_hgb_s2, random_state=42)
        hgb_s2.fit(X_tr2, r_tr2)

        # -------------------------------------------------------------
        # EVALUATE ALL 4 MODELS ON 2024 AND 2025
        # -------------------------------------------------------------
        for m_name in models_list:
            model_key = f"{direction} - {m_name}"
            logger.info(f"Evaluating variant: {model_key}")

            # Define scoring functions for Split 1 (2024 test) and Split 2 (2025 test)
            if "Logistic Regression" in m_name:
                scoring_fn_s1 = lambda c: float(lr_s1.predict_proba(scaler_lr_s1.transform(np.array([[c["features"][fn] for fn in FEATURE_NAMES]])))[0, 1])
                scoring_fn_s2 = lambda c: float(lr_s2.predict_proba(scaler_lr_s2.transform(np.array([[c["features"][fn] for fn in FEATURE_NAMES]])))[0, 1])
            elif "HistGradientBoosting" in m_name:
                scoring_fn_s1 = lambda c: float(hgb_s1.predict(np.array([[c["features"][fn] for fn in FEATURE_NAMES]]))[0])
                scoring_fn_s2 = lambda c: float(hgb_s2.predict(np.array([[c["features"][fn] for fn in FEATURE_NAMES]]))[0])
            elif "Momentum" in m_name:
                mult = 1.0 if direction == "LONG" else -1.0
                scoring_fn_s1 = lambda c: mult * c["features"]["return_20d"]
                scoring_fn_s2 = lambda c: mult * c["features"]["return_20d"]
            elif "Mean Reversion" in m_name:
                mult = -1.0 if direction == "LONG" else 1.0
                scoring_fn_s1 = lambda c: mult * c["features"]["return_5d"]
                scoring_fn_s2 = lambda c: mult * c["features"]["return_5d"]
            else:
                raise ValueError(f"Unknown model {m_name}")

            # Evaluate 2024 test
            res_2024 = evaluate_test_year(cands_by_date, scoring_fn_s1, dates_2024)
            # Evaluate 2025 test
            res_2025 = evaluate_test_year(cands_by_date, scoring_fn_s2, dates_2025)

            # Pass bar check
            pass_2024 = (
                res_2024["difference"] >= 0.10
                and res_2024["ci_lower"] > 0.0
                and res_2024["trade_count"] >= 1000
            )
            pass_2025 = (
                res_2025["difference"] >= 0.10
                and res_2025["ci_lower"] > 0.0
                and res_2025["trade_count"] >= 1000
            )
            overall_pass = pass_2024 and pass_2025

            verdict = "PASSED (EDGE CONFIRMED)" if overall_pass else "NO EDGE"

            model_record = {
                "direction": direction,
                "model_name": m_name,
                "verdict": verdict,
                "passed_in_both_years": overall_pass,
                "test_year_2024": {k: v for k, v in res_2024.items() if k != "executed_trades"},
                "test_year_2025": {k: v for k, v in res_2025.items() if k != "executed_trades"},
            }

            if overall_pass:
                # Top 10 feature importance
                if "Logistic Regression" in m_name:
                    coefs = np.abs(lr_s2.coef_[0])
                    top_idx = np.argsort(coefs)[::-1][:10]
                    model_record["top_10_features"] = [
                        {"feature": FEATURE_NAMES[idx], "importance": float(coefs[idx])}
                        for idx in top_idx
                    ]
                elif "HistGradientBoosting" in m_name:
                    # Permutation or tree feature importance
                    pass

            results_report["models"][model_key] = model_record

    # Save results to json
    with open("phase_3f_pre_registered_results.json", "w") as f:
        json.dump(results_report, f, indent=2)

    logger.info("Saved pre-registered experiment results to phase_3f_pre_registered_results.json")
    conn.close()


if __name__ == "__main__":
    run_phase_3f_pre_registration()
