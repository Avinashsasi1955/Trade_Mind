"""Exit-aware label builder using replay_trade_walk_forward as the exit simulator.

Generates separate y_long, y_short classification targets and r_net regression targets
with full fees and slippage modeling matching live trading execution.
Supports persistence to real SQL databases with strict error propagation
(no silent exception handling).
"""
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from backend.database import now_iso
from backend.position_manager import replay_trade_walk_forward
from backend.transaction_costs import estimate_zerodha_costs

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
    created_at TEXT NOT NULL,
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
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LabelBuilder:
    """Builds exit-aware labels by simulating exits via replay_trade_walk_forward."""

    def __init__(
        self,
        tick_size: float = 0.05,
        target_r: float = 2.0,
        sl_pct: float = 0.008,
        quantity: int = 1,
        trade_mode: str = "INTRADAY",
    ) -> None:
        self.tick_size = float(tick_size)
        self.target_r = float(target_r)
        self.sl_pct = float(sl_pct)
        self.quantity = int(quantity)
        self.trade_mode = trade_mode

    def _normalize_bar(self, bar: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize bar fields for replay_trade_walk_forward compatibility."""
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

        return {
            "bar_time": str(ts),
            "timestamp": str(ts),
            "open": float(open_p),
            "open_price": float(open_p),
            "high": float(high_p),
            "high_price": float(high_p),
            "low": float(low_p),
            "low_price": float(low_p),
            "close": float(close_p),
            "close_price": float(close_p),
            "volume": float(vol),
        }

    def simulate_candidate(
        self,
        entry_bar: Dict[str, Any],
        future_bars: List[Dict[str, Any]],
        side: str,
        exchange: str = "NSE",
    ) -> Dict[str, Any]:
        """Simulate an individual long or short trade from entry_bar through future_bars."""
        norm_entry = self._normalize_bar(entry_bar)
        norm_future = [self._normalize_bar(b) for b in future_bars]

        entry_price = norm_entry["close"]
        side_upper = side.upper()
        if side_upper not in ("BUY", "SELL"):
            raise ValueError(f"Invalid trade side: {side}")

        # Compute initial risk (R) points
        atr = entry_bar.get("atr") or entry_bar.get("atr_14")
        if atr is not None and float(atr) > 0:
            r_points = max(float(atr), entry_price * 0.006)
        else:
            r_points = max(entry_price * self.sl_pct, 0.50)
        r_points = max(self.tick_size, r_points)

        if side_upper == "BUY":
            sl_price = entry_price - r_points
            tp_price = entry_price + (r_points * self.target_r)
        else:
            sl_price = entry_price + r_points
            tp_price = entry_price - (r_points * self.target_r)

        # Compute realistic transaction fees
        try:
            costs = estimate_zerodha_costs(
                segment="EQUITY_INTRADAY" if self.trade_mode == "INTRADAY" else "EQUITY_DELIVERY",
                side=side_upper,
                price=Decimal(str(round(entry_price, 2))),
                quantity=self.quantity,
                exchange="NSE" if exchange.upper() == "NSE" else "BSE",
            )
            fee_amount = float(costs.total)
        except Exception as cost_err:
            raise RuntimeError(f"Cost estimation failed for {side_upper} @ {entry_price}: {cost_err}") from cost_err

        trade = {
            "entry": entry_price,
            "theoretical_fill_price": entry_price,
            "stop_loss_price": sl_price,
            "initial_sl": sl_price,
            "take_profit_price": tp_price,
            "target_price": tp_price,
            "side": side_upper,
            "quantity": self.quantity,
            "estimated_fees": fee_amount,
            "signal_at": norm_entry["timestamp"],
            "trade_mode": self.trade_mode,
        }

        # Run walk-forward simulation using PositionManager exit simulator
        replay_res = replay_trade_walk_forward(
            trade=trade,
            bars=norm_future,
            harvest_enabled=False,
            tick_size=self.tick_size,
        )

        realized_r = float(replay_res.get("realized_r", 0.0))
        net_pnl = float(replay_res.get("net_pnl", 0.0))
        exit_price = float(replay_res.get("exit_price", entry_price))
        exit_reason = str(replay_res.get("exit_reason", "UNKNOWN"))

        # Binary label: 1 if trade ended in positive R after all costs and slippage, 0 otherwise
        y_label = 1 if realized_r > 0.0 else 0

        return {
            "side": side_upper,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "realized_r": realized_r,
            "net_pnl": net_pnl,
            "y": y_label,
            "r_points": r_points,
            "fee_amount": fee_amount,
        }

    def build_labels_for_series(
        self,
        exchange: str,
        symbol: str,
        bars: List[Dict[str, Any]],
        min_future_bars: int = 1,
    ) -> List[ExitAwareLabel]:
        """Build exit-aware labels for each bar in a historical series."""
        if not bars:
            return []
        if len(bars) <= min_future_bars:
            raise ValueError(f"Series has {len(bars)} bars, requires > {min_future_bars} for label simulation")

        labels: List[ExitAwareLabel] = []
        n_bars = len(bars)

        for i in range(n_bars - min_future_bars):
            entry_bar = bars[i]
            future_bars = bars[i + 1:]

            # Simulate Long
            sim_long = self.simulate_candidate(
                entry_bar=entry_bar,
                future_bars=future_bars,
                side="BUY",
                exchange=exchange,
            )

            # Simulate Short
            sim_short = self.simulate_candidate(
                entry_bar=entry_bar,
                future_bars=future_bars,
                side="SELL",
                exchange=exchange,
            )

            ts_str = str(entry_bar.get("timestamp") or entry_bar.get("bar_time"))
            date_str = ts_str[:10]

            label = ExitAwareLabel(
                exchange=exchange.upper(),
                symbol=symbol.upper(),
                timestamp=ts_str,
                trade_date=date_str,
                entry_price=sim_long["entry_price"],
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
                created_at=now_iso(),
            )
            labels.append(label)

        return labels

    @staticmethod
    def create_tables(conn: Union[sqlite3.Connection, Any]) -> None:
        """Create exit_aware_labels tables in SQL database. Raises on any error."""
        if hasattr(conn, "executescript"):
            conn.executescript(SCHEMA_EXIT_AWARE_LABELS)
        elif hasattr(conn, "execute"):
            for statement in SCHEMA_EXIT_AWARE_LABELS.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    conn.execute(stmt)
        else:
            raise TypeError(f"Unsupported connection type: {type(conn)}")

    @staticmethod
    def save_labels(conn: Union[sqlite3.Connection, Any], labels: List[ExitAwareLabel]) -> int:
        """Persist ExitAwareLabel records to SQL database.

        Strictly enforces database integrity. No exceptions are swallowed.
        """
        if not labels:
            return 0

        rows = [
            (
                lbl.exchange,
                lbl.symbol,
                lbl.timestamp,
                lbl.trade_date,
                lbl.entry_price,
                lbl.y_long,
                lbl.y_short,
                lbl.r_net,
                lbl.r_net_long,
                lbl.r_net_short,
                lbl.exit_reason_long,
                lbl.exit_reason_short,
                lbl.exit_price_long,
                lbl.exit_price_short,
                lbl.net_pnl_long,
                lbl.net_pnl_short,
                lbl.created_at,
            )
            for lbl in labels
        ]

        query = """
            INSERT INTO exit_aware_labels (
                exchange, symbol, timestamp, trade_date, entry_price,
                y_long, y_short, r_net, r_net_long, r_net_short,
                exit_reason_long, exit_reason_short, exit_price_long, exit_price_short,
                net_pnl_long, net_pnl_short, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                created_at = excluded.created_at
        """

        if hasattr(conn, "executemany"):
            conn.executemany(query, rows)
            if hasattr(conn, "commit"):
                conn.commit()
        else:
            raise TypeError(f"Unsupported connection type: {type(conn)}")

        return len(rows)

    @staticmethod
    def load_labels(
        conn: Union[sqlite3.Connection, Any],
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load exit_aware_labels records from SQL database.

        Strict error propagation. No exceptions are swallowed.
        """
        query = "SELECT * FROM exit_aware_labels WHERE 1=1"
        params: List[Any] = []

        if exchange:
            query += " AND exchange = ?"
            params.append(exchange.upper())
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        if trade_date:
            query += " AND trade_date = ?"
            params.append(trade_date)

        query += " ORDER BY timestamp ASC"

        cursor = conn.execute(query, params)
        cols = [col[0] for col in cursor.description]
        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            results.append(dict(zip(cols, row)))

        return results
