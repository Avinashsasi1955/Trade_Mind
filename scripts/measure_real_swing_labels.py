#!/usr/bin/env python3
"""Measure and report swing labels from real daily bars in PostgreSQL.

Specifications:
- 200 symbols over the last 3 years (2023-2026).
- Entry is next day's open (+ slippage).
- Swing stop and target from swing_risk.py (ATR(14) and 20-day structure).
- <= 10-day holding limit.
- Report per direction:
  * label count
  * positive rate (y = 1)
  * hit_1r_first rate
  * censored share
  * mean and median r_net
  * exit-reason counts
  * counts by year
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import create_engine, text

from backend.position_manager import replay_trade_walk_forward
from backend.swing_risk import compute_swing_risk_parameters
from backend.transaction_costs import estimate_zerodha_costs

IST = ZoneInfo("Asia/Kolkata")
DB_URL = "postgresql://avinash@localhost:5432/nivesh_v3_staging"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("measure_swing_labels")


def get_top_200_symbols(engine: Any) -> List[str]:
    """Fetch top 200 NSE EQ symbols by liquidity with continuous daily data since 2023."""
    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT i.symbol
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.exchange = 'NSE' AND i.instrument_type = 'EQ'
              AND b.interval = 'day' AND b.bar_time >= '2023-01-01'
            GROUP BY i.symbol
            HAVING count(*) >= 700
            ORDER BY avg(b.volume * b.close_price) DESC
            LIMIT 200;
        """)).scalars().all()
    return list(res)


def fetch_symbol_daily_bars(engine: Any, symbol: str) -> List[Dict[str, Any]]:
    """Fetch daily candles from 2022-11-01 to 2026-06-30 for symbol on NSE."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT b.bar_time, b.open_price, b.high_price, b.low_price, b.close_price, b.volume
            FROM live_market_bars b
            JOIN instrument_master i ON i.id = b.instrument_id
            WHERE i.symbol = :symbol AND i.exchange = 'NSE' AND i.instrument_type = 'EQ'
              AND b.interval = 'day' AND b.bar_time >= '2022-11-01' AND b.bar_time <= '2026-06-30'
            ORDER BY b.bar_time ASC;
        """), {"symbol": symbol}).all()

    bars = []
    for r in rows:
        dt = r[0]
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=IST)
        bars.append({
            "dt": dt,
            "bar_time": dt.isoformat(),
            "timestamp": dt.isoformat(),
            "trade_date": dt.strftime("%Y-%m-%d"),
            "open": float(r[1]),
            "open_price": float(r[1]),
            "high": float(r[2]),
            "high_price": float(r[2]),
            "low": float(r[3]),
            "low_price": float(r[3]),
            "close": float(r[4]),
            "close_price": float(r[4]),
            "volume": int(r[5] or 0),
            "interval": "day",
        })
    return bars


def simulate_swing_trades_for_symbol(
    symbol: str,
    bars: List[Dict[str, Any]],
    holding_limit_days: int = 10,
    tick_size: float = 0.05,
) -> List[Dict[str, Any]]:
    """Simulate swing trades entering at next day's open with swing_risk stop and target."""
    n_bars = len(bars)
    if n_bars < 25:
        return []

    results = []

    # Iterate through days. Need at least 15 warmup bars for swing_risk
    for i in range(15, n_bars - 1):
        signal_bar = bars[i]
        next_bar = bars[i + 1]

        # Filter strictly to entry dates in 2023-2026
        entry_date = next_bar["trade_date"]
        if entry_date < "2023-01-01" or entry_date > "2026-06-30":
            continue

        entry_year = int(entry_date[:4])
        history_candles = bars[max(0, i - 25) : i + 1]

        # 10-day lookahead window starting at next_bar
        walk_end = min(n_bars, i + 1 + holding_limit_days)
        walk_bars = bars[i + 1 : walk_end]
        if not walk_bars:
            continue

        # Evaluate both directions
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
            stop_dist = float(risk_params["stop_distance"])

            # Transaction costs
            costs = estimate_zerodha_costs(
                segment="EQUITY_DELIVERY",
                side=side,
                price=Decimal(str(entry_price)),
                quantity=1,
                exchange="NSE",
            )
            fee_amount = float(costs.total)

            trade = {
                "entry": entry_price,
                "theoretical_fill_price": entry_price,
                "stop_loss_price": sl_price,
                "initial_sl": sl_price,
                "take_profit_price": tp_price,
                "target_price": tp_price,
                "side": side,
                "quantity": 1,
                "estimated_fees": fee_amount,
                "signal_at": next_bar["timestamp"],
                "trade_mode": "SWING",
            }

            sim_res = replay_trade_walk_forward(
                trade=trade,
                bars=walk_bars,
                harvest_enabled=False,
                tick_size=tick_size,
            )

            exit_reason = sim_res.get("exit_reason", "UNKNOWN")
            realized_r = float(sim_res.get("realized_r", 0.0))
            hit_1r_first = int(sim_res.get("hit_1r_first", 0))

            # Censored: reached 10-day holding limit without hitting stop or target
            is_censored = False
            if exit_reason in ("FELL_THROUGH_TO_RECORDED", "NO_BARS", "UNKNOWN"):
                exit_reason = "HOLDING_LIMIT_EXIT"
                is_censored = True

            y = 1 if (not is_censored and realized_r >= 1.0) else 0

            results.append({
                "symbol": symbol,
                "side": side,
                "entry_date": entry_date,
                "entry_year": entry_year,
                "entry_price": entry_price,
                "exit_price": sim_res.get("exit_price"),
                "exit_reason": exit_reason,
                "realized_r": realized_r,
                "hit_1r_first": hit_1r_first,
                "max_favourable_r": sim_res.get("max_favourable_r", 0.0),
                "max_adverse_r": sim_res.get("max_adverse_r", 0.0),
                "censored": is_censored,
                "y": y,
            })

    return results


