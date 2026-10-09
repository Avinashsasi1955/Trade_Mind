"""Exit-aware label builder using replay_trade_walk_forward as the exit simulator.

Specifications:
- Labels are y = 1 only if the simulated trade reaches >= +1R net before -1R.
  Continuous r_net is kept per direction. Labels between 0 and 1R remain y = 0 with r_net preserved.
- Enter at the NEXT bar's open plus 2 ticks of slippage (never the signal bar's close).
- Never create labels for entries after 14:45 IST for intraday mode.
- Truncate each simulated intraday trade at 15:15 IST force-flat of its own day, using only that day's bars.
- When no exit is reached before the series (or day) ends, mark censored = True with reason,
  and exclude from training sets by default.
- Uses live-path risk levels (identical to _chart_strategy_gate: 1.5x ATR long, 1.2x ATR short, 3.0x ATR target).
- Linear O(N) cost: normalizes bars once per series and caps lookahead window to 1 trading day.
- Persistence works on both Postgres (SQLAlchemy, bound parameters) and SQLite with real SQL.
"""
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from backend.database import now_iso
from backend.transaction_costs import estimate_zerodha_costs

IST = ZoneInfo("Asia/Kolkata")

SCHEMA_EXIT_AWARE_LABELS = """
CREATE TABLE IF NOT EXISTS exit_aware_labels (
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    y_long INTEGER NOT NULL,
    y_short INTEGER NOT NULL,
    r_net REAL NOT NULL,
    r_net_long REAL NOT NULL,
    r_net_short REAL NOT NULL,
    exit_reason_long TEXT NOT NULL,
    exit_reason_short TEXT NOT NULL,
    exit_price_long REAL NOT NULL,
    exit_price_short REAL NOT NULL,
    net_pnl_long REAL NOT NULL,
    net_pnl_short REAL NOT NULL,
    censored INTEGER NOT NULL DEFAULT 0,
    censored_reason TEXT,
    created_at TEXT NOT NULL,
    exit_date TEXT,
    PRIMARY KEY (exchange, symbol, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_exit_labels_date ON exit_aware_labels(trade_date);
CREATE INDEX IF NOT EXISTS idx_exit_labels_sym ON exit_aware_labels(exchange, symbol);
"""


@dataclass
class ExitAwareLabel:
    exchange: str
    symbol: str
    timestamp: str
    trade_date: str
    entry_price: float
    y_long: int
    y_short: int
    r_net: float
    r_net_long: float
    r_net_short: float
    exit_reason_long: str
    exit_reason_short: str
    exit_price_long: float
    exit_price_short: float
    net_pnl_long: float
    net_pnl_short: float
    censored: bool = False
    censored_reason: Optional[str] = None
    created_at: str = ""
    exit_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["censored"] = 1 if self.censored else 0
        return d


