import os
import sys
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
import psycopg2
import math
from statistics import mean, pstdev
from collections import defaultdict

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# Import transaction costs
from backend.transaction_costs import estimate_zerodha_costs
from backend.position_manager import TRADE_BAR_SOURCES

IST = timezone(timedelta(hours=5, minutes=30))

def run_simulation(trades, db_conn, breakeven_r=Decimal("1.10"), profit_lock_r=Decimal("1.60"), 
                   profit_lock_guaranteed_r=Decimal("1.00"), trailing_r=Decimal("1.50"),
                   trailing_giveback_r=Decimal("0.40")):
    cur = db_conn.cursor()
    sim_trades = []
    
    for t in trades:
        trade_id = t["id"]
        instrument_id = t["instrument_id"]
        signal_at = t["signal_at"]
        if t.get("stop_loss_price") is None:
            continue
        entry = Decimal(str(t["entry_price"]))
        side = t["side"].upper()
        initial_sl = Decimal(str(t["stop_loss_price"]))
        take_profit = Decimal(str(t["take_profit_price"])) if t.get("take_profit_price") else None
        quantity = int(t["quantity"])
        
        r_points = abs(entry - initial_sl)
        if r_points <= Decimal("0.001"):
            continue
            
        inst_type = t.get("instrument_type") or "EQ"
        if inst_type in {"CE", "PE"}:
            seg = "OPTIONS"
        elif inst_type == "FUT":
            seg = "FUTURES"
        else:
            seg = "EQUITY_INTRADAY"
        c_entry = estimate_zerodha_costs(seg, side, entry, quantity)
        fees = c_entry.total
        tick_size = Decimal("0.05")
        fee_buffer_per_share = ((fees * Decimal("2.5")) + (Decimal("2") * tick_size * Decimal(quantity))) / max(Decimal("1"), Decimal(quantity))
        
        # Fetch high-resolution bars starting at signal_at choosing highest available resolution
        bars = []
        for interval in ("1second", "1minute", "5minute"):
            cur.execute("""
                SELECT bar_time, open_price, high_price, low_price, close_price
                FROM live_market_bars
                WHERE instrument_id = %s
                  AND interval = %s
                  AND source = ANY(%s)
                  AND bar_time >= %s
                  AND bar_time <= %s + interval '8 hours'
                ORDER BY bar_time ASC;
            """, (instrument_id, interval, list(TRADE_BAR_SOURCES), signal_at, signal_at))
            rows = cur.fetchall()
            if rows:
                bars = rows
                break
        
        if not bars:
            continue
            
        best_favourable = entry
        best_favorable_r = Decimal("0")
        exit_price = None
        exit_reason = None
        exit_time = None
        
        for bar in bars:
            bar_time, o, h, l, c = bar[0], Decimal(str(bar[1])), Decimal(str(bar[2])), Decimal(str(bar[3])), Decimal(str(bar[4]))
            elapsed_seconds = (bar_time - signal_at).total_seconds()
            
            prices_to_check = [o, l, h, c] if side == "BUY" else [o, h, l, c]
            
            for p in prices_to_check:
                if side == "BUY":
                    favorable_r = (p - entry) / r_points
                    adverse_pnl_r = (entry - p) / r_points
                    if p > best_favourable:
                        best_favourable = p
                    best_favorable_r = (best_favourable - entry) / r_points
                else:
                    favorable_r = (entry - p) / r_points
                    adverse_pnl_r = (p - entry) / r_points
                    if p < best_favourable:
                        best_favourable = p
                    best_favorable_r = (entry - best_favourable) / r_points
                
                effective_sl = initial_sl
                
                # Rule 1: 90-sec Early Adverse Cut
                if elapsed_seconds <= 90.0 and adverse_pnl_r >= Decimal("0.40") and best_favorable_r <= Decimal("0.10"):
                    exit_price = p
                    exit_reason = "EARLY_ADVERSE_CUT"
                    exit_time = bar_time
                    break
                    
                # Rule 2: 5-min Stagnation Tighten
                if elapsed_seconds >= 300.0 and best_favorable_r < Decimal("0.20"):
                    tightened_sl = entry - Decimal("0.35") * r_points if side == "BUY" else entry + Decimal("0.35") * r_points
                    effective_sl = max(effective_sl, tightened_sl) if side == "BUY" else min(effective_sl, tightened_sl)
                    
                # Rule 3: 15-min Stagnation Guard
                if elapsed_seconds >= 900.0 and best_favorable_r < Decimal("0.25"):
                    exit_price = p
                    exit_reason = "STAGNATION_GUARD"
                    exit_time = bar_time
                    break
                    
                # Rule 4: +2.5R Spike Exit
                if favorable_r >= Decimal("2.50"):
                    exit_price = entry + Decimal("2.50") * r_points if side == "BUY" else entry - Decimal("2.50") * r_points
                    exit_reason = "PROFIT_CAPTURE"
                    exit_time = bar_time
                    break
                    
                # Rule 5: Stage 2 Profit Lock
                profit_lock_price = None
                if best_favorable_r >= profit_lock_r:
                    profit_lock_price = entry + profit_lock_guaranteed_r * r_points if side == "BUY" else entry - profit_lock_guaranteed_r * r_points
                    
                # Rule 6: Stage 1 Breakeven
                breakeven_price = None
                if best_favorable_r >= breakeven_r:
                    breakeven_price = entry + fee_buffer_per_share if side == "BUY" else entry - fee_buffer_per_share
                    
                # Rule 7: Trailing Stop
                trailing_stop_price = None
                if best_favorable_r >= trailing_r:
                    trailing_stop_price = best_favourable - trailing_giveback_r * r_points if side == "BUY" else best_favourable + trailing_giveback_r * r_points
                    
                # Evaluate price against boundaries
                if side == "BUY":
                    if trailing_stop_price and p <= trailing_stop_price:
                        exit_price = max(trailing_stop_price, profit_lock_price or breakeven_price or trailing_stop_price)
                        exit_reason = "TRAILING_STOP"
                        exit_time = bar_time
                        break
                    elif profit_lock_price and p <= profit_lock_price:
                        exit_price = profit_lock_price
                        exit_reason = "PROFIT_LOCK_STOP"
                        exit_time = bar_time
                        break
                    elif breakeven_price and p <= breakeven_price:
                        exit_price = breakeven_price
                        exit_reason = "BREAKEVEN_STOP"
                        exit_time = bar_time
                        break
                    elif effective_sl and p <= effective_sl:
                        exit_price = effective_sl
                        exit_reason = "STOP_LOSS"
                        exit_time = bar_time
                        break
                    elif take_profit and p >= take_profit:
                        exit_price = take_profit
                        exit_reason = "TAKE_PROFIT"
                        exit_time = bar_time
                        break
                else: # SELL
                    if trailing_stop_price and p >= trailing_stop_price:
                        exit_price = min(trailing_stop_price, profit_lock_price or breakeven_price or trailing_stop_price)
                        exit_reason = "TRAILING_STOP"
                        exit_time = bar_time
                        break
                    elif profit_lock_price and p >= profit_lock_price:
                        exit_price = profit_lock_price
                        exit_reason = "PROFIT_LOCK_STOP"
                        exit_time = bar_time
                        break
                    elif breakeven_price and p >= breakeven_price:
                        exit_price = breakeven_price
                        exit_reason = "BREAKEVEN_STOP"
                        exit_time = bar_time
                        break
                    elif effective_sl and p >= effective_sl:
                        exit_price = effective_sl
                        exit_reason = "STOP_LOSS"
                        exit_time = bar_time
                        break
                    elif take_profit and p <= take_profit:
                        exit_price = take_profit
                        exit_reason = "TAKE_PROFIT"
                        exit_time = bar_time
                        break
                        
                # Check 15:15 IST force flat
                dt_ist = bar_time.astimezone(IST) if bar_time.tzinfo else bar_time.replace(tzinfo=timezone.utc).astimezone(IST)
                if dt_ist.hour > 15 or (dt_ist.hour == 15 and dt_ist.minute >= 15):
                    exit_price = p
                    exit_reason = "SESSION_FORCE_FLAT"
                    exit_time = bar_time
                    break
                    
            if exit_price is not None:
                break
                
        if exit_price is None:
            last_bar = bars[-1]
            exit_price = Decimal(str(last_bar[4]))
            exit_reason = "TIME_EXIT"
            exit_time = last_bar[0]
            
        # Realistic Transaction Costs via transaction_costs.py
        entry_side = "BUY" if side == "BUY" else "SELL"
        exit_side = "SELL" if side == "BUY" else "BUY"
        inst_type = str(t.get("instrument_type") or "EQ").upper()
        if inst_type in {"CE", "PE"}:
            segment = "OPTIONS"
        elif inst_type == "FUT":
            segment = "FUTURES"
        else:
            segment = "EQUITY_INTRADAY"
        
        cost_entry = estimate_zerodha_costs(segment, entry_side, entry, quantity, slippage_bps=Decimal("2.0"))
        cost_exit = estimate_zerodha_costs(segment, exit_side, exit_price, quantity, slippage_bps=Decimal("2.0"))
        total_costs = cost_entry.total + cost_exit.total
        
        gross_pnl = (exit_price - entry) * Decimal(quantity) if side == "BUY" else (entry - exit_price) * Decimal(quantity)
        net_pnl = gross_pnl - total_costs
        
        realized_r = ((exit_price - entry) / r_points) if side == "BUY" else ((entry - exit_price) / r_points)
        
        sim_trades.append({
            "id": trade_id,
            "side": side,
            "entry": float(entry),
            "exit": float(exit_price),
            "exit_reason": exit_reason,
            "exit_time": exit_time.isoformat() if exit_time else None,
            "r_points": float(r_points),
            "realized_r": float(realized_r),
            "gross_pnl": float(gross_pnl),
            "total_costs": float(total_costs),
            "net_pnl": float(net_pnl),
            "quantity": quantity
        })
        
    return sim_trades

