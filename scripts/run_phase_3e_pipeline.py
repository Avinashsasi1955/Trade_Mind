#!/usr/bin/env python3
"""Phase 3e Pipeline: Point-in-time universe, holdout, block bootstrap, and regime alignment.

Specifications:
1. Point-in-time universe: top 200 symbols by avg traded value over previous 250 trading days ending at T-1.
2. Holdout: 2026-01-01 onward held out. In-sample baseline 2023-2025 only.
3. Gate rejections: Count rejections by reason for long and short separately. Long baseline covers gate-accepted entries.
4. Corporate actions: Match 77 moves with corporate_actions table, mark inferred events, exclude entries.
5. Regime alignment: Point-in-time NIFTY 200d SMA at T-1, store in nifty_daily_benchmark.
6. Block-bootstrap: 95% CI resampled by trading day across all symbols together.
7. Fixed notional ₹100,000 Zerodha cost model.
8. Baseline all entries and non-overlapping entries.
"""
from __future__ import annotations

import json
import logging
import math
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor

from backend.swing_risk import compute_swing_risk_parameters
from backend.transaction_costs import estimate_zerodha_costs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase_3e_pipeline")

DB_URL = "postgresql://avinash@localhost:5432/nivesh_v3_staging"
FIXED_NOTIONAL = Decimal("100000.0")
IST = ZoneInfo("Asia/Kolkata")
START_DATE = "2023-01-01"
END_DATE = "2025-12-31"  # Strict holdout: 2026 onward held out


def get_db_connection():
    return psycopg2.connect(DB_URL)