def main():
    engine = create_engine(DB_URL)
    symbols = get_top_200_symbols(engine)
    logger.info(f"Loaded {len(symbols)} liquid symbols for swing label analysis.")

    all_records = []
    for idx, sym in enumerate(symbols, 1):
        bars = fetch_symbol_daily_bars(engine, sym)
        if len(bars) < 20:
            continue
        recs = simulate_swing_trades_for_symbol(sym, bars)
        all_records.extend(recs)
        if idx % 25 == 0 or idx == len(symbols):
            logger.info(f"Processed {idx}/{len(symbols)} symbols. Accumulated {len(all_records)} labels...")

    logger.info(f"Total swing labels simulated: {len(all_records)}")

    # Aggregate by direction
    for direction in ("BUY", "SELL"):
        dir_recs = [r for r in all_records if r["side"] == direction]
        n_total = len(dir_recs)
        if n_total == 0:
            continue

        n_pos = sum(1 for r in dir_recs if r["y"] == 1)
        pos_rate = (n_pos / n_total) * 100.0

        n_hit_1r = sum(1 for r in dir_recs if r["hit_1r_first"] == 1)
        hit_1r_rate = (n_hit_1r / n_total) * 100.0

        n_censored = sum(1 for r in dir_recs if r["censored"])
        censored_share = (n_censored / n_total) * 100.0

        r_values = [r["realized_r"] for r in dir_recs]
        mean_r = float(np.mean(r_values))
        median_r = float(np.median(r_values))

        exit_reasons = Counter(r["exit_reason"] for r in dir_recs)
        years = Counter(r["entry_year"] for r in dir_recs)

        dir_title = "LONG (BUY)" if direction == "BUY" else "SHORT (SELL)"
        print("\n" + "=" * 80)
        print(f"REAL DATA SWING LABEL ANALYSIS: {dir_title}")
        print("=" * 80)
        print(f"Total Labels:        {n_total:,}")
        print(f"Positive Rate (y=1): {pos_rate:.2f}% ({n_pos:,} / {n_total:,})")
        print(f"Hit 1R First Rate:   {hit_1r_rate:.2f}% ({n_hit_1r:,} / {n_total:,})")
        print(f"Censored Share:      {censored_share:.2f}% ({n_censored:,} / {n_total:,})")
        print(f"Mean Net R:          {mean_r:.4f}")
        print(f"Median Net R:        {median_r:.4f}")
        print("\nExit Reason Counts:")
        for reason, count in exit_reasons.most_common():
            pct = (count / n_total) * 100.0
            print(f"  {reason:<25} : {count:>7,} ({pct:6.2f}%)")
        print("\nCounts By Year:")
        for yr in sorted(years.keys()):
            yr_count = years[yr]
            yr_pct = (yr_count / n_total) * 100.0
            print(f"  {yr}                       : {yr_count:>7,} ({yr_pct:6.2f}%)")
        print("=" * 80)

    # Dump full json summary for persistence
    summary_data = {
        "symbols_count": len(symbols),
        "total_labels": len(all_records),
        "directions": {}
    }
    for direction in ("BUY", "SELL"):
        dir_recs = [r for r in all_records if r["side"] == direction]
        n_total = len(dir_recs)
        r_values = [r["realized_r"] for r in dir_recs]
        summary_data["directions"][direction] = {
            "total_count": n_total,
            "positive_count": sum(1 for r in dir_recs if r["y"] == 1),
            "positive_rate_pct": round(sum(1 for r in dir_recs if r["y"] == 1) / n_total * 100.0, 2),
            "hit_1r_first_count": sum(1 for r in dir_recs if r["hit_1r_first"] == 1),
            "hit_1r_first_rate_pct": round(sum(1 for r in dir_recs if r["hit_1r_first"] == 1) / n_total * 100.0, 2),
            "censored_count": sum(1 for r in dir_recs if r["censored"]),
            "censored_share_pct": round(sum(1 for r in dir_recs if r["censored"]) / n_total * 100.0, 2),
            "mean_r_net": round(float(np.mean(r_values)), 4),
            "median_r_net": round(float(np.median(r_values)), 4),
            "exit_reasons": dict(Counter(r["exit_reason"] for r in dir_recs)),
            "counts_by_year": dict(Counter(r["entry_year"] for r in dir_recs)),
        }

    with open("swing_real_data_summary.json", "w") as f:
        json.dump(summary_data, f, indent=2)
    print("\nSaved measured metrics to swing_real_data_summary.json")


if __name__ == "__main__":
    main()