def calculate_metrics(trades, initial_capital=1_000_000):
    if not trades:
        return {}
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    
    win_rate = round(len(wins) / len(trades) * 100, 2)
    
    reasons = sorted(set(t["exit_reason"] for t in trades))
    r_breakdown = {}
    for r in reasons:
        subset = [t for t in trades if t["exit_reason"] == r]
        avg_r = round(mean(t["realized_r"] for t in subset), 3)
        tot_pnl = round(sum(t["net_pnl"] for t in subset), 2)
        r_breakdown[r] = {"count": len(subset), "avg_r": avg_r, "total_pnl": tot_pnl}
        
    avg_win_r = round(mean([t["realized_r"] for t in wins]), 3) if wins else 0.0
    avg_loss_r = round(mean([t["realized_r"] for t in losses]), 3) if losses else 0.0
    
    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    daily_pnl = defaultdict(float)
    
    for t in trades:
        equity += t["net_pnl"]
        peak = max(peak, equity)
        dd = (equity - peak) / peak
        max_dd = min(max_dd, dd)
        
        exit_time_raw = t.get("exit_time")
        if exit_time_raw:
            try:
                dt_obj = datetime.fromisoformat(exit_time_raw)
                dt_ist = dt_obj.astimezone(IST).date() if dt_obj.tzinfo else dt_obj.date()
                daily_pnl[dt_ist] += t["net_pnl"]
            except Exception:
                daily_pnl["default"] += t["net_pnl"]
        else:
            daily_pnl["default"] += t["net_pnl"]
            
    daily_returns = [pnl / initial_capital for pnl in daily_pnl.values()]
    volatility = pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = round(mean(daily_returns) / volatility * math.sqrt(252), 2) if volatility > 0 else 0.0
    
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "profit_factor": profit_factor,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "r_breakdown": r_breakdown,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "win_rate_pct": win_rate,
        "net_pnl": round(equity - initial_capital, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2)
    }

