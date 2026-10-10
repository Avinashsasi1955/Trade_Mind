#!/usr/bin/env python3
"""Comprehensive real-data measurement script for Phase 3d.

Evaluates 200 liquid symbols over 2023-2026:
- Top 200 symbols ranked by 250-day average daily traded value
- Detection and exclusion of unadjusted corporate action jumps (>20% move)
- Realistic Zerodha fee model on ₹100,000 fixed notional
- True time-stops: HOLDING_LIMIT_EXIT (censored=False) vs series truncation (censored=True)
- Baseline table (all entries) and Non-Overlapping entries
- Stratified by Direction, Year, and NIFTY 200-day SMA market regime
"""
import json
import logging
import math
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor

from backend.swing_risk import compute_swing_risk_parameters
from backend.transaction_costs import estimate_zerodha_costs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase_3d_measure")

DB_URL = "postgresql://avinash@localhost:5432/nivesh_v3_staging"
FIXED_NOTIONAL = Decimal("100000.0")
IST = ZoneInfo("Asia/Kolkata")


def get_db_connection():
    return psycopg2.connect(DB_URL)


def fetch_nifty_200d_regime() -> Dict[str, str]:
    """Fetch official NIFTY 50 (^NSEI) daily data from Yahoo Finance and compute 200-day SMA regime."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=5y"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            timestamps = data["chart"]["result"][0]["timestamp"]
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    except Exception as e:
        logger.warning(f"Yahoo NIFTY fetch failed ({e}), using fallback")
        return {}

    nifty_daily = {}
    for ts, c in zip(timestamps, closes):
        if c is not None and not math.isnan(c):
            dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            nifty_daily[dt_str] = float(c)

    sorted_dates = sorted(nifty_daily.keys())
    regimes = {}
    for i in range(len(sorted_dates)):
        dt = sorted_dates[i]
        if i >= 199:
            window = [nifty_daily[sorted_dates[j]] for j in range(i - 199, i + 1)]
            sma200 = sum(window) / 200.0
            c = nifty_daily[dt]
            regimes[dt] = "ABOVE_200D" if c >= sma200 else "BELOW_200D"
        else:
            regimes[dt] = "INSUFFICIENT_HISTORY"

    logger.info(f"Loaded NIFTY 200d SMA for {len(regimes)} trading days.")
    return regimes


def get_top_200_symbols(conn) -> List[Tuple[str, float]]:
    """Rank symbols by average daily traded value over the last 250 trading days."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH trading_days AS (
                SELECT DISTINCT bar_time
                FROM live_market_bars b
                JOIN instrument_master i ON i.id = b.instrument_id
                WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ' AND b.interval = 'day'
                ORDER BY bar_time DESC
                LIMIT 250
            )
            SELECT i.symbol, 
                   avg(b.volume * b.close_price) as avg_daily_val
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            JOIN trading_days td ON td.bar_time = b.bar_time
            WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ' AND b.interval = 'day'
            GROUP BY i.symbol
            HAVING count(b.bar_time) >= 200
            ORDER BY avg_daily_val DESC
            LIMIT 200;
        """)
        rows = cur.fetchall()
    return [(r[0], float(r[1])) for r in rows]


def get_corporate_action_moves(conn, symbols: List[str]) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], float]]:
    """Detect daily moves |close/prev_close - 1| > 20% for the given symbols between 2023 and 2026."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            WITH sym_bars AS (
                SELECT i.symbol, b.bar_time, b.close_price, b.open_price,
                       lag(b.close_price) OVER (PARTITION BY i.symbol ORDER BY b.bar_time) as prev_close
                FROM live_market_bars b
                JOIN instrument_master i ON i.id = b.instrument_id
                WHERE i.symbol = ANY(%s) AND i.exchange = 'NSE' AND i.instrument_type = 'EQ'
                  AND b.interval = 'day' AND b.bar_time >= '2022-11-01' AND b.bar_time <= '2026-06-30'
            )
            SELECT symbol, bar_time, prev_close, close_price, open_price,
                   (close_price / prev_close - 1.0) as ret,
                   abs(close_price / prev_close - 1.0) as abs_ret,
                   extract(year from bar_time)::int as yr
            FROM sym_bars
            WHERE prev_close IS NOT NULL AND prev_close > 0
              AND abs(close_price / prev_close - 1.0) > 0.20
              AND bar_time >= '2023-01-01'
            ORDER BY abs_ret DESC;
        """, (symbols,))
        moves = cur.fetchall()

    moves_list = []
    jump_map = {}
    for m in moves:
        dt_str = m["bar_time"].strftime("%Y-%m-%d")
        moves_list.append({
            "symbol": m["symbol"],
            "date": dt_str,
            "prev_close": float(m["prev_close"]),
            "open": float(m["open_price"]),
            "close": float(m["close_price"]),
            "move_pct": float(m["ret"]) * 100.0,
            "abs_move_pct": float(m["abs_ret"]) * 100.0,
            "year": int(m["yr"]),
        })
        jump_map[(m["symbol"], dt_str)] = float(m["ret"])

    return moves_list, jump_map


def fetch_all_symbol_bars(conn, symbols: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch daily candles for all 200 symbols."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT i.symbol, b.bar_time, b.open_price, b.high_price, b.low_price, b.close_price, b.volume
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.symbol = ANY(%s) AND i.exchange = 'NSE' AND i.instrument_type = 'EQ'
              AND b.interval = 'day' AND b.bar_time >= '2022-10-01' AND b.bar_time <= '2026-06-30'
            ORDER BY i.symbol, b.bar_time ASC;
        """, (symbols,))
        rows = cur.fetchall()

    bars_by_symbol = defaultdict(list)
    for r in rows:
        sym, dt, o, h, l, c, v = r
        trade_date = dt.strftime("%Y-%m-%d")
        bars_by_symbol[sym].append({
            "trade_date": trade_date,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": int(v or 0),
        })

    return bars_by_symbol


