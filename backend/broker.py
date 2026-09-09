import sqlite3
from typing import Dict, Optional

from .agent import Signal
from .database import now_iso


RISK_FRACTION = {"conservative": 0.01, "balanced": 0.02, "aggressive": 0.035}


class PaperBroker:
    """Executes simulated orders against the latest mock quote — supports both BUY and SELL directions."""

    def _portfolio(self, db: sqlite3.Connection, user_id: int):
        return db.execute("SELECT * FROM portfolios WHERE user_id=?", (user_id,)).fetchone()

    def _position_size(self, portfolio, signal: Signal, risk_profile: str) -> int:
        risk_budget = portfolio["starting_capital"] * RISK_FRACTION.get(risk_profile, .02)
        per_share_risk = max(.01, abs(signal.price - signal.stop_loss))
        risk_qty = int(risk_budget / per_share_risk)
        max_position_value = portfolio["starting_capital"] * .05
        return max(1, min(risk_qty, int(max_position_value / signal.price), int(portfolio["cash"] / signal.price)))

    def execute_buy(self, db: sqlite3.Connection, user_id: int, signal: Signal, risk_profile: str) -> Optional[Dict]:
        """Open a LONG (BUY/CALL) paper position."""
        portfolio = self._portfolio(db, user_id)
        if not portfolio:
            return None
        quantity = self._position_size(portfolio, signal, risk_profile)
        cost = round(quantity * signal.price, 2)
        if quantity < 1 or cost > portfolio["cash"]:
            return None
        existing = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (user_id, signal.symbol)).fetchone()
        if existing:
            total_qty = existing["quantity"] + quantity
            avg = (existing["quantity"] * existing["average_price"] + cost) / total_qty
            db.execute("UPDATE holdings SET quantity=?, average_price=? WHERE id=?", (total_qty, avg, existing["id"]))
        else:
            db.execute(
                "INSERT INTO holdings(user_id,symbol,name,quantity,average_price,opened_at) VALUES(?,?,?,?,?,?)",
                (user_id, signal.symbol, signal.name, quantity, signal.price, now_iso()),
            )
        db.execute("UPDATE portfolios SET cash=cash-?, updated_at=? WHERE user_id=?", (cost, now_iso(), user_id))
        cursor = db.execute(
            "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, signal.symbol, signal.name, "BUY", quantity, signal.price, signal.confidence, signal.strategy, signal.reasoning, now_iso()),
        )
        db.commit()
        return {"id": cursor.lastrowid, "symbol": signal.symbol, "action": "BUY", "direction": "LONG", "quantity": quantity, "price": signal.price, "value": cost}

    def execute_sell(self, db: sqlite3.Connection, user_id: int, signal: Signal, risk_profile: str) -> Optional[Dict]:
        """Open a SHORT (SELL/PUT) paper position.

        For paper trading, a short is simulated by crediting cash immediately and
        recording a negative quantity holding (bearish exposure). P&L is the inverse
        of price movement relative to entry.
        """
        portfolio = self._portfolio(db, user_id)
        if not portfolio:
            return None
        quantity = self._position_size(portfolio, signal, risk_profile)
        if quantity < 1:
            return None
        # For paper shorts: we credit the proceeds to cash (short sale proceeds)
        proceeds = round(quantity * signal.price, 2)
        existing = db.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?", (user_id, signal.symbol)).fetchone()
        if existing and existing["quantity"] > 0:
            # Reduce existing long before opening short
            reduce_qty = min(existing["quantity"], quantity)
            if existing["quantity"] - reduce_qty == 0:
                db.execute("DELETE FROM holdings WHERE id=?", (existing["id"],))
            else:
                db.execute("UPDATE holdings SET quantity=? WHERE id=?", (existing["quantity"] - reduce_qty, existing["id"]))
            quantity = reduce_qty  # only traded what we closed
            proceeds = round(quantity * signal.price, 2)
        else:
            # Pure paper short — store as negative quantity
            short_qty = -quantity
            if existing:
                db.execute("UPDATE holdings SET quantity=?, average_price=? WHERE id=?", (existing["quantity"] + short_qty, signal.price, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO holdings(user_id,symbol,name,quantity,average_price,opened_at) VALUES(?,?,?,?,?,?)",
                    (user_id, signal.symbol, signal.name, short_qty, signal.price, now_iso()),
                )
        db.execute("UPDATE portfolios SET cash=cash+?, updated_at=? WHERE user_id=?", (proceeds, now_iso(), user_id))
        cursor = db.execute(
            "INSERT INTO trades(user_id,symbol,name,action,quantity,price,confidence,strategy,reasoning,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, signal.symbol, signal.name, "SELL", quantity, signal.price, signal.confidence, signal.strategy, signal.reasoning, now_iso()),
        )
        db.commit()
        return {"id": cursor.lastrowid, "symbol": signal.symbol, "action": "SELL", "direction": "SHORT", "quantity": quantity, "price": signal.price, "value": proceeds}