if __name__ == "__main__":
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL environment variable is not set. Please set DATABASE_URL to connect.")
    if dsn.startswith("postgresql+psycopg2://"):
        dsn = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    
    cur.execute("""
        SELECT a.id, a.instrument_id, a.signal_at, a.side, a.theoretical_fill_price, 
               a.stop_loss_price, a.take_profit_price, a.quantity,
               COALESCE(i.instrument_type, 'EQ') AS instrument_type
        FROM shadow_execution_audits a
        JOIN instrument_master i ON i.id = a.instrument_id
        WHERE a.realised_exit_price IS NOT NULL
          AND a.model_version LIKE 'direction-v2.5%'
          AND NOT COALESCE(a.mistake_tags @> '["synthetic_option_model"]'::jsonb, FALSE)
        ORDER BY a.signal_at ASC;
    """)
    rows = cur.fetchall()
    v25_trades = [{
        "id": r[0], "instrument_id": r[1], "signal_at": r[2], "side": r[3],
        "entry_price": r[4], "stop_loss_price": r[5], "take_profit_price": r[6], "quantity": r[7],
        "instrument_type": r[8]
    } for r in rows]
    
    print(f"Loaded {len(v25_trades)} trades from shadow_execution_audits (direction-v2.5)")
    
    print("Running direction-v2.5 with OLD thresholds...")
    res_v25_old = run_simulation(v25_trades, conn, breakeven_r=Decimal("0.70"), profit_lock_r=Decimal("1.20"),
                                 profit_lock_guaranteed_r=Decimal("0.60"), trailing_r=Decimal("1.50"),
                                 trailing_giveback_r=Decimal("0.40"))
    metrics_v25_old = calculate_metrics(res_v25_old)
    
    print("Running direction-v2.5 with NEW thresholds (Commit 1)...")
    res_v25_new = run_simulation(v25_trades, conn, breakeven_r=Decimal("1.10"), profit_lock_r=Decimal("1.60"),
                                 profit_lock_guaranteed_r=Decimal("1.00"), trailing_r=Decimal("1.50"),
                                 trailing_giveback_r=Decimal("0.40"))
    metrics_v25_new = calculate_metrics(res_v25_new)
    
    # direction-v3.0 signals
    cur.execute("""
        SELECT p.id, i.id, p.timestamp, 
               CASE WHEN p.signal = 1 THEN 'BUY' ELSE 'SELL' END as side,
               p.decision_price,
               CASE WHEN p.signal = 1 THEN p.decision_price * 0.992 ELSE p.decision_price * 1.008 END as sl,
               CASE WHEN p.signal = 1 THEN p.decision_price * 1.016 ELSE p.decision_price * 0.984 END as tp,
               GREATEST(1, ROUND(25000 / p.decision_price)) as qty,
               COALESCE(i.instrument_type, 'EQ') as instrument_type
        FROM shadow_predictions p
        JOIN instrument_master i ON i.symbol = p.symbol AND i.exchange = p.exchange
        WHERE p.model_version LIKE 'direction-v3.0%' 
          AND p.signal != 0
          AND p.decision_price > 0
        ORDER BY p.timestamp ASC;
    """)
    v30_rows = cur.fetchall()
    v30_trades = [{
        "id": r[0], "instrument_id": r[1], "signal_at": r[2], "side": r[3],
        "entry_price": r[4], "stop_loss_price": r[5], "take_profit_price": r[6], "quantity": r[7],
        "instrument_type": r[8]
    } for r in v30_rows]
    print(f"Loaded {len(v30_trades)} signals for direction-v3.0")
    
    print("Running direction-v3.0 with NEW thresholds (Commit 1)...")
    res_v30_new = run_simulation(v30_trades, conn, breakeven_r=Decimal("1.10"), profit_lock_r=Decimal("1.60"),
                                 profit_lock_guaranteed_r=Decimal("1.00"), trailing_r=Decimal("1.50"),
                                 trailing_giveback_r=Decimal("0.40"))
    metrics_v30_new = calculate_metrics(res_v30_new)
    
    import json
    os.makedirs("data", exist_ok=True)
    with open("data/comparative_backtest_results.json", "w") as f:
        json.dump({
            "v25_old_thresholds": metrics_v25_old,
            "v25_new_thresholds": metrics_v25_new,
            "v30_new_thresholds": metrics_v30_new
        }, f, indent=2)
    print("Backtest completed successfully. Results saved to data/comparative_backtest_results.json")