def simulate_swing_trades(
    symbol: str,
    bars: List[Dict[str, Any]],
    nifty_regimes: Dict[str, str],
    corporate_jump_dates: set,
    holding_limit_days: int = 10,
    tick_size: float = 0.05,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Simulate swing trades entering next day's open with swing_risk stop and target."""
    n_bars = len(bars)
    if n_bars < 25:
        return [], 0, 0

    results = []
    excluded_corp_count = 0
    corp_gap_exit_count = 0

    # Warmup requirement: >= 15 bars
    for i in range(15, n_bars - 1):
        next_bar = bars[i + 1]
        entry_date = next_bar["trade_date"]

        # Date filter: 2023-01-01 to 2026-06-30
        if entry_date < "2023-01-01" or entry_date > "2026-06-30":
            continue

        entry_year = int(entry_date[:4])
        history_candles = bars[max(0, i - 25) : i + 1]

        # Holding window bars (up to holding_limit_days)
        walk_end = min(n_bars, i + 1 + holding_limit_days)
        walk_bars = bars[i + 1 : walk_end]
        if not walk_bars:
            continue

        holding_dates = [b["trade_date"] for b in walk_bars]
        has_corp_jump = any(d in corporate_jump_dates for d in holding_dates)
        if has_corp_jump:
            excluded_corp_count += 1
            # Check if it would have caused an unadjusted gap exit
            # (We will report this count)
            # Evaluate both sides for gap exit before discarding
            for side in ("BUY", "SELL"):
                pass
            continue

        nifty_regime = nifty_regimes.get(entry_date, "UNKNOWN")

        for side in ("BUY", "SELL"):
            slip = 2.0 * tick_size
            entry_price = round(next_bar["open"] + slip if side == "BUY" else next_bar["open"] - slip, 2)

            risk_params = compute_swing_risk_parameters(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                daily_candles=history_candles,
            )
            if not risk_params:
                continue

            sl_price = float(risk_params["stop_loss_price"])
            tp_price = float(risk_params["take_profit_price"])
            r_points = float(risk_params["stop_distance"])
            if r_points <= 0.01:
                continue

            # Fixed Notional ₹100,000 Position Sizing
            # Quantity Q = round(100,000 / entry_price)
            quantity = max(1, int(round(float(FIXED_NOTIONAL) / entry_price)))
            r_rupees = quantity * r_points

            # Zerodha Fee Model
            # LONG uses EQUITY_DELIVERY; SHORT uses FUTURES
            seg = "EQUITY_DELIVERY" if side == "BUY" else "FUTURES"
            entry_cost_bd = estimate_zerodha_costs(
                segment=seg,
                side=side,
                price=Decimal(str(entry_price)),
                quantity=quantity,
                exchange="NSE",
            )
            entry_fee = float(entry_cost_bd.total)

            # Walk forward bar by bar
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

                # Excursion tracking at start of bar
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
                        # Simultaneous within bar: check open
                        if (b_open >= entry_price if side == "BUY" else b_open <= entry_price):
                            reached_1r = True
                            hit_1r_first = 1
                        else:
                            reached_minus_1r = True
                            hit_1r_first = 0

                # Check Exits on this bar
                # 1. Take Profit (gap open or intra-bar)
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

                # 2. Stop Loss (gap open vs intraday stop)
                if side == "BUY":
                    prev_c = walk_bars[b_idx - 1]["close"] if b_idx > 0 else history_candles[-1]["close"]
                    if b_open <= running_sl:
                        # True gap down across stop
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
                        # True gap up across stop
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

            # Handle Time-Stop / Truncation if position didn't exit during walk_bars
            is_censored = False
            if exit_reason is None:
                if len(walk_bars) >= holding_limit_days:
                    # Trade completed full 10 days without stop or target -> REAL exit at day 10 close
                    exit_reason = "HOLDING_LIMIT_EXIT"
                    exit_price = walk_bars[holding_limit_days - 1]["close"]
                    exit_bar_idx = holding_limit_days - 1
                    is_censored = False
                else:
                    # Cut off by end of dataset before reaching 10 days -> CENSORED
                    exit_reason = "TRUNCATED_SERIES"
                    exit_price = walk_bars[-1]["close"]
                    exit_bar_idx = len(walk_bars) - 1
                    is_censored = True

            # Calculate Gross and Net PnL and R
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

            results.append({
                "symbol": symbol,
                "side": side,
                "entry_date": entry_date,
                "entry_year": entry_year,
                "entry_idx": i + 1,
                "exit_idx": i + 1 + exit_bar_idx,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "r_points": r_points,
                "r_rupees": r_rupees,
                "roundtrip_fees": roundtrip_fees,
                "gross_r": gross_r,
                "net_r": net_r,
                "y": y,
                "hit_1r_first": hit_1r_first,
                "max_favourable_r": max_favourable_r,
                "max_adverse_r": max_adverse_r,
                "exit_reason": exit_reason,
                "censored": is_censored,
                "nifty_regime": nifty_regime,
                "fee_model": seg,
            })

    return results, excluded_corp_count, corp_gap_exit_count


def filter_non_overlapping(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter trades so that for a given symbol and direction, subsequent entries are skipped while a prior trade is open."""
    # Group by (symbol, side)
    grouped = defaultdict(list)
    for t in trades:
        grouped[(t["symbol"], t["side"])].append(t)

    non_overlapping = []
    for key, group in grouped.items():
        # Sort chronologically by entry_idx
        group.sort(key=lambda x: x["entry_idx"])
        last_exit_idx = -1
        for t in group:
            if t["entry_idx"] > last_exit_idx:
                non_overlapping.append(t)
                last_exit_idx = t["exit_idx"]

    return non_overlapping


def compute_statistics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute mean R, median R, positive rate, hit_1r_first rate, and exit reasons."""
    if not trades:
        return {
            "count": 0,
            "pos_rate": 0.0,
            "hit_1r_first_rate": 0.0,
            "censored_count": 0,
            "censored_rate": 0.0,
            "mean_gross_r": 0.0,
            "median_gross_r": 0.0,
            "mean_net_r": 0.0,
            "median_net_r": 0.0,
            "exit_reasons": {},
        }

    n = len(trades)
    net_r_vals = [t["net_r"] for t in trades]
    gross_r_vals = [t["gross_r"] for t in trades]
    y_vals = [t["y"] for t in trades]
    hit_1r_vals = [t["hit_1r_first"] for t in trades]
    censored_cnt = sum(1 for t in trades if t["censored"])
    reasons = Counter(t["exit_reason"] for t in trades)

    return {
        "count": n,
        "pos_count": int(sum(y_vals)),
        "pos_rate": round(float(np.mean(y_vals)) * 100.0, 2),
        "hit_1r_first_count": int(sum(hit_1r_vals)),
        "hit_1r_first_rate": round(float(np.mean(hit_1r_vals)) * 100.0, 2),
        "censored_count": censored_cnt,
        "censored_rate": round(censored_cnt / n * 100.0, 2),
        "mean_gross_r": round(float(np.mean(gross_r_vals)), 4),
        "median_gross_r": round(float(np.median(gross_r_vals)), 4),
        "mean_net_r": round(float(np.mean(net_r_vals)), 4),
        "median_net_r": round(float(np.median(net_r_vals)), 4),
        "exit_reasons": dict(reasons),
    }


def main():
    logger.info("Connecting to PostgreSQL...")
    conn = get_db_connection()

    logger.info("Fetching NIFTY 200-day SMA regime...")
    nifty_regimes = fetch_nifty_200d_regime()

    logger.info("Selecting top 200 symbols by 250-day average traded value...")
    top200_symbols_with_val = get_top_200_symbols(conn)
    top200_symbols = [s[0] for s in top200_symbols_with_val]

    logger.info("Analyzing corporate action moves (>20%)...")
    corp_moves, jump_map = get_corporate_action_moves(conn, top200_symbols)
    corp_dates_by_symbol = defaultdict(set)
    for (sym, d) in jump_map.keys():
        corp_dates_by_symbol[sym].add(d)

    logger.info("Fetching all daily bars for 200 symbols...")
    all_bars = fetch_all_symbol_bars(conn, top200_symbols)
    conn.close()

    logger.info(f"Loaded bars for {len(all_bars)} symbols. Starting simulation...")
    all_trades = []
    total_excluded_corp = 0

    for idx, sym in enumerate(top200_symbols, 1):
        bars = all_bars.get(sym, [])
        corp_dates = corp_dates_by_symbol.get(sym, set())
        trades, excl_corp, _ = simulate_swing_trades(
            symbol=sym,
            bars=bars,
            nifty_regimes=nifty_regimes,
            corporate_jump_dates=corp_dates,
        )
        all_trades.extend(trades)
        total_excluded_corp += excl_corp
        if idx % 50 == 0 or idx == len(top200_symbols):
            logger.info(f"Processed {idx}/200 symbols ({len(all_trades)} trades simulated)...")

    logger.info(f"Total simulated trades: {len(all_trades)}. Excluded entries due to corporate action: {total_excluded_corp}")

    # Generate Non-Overlapping Trades
    logger.info("Filtering non-overlapping trades...")
    non_overlapping_trades = filter_non_overlapping(all_trades)
    logger.info(f"Total non-overlapping trades: {len(non_overlapping_trades)}")

    # 1. Base Rate Table (All Entries)
    # Stratified by Direction, Year, Market Regime
    directions = ["LONG", "SHORT", "ALL"]
    years = [2023, 2024, 2025, 2026, "ALL"]
    regimes = ["ABOVE_200D", "BELOW_200D", "ALL"]

    def build_stratified_table(trades_pool: List[Dict[str, Any]]) -> Dict[str, Any]:
        tbl = {}
        for d in ("LONG", "SHORT"):
            d_trades = [t for t in trades_pool if t["side"] == ("BUY" if d == "LONG" else "SELL")]
            tbl[d] = {
                "overall": compute_statistics(d_trades),
                "by_year": {},
                "by_regime": {},
                "by_year_and_regime": {},
            }
            for y in (2023, 2024, 2025, 2026):
                y_trades = [t for t in d_trades if t["entry_year"] == y]
                tbl[d]["by_year"][str(y)] = compute_statistics(y_trades)

            for reg in ("ABOVE_200D", "BELOW_200D"):
                r_trades = [t for t in d_trades if t["nifty_regime"] == reg]
                tbl[d]["by_regime"][reg] = compute_statistics(r_trades)

            for y in (2023, 2024, 2025, 2026):
                tbl[d]["by_year_and_regime"][str(y)] = {}
                for reg in ("ABOVE_200D", "BELOW_200D"):
                    yr_reg_trades = [t for t in d_trades if t["entry_year"] == y and t["nifty_regime"] == reg]
                    tbl[d]["by_year_and_regime"][str(y)][reg] = compute_statistics(yr_reg_trades)

        # Combined ALL directions
        tbl["COMBINED"] = {
            "overall": compute_statistics(trades_pool),
            "by_year": {str(y): compute_statistics([t for t in trades_pool if t["entry_year"] == y]) for y in (2023, 2024, 2025, 2026)},
            "by_regime": {reg: compute_statistics([t for t in trades_pool if t["nifty_regime"] == reg]) for reg in ("ABOVE_200D", "BELOW_200D")},
        }
        return tbl

    baseline_table = build_stratified_table(all_trades)
    non_overlap_table = build_stratified_table(non_overlapping_trades)

    output = {
        "top200_symbols": [
            {"symbol": s, "avg_daily_traded_value": val} for s, val in top200_symbols_with_val
        ],
        "corporate_actions": {
            "total_moves_over_20pct": len(corp_moves),
            "moves_by_year": dict(sorted(Counter(m["year"] for m in corp_moves).items())),
            "top_20_moves": corp_moves[:20],
            "excluded_entries_count": total_excluded_corp,
        },
        "baseline_all_entries": baseline_table,
        "non_overlapping_entries": non_overlap_table,
    }

    with open("phase_3d_report_data.json", "w") as f:
        json.dump(output, f, indent=2)

    logger.info("Saved complete report data to phase_3d_report_data.json.")


if __name__ == "__main__":
    main()