class LabelBuilder:
    """Builds exit-aware labels by simulating execution via replay_trade_walk_forward."""

    def __init__(
        self,
        tick_size: float = 0.05,
        target_r: float = 2.0,
        sl_pct: float = 0.008,
        quantity: int = 1,
        trade_mode: str = "INTRADAY",
        swing_holding_limit_bars: int = 1950,
    ) -> None:
        self.tick_size = float(tick_size)
        self.target_r = float(target_r)
        self.sl_pct = float(sl_pct)
        self.quantity = int(quantity)
        self.trade_mode = trade_mode.upper()
        self.swing_holding_limit_bars = int(swing_holding_limit_bars)

    @staticmethod
    def _parse_ist(ts_val: Any) -> datetime:
        """Parse timestamp to timezone-aware IST datetime."""
        if isinstance(ts_val, datetime):
            dt = ts_val
        elif isinstance(ts_val, str):
            dt = datetime.fromisoformat(ts_val)
        else:
            raise ValueError(f"Cannot parse timestamp: {ts_val}")
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)

    def _normalize_bar(self, bar: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize bar fields once per series for O(N) operations."""
        ts = bar.get("timestamp") or bar.get("bar_time")
        if not ts:
            raise ValueError(f"Bar missing timestamp/bar_time: {bar}")
        close_p = bar.get("close_price") if bar.get("close_price") is not None else bar.get("close")
        if close_p is None:
            raise ValueError(f"Bar missing close/close_price: {bar}")
        open_p = bar.get("open_price") if bar.get("open_price") is not None else bar.get("open", close_p)
        high_p = bar.get("high_price") if bar.get("high_price") is not None else bar.get("high", close_p)
        low_p = bar.get("low_price") if bar.get("low_price") is not None else bar.get("low", close_p)
        vol = bar.get("volume", 0)

        dt = self._parse_ist(ts)

        return {
            "dt": dt,
            "bar_time": dt.isoformat(),
            "timestamp": dt.isoformat(),
            "trade_date": dt.strftime("%Y-%m-%d"),
            "open": float(open_p),
            "open_price": float(open_p),
            "high": float(high_p),
            "high_price": float(high_p),
            "low": float(low_p),
            "low_price": float(low_p),
            "close": float(close_p),
            "close_price": float(close_p),
            "volume": float(vol),
            "interval": bar.get("interval", "1minute"),
        }

    def _compute_risk_levels(
        self,
        entry_price: float,
        side: str,
        atr: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """Calculate SL, TP, and R-points using live-path formula (_chart_strategy_gate).

        Offline limitation note:
        Full _chart_strategy_gate requires real-time WebSocket book depth, contemporaneous PCR,
        and at least 12 live 5m candles. When simulating offline across historical bars, we use
        the exact deterministic formula used by _chart_strategy_gate:
          - Long: risk = max(atr * 1.5, entry * 0.006), reward = max(atr * 3.0, entry * 0.012)
          - Short: risk = max(atr * 1.2, entry * 0.005), reward = max(atr * 3.0, entry * 0.012)
        """
        if atr is not None and atr > 0:
            effective_atr = atr
        else:
            effective_atr = entry_price * self.sl_pct

        if side.upper() == "BUY":
            risk = max(effective_atr * 1.5, entry_price * 0.006)
            reward = max(effective_atr * 3.0, entry_price * 0.012)
            sl = entry_price - risk
            tp = entry_price + reward
        else:
            risk = max(effective_atr * 1.2, entry_price * 0.005)
            reward = max(effective_atr * 3.0, entry_price * 0.012)
            sl = entry_price + risk
            tp = entry_price - reward

        r_points = max(self.tick_size, risk)
        return round(sl, 4), round(tp, 4), round(r_points, 4)

    def simulate_candidate(
        self,
        entry_price: Union[float, Dict[str, Any]],
        entry_time: Union[str, List[Dict[str, Any]]] = "",
        walk_bars: Optional[List[Dict[str, Any]]] = None,
        side: str = "BUY",
        exchange: str = "NSE",
        atr: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Simulate an individual trade from entry_price across pre-filtered walk_bars.

        Supports both:
        - simulate_candidate(entry_price=100.1, entry_time="...", walk_bars=[...], side="BUY", ...)
        - simulate_candidate(entry_bar, future_bars, side="BUY") (entering at NEXT bar open + 2 ticks slippage)
        """
        if isinstance(entry_price, dict):
            entry_bar = entry_price
            bars_list = entry_time if isinstance(entry_time, list) else (walk_bars or [])
            side_upper = side.upper()
            if side_upper not in ("BUY", "SELL"):
                raise ValueError(f"Invalid trade side: {side}")
            if not bars_list:
                return {
                    "side": side_upper,
                    "entry_price": float(entry_bar.get("close", 100.0)),
                    "exit_price": float(entry_bar.get("close", 100.0)),
                    "exit_reason": "NO_FUTURE_BARS",
                    "realized_r": 0.0,
                    "net_pnl": 0.0,
                    "y": 0,
                    "censored": True,
                    "censored_reason": "NO_FUTURE_BARS_BEFORE_END",
                    "fee_amount": 0.0,
                }
            next_b = bars_list[0]
            open_p = float(next_b.get("open", entry_bar.get("close", 100.0)))
            slip = 2.0 * self.tick_size
            act_entry_price = round((open_p + slip) if side_upper == "BUY" else (open_p - slip), 4)
            act_entry_time = str(next_b.get("timestamp") or next_b.get("bar_time") or entry_bar.get("timestamp", ""))
            act_walk_bars = bars_list
            if atr is None:
                atr_raw = entry_bar.get("atr")
                act_atr = float(atr_raw) if atr_raw is not None and float(atr_raw) > 0 else None
            else:
                act_atr = atr
        else:
            act_entry_price = float(entry_price)
            act_entry_time = str(entry_time)
            act_walk_bars = walk_bars or []
            side_upper = side.upper()
            if side_upper not in ("BUY", "SELL"):
                raise ValueError(f"Invalid trade side: {side}")
            act_atr = atr

        if not act_walk_bars:
            return {
                "side": side_upper,
                "entry_price": act_entry_price,
                "exit_price": act_entry_price,
                "exit_reason": "NO_FUTURE_BARS",
                "realized_r": 0.0,
                "net_pnl": 0.0,
                "y": 0,
                "censored": True,
                "censored_reason": "NO_FUTURE_BARS_BEFORE_END",
                "fee_amount": 0.0,
            }

        sl_price, tp_price, r_points = self._compute_risk_levels(act_entry_price, side_upper, act_atr)

        # Transaction costs
        try:
            costs = estimate_zerodha_costs(
                segment="EQUITY_INTRADAY" if self.trade_mode == "INTRADAY" else "EQUITY_DELIVERY",
                side=side_upper,
                price=Decimal(str(round(act_entry_price, 2))),
                quantity=self.quantity,
                exchange="NSE" if exchange.upper() == "NSE" else "BSE",
            )
            fee_amount = float(costs.total)
        except Exception as cost_err:
            raise RuntimeError(f"Cost estimation failed for {side_upper} @ {act_entry_price}: {cost_err}") from cost_err

        trade = {
            "entry": act_entry_price,
            "theoretical_fill_price": act_entry_price,
            "stop_loss_price": sl_price,
            "initial_sl": sl_price,
            "take_profit_price": tp_price,
            "target_price": tp_price,
            "side": side_upper,
            "quantity": self.quantity,
            "estimated_fees": fee_amount,
            "signal_at": act_entry_time,
            "trade_mode": self.trade_mode,
        }

        from backend.position_manager import replay_trade_walk_forward
        replay_res = replay_trade_walk_forward(
            trade=trade,
            bars=act_walk_bars,
            harvest_enabled=False,
            tick_size=self.tick_size,
        )

        realized_r = float(replay_res.get("realized_r", 0.0))
        net_pnl = float(replay_res.get("net_pnl", 0.0))
        exit_price = float(replay_res.get("exit_price", act_entry_price))
        exit_reason = str(replay_res.get("exit_reason", "UNKNOWN"))
        exit_time = replay_res.get("exit_time")
        exit_date = replay_res.get("exit_date")

        # Censoring: when no modeled exit was reached before series/day ended
        is_censored = False
        censored_reason = None
        if exit_reason in ("FELL_THROUGH_TO_RECORDED", "NO_BARS", "UNKNOWN"):
            is_censored = True
            censored_reason = "NO_EXIT_REACHED_BEFORE_SERIES_END"

        # Label rule: y = 1 ONLY if simulated trade reaches >= +1R net before -1R.
        # Intermediate outcomes (e.g. 0 < r_net < 1.0) stay y = 0 while continuous r_net is kept.
        if is_censored:
            y_label = 0
        else:
            y_label = 1 if realized_r >= 1.0 else 0

        return {
            "side": side_upper,
            "entry_price": act_entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "exit_time": exit_time,
            "exit_date": exit_date,
            "realized_r": realized_r,
            "net_pnl": net_pnl,
            "fee_amount": fee_amount,
            "y": y_label,
            "censored": is_censored,
            "censored_reason": censored_reason,
        }

    def build_labels_for_series(
        self,
        exchange: str,
        symbol: str,
        bars: List[Dict[str, Any]],
        min_future_bars: int = 1,
    ) -> List[ExitAwareLabel]:
        """Build exit-aware labels with O(N) linear complexity.

        - Normalizes all bars once upfront.
        - Enters at the NEXT bar's open plus 2 ticks of slippage.
        - Skips entries after 14:45 IST for intraday mode.
        - Truncates intraday walks at 15:15 IST force-flat using only the same day's bars.
        - Marks unexited trades as censored.
        """
        if not bars:
            return []
        if len(bars) <= min_future_bars:
            raise ValueError(f"Series has {len(bars)} bars, requires > {min_future_bars} for label simulation")

        # 1. Normalize all bars once upfront (Linear O(N) cost)
        norm_bars = [self._normalize_bar(b) for b in bars]
        n_bars = len(norm_bars)

        # Pre-compute rolling 14-bar ATR for risk-level calculation
        rolling_atr: List[float] = []
        tr_list = []
        for i in range(n_bars):
            if i == 0:
                tr = norm_bars[i]["high"] - norm_bars[i]["low"]
            else:
                prev_c = norm_bars[i - 1]["close"]
                tr = max(
                    norm_bars[i]["high"] - norm_bars[i]["low"],
                    abs(norm_bars[i]["high"] - prev_c),
                    abs(norm_bars[i]["low"] - prev_c),
                )
            tr_list.append(tr)
            if len(tr_list) >= 14:
                rolling_atr.append(sum(tr_list[-14:]) / 14.0)
            else:
                rolling_atr.append(sum(tr_list) / len(tr_list))

        labels: List[ExitAwareLabel] = []

        # 2. Iterate signal bars. Entry is strictly at bar i+1's open.
        for i in range(n_bars - min_future_bars):
            signal_bar = norm_bars[i]
            next_bar = norm_bars[i + 1]

            next_dt = next_bar["dt"]
            next_date = next_bar["trade_date"]

            # Rule: Never create labels for entries after 14:45 IST for intraday mode
            if self.trade_mode == "INTRADAY":
                if next_dt.time() > time(14, 45):
                    continue

            # Lookahead window slice: cap at one trading day or swing limit
            if self.trade_mode == "INTRADAY":
                # Only use bars from the SAME trading day up to and including 15:15 IST force flat
                end_idx = i + 1
                while end_idx < n_bars:
                    if norm_bars[end_idx]["trade_date"] != next_date:
                        break
                    if norm_bars[end_idx]["dt"].time() > time(15, 15):
                        # Include the force-flat bar itself, then stop
                        end_idx += 1
                        break
                    end_idx += 1
                future_bars = norm_bars[i + 1 : end_idx]
            else:
                max_lookahead = min(n_bars, i + 1 + self.swing_holding_limit_bars)
                future_bars = norm_bars[i + 1 : max_lookahead]

            if not future_bars:
                continue

            # Entry at NEXT bar's open + 2 ticks of slippage
            slip_amount = 2.0 * self.tick_size
            entry_long = next_bar["open"] + slip_amount
            entry_short = next_bar["open"] - slip_amount
            atr_val = rolling_atr[i]

            # Simulate Long
            sim_long = self.simulate_candidate(
                entry_price=entry_long,
                entry_time=next_bar["timestamp"],
                walk_bars=future_bars,
                side="BUY",
                exchange=exchange,
                atr=atr_val,
            )

            # Simulate Short
            sim_short = self.simulate_candidate(
                entry_price=entry_short,
                entry_time=next_bar["timestamp"],
                walk_bars=future_bars,
                side="SELL",
                exchange=exchange,
                atr=atr_val,
            )

            is_censored = sim_long["censored"] or sim_short["censored"]
            censored_reason = sim_long.get("censored_reason") or sim_short.get("censored_reason")

            label = ExitAwareLabel(
                exchange=exchange.upper(),
                symbol=symbol.upper(),
                timestamp=signal_bar["timestamp"],
                trade_date=signal_bar["trade_date"],
                entry_price=entry_long,
                y_long=sim_long["y"],
                y_short=sim_short["y"],
                r_net=sim_long["realized_r"],
                r_net_long=sim_long["realized_r"],
                r_net_short=sim_short["realized_r"],
                exit_reason_long=sim_long["exit_reason"],
                exit_reason_short=sim_short["exit_reason"],
                exit_price_long=sim_long["exit_price"],
                exit_price_short=sim_short["exit_price"],
                net_pnl_long=sim_long["net_pnl"],
                net_pnl_short=sim_short["net_pnl"],
                censored=is_censored,
                censored_reason=censored_reason,
                created_at=now_iso(),
                exit_date=(sim_long.get("exit_date") or sim_short.get("exit_date") or signal_bar["trade_date"]),
            )
            labels.append(label)

        return labels

    @staticmethod
    def create_tables(conn_or_engine: Any) -> None:
        """Create exit_aware_labels tables in SQL database. Supports SQLite and PostgreSQL."""
        if hasattr(conn_or_engine, "executescript"):
            conn_or_engine.executescript(SCHEMA_EXIT_AWARE_LABELS)
        elif hasattr(conn_or_engine, "begin"):
            from sqlalchemy import text
            with conn_or_engine.begin() as conn:
                for statement in SCHEMA_EXIT_AWARE_LABELS.strip().split(";"):
                    stmt = statement.strip()
                    if stmt:
                        conn.execute(text(stmt))
        elif hasattr(conn_or_engine, "execute"):
            from sqlalchemy import text
            for statement in SCHEMA_EXIT_AWARE_LABELS.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    conn_or_engine.execute(text(stmt))
        else:
            raise TypeError(f"Unsupported connection or engine type: {type(conn_or_engine)}")

    @staticmethod
    def save_labels(conn_or_engine: Any, labels: List[ExitAwareLabel]) -> int:
        """Persist ExitAwareLabel records using bound parameters on both Postgres and SQLite.

        No silent exception handling: raises on schema or operational failures.
        """
        if not labels:
            return 0

        # Check if using raw sqlite3 connection
        if isinstance(conn_or_engine, sqlite3.Connection):
            query = """
                INSERT INTO exit_aware_labels (
                    exchange, symbol, timestamp, trade_date, entry_price,
                    y_long, y_short, r_net, r_net_long, r_net_short,
                    exit_reason_long, exit_reason_short, exit_price_long, exit_price_short,
                    net_pnl_long, net_pnl_short, censored, censored_reason, created_at, exit_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                    trade_date = excluded.trade_date,
                    entry_price = excluded.entry_price,
                    y_long = excluded.y_long,
                    y_short = excluded.y_short,
                    r_net = excluded.r_net,
                    r_net_long = excluded.r_net_long,
                    r_net_short = excluded.r_net_short,
                    exit_reason_long = excluded.exit_reason_long,
                    exit_reason_short = excluded.exit_reason_short,
                    exit_price_long = excluded.exit_price_long,
                    exit_price_short = excluded.exit_price_short,
                    net_pnl_long = excluded.net_pnl_long,
                    net_pnl_short = excluded.net_pnl_short,
                    censored = excluded.censored,
                    censored_reason = excluded.censored_reason,
                    created_at = excluded.created_at,
                    exit_date = excluded.exit_date
            """
            rows = [
                (
                    lbl.exchange, lbl.symbol, lbl.timestamp, lbl.trade_date, lbl.entry_price,
                    lbl.y_long, lbl.y_short, lbl.r_net, lbl.r_net_long, lbl.r_net_short,
                    lbl.exit_reason_long, lbl.exit_reason_short, lbl.exit_price_long, lbl.exit_price_short,
                    lbl.net_pnl_long, lbl.net_pnl_short, 1 if lbl.censored else 0, lbl.censored_reason, lbl.created_at,
                    lbl.exit_date
                )
                for lbl in labels
            ]
            conn_or_engine.executemany(query, rows)
            conn_or_engine.commit()
            return len(rows)

        # SQLAlchemy engine or connection (Postgres / SQLite) with named bound parameters
        from sqlalchemy import text
        sql_stmt = text("""
            INSERT INTO exit_aware_labels (
                exchange, symbol, timestamp, trade_date, entry_price,
                y_long, y_short, r_net, r_net_long, r_net_short,
                exit_reason_long, exit_reason_short, exit_price_long, exit_price_short,
                net_pnl_long, net_pnl_short, censored, censored_reason, created_at, exit_date
            ) VALUES (
                :exchange, :symbol, :timestamp, :trade_date, :entry_price,
                :y_long, :y_short, :r_net, :r_net_long, :r_net_short,
                :exit_reason_long, :exit_reason_short, :exit_price_long, :exit_price_short,
                :net_pnl_long, :net_pnl_short, :censored, :censored_reason, :created_at, :exit_date
            )
            ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                trade_date = excluded.trade_date,
                entry_price = excluded.entry_price,
                y_long = excluded.y_long,
                y_short = excluded.y_short,
                r_net = excluded.r_net,
                r_net_long = excluded.r_net_long,
                r_net_short = excluded.r_net_short,
                exit_reason_long = excluded.exit_reason_long,
                exit_reason_short = excluded.exit_reason_short,
                exit_price_long = excluded.exit_price_long,
                exit_price_short = excluded.exit_price_short,
                net_pnl_long = excluded.net_pnl_long,
                net_pnl_short = excluded.net_pnl_short,
                censored = excluded.censored,
                censored_reason = excluded.censored_reason,
                created_at = excluded.created_at,
                exit_date = excluded.exit_date
        """)

        param_rows = [lbl.to_dict() for lbl in labels]

        if hasattr(conn_or_engine, "begin"):
            with conn_or_engine.begin() as conn:
                conn.execute(sql_stmt, param_rows)
        elif hasattr(conn_or_engine, "execute"):
            conn_or_engine.execute(sql_stmt, param_rows)
            if hasattr(conn_or_engine, "commit"):
                conn_or_engine.commit()
        else:
            raise TypeError(f"Unsupported connection type: {type(conn_or_engine)}")

        return len(labels)

    @staticmethod
    def load_labels(
        conn_or_engine: Any,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
        include_censored: bool = False,
    ) -> List[Dict[str, Any]]:
        """Load exit_aware_labels from SQL database.

        By default excludes censored labels (include_censored=False).
        Strict error propagation: no exceptions swallowed.
        """
        query_parts = ["SELECT * FROM exit_aware_labels WHERE 1=1"]
        params: Dict[str, Any] = {}

        if not include_censored:
            query_parts.append("AND censored = 0")
        if exchange:
            query_parts.append("AND exchange = :exchange")
            params["exchange"] = exchange.upper()
        if symbol:
            query_parts.append("AND symbol = :symbol")
            params["symbol"] = symbol.upper()
        if trade_date:
            query_parts.append("AND trade_date = :trade_date")
            params["trade_date"] = trade_date

        query_parts.append("ORDER BY timestamp ASC")
        query_sql = " ".join(query_parts)

        if isinstance(conn_or_engine, sqlite3.Connection):
            # Convert :param to ? for sqlite3 driver
            q_sqlite = query_sql
            sqlite_params = []
            for k in ["exchange", "symbol", "trade_date"]:
                if f":{k}" in q_sqlite:
                    q_sqlite = q_sqlite.replace(f":{k}", "?")
                    sqlite_params.append(params[k])
            cursor = conn_or_engine.execute(q_sqlite, sqlite_params)
            cols = [col[0] for col in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

        from sqlalchemy import text
        if hasattr(conn_or_engine, "connect"):
            with conn_or_engine.connect() as conn:
                res = conn.execute(text(query_sql), params)
                return [dict(r) for r in res.mappings().all()]
        elif hasattr(conn_or_engine, "execute"):
            res = conn_or_engine.execute(text(query_sql), params)
            return [dict(r) for r in res.mappings().all()]
        else:
            raise TypeError(f"Unsupported connection type: {type(conn_or_engine)}")