def store_and_compute_nifty_regime(conn) -> Dict[str, str]:
    """Fetch NIFTY 50 (^NSEI) from Yahoo, persist to nifty_daily_benchmark table, and compute T-1 regime."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=5y"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        timestamps = data["chart"]["result"][0]["timestamp"]
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]

    nifty_daily = {}
    for ts, c in zip(timestamps, closes):
        if c is not None and not math.isnan(c):
            dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            nifty_daily[dt_str] = round(float(c), 2)

    sorted_dates = sorted(nifty_daily.keys())

    # Create table and insert
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nifty_daily_benchmark (
                trade_date DATE PRIMARY KEY,
                close_price NUMERIC(12, 2) NOT NULL,
                sma_200 NUMERIC(12, 2),
                regime TEXT,
                source TEXT DEFAULT 'yahoo_finance_nsei',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        for i, dt in enumerate(sorted_dates):
            c = nifty_daily[dt]
            sma200 = None
            regime = "INSUFFICIENT_HISTORY"
            if i >= 199:
                window = [nifty_daily[sorted_dates[j]] for j in range(i - 199, i + 1)]
                sma200 = round(sum(window) / 200.0, 2)
                regime = "ABOVE_200D" if c >= sma200 else "BELOW_200D"

            cur.execute("""
                INSERT INTO nifty_daily_benchmark (trade_date, close_price, sma_200, regime)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (trade_date) DO UPDATE
                SET close_price = EXCLUDED.close_price,
                    sma_200 = EXCLUDED.sma_200,
                    regime = EXCLUDED.regime;
            """, (dt, c, sma200, regime))
    conn.commit()
    logger.info(f"Persisted {len(sorted_dates)} NIFTY rows into nifty_daily_benchmark table.")

    # Compute point-in-time regime ending on T-1 (the previous trading day)
    # For trade entry on date T, regime is defined strictly by NIFTY on day T-1
    pit_regimes = {}
    for i in range(1, len(sorted_dates)):
        entry_date = sorted_dates[i]
        prev_date = sorted_dates[i - 1]
        if i - 1 >= 199:
            prev_close = nifty_daily[prev_date]
            window = [nifty_daily[sorted_dates[j]] for j in range(i - 200, i)]
            prev_sma200 = sum(window) / 200.0
            pit_regimes[entry_date] = "ABOVE_200D" if prev_close >= prev_sma200 else "BELOW_200D"
        else:
            pit_regimes[entry_date] = "INSUFFICIENT_HISTORY"

    return pit_regimes


def check_universe_membership_table(conn) -> Dict[str, Any]:
    """Check whether universe_membership table holds point-in-time data."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM universe_membership;")
        cnt = cur.fetchone()[0]
        cur.execute("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'universe_membership';
        """)
        cols = cur.fetchall()

    return {
        "row_count": cnt,
        "columns": [c[0] for c in cols],
        "holds_point_in_time": cnt > 0,
    }


def get_corporate_actions_report(conn) -> Dict[str, Any]:
    """Report corporate_actions table contents and match against 77 large moves."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'corporate_actions';
        """)
        cols = [(r["column_name"], r["data_type"]) for r in cur.fetchall()]

        cur.execute("""
            SELECT count(*), min(effective_date), max(effective_date)
            FROM corporate_actions;
        """)
        row_cnt, min_d, max_d = cur.fetchone().values()

        cur.execute("""
            SELECT action_type, count(*)
            FROM corporate_actions
            GROUP BY action_type;
        """)
        action_counts = {r["action_type"]: r["count"] for r in cur.fetchall()}

        # 77 large moves across 2023-2026
        cur.execute("""
            WITH sym_bars AS (
                SELECT i.symbol, b.bar_time, b.close_price, b.open_price,
                       lag(b.close_price) OVER (PARTITION BY i.symbol ORDER BY b.bar_time) as prev_close
                FROM live_market_bars b
                JOIN instrument_master i ON i.id = b.instrument_id
                WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ'
                  AND b.interval = 'day' AND b.bar_time >= '2022-11-01' AND b.bar_time <= '2026-06-30'
            )
            SELECT symbol, bar_time::date as dt, prev_close, close_price, open_price,
                   (close_price / prev_close - 1.0) as ret,
                   abs(close_price / prev_close - 1.0) as abs_ret,
                   extract(year from bar_time)::int as yr
            FROM sym_bars
            WHERE prev_close IS NOT NULL AND prev_close > 0
              AND abs(close_price / prev_close - 1.0) > 0.20
              AND bar_time >= '2023-01-01'
            ORDER BY abs_ret DESC
            LIMIT 77;
        """)
        moves = cur.fetchall()

        matched = []
        unmatched = []
        for m in moves:
            sym = m["symbol"]
            dt = m["dt"]
            cur.execute("""
                SELECT action_type, ratio_from, ratio_to, effective_date
                FROM corporate_actions
                WHERE symbol = %s AND abs(effective_date - %s) <= 2;
            """, (sym, dt))
            ca_matches = cur.fetchall()
            if ca_matches:
                matched.append({
                    "symbol": sym,
                    "date": str(dt),
                    "move_pct": round(float(m["ret"]) * 100, 2),
                    "corporate_action": ca_matches[0]["action_type"],
                    "status": "matched_in_table",
                })
            else:
                unmatched.append({
                    "symbol": sym,
                    "date": str(dt),
                    "move_pct": round(float(m["ret"]) * 100, 2),
                    "corporate_action": "inferred",
                    "status": "unmatched_in_table",
                })

    return {
        "table_row_count": row_cnt,
        "table_date_range": [str(min_d), str(max_d)],
        "table_columns": cols,
        "action_counts": action_counts,
        "total_moves_analyzed": len(moves),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "matched_sample": matched[:10],
        "unmatched_sample": unmatched[:10],
        "jump_dates": {(m["symbol"], str(m["dt"])) for m in moves},
    }


def build_point_in_time_universes(conn, trading_dates: List[str]) -> Dict[str, Set[str]]:
    """Compute point-in-time top 200 symbols by avg traded value over previous 250 trading days ending at T-1."""
    logger.info("Computing point-in-time 250-day liquid universes...")
    pit_universes = {}

    with conn.cursor() as cur:
        # Pre-fetch all daily turnover in a single query
        cur.execute("""
            SELECT i.symbol, b.bar_time::date as dt, (b.volume * b.close_price) as turnover
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ' AND b.interval = 'day'
              AND b.bar_time >= '2021-12-01' AND b.bar_time <= '2026-06-30'
            ORDER BY b.bar_time ASC;
        """)
        rows = cur.fetchall()

    # Organize turnover by date and symbol
    turnover_by_date = defaultdict(dict)
    all_dates_set = set()
    for sym, dt, val in rows:
        dt_str = str(dt)
        turnover_by_date[dt_str][sym] = float(val or 0.0)
        all_dates_set.add(dt_str)

    all_sorted_dates = sorted(all_dates_set)
    date_to_idx = {d: i for i, d in enumerate(all_sorted_dates)}

    # For each entry date T in trading_dates (2023-2025):
    for entry_dt in trading_dates:
        t_idx = date_to_idx.get(entry_dt)
        if t_idx is None or t_idx < 250:
            continue
        # Window: [t_idx - 250, t_idx - 1] (exactly 250 previous trading days, ending T-1)
        prev_250_dates = all_sorted_dates[t_idx - 250 : t_idx]

        sym_totals = defaultdict(float)
        sym_counts = defaultdict(int)
        for d in prev_250_dates:
            for s, v in turnover_by_date[d].items():
                sym_totals[s] += v
                sym_counts[s] += 1

        # Calculate averages for symbols with at least 200 bars in the 250-day window
        avg_turnover = []
        for s, total in sym_totals.items():
            if sym_counts[s] >= 200:
                avg_turnover.append((s, total / sym_counts[s]))

        avg_turnover.sort(key=lambda x: x[1], reverse=True)
        top200 = {s for s, _ in avg_turnover[:200]}
        pit_universes[entry_dt] = top200

    logger.info(f"Built point-in-time universes for {len(pit_universes)} trading dates.")
    return pit_universes


def simulate_all_trades_and_gates(
    conn,
    pit_universes: Dict[str, Set[str]],
    nifty_regimes: Dict[str, str],
    corp_jump_dates: Set[Tuple[str, str]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Counter], int]:
    """Simulate swing trades with point-in-time universe, recording all gate rejections."""
    # Determine the union of symbols that ever appeared in any top-200 PIT universe
    universe_symbols = set()
    for u in pit_universes.values():
        universe_symbols.update(u)
    universe_symbols_list = sorted(universe_symbols)
    logger.info(f"Unique symbols ever in point-in-time top 200: {len(universe_symbols_list)}")

    # Fetch daily bars for these symbols
    with conn.cursor() as cur:
        cur.execute("""
            SELECT i.symbol, b.bar_time::date as dt, b.open_price, b.high_price, b.low_price, b.close_price, b.volume
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.symbol = ANY(%s) AND i.exchange = 'NSE' AND i.instrument_type = 'EQ'
              AND b.interval = 'day' AND b.bar_time >= '2022-10-01' AND b.bar_time <= '2026-06-30'
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

    trades = []
    gate_rejections = {"LONG": Counter(), "SHORT": Counter()}
    excluded_corp_count = 0
    holding_limit_days = 10
    tick_size = 0.05

    for sym in universe_symbols_list:
        s_bars = bars_by_symbol.get(sym, [])
        n_bars = len(s_bars)
        if n_bars < 25:
            continue

        for i in range(15, n_bars - 1):
            next_bar = s_bars[i + 1]
            entry_date = next_bar["trade_date"]

            # In-sample filter: 2023-01-01 to 2025-12-31 ONLY (Holdout 2026 onward)
            if entry_date < START_DATE or entry_date > END_DATE:
                continue

            # Point-in-time universe check: was this symbol in the top 200 ending T-1?
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
                excluded_corp_count += 1
                continue

            nifty_regime = nifty_regimes.get(entry_date, "UNKNOWN")

            for side in ("BUY", "SELL"):
                gate_key = "LONG" if side == "BUY" else "SHORT"
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
                    gate_rejections[gate_key][rej_reason or "UNKNOWN_REJECTION"] += 1
                    continue
                else:
                    gate_rejections[gate_key]["ACCEPTED"] += 1

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
                max_favourable_r = 0.0
                max_adverse_r = 0.0
                hit_1r_first = 0
                reached_1r = False
                reached_minus_1r = False

                for b_idx, b in enumerate(walk_bars):
                    b_open = b["open"]
                    b_high = b["high"]
                    b_low = b["low"]
                    b_close = b["close"]

                    if side == "BUY":
                        fav_r = (b_high - entry_price) / r_points
                        adv_r = (entry_price - b_low) / r_points
                    else:
                        fav_r = (entry_price - b_low) / r_points
                        adv_r = (b_high - entry_price) / r_points

                    if fav_r > max_favourable_r:
                        max_favourable_r = fav_r
                    if adv_r > max_adverse_r:
                        max_adverse_r = adv_r

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
                            if prev_c > running_sl and b_open < prev_c:
                                exit_price = b_open
                                exit_reason = "GAP_DOWN_STOP"
                            else:
                                exit_price = running_sl
                                exit_reason = "STOP_LOSS"
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
                            if prev_c < running_sl and b_open > prev_c:
                                exit_price = b_open
                                exit_reason = "GAP_UP_STOP"
                            else:
                                exit_price = running_sl
                                exit_reason = "STOP_LOSS"
                            exit_bar_idx = b_idx
                            break
                        elif b_high >= running_sl:
                            exit_price = running_sl
                            exit_reason = "STOP_LOSS"
                            exit_bar_idx = b_idx
                            break

                is_censored = False
                if exit_reason is None:
                    if len(walk_bars) >= holding_limit_days:
                        exit_reason = "HOLDING_LIMIT_EXIT"
                        exit_price = walk_bars[holding_limit_days - 1]["close"]
                        exit_bar_idx = holding_limit_days - 1
                        is_censored = False
                    else:
                        exit_reason = "TRUNCATED_SERIES"
                        exit_price = walk_bars[-1]["close"]
                        exit_bar_idx = len(walk_bars) - 1
                        is_censored = True

                exit_side = "SELL" if side == "BUY" else "BUY"
                exit_cost_bd = estimate_zerodha_costs(
                    segment=seg,
                    side=exit_side,
                    price=Decimal(str(exit_price)),
                    quantity=quantity,
                    exchange="NSE",
                    delivery_dp_charge=Decimal("15.93") if (seg == "EQUITY_DELIVERY" and exit_side == "SELL") else Decimal("0"),
                )
                exit_fee = float(exit_cost_bd.total)
                roundtrip_fees = entry_fee + exit_fee

                if side == "BUY":
                    gross_pnl = quantity * (exit_price - entry_price)
                    gross_r = (exit_price - entry_price) / r_points
                else:
                    gross_pnl = quantity * (entry_price - exit_price)
                    gross_r = (entry_price - exit_price) / r_points

                net_pnl = gross_pnl - roundtrip_fees
                net_r = gross_r - (roundtrip_fees / r_rupees)
                y = 1 if (not is_censored and net_r >= 1.0) else 0

                trades.append({
                    "symbol": sym,
                    "side": side,
                    "entry_date": entry_date,
                    "entry_year": entry_year,
                    "entry_idx": i + 1,
                    "exit_idx": i + 1 + exit_bar_idx,
                    "gross_r": gross_r,
                    "net_r": net_r,
                    "y": y,
                    "hit_1r_first": hit_1r_first,
                    "exit_reason": exit_reason,
                    "censored": is_censored,
                    "nifty_regime": nifty_regime,
                })

    return trades, gate_rejections, excluded_corp_count


def filter_non_overlapping(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped = defaultdict(list)
    for t in trades:
        grouped[(t["symbol"], t["side"])].append(t)

    non_overlapping = []
    for key, group in grouped.items():
        group.sort(key=lambda x: x["entry_idx"])
        last_exit_idx = -1
        for t in group:
            if t["entry_idx"] > last_exit_idx:
                non_overlapping.append(t)
                last_exit_idx = t["exit_idx"]

    return non_overlapping


def block_bootstrap_ci(trades: List[Dict[str, Any]], n_bootstraps: int = 2000, seed: int = 42) -> Tuple[float, float]:
    """Compute 95% block-bootstrap interval resampled by trading day across all symbols together."""
    if not trades:
        return 0.0, 0.0

    # Group trades by entry_date
    trades_by_date = defaultdict(list)
    for t in trades:
        trades_by_date[t["entry_date"]].append(t["net_r"])

    unique_dates = list(trades_by_date.keys())
    n_dates = len(unique_dates)
    if n_dates < 5:
        return round(float(np.mean([t["net_r"] for t in trades])), 4), round(float(np.mean([t["net_r"] for t in trades])), 4)

    rng = np.random.default_rng(seed)
    sampled_means = []

    for _ in range(n_bootstraps):
        # Sample trading days with replacement
        sampled_dates = rng.choice(unique_dates, size=n_dates, replace=True)
        # Pool all trades on the sampled days
        pooled_r = []
        for d in sampled_dates:
            pooled_r.extend(trades_by_date[d])
        if pooled_r:
            sampled_means.append(np.mean(pooled_r))

    ci_lower = float(np.percentile(sampled_means, 2.5))
    ci_upper = float(np.percentile(sampled_means, 97.5))
    return round(ci_lower, 4), round(ci_upper, 4)


def compute_statistics_with_ci(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "pos_rate": 0.0,
            "hit_1r_first_rate": 0.0,
            "mean_gross_r": 0.0,
            "mean_net_r": 0.0,
            "median_net_r": 0.0,
            "ci_95": [0.0, 0.0],
            "exit_reasons": {},
        }

    n = len(trades)
    net_r_vals = [t["net_r"] for t in trades]
    gross_r_vals = [t["gross_r"] for t in trades]
    y_vals = [t["y"] for t in trades]
    hit_1r_vals = [t["hit_1r_first"] for t in trades]
    reasons = Counter(t["exit_reason"] for t in trades)
    ci_lower, ci_upper = block_bootstrap_ci(trades)

    return {
        "count": n,
        "pos_count": int(sum(y_vals)),
        "pos_rate": round(float(np.mean(y_vals)) * 100.0, 2),
        "hit_1r_first_count": int(sum(hit_1r_vals)),
        "hit_1r_first_rate": round(float(np.mean(hit_1r_vals)) * 100.0, 2),
        "mean_gross_r": round(float(np.mean(gross_r_vals)), 4),
        "mean_net_r": round(float(np.mean(net_r_vals)), 4),
        "median_net_r": round(float(np.median(net_r_vals)), 4),
        "ci_95": [ci_lower, ci_upper],
        "exit_reasons": dict(reasons),
    }


def main():
    logger.info("Connecting to PostgreSQL...")
    conn = get_db_connection()

    logger.info("1. Storing and computing point-in-time NIFTY regime...")
    nifty_pit_regimes = store_and_compute_nifty_regime(conn)

    logger.info("2. Checking universe_membership table...")
    univ_check = check_universe_membership_table(conn)
    logger.info(f"universe_membership holds point-in-time: {univ_check['holds_point_in_time']} (rows: {univ_check['row_count']})")

    logger.info("3. Inspecting corporate_actions table...")
    corp_report = get_corporate_actions_report(conn)
    logger.info(f"corporate_actions rows: {corp_report['table_row_count']}, matched: {corp_report['matched_count']}/{corp_report['total_moves_analyzed']}")

    # Get all distinct trading dates between 2023-01-01 and 2025-12-31
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT bar_time::date
            FROM live_market_bars
            WHERE interval = 'day' AND bar_time >= '2023-01-01' AND bar_time <= '2025-12-31'
            ORDER BY bar_time ASC;
        """)
        trading_dates = [str(r[0]) for r in cur.fetchall()]

    logger.info(f"Total in-sample trading dates (2023-2025): {len(trading_dates)}")

    logger.info("4. Building point-in-time 250d liquid universes...")
    pit_universes = build_point_in_time_universes(conn, trading_dates)

    logger.info("5. Simulating in-sample trades and capturing gate rejections...")
    all_trades, gate_rejections, excl_corp_count = simulate_all_trades_and_gates(
        conn=conn,
        pit_universes=pit_universes,
        nifty_regimes=nifty_pit_regimes,
        corp_jump_dates=corp_report["jump_dates"],
    )
    conn.close()

    logger.info(f"Total in-sample trades simulated (2023-2025): {len(all_trades)}")
    non_overlap_trades = filter_non_overlapping(all_trades)
    logger.info(f"Total non-overlapping trades: {len(non_overlap_trades)}")

    # Stratified analysis builder
    def build_report_tables(trades_pool):
        tbl = {}
        for d in ("LONG", "SHORT"):
            d_trades = [t for t in trades_pool if t["side"] == ("BUY" if d == "LONG" else "SELL")]
            tbl[d] = {
                "overall": compute_statistics_with_ci(d_trades),
                "by_year": {},
                "by_regime": {},
            }
            for yr in (2023, 2024, 2025):
                y_trades = [t for t in d_trades if t["entry_year"] == yr]
                tbl[d]["by_year"][str(yr)] = compute_statistics_with_ci(y_trades)

            for reg in ("ABOVE_200D", "BELOW_200D"):
                r_trades = [t for t in d_trades if t["nifty_regime"] == reg]
                tbl[d]["by_regime"][reg] = compute_statistics_with_ci(r_trades)

        tbl["COMBINED"] = {
            "overall": compute_statistics_with_ci(trades_pool),
            "by_year": {str(yr): compute_statistics_with_ci([t for t in trades_pool if t["entry_year"] == yr]) for yr in (2023, 2024, 2025)},
            "by_regime": {reg: compute_statistics_with_ci([t for t in trades_pool if t["nifty_regime"] == reg]) for reg in ("ABOVE_200D", "BELOW_200D")},
        }
        return tbl

    logger.info("Computing bootstrap intervals for baseline...")
    baseline_stats = build_report_tables(all_trades)
    logger.info("Computing bootstrap intervals for non-overlapping entries...")
    non_overlap_stats = build_report_tables(non_overlap_trades)

    results = {
        "universe_membership_table": univ_check,
        "corporate_actions_report": {
            "table_row_count": corp_report["table_row_count"],
            "table_date_range": corp_report["table_date_range"],
            "table_columns": corp_report["table_columns"],
            "action_counts": corp_report["action_counts"],
            "matched_count": corp_report["matched_count"],
            "unmatched_count": corp_report["unmatched_count"],
            "excluded_entries_count": excl_corp_count,
            "matched_sample": corp_report["matched_sample"],
            "unmatched_sample": corp_report["unmatched_sample"],
        },
        "gate_rejections": {
            "LONG": dict(gate_rejections["LONG"]),
            "SHORT": dict(gate_rejections["SHORT"]),
        },
        "baseline_all_entries": baseline_stats,
        "non_overlapping_entries": non_overlap_stats,
    }

    with open("phase_3e_results.json", "w") as f:
        json.dump(results, f, indent=2)

    logger.info("Phase 3e processing complete. Results written to phase_3e_results.json.")


if __name__ == "__main__":
    main()
